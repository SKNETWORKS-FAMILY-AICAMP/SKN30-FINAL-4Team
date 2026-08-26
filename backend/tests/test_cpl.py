import asyncio
import io
import json
import os
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings
from app.core.security import hash_password
from app.infrastructure.openai_llm_client import OpenAILLMClient
from app.ports.document_parser import FileSource
from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.schemas.cpl import (
    CPL_FIELDS,
    CplFieldCode,
    CplItem,
    CplResult,
    CplSemanticResponse,
    CplStatus,
)
from app.schemas.parsed_document import DocumentBlock, ParsedDocument
from app.services.analysis_pipeline import _complete_semantic_review
from app.services.cpl.logic_validator import (
    evaluate_cpl_rules,
    ground_llm_response,
    merge_llm_result,
)


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"


def parsed_document() -> ParsedDocument:
    return ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="CPL fixture",
        blocks=[
            DocumentBlock(
                block_id="request:1",
                block_type="paragraph",
                text="■ 세부사업 신설  □ 내역사업 신설  □ 내내역사업 신설  □ 사업내용 변경",
                page_no=1,
                section_path=["신청 개요"],
                source_locator={"paragraph_index": 1},
            ),
            DocumentBlock(
                block_id="purpose:1",
                block_type="paragraph",
                text="사업 목적: 지역 기업의 디지털 전환 지원",
                page_no=2,
                section_path=["사업 개요", "목적"],
                source_locator={"paragraph_index": 2},
            ),
            DocumentBlock(
                block_id="purpose:2",
                block_type="paragraph",
                text="사업 목적: 생산성 향상",
                page_no=3,
                section_path=["사업 개요", "목적"],
                source_locator={"paragraph_index": 3},
            ),
            DocumentBlock(
                block_id="need:1",
                block_type="paragraph",
                text="사업 필요성: 지역 기업의 설비 노후화가 심각함",
                page_no=4,
                section_path=["필요성"],
                source_locator={"paragraph_index": 4},
            ),
            DocumentBlock(
                block_id="period:1",
                block_type="paragraph",
                text="사업기간: 2026년 ~ 2028년",
                page_no=5,
                section_path=["사업 개요"],
                source_locator={"paragraph_index": 5},
            ),
            DocumentBlock(
                block_id="budget:1",
                block_type="paragraph",
                text="사업예산: 3억원",
                page_no=6,
                section_path=["예산"],
                source_locator={"paragraph_index": 6},
            ),
        ],
    )


def result_with_statuses(statuses: list[CplStatus]) -> CplResult:
    return CplResult(
        ruleset_version="cpl-alpha-v0.1",
        items=[
            CplItem(field_code=field_code, status=status)
            for field_code, status in zip(CPL_FIELDS, statuses, strict=True)
        ],
    )


def test_cpl_contract_has_exact_fields_and_confirmation_rate() -> None:
    assert [field.value for field in CPL_FIELDS] == [
        "REQUEST_TYPE",
        "PURPOSE_GOAL",
        "IMPLEMENTATION_PLAN",
        "BUSINESS_PERIOD",
        "NEW_OR_CHANGED_CONTENT",
        "BUSINESS_NEED",
        "LEGAL_BASIS",
        "LINKED_POLICY",
        "BUDGET",
        "TARGET_AND_CONDITIONS",
        "SUPPORT_CONTENT_AND_SCALE",
        "DELIVERY_SYSTEM",
        "EXPECTED_EFFECTS_AND_PERFORMANCE",
    ]
    result = result_with_statuses(
        [
            CplStatus.PRESENT,
            CplStatus.NOT_APPLICABLE,
            CplStatus.PRESENT,
            *([CplStatus.MISSING] * 10),
        ]
    )
    assert result.confirmed_count == 3
    assert result.total_count == 13
    assert result.confirmation_rate == 3 / 13 * 100


def test_rules_preserve_every_occurrence_and_parser_lineage() -> None:
    result = evaluate_cpl_rules(parsed_document())
    purpose = next(
        item for item in result.items if item.field_code == CplFieldCode.PURPOSE_GOAL
    )
    assert purpose.status == CplStatus.NEEDS_CONFIRMATION
    assert [occurrence.raw_text for occurrence in purpose.occurrences] == [
        "지역 기업의 디지털 전환 지원",
        "생산성 향상",
    ]
    assert [occurrence.page_no for occurrence in purpose.occurrences] == [2, 3]
    assert [occurrence.block_id for occurrence in purpose.occurrences] == [
        "purpose:1",
        "purpose:2",
    ]
    for occurrence in purpose.occurrences:
        assert occurrence.normalized_value
        assert occurrence.section_path == ["사업 개요", "목적"]
        assert occurrence.source_locator
        assert occurrence.extraction_method == "RULE"
    assert all(item.status != CplStatus.PARSE_FAILED for item in result.items)


