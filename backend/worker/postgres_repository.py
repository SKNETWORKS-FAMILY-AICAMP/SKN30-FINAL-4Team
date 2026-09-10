"""Trusted PostgreSQL implementation of :class:`worker.runtime.JobRepository`.

The SQL state machine deliberately lives in migration 21.  This adapter is a
small transport boundary: it opens a *fresh synchronous psycopg connection for
each method call*, invokes one trusted function, commits that function call,
and closes the connection.  In particular a heartbeat thread never shares a
connection or cursor with the handler/terminal transition thread.

The adapter never accepts a frontend credential and must be configured with a
trusted database DSN for the worker process only.  It does not log the DSN or
database exception text.  Database errors therefore remain safe when the
runtime logs them during a heartbeat failure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from worker.runtime import ClaimedJob, JobFailure, JobKey, JobResult, ProcessingRunPK

__all__ = [
    "PostgresJobRepository",
    "WorkerDatabaseUnavailable",
    "WorkerQueueContractError",
]


_CLAIM_SQL = """
SELECT
    analysis_run_pk,
    source_bucket,
    source_object_key,
    source_content_sha256,
    processing_run_pk,
    attempt_count,
    lease_expires_at,
    heartbeat_interval_seconds
FROM workspace.claim_next_analysis_run(%s, %s)
"""

_HEARTBEAT_SQL = """
SELECT workspace.heartbeat_analysis_run(%s, %s, %s) AS is_live
"""

_PERSIST_RESULT_SQL = """
SELECT workspace.persist_analysis_result_core(%s, %s, %s) AS analysis_case_pk
"""

_FAIL_SQL = """
SELECT workspace.fail_analysis_run(%s, %s, %s, %s, %s) AS is_failed
"""


class WorkerDatabaseUnavailable(RuntimeError):
    """A safe operational error; its text is suitable for runtime logging."""


class WorkerQueueContractError(RuntimeError):
    """The migration-21/22 worker queue contract is unsafe to process."""


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


# A failure can include parser output, an HTTP URL, or a libpq-style DSN.  The
# public error never uses it; this redaction is a second guard for the
# operations-only database column as well.
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

_PUBLIC_FAILURE_CODE = "ANALYSIS_FAILED"
_PUBLIC_FAILURE_MESSAGE = "분석에 실패했습니다. 잠시 후 다시 시도해 주세요."


def _redact_internal_error(value: str) -> str:
    """Return a bounded, one-line diagnostic without credentials."""

    collapsed = _WHITESPACE.sub(" ", value).strip()
    collapsed = _URL_CREDENTIALS.sub(r"\g<scheme>://[redacted]@", collapsed)
    collapsed = _SECRET_ASSIGNMENT.sub(
        r"\g<name>\g<separator>[redacted]", collapsed
    )
    return collapsed[:_MAX_INTERNAL_ERROR_CHARS]


def _internal_failure(failure: JobFailure) -> str:
    # Runtime-produced ``kind`` normally is an exception class name.  Keep the
    # stored shape predictable even if a custom handler constructs JobFailure.
    kind = re.sub(r"[^A-Za-z0-9_.-]", "_", failure.kind).strip("_")[:80]
    message = _redact_internal_error(failure.message)
    if not kind:
        kind = "WorkerFailure"
    return f"{kind}: {message}" if message else kind


def _required(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise WorkerQueueContractError("The worker queue returned an incomplete claim")
    return value


def _bool_result(row: Mapping[str, Any] | None, key: str) -> bool:
    if row is None or key not in row or not isinstance(row[key], bool):
        raise WorkerQueueContractError("The worker queue returned an invalid transition")
    return row[key]


def _result_json(result: JobResult) -> Jsonb:
    """Make a fail-closed JSONB payload for migration 22.

    A successful worker is only allowed to publish the explicit comparison
    payload.  Serialising before opening a database connection rejects NaN,
    infinities, datetimes, bytes, model objects and other accidentally opaque
    values.  The encode/decode roundtrip also gives psycopg only JSON-native
    objects, so its later JSONB adaptation cannot reintroduce ``allow_nan``.
    """

    if not isinstance(result, Mapping):
        raise WorkerQueueContractError(
            "The worker result must be a JSON-compatible mapping"
        )
    try:
        serialized = json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        normalized = json.loads(serialized)
    except (TypeError, ValueError, OverflowError):
        raise WorkerQueueContractError(
            "The worker result must be JSON-compatible without non-finite values"
        ) from None
    if not isinstance(normalized, dict):  # defensive: Mapping should encode as object
        raise WorkerQueueContractError("The worker result must be a JSON object")
    return Jsonb(normalized)


def _persisted_result(row: Mapping[str, Any] | None) -> bool:
    """Map migration 22's UUID/NULL result to success/fenced semantics."""

    if row is None or "analysis_case_pk" not in row:
        raise WorkerQueueContractError("The worker queue returned an invalid transition")
    analysis_case_pk = row["analysis_case_pk"]
    if analysis_case_pk is None:
        return False
    if not isinstance(analysis_case_pk, UUID):
        raise WorkerQueueContractError("The worker queue returned an invalid case id")
    return True


