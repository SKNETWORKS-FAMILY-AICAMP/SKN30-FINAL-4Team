"""Trusted PostgreSQL adapter for the fenced PDF report worker queue.

Migration 41 owns every queue transition.  This module only validates the
transport contract and opens a fresh psycopg connection per operation so the
runtime heartbeat never shares a connection with rendering or completion.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, Self
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from worker.runtime import ClaimedJob, JobFailure, JobKey, JobResult, ProcessingRunPK

__all__ = [
    "PostgresReportJobRepository",
    "ReportWorkerDatabaseUnavailable",
    "ReportWorkerQueueContractError",
]


_CLAIM_SQL = """
SELECT
    report_artifact_id,
    analysis_case_id,
    owner_id,
    source_analysis_run_id,
    result_payload,
    sim_details,
    processing_run_pk,
    storage_object_key,
    attempt_count,
    lease_expires_at,
    heartbeat_interval_seconds
FROM workspace.claim_next_pdf_report_v1(%s, %s)
"""

_HEARTBEAT_SQL = """
SELECT workspace.heartbeat_pdf_report_v1(%s, %s, %s) AS is_live
"""

_COMPLETE_SQL = """
SELECT workspace.complete_pdf_report_v1(%s, %s, %s, %s, %s, %s, %s) AS is_completed
"""

_FAIL_SQL = """
SELECT workspace.fail_pdf_report_v1(%s, %s, %s, %s, %s) AS is_failed
"""


_CLAIM_CLEANUP_SQL = """
SELECT * FROM workspace.claim_next_pdf_report_cleanup_v1(%s, %s)
"""

_COMPLETE_CLEANUP_SQL = """
SELECT workspace.complete_pdf_report_cleanup_v1(%s, %s) AS is_completed
"""
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
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_INTERNAL_ERROR_CHARS = 1_000

_REPORT_BUCKET = "analysis-reports"
_PDF_MIME_TYPE = "application/pdf"
_PUBLIC_FAILURE_CODE = "PDF_REPORT_FAILED"
_PUBLIC_FAILURE_MESSAGE = "PDF 보고서를 생성하지 못했습니다. 잠시 후 다시 시도해 주세요."


class ReportWorkerDatabaseUnavailable(RuntimeError):
    """A safe operational error suitable for runtime logs."""


class ReportWorkerQueueContractError(RuntimeError):
    """The report queue returned an incomplete or unsafe value."""


class _Cursor(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def execute(self, query: str, params: tuple[object, ...]) -> object: ...

    def fetchone(self) -> Mapping[str, Any] | None: ...


class _Connection(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def cursor(self) -> _Cursor: ...


ConnectionFactory = Callable[..., _Connection]


def _required(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ReportWorkerQueueContractError(
            "The report worker queue returned an incomplete claim"
        )
    return value


def _uuid(value: Any, *, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ReportWorkerQueueContractError(
            f"The report worker queue returned an invalid {field}"
        ) from None


def _bool_result(row: Mapping[str, Any] | None, key: str) -> bool:
    if row is None or key not in row or not isinstance(row[key], bool):
        raise ReportWorkerQueueContractError(
            "The report worker queue returned an invalid transition"
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


def _result_artifact(result: JobResult) -> tuple[str, str, str, str, int]:
    """Validate the immutable PDF metadata produced by ``ReportJobHandler``."""

    if not isinstance(result, Mapping):
        raise ReportWorkerQueueContractError("The report result must be a mapping")
    expected = {
        "storage_bucket",
        "storage_object_key",
        "content_sha256",
        "mime_type",
        "size_bytes",
    }
    if set(result) != expected:
        raise ReportWorkerQueueContractError("The report result has an invalid shape")
    bucket = result["storage_bucket"]
    object_key = result["storage_object_key"]
    digest = result["content_sha256"]
    mime_type = result["mime_type"]
    size_bytes = result["size_bytes"]
    if bucket != _REPORT_BUCKET:
        raise ReportWorkerQueueContractError("The report result bucket is invalid")
    if (
        not isinstance(object_key, str)
        or not object_key.strip()
        or object_key.startswith("/")
        or ".." in object_key
    ):
        raise ReportWorkerQueueContractError("The report result object key is invalid")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ReportWorkerQueueContractError("The report result digest is invalid")
    if mime_type != _PDF_MIME_TYPE:
        raise ReportWorkerQueueContractError("The report result MIME type is invalid")
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes <= 0
    ):
        raise ReportWorkerQueueContractError("The report result size is invalid")
    return bucket, object_key, digest, mime_type, size_bytes


class PostgresReportJobRepository:
    """Migration-41 implementation of :class:`worker.runtime.JobRepository`."""

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
            "PostgresReportJobRepository("
            f"connect_timeout_seconds={self._connect_timeout_seconds})"
        )

    def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        row = self._fetchone(_CLAIM_SQL, (worker_id, lease_seconds))
        if row is None:
            return None
        attempt_count = _required(row, "attempt_count")
        if not isinstance(attempt_count, int) or isinstance(attempt_count, bool):
            raise ReportWorkerQueueContractError(
                "The report worker queue returned an invalid claim"
            )
        result_payload = _required(row, "result_payload")
        sim_details = _required(row, "sim_details")
        if not isinstance(result_payload, Mapping):
            raise ReportWorkerQueueContractError(
                "The report worker queue returned an invalid result payload"
            )
        if not isinstance(sim_details, Sequence) or isinstance(sim_details, (str, bytes)):
            raise ReportWorkerQueueContractError(
                "The report worker queue returned invalid SIM details"
            )
        if not all(isinstance(detail, Mapping) for detail in sim_details):
            raise ReportWorkerQueueContractError(
                "The report worker queue returned invalid SIM details"
            )
        payload: dict[str, object] = {
            "analysis_case_id": _uuid(
                _required(row, "analysis_case_id"), field="analysis case id"
            ),
            "owner_id": _uuid(_required(row, "owner_id"), field="owner id"),
            "source_analysis_run_id": _uuid(
                _required(row, "source_analysis_run_id"), field="source analysis run id"
            ),
            "storage_object_key": _required(row, "storage_object_key"),
            "result_payload": dict(result_payload),
            "sim_details": list(sim_details),
            "attempt_count": attempt_count,
            "lease_expires_at": _required(row, "lease_expires_at"),
            "heartbeat_interval_seconds": _required(
                row, "heartbeat_interval_seconds"
            ),
        }
        return ClaimedJob(
            job_pk=_uuid(
                _required(row, "report_artifact_id"), field="report artifact id"
            ),
            processing_run_pk=_uuid(
                _required(row, "processing_run_pk"), field="processing run id"
            ),
            payload=payload,
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
            (
                _uuid(job_pk, field="report artifact id"),
                _uuid(processing_run_pk, field="processing run id"),
                lease_seconds,
            ),
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
        bucket, object_key, digest, mime_type, size_bytes = _result_artifact(result)
        row = self._fetchone(
            _COMPLETE_SQL,
            (
                _uuid(job_pk, field="report artifact id"),
                _uuid(processing_run_pk, field="processing run id"),
                bucket,
                object_key,
                digest,
                mime_type,
                size_bytes,
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
                _uuid(job_pk, field="report artifact id"),
                _uuid(processing_run_pk, field="processing run id"),
                _PUBLIC_FAILURE_CODE,
                _PUBLIC_FAILURE_MESSAGE,
                _internal_failure(failure),
            ),
        )
        return _bool_result(row, "is_failed")

    def cleanup_once(self, *, storage: Any, worker_id: str, lease_seconds: int) -> bool:
        """Delete one durable orphan/expired object only after its DB fence."""
        row = self._fetchone(_CLAIM_CLEANUP_SQL, (worker_id, lease_seconds))
        if row is None:
            return False
        cleanup_id = _uuid(
            _required(row, "report_pdf_object_cleanup_id"), field="report cleanup id"
        )
        cleanup_run_pk = _uuid(
            _required(row, "cleanup_run_pk"), field="report cleanup run id"
        )
        bucket = _required(row, "storage_bucket")
        object_key = _required(row, "storage_object_key")
        if bucket != _REPORT_BUCKET or not isinstance(object_key, str):
            raise ReportWorkerQueueContractError("The report cleanup claim is invalid")
        storage.delete(bucket=bucket, object_key=object_key)
        completed = self._fetchone(
            _COMPLETE_CLEANUP_SQL, (cleanup_id, cleanup_run_pk)
        )
        return _bool_result(completed, "is_completed")

    def _fetchone(
        self, query: str, params: tuple[object, ...]
    ) -> Mapping[str, Any] | None:
        try:
            with (
                self._connect(
                    self._database_url,
                    connect_timeout=self._connect_timeout_seconds,
                    row_factory=dict_row,
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(query, params)
                return cursor.fetchone()
        except (psycopg.Error, OSError, ConnectionError, TimeoutError):
            raise ReportWorkerDatabaseUnavailable(
                "Report worker database is unavailable"
            ) from None