def test_llm_failure_keeps_rule_evidence_and_marks_only_unresolved_semantics() -> None:
    rule_result = evaluate_cpl_rules(parsed_document())
    result = merge_llm_result(rule_result, llm_error="LLM_TIMEOUT")
    purpose = next(
        item for item in result.items if item.field_code == CplFieldCode.PURPOSE_GOAL
    )
    need = next(
        item for item in result.items if item.field_code == CplFieldCode.BUSINESS_NEED
    )
    plan = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )
    assert purpose.status == CplStatus.NEEDS_CONFIRMATION
    assert purpose.reason_code == "LLM_TIMEOUT"
    assert len(purpose.occurrences) == 2
    assert need.status == CplStatus.NEEDS_CONFIRMATION
    assert need.reason_code == "LLM_TIMEOUT"
    assert need.occurrences[0].raw_text.startswith("지역 기업")
    assert plan.status == CplStatus.NEEDS_CONFIRMATION
    assert plan.reason_code == "LLM_TIMEOUT"
    assert plan.occurrences == []
    assert "CPL semantic extraction incomplete: LLM_TIMEOUT" in result.warnings


def test_llm_evidence_is_grounded_from_parser_metadata() -> None:
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "BUSINESS_NEED",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "block_id": "need:1",
                            "raw_text": "지역 기업의 설비 노후화가 심각함",
                            "normalized_value": "문제 현황",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    grounded = ground_llm_response(
        parsed_document(), response, {CplFieldCode.BUSINESS_NEED}
    )
    occurrence = grounded[0].occurrences[0]
    assert occurrence.page_no == 4
    assert occurrence.section_path == ["필요성"]
    assert occurrence.source_locator == {"paragraph_index": 4}
    assert occurrence.extraction_method == "LLM"

    response.items[0].occurrences[0].raw_text = "문서에 없는 문장"
    with pytest.raises(ValueError, match="grounded"):
        ground_llm_response(
            parsed_document(), response, {CplFieldCode.BUSINESS_NEED}
        )