class PostgresJobRepository:
    """Migration-21/22-backed queue adapter for one trusted worker process.

    Each public operation obtains its own connection.  That makes concurrent
    heartbeats safe and also ensures a completed/failed transition is committed
    before the next poll.  ``complete`` invokes migration 22's fenced result
    materialiser, so result rows and the success state share one PostgreSQL
    function transaction.  ``processing_run_pk`` is passed to every mutating
    function; PostgreSQL performs the actual fencing check.
    """

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
        """Do not expose the private DSN through debugging output."""

        return (
            "PostgresJobRepository("
            f"connect_timeout_seconds={self._connect_timeout_seconds})"
        )

    def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        row = self._fetchone(_CLAIM_SQL, (worker_id, lease_seconds))
        if row is None:
            return None

        job_pk = _required(row, "analysis_run_pk")
        processing_run_pk = _required(row, "processing_run_pk")
        attempt_count = _required(row, "attempt_count")
        if not isinstance(attempt_count, int) or isinstance(attempt_count, bool):
            raise WorkerQueueContractError("The worker queue returned an invalid claim")

        # Field names intentionally match migration 21's return contract so a
        # handler can download the immutable request object without querying
        # browser-visible tables.
        payload = {
            "source_bucket": _required(row, "source_bucket"),
            "source_object_key": _required(row, "source_object_key"),
            "source_content_sha256": _required(row, "source_content_sha256"),
            "attempt_count": attempt_count,
            "lease_expires_at": _required(row, "lease_expires_at"),
            "heartbeat_interval_seconds": _required(
                row, "heartbeat_interval_seconds"
            ),
        }
        return ClaimedJob(
            job_pk=job_pk,
            processing_run_pk=processing_run_pk,
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
        # ``worker_id`` is intentionally not a SQL argument: migration 21
        # fences on the processing-run token created at claim time.
        del worker_id
        row = self._fetchone(
            _HEARTBEAT_SQL,
            (job_pk, processing_run_pk, lease_seconds),
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
            _PERSIST_RESULT_SQL,
            (job_pk, processing_run_pk, _result_json(result)),
        )
        return _persisted_result(row)

    def fail(
        self,
        *,
        job_pk: JobKey,
        processing_run_pk: ProcessingRunPK,
        worker_id: str,
        failure: JobFailure,
    ) -> bool:
        del worker_id
        # p_error_message is the only value the user-facing run reads.  Never
        # put exception text there.  The redacted internal diagnostic is kept
        # in ops/dispatch by migration 21 for operators.
        row = self._fetchone(
            _FAIL_SQL,
            (
                job_pk,
                processing_run_pk,
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
            # Do not cache this connection.  WorkerRuntime's heartbeat owns a
            # separate thread, and psycopg cursors/connections must not be
            # concurrently driven by it and the main handler thread.
            with self._connect(
                self._database_url,
                connect_timeout=self._connect_timeout_seconds,
                row_factory=dict_row,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
                    return cursor.fetchone()
        except (psycopg.Error, OSError, ConnectionError, TimeoutError):
            # The exception and chained cause may contain a DSN, host or query
            # detail.  Runtime logging receives only this stable message.
            raise WorkerDatabaseUnavailable("Worker database is unavailable") from None
