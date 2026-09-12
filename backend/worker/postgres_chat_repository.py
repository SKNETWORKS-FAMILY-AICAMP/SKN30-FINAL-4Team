"""Trusted PostgreSQL queue adapter for asynchronous chat responses.

Migration 26 owns the queue state machine and lease fencing.  This adapter is
only a transport boundary: one fresh psycopg connection per operation, no
frontend credentials, and no database access from the chat handler itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from worker.runtime import ClaimedJob, JobFailure, JobKey, JobResult, ProcessingRunPK

__all__ = [
    "PostgresChatJobRepository",
    "ChatWorkerDatabaseUnavailable",
    "ChatWorkerQueueContractError",
]


_CLAIM_SQL = """
SELECT
    assistant_message_id,
    analysis_case_id,
    analysis_session_id,
    user_message_id,
    owner_id,
    question,
    result_payload,
    conversation,
    processing_run_pk,
    attempt_count,
    lease_expires_at,
    heartbeat_interval_seconds
FROM workspace.claim_next_conversation_message(%s, %s)
"""

_HEARTBEAT_SQL = """
SELECT workspace.heartbeat_conversation_message(%s, %s, %s) AS is_live
"""

_COMPLETE_SQL = """
SELECT workspace.complete_conversation_message(%s, %s, %s, %s::uuid[]) AS is_completed
"""

_FAIL_SQL = """
SELECT workspace.fail_conversation_message(%s, %s, %s, %s, %s) AS is_failed
"""


class ChatWorkerDatabaseUnavailable(RuntimeError):
    """A safe operational error suitable for worker logs."""


class ChatWorkerQueueContractError(RuntimeError):
    """Migration 26 returned an unsafe or incomplete queue value."""


class _Cursor(Protocol):
    def __enter__(self) -> "_Cursor": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def execute(self, query: str, params: tuple[object, ...]) -> object: ...

    def fetchone(self) -> Mapping[str, Any] | None: ...


class _Connection(Protocol):
    def __enter__(self) -> "_Connection": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def cursor(self) -> _Cursor: ...


ConnectionFactory = Callable[..., _Connection]

_URL_CREDENTIALS = re.compile(
    r"\b(?P<scheme>postgres(?:ql)?|https?)://[^\s/@:]+(?::[^\s/@]*)?@",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?P<name>password|passwd|pwd|token|api[_-]?key|secret)"
    r"(?P<separator>\s*[=:]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")
_MAX_INTERNAL_ERROR_CHARS = 1_000

_PUBLIC_FAILURE_CODE = "CHAT_LLM_FAILED"
_PUBLIC_FAILURE_MESSAGE = "답변을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요."


def _required(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ChatWorkerQueueContractError(
            "The chat worker queue returned an incomplete claim"
        )
    return value


def _uuid(value: Any, *, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ChatWorkerQueueContractError(
            f"The chat worker queue returned an invalid {field}"
        ) from None


def _bool_result(row: Mapping[str, Any] | None, key: str) -> bool:
    if row is None or key not in row or not isinstance(row[key], bool):
        raise ChatWorkerQueueContractError(
            "The chat worker queue returned an invalid transition"
        )
    return row[key]


def _redact_internal_error(value: str) -> str:
    collapsed = _WHITESPACE.sub(" ", value).strip()
    collapsed = _URL_CREDENTIALS.sub(r"\g<scheme>://[redacted]@", collapsed)
    collapsed = _SECRET_ASSIGNMENT.sub(
        r"\g<name>\g<separator>[redacted]", collapsed
    )
    return collapsed[:_MAX_INTERNAL_ERROR_CHARS]


def _internal_failure(failure: JobFailure) -> str:
    kind = re.sub(r"[^A-Za-z0-9_.-]", "_", failure.kind).strip("_")[:80]
    message = _redact_internal_error(failure.message)
    return f"{kind or 'WorkerFailure'}: {message}" if message else kind or "WorkerFailure"


def _evidence_ids(result: JobResult) -> list[UUID]:
    """Extract only UUID evidence references from the handler result.

    ``intent``, ``warnings`` and prompt metadata are intentionally not stored:
    migration 26 has no corresponding user-facing columns.  Invalid or null
    IDs are ignored so a useful answer is not discarded because the LLM added
    one malformed reference.
    """

    if not isinstance(result, Mapping):
        raise ChatWorkerQueueContractError("The chat result must be a mapping")
    references = result.get("references", [])
    if references is None:
        return []
    if not isinstance(references, Sequence) or isinstance(references, (str, bytes)):
        raise ChatWorkerQueueContractError("The chat result references must be a list")
    ids: list[UUID] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        raw = reference.get("evidence_id")
        if raw in (None, ""):
            continue
        try:
            evidence_id = UUID(str(raw))
        except (TypeError, ValueError, AttributeError):
            continue
        if evidence_id not in ids:
            ids.append(evidence_id)
    return ids


def _content(result: JobResult) -> str:
    if not isinstance(result, Mapping):
        raise ChatWorkerQueueContractError("The chat result must be a mapping")
    value = result.get("content")
    if not isinstance(value, str) or not value.strip():
        raise ChatWorkerQueueContractError("The chat result content is required")
    if len(value) > 12_000:
        raise ChatWorkerQueueContractError("The chat result content is too long")
    return value.strip()


class PostgresChatJobRepository:
    """Migration-26-backed implementation of ``worker.runtime.JobRepository``."""

    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 10,
        connect: ConnectionFactory | None = None,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url must not be empty")
        if connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        self._database_url = database_url
        self._connect_timeout_seconds = connect_timeout_seconds
        self._connect: ConnectionFactory = connect or psycopg.connect

    def __repr__(self) -> str:
        return (
            "PostgresChatJobRepository("
            f"connect_timeout_seconds={self._connect_timeout_seconds})"
        )

    def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        row = self._fetchone(_CLAIM_SQL, (worker_id, lease_seconds))
        if row is None:
            return None

        payload_result = _required(row, "result_payload")
        if not isinstance(payload_result, Mapping):
            raise ChatWorkerQueueContractError(
                "The chat worker queue returned an invalid result payload"
            )
        attempt_count = _required(row, "attempt_count")
        if not isinstance(attempt_count, int) or isinstance(attempt_count, bool):
            raise ChatWorkerQueueContractError(
                "The chat worker queue returned an invalid attempt count"
            )
        heartbeat_interval = _required(row, "heartbeat_interval_seconds")
        if not isinstance(heartbeat_interval, int) or isinstance(heartbeat_interval, bool):
            raise ChatWorkerQueueContractError(
                "The chat worker queue returned an invalid heartbeat interval"
            )
        conversation = row.get("conversation")
        if conversation is None:
            conversation = []
        if not isinstance(conversation, list):
            raise ChatWorkerQueueContractError(
                "The chat worker queue returned an invalid conversation"
            )

        return ClaimedJob(
            job_pk=_uuid(_required(row, "assistant_message_id"), field="assistant message id"),
            processing_run_pk=_uuid(
                _required(row, "processing_run_pk"), field="processing run id"
            ),
            payload={
                "question": _required(row, "question"),
                "result_payload": dict(payload_result),
                "conversation": conversation,
                "analysis_case_id": _uuid(
                    _required(row, "analysis_case_id"), field="analysis case id"
                ),
                "analysis_session_id": _uuid(
                    _required(row, "analysis_session_id"), field="analysis session id"
                ),
                "user_message_id": _uuid(
                    _required(row, "user_message_id"), field="user message id"
                ),
                "owner_id": _uuid(_required(row, "owner_id"), field="owner id"),
                "attempt_count": attempt_count,
                "lease_expires_at": _required(row, "lease_expires_at"),
                "heartbeat_interval_seconds": heartbeat_interval,
            },
        )

    def heartbeat(
        self,
        *,
        job_pk: JobKey,
        processing_run_pk: ProcessingRunPK,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        del worker_id
        row = self._fetchone(
            _HEARTBEAT_SQL,
            (_uuid(job_pk, field="assistant message id"), _uuid(processing_run_pk, field="processing run id"), lease_seconds),
        )
        return _bool_result(row, "is_live")

    def complete(
        self,
        *,
        job_pk: JobKey,
        processing_run_pk: ProcessingRunPK,
        worker_id: str,
        result: JobResult,
    ) -> bool:
        del worker_id
        row = self._fetchone(
            _COMPLETE_SQL,
            (
                _uuid(job_pk, field="assistant message id"),
                _uuid(processing_run_pk, field="processing run id"),
                _content(result),
                _evidence_ids(result),
            ),
        )
        return _bool_result(row, "is_completed")

    def fail(
        self,
        *,
        job_pk: JobKey,
        processing_run_pk: ProcessingRunPK,
        worker_id: str,
        failure: JobFailure,
    ) -> bool:
        del worker_id
        row = self._fetchone(
            _FAIL_SQL,
            (
                _uuid(job_pk, field="assistant message id"),
                _uuid(processing_run_pk, field="processing run id"),
                _PUBLIC_FAILURE_CODE,
                _PUBLIC_FAILURE_MESSAGE,
                _internal_failure(failure),
            ),
        )
        return _bool_result(row, "is_failed")

    def _fetchone(
        self, query: str, params: tuple[object, ...]
    ) -> Mapping[str, Any] | None:
        try:
            with self._connect(
                self._database_url,
                connect_timeout=self._connect_timeout_seconds,
                row_factory=dict_row,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
                    return cursor.fetchone()
        except (psycopg.Error, OSError, ConnectionError, TimeoutError):
            raise ChatWorkerDatabaseUnavailable(
                "Chat worker database is unavailable"
            ) from None
