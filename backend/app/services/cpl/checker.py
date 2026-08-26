import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, Engine, text

from app.schemas.cpl import CPL_FIELDS, CplFieldCode, CplResult, CplStatus


RequestReason = Literal[
    "DETAIL_NEW",
    "SUBPROGRAM_NEW",
    "SUBSUBPROGRAM_NEW",
    "CONTENT_CHANGE",
    "UNKNOWN",
]

FORM_SCHEMA_NAME = "sims-cpl"
FORM_SCHEMA_VERSION = 1
FORM_SCHEMA_DESCRIPTION = "SIMS Pre-review alpha CPL 13-field contract"

CPL_FIELD_LABELS: dict[str, str] = {
    "REQUEST_TYPE": "요청유형 체크값",
    "PURPOSE_GOAL": "사업 목적·목표",
    "IMPLEMENTATION_PLAN": "연차별·내역사업별 추진계획",
    "BUSINESS_PERIOD": "사업기간",
    "NEW_OR_CHANGED_CONTENT": "신설·변경 주요내용",
    "BUSINESS_NEED": "사업필요성 최소 논리구조",
    "LEGAL_BASIS": "지원근거",
    "LINKED_POLICY": "연계정책",
    "BUDGET": "사업예산",
    "TARGET_AND_CONDITIONS": "지원대상·지원조건",
    "SUPPORT_CONTENT_AND_SCALE": "지원내용·지원규모",
    "DELIVERY_SYSTEM": "수행기관·수행방식·수행체계",
    "EXPECTED_EFFECTS_AND_PERFORMANCE": "기대효과·성과 관련 정보",
}


class CplPersistenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedCplRun:
    request_extraction_id: int
    missing_check_run_id: int