def test_openai_adapter_uses_responses_structured_output_contract() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        output = {
            "items": [
                {
                    "field_code": "PURPOSE_GOAL",
                    "status": "MISSING",
                    "occurrences": [],
                    "reason_code": "EXPLICIT_VALUE_NOT_FOUND",
                    "explanation": None,
                }
            ]
        }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": json.dumps(output)}
                        ]
                    }
                ],
            },
        )

    client = OpenAILLMClient(
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        model_profiles={"gpt-4o-mini": "gpt-4o-mini"},
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.generate_structured(
            task_name="cpl_semantic_evidence",
            messages=[Message(role="user", content="fixture")],
            response_schema=CplSemanticResponse,
            model_profile="gpt-4o-mini",
        )
    )
    assert isinstance(result, CplSemanticResponse)
    assert captured["model"] == "gpt-4o-mini"
    assert captured["store"] is False
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (LLMUnavailableError("unavailable"), "LLM_UNAVAILABLE"),
        (LLMTimeoutError("timeout"), "LLM_TIMEOUT"),
        (LLMInvalidResponseError("invalid"), "LLM_INVALID_RESPONSE"),
    ],
)
def test_pipeline_maps_llm_failures_without_losing_rule_occurrences(
    error: Exception,
    reason_code: str,
) -> None:
    class FailingLlm:
        async def generate_structured(self, **_kwargs):
            raise error

    document = parsed_document()
    rule_result = evaluate_cpl_rules(document)
    semantic_fields = {
        item.field_code
        for item in rule_result.items
        if item.field_code
        in {
            CplFieldCode.PURPOSE_GOAL,
            CplFieldCode.IMPLEMENTATION_PLAN,
            CplFieldCode.NEW_OR_CHANGED_CONTENT,
            CplFieldCode.BUSINESS_NEED,
            CplFieldCode.TARGET_AND_CONDITIONS,
            CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
            CplFieldCode.DELIVERY_SYSTEM,
            CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
        }
        and item.status != CplStatus.PRESENT
    }
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret=JWT_SECRET,
        openai_api_key=None,
    )
    result = asyncio.run(
        _complete_semantic_review(
            document,
            rule_result,
            semantic_fields,
            FailingLlm(),
            settings,
        )
    )
    need = next(
        item for item in result.items if item.field_code == CplFieldCode.BUSINESS_NEED
    )
    assert need.reason_code == reason_code
    assert need.status == CplStatus.NEEDS_CONFIRMATION
    assert need.occurrences[0].raw_text.startswith("지역 기업")
    assert f"CPL semantic extraction incomplete: {reason_code}" in result.warnings


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if value is None:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL integration")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    value = create_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def create_user(engine: Engine) -> Iterator[Callable[[str], int]]:
    user_ids: list[int] = []

    def create(login_id: str) -> int:
        with engine.begin() as connection:
            user_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.app_user (login_id, email, password_hash)
                    VALUES (:login_id, :email, :password_hash)
                    RETURNING id
                    """
                ),
                {
                    "login_id": login_id,
                    "email": f"{login_id}@example.com",
                    "password_hash": hash_password(PASSWORD),
                },
            )
        assert user_id is not None
        user_ids.append(user_id)
        return user_id

    yield create
    with engine.begin() as connection:
        for user_id in user_ids:
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


class CplFakeParser:
    name = "cpl-fixture-parser"
    version = "1.0"

    def supports(self, mime_type: str, extension: str) -> bool:
        return mime_type == "application/hwp+zip" and extension == "hwpx"

    async def parse(self, source: FileSource) -> ParsedDocument:
        assert source.content.read(2) == b"PK"
        return parsed_document().model_copy(
            update={"parser_name": self.name, "parser_version": self.version}
        )


def hwpx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "mimetype", b"application/hwp+zip", compress_type=zipfile.ZIP_STORED
        )
        archive.writestr("Contents/section0.xml", "<section />")
    return output.getvalue()


def test_pipeline_persists_cpl_snapshot_and_all_occurrences(
    database_url: str,
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
) -> None:
    from main import create_app

    login_id = "cpl-pipeline"
    create_user(login_id)
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path,
    )
    with TestClient(create_app(settings, CplFakeParser(), None, None)) as client:
        token = client.post(
            "/api/v1/auth/login",
            json={"login_id": login_id, "password": PASSWORD},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        uploaded = client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
        )
        assert uploaded.status_code == 201
        case_id = uploaded.json()["case_id"]
        assert client.post(
            f"/api/v1/cases/{case_id}/analyze", headers=headers
        ).status_code == 202

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with engine.connect() as connection:
                extraction_id = connection.scalar(
                    text(
                        "SELECT id FROM sims.request_extraction "
                        "WHERE inspection_case_id = :case_id"
                    ),
                    {"case_id": case_id},
                )
            if extraction_id is not None:
                break
            time.sleep(0.01)
        else:
            with engine.connect() as connection:
                diagnostic = connection.execute(
                    text(
                        """
                        SELECT c.status, c.failure_code, r.status AS parse_status,
                               r.error_code AS parse_error_code
                        FROM sims.inspection_case c
                        JOIN sims.uploaded_document d ON d.inspection_case_id = c.id
                        JOIN sims.document_parse_run r ON r.file_asset_id = d.file_asset_id
                        WHERE c.id = :case_id
                        """
                    ),
                    {"case_id": case_id},
                ).mappings().one()
            pytest.fail(f"CPL extraction was not persisted: {dict(diagnostic)}")

    with engine.connect() as connection:
        extraction = connection.execute(
            text(
                """
                SELECT e.request_reason, e.status, e.raw_extraction,
                       c.status AS case_status, c.failure_code
                FROM sims.request_extraction e
                JOIN sims.inspection_case c ON c.id = e.inspection_case_id
                WHERE e.inspection_case_id = :case_id
                """
            ),
            {"case_id": case_id},
        ).mappings().one()
        purpose = connection.execute(
            text(
                """
                SELECT v.raw_text, v.page_no, v.normalized_value, v.source_locator
                FROM sims.request_field_value v
                JOIN sims.form_field_definition f ON f.id = v.field_definition_id
                WHERE v.request_extraction_id = :extraction_id
                  AND f.field_code = 'PURPOSE_GOAL'
                """
            ),
            {"extraction_id": extraction_id},
        ).mappings().one()
        items = connection.execute(
            text(
                """
                SELECT f.field_code, i.result_status, i.reason_code,
                       i.evidence_field_value_id
                FROM sims.missing_check_item i
                JOIN sims.form_field_definition f ON f.id = i.field_definition_id
                JOIN sims.missing_check_run r ON r.id = i.missing_check_run_id
                WHERE r.inspection_case_id = :case_id
                ORDER BY f.display_order
                """
            ),
            {"case_id": case_id},
        ).mappings().all()

    snapshot = extraction["raw_extraction"]
    assert extraction["request_reason"] == "DETAIL_NEW"
    assert extraction["status"] == "SUCCESS"
    assert extraction["case_status"] == "FAILED"
    assert extraction["failure_code"] == "RETRIEVAL_UNAVAILABLE"
    assert len(snapshot["items"]) == 13
    assert snapshot["confirmed_count"] == 3
    assert snapshot["total_count"] == 13
    assert snapshot["confirmation_rate"] == 3 / 13 * 100
    assert snapshot["model_profile"] == "gpt-4o-mini"
    assert "CPL semantic extraction incomplete: LLM_UNAVAILABLE" in snapshot[
        "warnings"
    ]

    occurrences = purpose["normalized_value"]["occurrences"]
    assert purpose["raw_text"] is None
    assert purpose["page_no"] is None
    assert len(occurrences) == 2
    assert {item["page_no"] for item in occurrences} == {2, 3}
    assert all(
        {
            "raw_text",
            "normalized_value",
            "page_no",
            "section_path",
            "source_locator",
            "block_id",
            "extraction_method",
        }.issubset(item)
        for item in occurrences
    )
    assert len(purpose["source_locator"]["occurrences"]) == 2
    assert [item["field_code"] for item in items] == [
        field.value for field in CPL_FIELDS
    ]
    unresolved = next(
        item for item in items if item["field_code"] == "IMPLEMENTATION_PLAN"
    )
    assert unresolved["result_status"] == "NEEDS_CONFIRMATION"
    assert unresolved["reason_code"] == "LLM_UNAVAILABLE"
    assert unresolved["evidence_field_value_id"] is None
