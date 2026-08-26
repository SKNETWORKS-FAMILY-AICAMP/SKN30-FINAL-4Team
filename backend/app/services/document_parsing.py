import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Literal

from sqlalchemy import Engine, text

from app.ports.document_parser import DocumentParser, FileSource
from app.ports.job_dispatcher import JobDispatcher
from app.ports.object_storage import ObjectStorage


logger = logging.getLogger(__name__)
SAFE_FAILURE_CODE = "DOCUMENT_PARSE_FAILED"
SAFE_FAILURE_MESSAGE = "The uploaded document could not be parsed"


class CaseNotFoundError(LookupError):
    pass


class CaseStateConflictError(ValueError):
    pass


@dataclass(frozen=True)
class StartedAnalysis:
    case_id: int
    job_id: str
    status: Literal["PARSING"] = "PARSING"


@dataclass(frozen=True)
class CaseStatus:
    case_id: int
    status: Literal["분석 중", "분석 완료", "분석 실패"]
    failure_code: str | None
    failure_message: str | None


# ponytail: 데모 계정 하나의 이력은 수십 건 규모라 고정 상한으로 충분하다.
# 프론트가 실제로 더 요구하면 그때 페이지네이션을 넣는다.
MAX_HISTORY_ITEMS = 50


@dataclass(frozen=True)
class CaseSummary:
    case_id: int
    title: str | None
    status: Literal["분석 중", "분석 완료", "분석 실패"]
    created_at: datetime