def run_cpl(
    engine: Engine,
    case_id: int,
    result: CplResult,
    *,
    request_reason: RequestReason,
    extractor_name: str,
    extractor_version: str,
) -> PersistedCplRun:
    """Persist one completed CPL extraction without updating prior results."""
    if request_reason not in {
        "DETAIL_NEW",
        "SUBPROGRAM_NEW",
        "SUBSUBPROGRAM_NEW",
        "CONTENT_CHANGE",
        "UNKNOWN",
    }:
        raise CplPersistenceError("Unsupported request reason")
    if request_reason != request_reason_from_result(result):
        raise CplPersistenceError("Request reason does not match the CPL result")
    if not result.ruleset_version.strip():
        raise CplPersistenceError("CPL ruleset version must not be blank")
    if not extractor_name.strip() or not extractor_version.strip():
        raise CplPersistenceError("CPL extractor identity must not be blank")
    _validate_evidence_contract(result)

    snapshot = result.model_dump(mode="json")
    snapshot["request_reason"] = request_reason
    snapshot["extractor_name"] = extractor_name
    snapshot["extractor_version"] = extractor_version

    with engine.begin() as connection:
        parse_run_id = _lock_ready_case(connection, case_id)
        form_schema_id, field_definition_ids = _seed_cpl_schema(connection)

        existing_extraction = connection.scalar(
            text(
                """
                SELECT id
                FROM sims.request_extraction
                WHERE inspection_case_id = :case_id
                FOR UPDATE
                """
            ),
            {"case_id": case_id},
        )
        if existing_extraction is not None:
            raise CplPersistenceError("A CPL extraction already exists for this case")

        extraction_status = (
            "PARTIAL_SUCCESS"
            if any(item.status == CplStatus.PARSE_FAILED for item in result.items)
            else "SUCCESS"
        )
        request_extraction_id = connection.scalar(
            text(
                """
                INSERT INTO sims.request_extraction (
                    inspection_case_id,
                    form_schema_id,
                    parse_run_id,
                    request_reason,
                    status,
                    extractor_name,
                    extractor_version,
                    raw_extraction,
                    completed_at
                )
                VALUES (
                    :case_id,
                    :form_schema_id,
                    :parse_run_id,
                    :request_reason,
                    :status,
                    :extractor_name,
                    :extractor_version,
                    CAST(:raw_extraction AS jsonb),
                    statement_timestamp()
                )
                RETURNING id
                """
            ),
            {
                "case_id": case_id,
                "form_schema_id": form_schema_id,
                "parse_run_id": parse_run_id,
                "request_reason": request_reason,
                "status": extraction_status,
                "extractor_name": extractor_name,
                "extractor_version": extractor_version,
                "raw_extraction": json.dumps(snapshot, ensure_ascii=False),
            },
        )
        if request_extraction_id is None:
            raise RuntimeError("Failed to create CPL extraction")

        evidence_ids: dict[str, int] = {}
        for item in result.items:
            field_code = item.field_code.value
            occurrences = [
                occurrence.model_dump(mode="json") for occurrence in item.occurrences
            ]
            lineage = [
                {
                    "block_id": occurrence.block_id,
                    "page_no": occurrence.page_no,
                    "section_path": occurrence.section_path,
                    "source_locator": occurrence.source_locator,
                    "extraction_method": occurrence.extraction_method,
                    "axis_code": occurrence.axis_code,
                    "source_role": occurrence.source_role,
                }
                for occurrence in item.occurrences
            ]
            field_value_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.request_field_value (
                        request_extraction_id,
                        field_definition_id,
                        raw_text,
                        normalized_value,
                        page_no,
                        source_locator
                    )
                    VALUES (
                        :request_extraction_id,
                        :field_definition_id,
                        NULL,
                        CAST(:normalized_value AS jsonb),
                        NULL,
                        CAST(:source_locator AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "request_extraction_id": request_extraction_id,
                    "field_definition_id": field_definition_ids[field_code],
                    "normalized_value": json.dumps(
                        {"occurrences": occurrences}, ensure_ascii=False
                    ),
                    "source_locator": json.dumps(
                        {"occurrences": lineage}, ensure_ascii=False
                    ),
                },
            )
            if field_value_id is None:
                raise RuntimeError("Failed to create CPL field value")
            evidence_ids[field_code] = field_value_id

        missing_check_run_id = connection.scalar(
            text(
                """
                INSERT INTO sims.missing_check_run (
                    inspection_case_id,
                    request_extraction_id,
                    ruleset_version,
                    status,
                    started_at,
                    completed_at
                )
                VALUES (
                    :case_id,
                    :request_extraction_id,
                    :ruleset_version,
                    'SUCCESS',
                    statement_timestamp(),
                    statement_timestamp()
                )
                RETURNING id
                """
            ),
            {
                "case_id": case_id,
                "request_extraction_id": request_extraction_id,
                "ruleset_version": result.ruleset_version,
            },
        )
        if missing_check_run_id is None:
            raise RuntimeError("Failed to create CPL missing-check run")

        connection.execute(
            text(
                """
                INSERT INTO sims.missing_check_item (
                    missing_check_run_id,
                    field_definition_id,
                    evidence_field_value_id,
                    result_status,
                    reason_code,
                    explanation
                )
                VALUES (
                    :missing_check_run_id,
                    :field_definition_id,
                    :evidence_field_value_id,
                    :result_status,
                    :reason_code,
                    :explanation
                )
                """
            ),
            [
                {
                    "missing_check_run_id": missing_check_run_id,
                    "field_definition_id": field_definition_ids[
                        item.field_code.value
                    ],
                    "evidence_field_value_id": (
                        evidence_ids[item.field_code.value]
                        if item.occurrences
                        else None
                    ),
                    "result_status": item.status.value,
                    "reason_code": item.reason_code,
                    "explanation": item.explanation,
                }
                for item in result.items
            ],
        )

    return PersistedCplRun(
        request_extraction_id=request_extraction_id,
        missing_check_run_id=missing_check_run_id,
    )


def _lock_ready_case(connection: Connection, case_id: int) -> int:
    row = connection.execute(
        text(
            """
            SELECT c.status,
                   (
                       SELECT r.id
                       FROM sims.document_parse_run r
                       JOIN sims.uploaded_document d2
                         ON d2.file_asset_id = r.file_asset_id
                       WHERE d2.inspection_case_id = c.id
                         AND r.status IN ('SUCCESS', 'PARTIAL_SUCCESS')
                       ORDER BY r.attempt_no DESC
                       LIMIT 1
                   ) AS parse_run_id
            FROM sims.inspection_case c
            WHERE c.id = :case_id
            FOR UPDATE OF c
            """
        ),
        {"case_id": case_id},
    ).mappings().one_or_none()
    if row is None:
        raise CplPersistenceError("Inspection case was not found")
    if row["status"] != "CHECKING":
        raise CplPersistenceError("Inspection case is not ready for CPL")
    if row["parse_run_id"] is None:
        raise CplPersistenceError("A successful parse snapshot was not found")
    return row["parse_run_id"]


