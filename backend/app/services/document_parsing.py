import asyncio
import base64
import hashlib
import json
import logging
import time
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


class InvalidCursorError(ValueError):
    pass


@dataclass(frozen=True)
class StartedAnalysis:
    case_id: int
    job_id: str
    status: Literal["PARSING"] = "PARSING"


UiStatus = Literal["IN_PROGRESS", "COMPLETED", "FAILED"]

# 롱폴링 한 번이 응답을 붙잡고 있는 시간. 중간 네트워크 장비가 보통 30~60초
# 유휴 연결을 끊으므로 그 전에 한 번 끊고 다시 잇는다.
STATUS_WAIT_SECONDS = 25
STATUS_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class CaseStatus:
    case_id: int
    status: UiStatus


DEFAULT_HISTORY_LIMIT = 5
MAX_HISTORY_LIMIT = 50


@dataclass(frozen=True)
class CaseSummary:
    case_id: int
    title: str | None
    completed_at: datetime


@dataclass(frozen=True)
class CasePage:
    items: list[CaseSummary]
    next_cursor: str | None


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
    """실패 사유는 내보내지 않는다. 화면은 실패 하나로만 다룬다."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT id, status
                FROM sims.inspection_case
                WHERE id = :case_id AND owner_user_id = :owner_user_id
                """
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        ).mappings().one_or_none()
    if row is None:
        raise CaseNotFoundError
    return CaseStatus(case_id=row["id"], status=_ui_status(row["status"]))


async def wait_for_case_status(
    engine: Engine,
    owner_user_id: int,
    case_id: int,
    *,
    timeout_seconds: float = STATUS_WAIT_SECONDS,
    interval_seconds: float = STATUS_POLL_INTERVAL_SECONDS,
) -> CaseStatus:
    """상태가 바뀔 때까지 응답을 붙잡고 있다가 바뀌는 즉시 돌려준다.

    짧은 주기로 계속 물어보는 방식은 프론트가 거부했고, SSE 와 WebSocket 은
    브라우저 API 가 Authorization 헤더를 지원하지 않아 인증 우회가 필요하다.
    같은 엔드포인트를 그대로 두고 서버가 기다리는 편이 요청 수도 적고
    완료를 더 빨리 알린다.

    제한 시간 안에 바뀌지 않으면 현재 상태로 응답한다. 호출자가 다시 부른다.
    """
    deadline = time.monotonic() + timeout_seconds
    current = await asyncio.to_thread(
        get_case_status, engine, owner_user_id, case_id
    )
    while current.status == "IN_PROGRESS" and time.monotonic() < deadline:
        await asyncio.sleep(interval_seconds)
        current = await asyncio.to_thread(
            get_case_status, engine, owner_user_id, case_id
        )
    return current


def list_cases(
    engine: Engine,
    owner_user_id: int,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
    cursor: str | None = None,
) -> CasePage:
    """완료된 분석만 최신순으로 한 쪽씩 돌려준다.

    진행 중인 건은 담지 않는다. 업로드한 브라우저가 case_id 로 복구하며,
    분석이 끝나면 이력에 나타난다. 실패한 건은 업로드 화면에서만 알린다.
    그래서 이력 항목에는 상태 필드가 없다.
    """
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    keyset = _decode_cursor(cursor)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.id, c.completed_at, f.original_filename
                FROM sims.inspection_case c
                LEFT JOIN sims.uploaded_document d
                       ON d.inspection_case_id = c.id
                LEFT JOIN sims.file_asset f
                       ON f.id = d.file_asset_id
                WHERE c.owner_user_id = :owner_user_id
                  AND c.status = 'COMPLETED'
                  AND c.completed_at IS NOT NULL
                  AND (
                        CAST(:cursor_completed_at AS timestamptz) IS NULL
                     OR (c.completed_at, c.id)
                        < (CAST(:cursor_completed_at AS timestamptz), :cursor_id)
                  )
                ORDER BY c.completed_at DESC, c.id DESC
                LIMIT :limit
                """
            ),
            {
                "owner_user_id": owner_user_id,
                "cursor_completed_at": keyset[0] if keyset else None,
                "cursor_id": keyset[1] if keyset else 0,
                "limit": limit + 1,
            },
        ).mappings().all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    # ADR-006: 요청서에서 추출한 사업명을 쓰기로 했으나 추출 기능을 만들지
    # 않기로 해서 업로드한 파일명을 그대로 쓴다.
    items = [
        CaseSummary(
            case_id=row["id"],
            title=row["original_filename"],
            completed_at=row["completed_at"],
        )
        for row in rows
    ]
    next_cursor = (
        _encode_cursor(items[-1].completed_at, items[-1].case_id)
        if has_more and items
        else None
    )
    return CasePage(items=items, next_cursor=next_cursor)


def _encode_cursor(completed_at: datetime, case_id: int) -> str:
    raw = f"{completed_at.isoformat()}|{case_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    """커서는 서버가 만든 값만 받는다. 조작된 값은 조용히 무시한다."""
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        completed_at, _, case_id = base64.urlsafe_b64decode(padded).decode(
            "utf-8"
        ).rpartition("|")
        return datetime.fromisoformat(completed_at), int(case_id)
    except (ValueError, TypeError):
        raise InvalidCursorError from None


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


def _ui_status(internal_status: str) -> UiStatus:
    if internal_status == "COMPLETED":
        return "COMPLETED"
    if internal_status == "FAILED":
        return "FAILED"
    return "IN_PROGRESS"


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