async def start_analysis(
    engine: Engine,
    dispatcher: JobDispatcher,
    parser: DocumentParser,
    owner_user_id: int,
    case_id: int,
) -> StartedAnalysis:
    with engine.begin() as connection:
        case = connection.execute(
            text(
                """
                SELECT c.status, d.file_asset_id
                FROM sims.inspection_case c
                JOIN sims.uploaded_document d ON d.inspection_case_id = c.id
                WHERE c.id = :case_id AND c.owner_user_id = :owner_user_id
                FOR UPDATE OF c
                """
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        ).mappings().one_or_none()
        if case is None:
            raise CaseNotFoundError
        if case["status"] != "UPLOADED":
            raise CaseStateConflictError(
                f"Analysis cannot start from {case['status']} status"
            )

        attempt_no = connection.scalar(
            text(
                """
                SELECT COALESCE(max(attempt_no), 0) + 1
                FROM sims.document_parse_run
                WHERE file_asset_id = :file_asset_id
                """
            ),
            {"file_asset_id": case["file_asset_id"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO sims.document_parse_run (
                    file_asset_id, attempt_no, parser_name, parser_version, status
                )
                VALUES (
                    :file_asset_id, :attempt_no, :parser_name, :parser_version, 'PENDING'
                )
                """
            ),
            {
                "file_asset_id": case["file_asset_id"],
                "attempt_no": attempt_no,
                "parser_name": parser.name,
                "parser_version": parser.version,
            },
        )
        connection.execute(
            text(
                """
                UPDATE sims.inspection_case
                SET status = 'PARSING', failure_code = NULL, failure_message = NULL
                WHERE id = :case_id
                """
            ),
            {"case_id": case_id},
        )

    try:
        job_id = await dispatcher.enqueue_analysis(case_id)
    except asyncio.CancelledError:
        _record_failure(engine, case_id)
        raise
    except Exception:
        _record_failure(engine, case_id)
        raise
    return StartedAnalysis(case_id=case_id, job_id=job_id)


async def run_case_parsing(
    engine: Engine,
    storage: ObjectStorage,
    parser: DocumentParser,
    case_id: int,
) -> None:
    try:
        source_row, parse_run_id = _claim_parse_run(engine, case_id)
        if not parser.supports(
            source_row["detected_mime_type"], source_row["extension"]
        ):
            raise ValueError("Parser does not support the uploaded document")
        content = await storage.open(source_row["storage_key"])
        try:
            await asyncio.to_thread(
                _verify_stored_source,
                content,
                source_row["size_bytes"],
                source_row["sha256_hex"],
            )
            parsed = await parser.parse(
                FileSource(
                    content=content,
                    filename=source_row["original_filename"],
                    mime_type=source_row["detected_mime_type"],
                    extension=source_row["extension"],
                )
            )
        finally:
            content.close()

        if (
            parsed.parser_name != parser.name
            or parsed.parser_version != parser.version
            or not parsed.text.strip()
            or not parsed.blocks
            or len({block.block_id for block in parsed.blocks}) != len(parsed.blocks)
            or any(not block.source_locator for block in parsed.blocks)
        ):
            raise ValueError("Parser returned an invalid result contract")

        structured_content = json.dumps(
            {
                "blocks": [block.model_dump(mode="json") for block in parsed.blocks],
                "warnings": parsed.warnings,
                "partial": parsed.partial,
            },
            ensure_ascii=False,
        )
        digest = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()
        terminal_status = "PARTIAL_SUCCESS" if parsed.partial else "SUCCESS"
        with engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE sims.document_parse_run
                    SET status = :status,
                        parser_name = :parser_name,
                        parser_version = :parser_version,
                        extracted_text = :extracted_text,
                        structured_content = CAST(:structured_content AS jsonb),
                        text_sha256_hex = :text_sha256_hex,
                        finished_at = statement_timestamp()
                    WHERE id = :parse_run_id AND status = 'PARSING'
                    """
                ),
                {
                    "status": terminal_status,
                    "parser_name": parsed.parser_name,
                    "parser_version": parsed.parser_version,
                    "extracted_text": parsed.text,
                    "structured_content": structured_content,
                    "text_sha256_hex": digest,
                    "parse_run_id": parse_run_id,
                },
            )
            if updated.rowcount != 1:
                raise RuntimeError("Parse run is not in PARSING status")
            case_updated = connection.execute(
                text(
                    """
                    UPDATE sims.inspection_case
                    SET status = 'CHECKING'
                    WHERE id = :case_id AND status = 'PARSING'
                    """
                ),
                {"case_id": case_id},
            )
            if case_updated.rowcount != 1:
                raise RuntimeError("Case is not in PARSING status")
    except asyncio.CancelledError:
        _record_failure(engine, case_id)
        raise
    except Exception as error:
        logger.warning(
            "Document parsing failed for case %s: %s",
            case_id,
            type(error).__name__,
        )
        _record_failure(engine, case_id)


def get_case_status(
    engine: Engine,
    owner_user_id: int,
    case_id: int,
) -> CaseStatus:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT id, status, failure_code, failure_message
                FROM sims.inspection_case
                WHERE id = :case_id AND owner_user_id = :owner_user_id
                """
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        ).mappings().one_or_none()
    if row is None:
        raise CaseNotFoundError
    return CaseStatus(
        case_id=row["id"],
        status=_ui_status(row["status"]),
        failure_code=row["failure_code"],
        failure_message=row["failure_message"],
    )


def list_cases(engine: Engine, owner_user_id: int) -> list[CaseSummary]:
    """Return one owner's analysis history, newest first."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.id, c.status, c.created_at, f.original_filename
                FROM sims.inspection_case c
                LEFT JOIN sims.uploaded_document d
                       ON d.inspection_case_id = c.id
                LEFT JOIN sims.file_asset f
                       ON f.id = d.file_asset_id
                WHERE c.owner_user_id = :owner_user_id
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT :limit
                """
            ),
            {"owner_user_id": owner_user_id, "limit": MAX_HISTORY_ITEMS},
        ).mappings().all()

    # ADR-006: 요청서에서 추출한 사업명을 쓰고, 없으면 원본 파일명을 쓴다.
    # 사업명 추출은 아직 없으므로 지금은 파일명만 채운다.
    return [
        CaseSummary(
            case_id=row["id"],
            title=row["original_filename"],
            status=_ui_status(row["status"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def _claim_parse_run(engine: Engine, case_id: int) -> tuple[dict, int]:
    with engine.begin() as connection:
        source = connection.execute(
            text(
                """
                SELECT f.storage_key, f.original_filename,
                       f.detected_mime_type, f.extension,
                       f.size_bytes, f.sha256_hex
                FROM sims.inspection_case c
                JOIN sims.uploaded_document d ON d.inspection_case_id = c.id
                JOIN sims.file_asset f ON f.id = d.file_asset_id
                WHERE c.id = :case_id AND c.status = 'PARSING'
                FOR UPDATE OF c
                """
            ),
            {"case_id": case_id},
        ).mappings().one_or_none()
        if source is None:
            raise RuntimeError("Case is not ready for parsing")
        parse_run_id = connection.scalar(
            text(
                """
                UPDATE sims.document_parse_run
                SET status = 'PARSING', started_at = statement_timestamp()
                WHERE id = (
                    SELECT r.id
                    FROM sims.document_parse_run r
                    JOIN sims.uploaded_document d ON d.file_asset_id = r.file_asset_id
                    WHERE d.inspection_case_id = :case_id AND r.status = 'PENDING'
                    ORDER BY r.attempt_no DESC
                    LIMIT 1
                    FOR UPDATE OF r
                )
                RETURNING id
                """
            ),
            {"case_id": case_id},
        )
        if parse_run_id is None:
            raise RuntimeError("Pending parse run was not found")
    return dict(source), parse_run_id


def _verify_stored_source(
    content: BinaryIO,
    expected_size: int,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    size = 0
    content.seek(0)
    while chunk := content.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    content.seek(0)
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise ValueError("Stored source does not match its upload snapshot")


def _ui_status(
    internal_status: str,
) -> Literal["분석 중", "분석 완료", "분석 실패"]:
    if internal_status == "COMPLETED":
        return "분석 완료"
    if internal_status == "FAILED":
        return "분석 실패"
    return "분석 중"


def _record_failure(engine: Engine, case_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE sims.document_parse_run r
                SET status = 'FAILED',
                    error_code = :failure_code,
                    error_message = :failure_message,
                    finished_at = statement_timestamp()
                FROM sims.uploaded_document d
                WHERE d.inspection_case_id = :case_id
                  AND r.file_asset_id = d.file_asset_id
                  AND r.status IN ('PENDING', 'PARSING')
                """
            ),
            {
                "case_id": case_id,
                "failure_code": SAFE_FAILURE_CODE,
                "failure_message": SAFE_FAILURE_MESSAGE,
            },
        )
        connection.execute(
            text(
                """
                UPDATE sims.inspection_case
                SET status = 'FAILED',
                    failure_code = :failure_code,
                    failure_message = :failure_message
                WHERE id = :case_id AND status = 'PARSING'
                """
            ),
            {
                "case_id": case_id,
                "failure_code": SAFE_FAILURE_CODE,
                "failure_message": SAFE_FAILURE_MESSAGE,
            },
        )