def _seed_cpl_schema(connection: Connection) -> tuple[int, dict[str, int]]:
    form_schema_id = connection.scalar(
        text(
            """
            INSERT INTO sims.form_schema (
                schema_name, version_no, description, is_active
            )
            VALUES (
                :schema_name, :version_no, :description, true
            )
            ON CONFLICT (schema_name, version_no) DO NOTHING
            RETURNING id
            """
        ),
        {
            "schema_name": FORM_SCHEMA_NAME,
            "version_no": FORM_SCHEMA_VERSION,
            "description": FORM_SCHEMA_DESCRIPTION,
        },
    )
    if form_schema_id is None:
        form_schema_id = connection.scalar(
            text(
                """
                SELECT id
                FROM sims.form_schema
                WHERE schema_name = :schema_name AND version_no = :version_no
                """
            ),
            {
                "schema_name": FORM_SCHEMA_NAME,
                "version_no": FORM_SCHEMA_VERSION,
            },
        )
    if form_schema_id is None:
        raise RuntimeError("Failed to seed CPL form schema")

    definitions = [
        {
            "form_schema_id": form_schema_id,
            "field_code": field_code.value,
            "field_label": CPL_FIELD_LABELS[field_code.value],
            "display_order": display_order,
        }
        for display_order, field_code in enumerate(CPL_FIELDS, start=1)
    ]
    connection.execute(
        text(
            """
            INSERT INTO sims.form_field_definition (
                form_schema_id,
                field_code,
                field_label,
                data_type,
                required_rule,
                display_order
            )
            VALUES (
                :form_schema_id,
                :field_code,
                :field_label,
                'JSON',
                '{}'::jsonb,
                :display_order
            )
            ON CONFLICT (form_schema_id, field_code) DO NOTHING
            """
        ),
        definitions,
    )
    rows = connection.execute(
        text(
            """
            SELECT id, field_code, field_label, parent_field_code,
                   data_type, display_order
            FROM sims.form_field_definition
            WHERE form_schema_id = :form_schema_id
            ORDER BY display_order
            """
        ),
        {"form_schema_id": form_schema_id},
    ).mappings().all()
    actual = {
        row["field_code"]: (
            row["id"],
            row["field_label"],
            row["parent_field_code"],
            row["data_type"],
            row["display_order"],
        )
        for row in rows
    }
    expected_codes = {field_code.value for field_code in CPL_FIELDS}
    if set(actual) != expected_codes:
        raise CplPersistenceError("CPL form schema does not contain exactly 13 fields")
    for definition in definitions:
        row = actual[definition["field_code"]]
        if row[1:] != (
            definition["field_label"],
            None,
            "JSON",
            definition["display_order"],
        ):
            raise CplPersistenceError("Existing CPL field definition is incompatible")
    return form_schema_id, {
        field_code: values[0] for field_code, values in actual.items()
    }


def _validate_evidence_contract(result: CplResult) -> None:
    for item in result.items:
        if item.status == CplStatus.PRESENT:
            if not item.occurrences:
                raise CplPersistenceError(
                    f"{item.field_code.value} requires at least one evidence occurrence"
                )
        for occurrence in item.occurrences:
            if (
                not occurrence.raw_text.strip()
                or not occurrence.block_id.strip()
                or not occurrence.extraction_method.strip()
                or not occurrence.source_locator
            ):
                raise CplPersistenceError(
                    f"{item.field_code.value} contains an invalid evidence occurrence"
                )


def request_reason_from_result(result: CplResult) -> RequestReason:
    request_type = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.REQUEST_TYPE
    )
    if request_type.status != CplStatus.PRESENT:
        return "UNKNOWN"

    selected = [
        occurrence.normalized_value.get("request_reason")
        for occurrence in request_type.occurrences
        if isinstance(occurrence.normalized_value, dict)
        and occurrence.normalized_value.get("selected") is True
    ]
    if len(selected) != 1 or selected[0] not in {
        "DETAIL_NEW",
        "SUBPROGRAM_NEW",
        "SUBSUBPROGRAM_NEW",
        "CONTENT_CHANGE",
    }:
        raise CplPersistenceError("CPL request type evidence is invalid")
    return selected[0]
