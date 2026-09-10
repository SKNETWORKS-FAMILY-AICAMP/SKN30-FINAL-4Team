# =============================================================================
# 보류 — 미완성 구현이다. 사용하지 않는다.
#
# 이 모듈은 워커 큐를 다른 방식으로 만들다가 중단된 작업이다. 아래 docstring 은
# "팀원 스키마에 워커 전용 lease 컬럼이 없어 run_metadata 에 토큰을 넣는다" 고
# 하는데, ``run_metadata`` 는 ``ops.processing_run`` 의 컬럼이고
# ``workspace.analysis_run`` 에는 없다. 그 컬럼을 만드는 migration 도 저장소에
# 없다. 팀원 스키마를 실제로 설치하고 돌린 결과:
#
#     UndefinedColumn: column "run_metadata" of relation "analysis_run"
#                      does not exist          (6 tests, 전부 실패)
#
# 현재 동작하는 큐는 ``backend/worker/postgres_repository.py`` 와 migration 21/22다.
# ``workspace.claim_next_analysis_run``/heartbeat/fenced materialisation을 호출하며
# 실제 lease와 fencing state는 ``workspace.analysis_run_dispatch``에 둔다.
# ``jobs.py``도 이 모듈과 같은 이전 `analysis_run` 컬럼 계약을 전제한 레거시다.
#
# 지우지 않고 남기는 이유: 워커 시도를 ``ops.processing_run`` 행으로 남기는
# 발상은 초안 §9.5 의 실행 진단(실패 단위·호출 ID·보완 이력)에 그대로 쓸 수
# 있다. 현재 migration 21은 `ops.processing_run`으로 이 발상을 구현한다.
#
# 되살리려면 analysis_run.run_metadata 를 추가하는 migration 이 먼저 필요하다.
# =============================================================================

"""PostgreSQL-backed queue primitives for ``workspace.analysis_run``.

The workspace row is the queue item.  Each claim creates one
``ops.processing_run`` row; its primary key is the lease/fencing token for
that attempt.  The token is copied into ``run_metadata`` because the team
schema deliberately has no worker-owned lease columns.

This module only owns queue state.  It does not register analyses, poll in a
daemon, or write result tables.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text


LEASE_EXPIRED_ERROR_CODE = "WORKER_LEASE_EXPIRED"
MAX_ATTEMPTS_ERROR_CODE = "WORKER_MAX_ATTEMPTS_EXCEEDED"
FENCE_TOKEN_MISSING_ERROR_CODE = "WORKER_FENCE_TOKEN_MISSING"
FENCE_TOKEN_INVALID_ERROR_CODE = "WORKER_FENCE_TOKEN_INVALID"
FENCE_TOKEN_NOT_LIVE_ERROR_CODE = "WORKER_FENCE_TOKEN_NOT_LIVE"


class QueueInvariantError(RuntimeError):
    """The queue tables could not be advanced as one fenced transaction."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """The immutable identity of one claimed analysis attempt."""

    analysis_run_pk: UUID
    processing_run_pk: UUID
    user_id: UUID
    attempt_no: int
    run_type: str
    claimed_at: datetime


@dataclass(frozen=True, slots=True)
class LeaseRecovery:
    """The result of recovering one expired lease."""

    analysis_run_pk: UUID
    processing_run_pk: UUID | None
    attempt_no: int
    status: str
    error_code: str


_DB_NOW = text("SELECT clock_timestamp()")

_LOCK_RUNNING = text(
    """
    SELECT analysis_run_pk, started_at, run_metadata
      FROM workspace.analysis_run
     WHERE status = 'running'
     ORDER BY created_at, analysis_run_pk
     FOR UPDATE SKIP LOCKED
    """
)

_LOCK_QUEUED = text(
    """
    SELECT analysis_run_pk, user_id, run_metadata
      FROM workspace.analysis_run
     WHERE status = 'queued'
     ORDER BY created_at, analysis_run_pk
     LIMIT 1
     FOR UPDATE SKIP LOCKED
    """
)

_LOCK_LIVE_JOB = text(
    """
    SELECT analysis_run_pk, run_metadata
      FROM workspace.analysis_run
     WHERE analysis_run_pk = :analysis_run_pk
       AND status = 'running'
       AND run_metadata->>'processing_run_pk' = :processing_run_pk
     FOR UPDATE
    """
)

_LOCK_PROCESSING_RUN = text(
    """
    SELECT processing_run_pk, status
      FROM ops.processing_run
     WHERE processing_run_pk = :processing_run_pk
     FOR UPDATE
    """
)

_INSERT_PROCESSING_RUN = text(
    """
    INSERT INTO ops.processing_run (
        source_analysis_run_id, run_type, status, started_at, run_metadata
    )
    VALUES (
        :source_analysis_run_id, :run_type, 'running', :started_at,
        CAST(:run_metadata AS jsonb)
    )
    RETURNING processing_run_pk
    """
)

_UPDATE_PROCESSING_METADATA = text(
    """
    UPDATE ops.processing_run
       SET run_metadata = CAST(:run_metadata AS jsonb)
     WHERE processing_run_pk = :processing_run_pk
       AND status = 'running'
    RETURNING processing_run_pk
    """
)

_UPDATE_WORKSPACE_CLAIM = text(
    """
    UPDATE workspace.analysis_run
       SET status = 'running',
           started_at = :started_at,
           completed_at = NULL,
           updated_at = :updated_at,
           run_metadata = CAST(:run_metadata AS jsonb)
     WHERE analysis_run_pk = :analysis_run_pk
       AND status = 'queued'
    RETURNING analysis_run_pk
    """
)

_UPDATE_WORKSPACE_RECOVERY = text(
    """
    UPDATE workspace.analysis_run
       SET status = :status,
           started_at = CASE WHEN :status = 'queued' THEN NULL ELSE started_at END,
           completed_at = CASE WHEN :status = 'queued' THEN NULL ELSE :completed_at END,
           updated_at = :updated_at,
           run_metadata = CAST(:run_metadata AS jsonb)
     WHERE analysis_run_pk = :analysis_run_pk
       AND status = 'running'
       AND run_metadata->>'processing_run_pk' = :processing_run_pk
    RETURNING analysis_run_pk
    """
)

_UPDATE_PROCESSING_RECOVERY = text(
    """
    UPDATE ops.processing_run
       SET status = 'failed',
           finished_at = :finished_at,
           run_metadata = CAST(:run_metadata AS jsonb),
           error_code = :error_code,
           error_message = :error_message
     WHERE processing_run_pk = :processing_run_pk
       AND status = 'running'
    RETURNING processing_run_pk
    """
)

_UPDATE_WORKSPACE_HEARTBEAT = text(
    """
    UPDATE workspace.analysis_run
       SET updated_at = :updated_at,
           run_metadata = CAST(:run_metadata AS jsonb)
     WHERE analysis_run_pk = :analysis_run_pk
       AND status = 'running'
       AND run_metadata->>'processing_run_pk' = :processing_run_pk
    RETURNING analysis_run_pk
    """
)

_UPDATE_WORKSPACE_TERMINAL = text(
    """
    UPDATE workspace.analysis_run
       SET status = :status,
           completed_at = :completed_at,
           updated_at = :updated_at,
           run_metadata = CAST(:run_metadata AS jsonb)
     WHERE analysis_run_pk = :analysis_run_pk
       AND status = 'running'
       AND run_metadata->>'processing_run_pk' = :processing_run_pk
    RETURNING analysis_run_pk
    """
)

_UPDATE_QUEUED_EXHAUSTED = text(
    """
    UPDATE workspace.analysis_run
       SET status = 'failed',
           completed_at = :completed_at,
           updated_at = :updated_at,
           run_metadata = CAST(:run_metadata AS jsonb)
     WHERE analysis_run_pk = :analysis_run_pk
       AND status = 'queued'
    RETURNING analysis_run_pk
    """
)

_UPDATE_PROCESSING_TERMINAL = text(
    """
    UPDATE ops.processing_run
       SET status = :status,
           finished_at = :finished_at,
           run_metadata = CAST(:run_metadata AS jsonb),
           error_code = :error_code,
           error_message = :error_message
     WHERE processing_run_pk = :processing_run_pk
       AND status = 'running'
    RETURNING processing_run_pk
    """
)


def _metadata(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _attempt_no(metadata: Mapping[str, Any]) -> int:
    value = metadata.get("attempt_no", 0)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _lease_timestamp(metadata: Mapping[str, Any], started_at: object) -> datetime | None:
    heartbeat_at = _parse_timestamp(metadata.get("heartbeat_at"))
    if heartbeat_at is not None:
        return heartbeat_at
    return _as_utc(started_at if isinstance(started_at, datetime) else None)


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))


def _uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _lease_metadata(
    metadata: Mapping[str, Any],
    *,
    attempt_no: int,
    heartbeat_at: datetime,
    processing_run_pk: UUID,
) -> dict[str, Any]:
    result = dict(metadata)
    result["attempt_no"] = attempt_no
    result["heartbeat_at"] = heartbeat_at.isoformat()
    result["processing_run_pk"] = str(processing_run_pk)
    result.pop("last_error_code", None)
    result.pop("last_error_message", None)
    return result


def _finished_metadata(
    metadata: Mapping[str, Any],
    *,
    finished_at: datetime,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    result = dict(metadata)
    result.pop("heartbeat_at", None)
    result.pop("processing_run_pk", None)
    result["finished_at"] = finished_at.isoformat()
    if error_code is None:
        result.pop("last_error_code", None)
        result.pop("last_error_message", None)
    else:
        result["last_error_code"] = error_code
        if error_message:
            result["last_error_message"] = error_message
    return result


class AnalysisRunQueue:
    """Small synchronous queue facade over the team's PostgreSQL schema."""

    def __init__(
        self,
        engine: Engine,
        *,
        run_type: str = "analysis",
        lease_timeout: timedelta | float = timedelta(minutes=10),
        max_attempts: int = 3,
    ) -> None:
        if not run_type.strip():
            raise ValueError("run_type must not be blank")
        if isinstance(lease_timeout, timedelta):
            timeout = lease_timeout
        else:
            timeout = timedelta(seconds=float(lease_timeout))
        if timeout.total_seconds() <= 0:
            raise ValueError("lease_timeout must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._engine = engine
        self._run_type = run_type
        self._lease_timeout = timeout
        self._max_attempts = max_attempts

    def claim_next(self) -> ClaimedJob | None:
        """Recover expired work and atomically claim the oldest queued row."""

        with self._engine.begin() as connection:
            now = _as_utc(connection.scalar(_DB_NOW))
            if now is None:  # pragma: no cover - PostgreSQL always returns one
                raise QueueInvariantError("database clock returned NULL")
            self._recover_stale_leases(connection, now=now)

            while True:
                row = connection.execute(_LOCK_QUEUED).mappings().first()
                if row is None:
                    return None
                previous = _metadata(row["run_metadata"])
                attempt_no = _attempt_no(previous) + 1
                if attempt_no <= self._max_attempts:
                    break
                # This can only be produced by a manually edited row; normal
                # recovery never leaves an exhausted row queued.
                exhausted = _finished_metadata(
                    previous,
                    finished_at=now,
                    error_code=MAX_ATTEMPTS_ERROR_CODE,
                    error_message=(
                        f"attempt limit {self._max_attempts} was already reached"
                    ),
                )
                connection.execute(
                    _UPDATE_QUEUED_EXHAUSTED,
                    {
                        "analysis_run_pk": row["analysis_run_pk"],
                        "completed_at": now,
                        "updated_at": now,
                        "run_metadata": _json(exhausted),
                    },
                )

            seed_metadata = dict(previous)
            seed_metadata["attempt_no"] = attempt_no
            seed_metadata["heartbeat_at"] = now.isoformat()
            seed_metadata.pop("processing_run_pk", None)
            token = connection.scalar(
                _INSERT_PROCESSING_RUN,
                {
                    "source_analysis_run_id": row["analysis_run_pk"],
                    "run_type": self._run_type,
                    "started_at": now,
                    "run_metadata": _json(seed_metadata),
                },
            )
            if token is None:
                raise QueueInvariantError("processing run token was not created")
            token = _uuid(token)
            metadata = _lease_metadata(
                previous,
                attempt_no=attempt_no,
                heartbeat_at=now,
                processing_run_pk=token,
            )
            claimed = connection.execute(
                _UPDATE_WORKSPACE_CLAIM,
                {
                    "analysis_run_pk": row["analysis_run_pk"],
                    "started_at": now,
                    "updated_at": now,
                    "run_metadata": _json(metadata),
                },
            ).mappings().first()
            if claimed is None:
                raise QueueInvariantError("queued row changed while it was locked")
            updated = connection.execute(
                _UPDATE_PROCESSING_METADATA,
                {
                    "processing_run_pk": token,
                    "run_metadata": _json(metadata),
                },
            ).mappings().first()
            if updated is None:
                raise QueueInvariantError("claim token is not a live processing run")
            return ClaimedJob(
                analysis_run_pk=_uuid(row["analysis_run_pk"]),
                processing_run_pk=token,
                user_id=_uuid(row["user_id"]),
                attempt_no=attempt_no,
                run_type=self._run_type,
                claimed_at=now,
            )

    def recover_stale_leases(
        self,
        *,
        now: datetime | None = None,
    ) -> list[LeaseRecovery]:
        """Requeue expired attempts or fail them after ``max_attempts``.

        ``now`` is injectable for deterministic integration tests; production
        callers should omit it so PostgreSQL's clock remains authoritative.
        """

        with self._engine.begin() as connection:
            current = _as_utc(now) or _as_utc(connection.scalar(_DB_NOW))
            if current is None:  # pragma: no cover - PostgreSQL always returns one
                raise QueueInvariantError("database clock returned NULL")
            return self._recover_stale_leases(connection, now=current)

    def heartbeat(
        self,
        claim_or_analysis_run_pk: ClaimedJob | UUID | str,
        processing_run_pk: UUID | str | None = None,
    ) -> bool:
        """Refresh a lease only when both the job and live token match."""

        analysis_run_pk, token = self._claim_ids(
            claim_or_analysis_run_pk, processing_run_pk
        )
        with self._engine.begin() as connection:
            now = _as_utc(connection.scalar(_DB_NOW))
            if now is None:  # pragma: no cover
                raise QueueInvariantError("database clock returned NULL")
            row = connection.execute(
                _LOCK_LIVE_JOB,
                {
                    "analysis_run_pk": analysis_run_pk,
                    "processing_run_pk": str(token),
                },
            ).mappings().first()
            if row is None:
                return False
            processing = connection.execute(
                _LOCK_PROCESSING_RUN,
                {"processing_run_pk": token},
            ).mappings().first()
            if processing is None or processing["status"] != "running":
                return False
            metadata = _lease_metadata(
                _metadata(row["run_metadata"]),
                attempt_no=_attempt_no(_metadata(row["run_metadata"])),
                heartbeat_at=now,
                processing_run_pk=token,
            )
            workspace = connection.execute(
                _UPDATE_WORKSPACE_HEARTBEAT,
                {
                    "analysis_run_pk": analysis_run_pk,
                    "processing_run_pk": str(token),
                    "updated_at": now,
                    "run_metadata": _json(metadata),
                },
            ).mappings().first()
            if workspace is None:
                return False
            updated = connection.execute(
                _UPDATE_PROCESSING_METADATA,
                {
                    "processing_run_pk": token,
                    "run_metadata": _json(metadata),
                },
            ).mappings().first()
            if updated is None:
                raise QueueInvariantError("heartbeat token stopped during update")
            return True

    def complete(
        self,
        claim_or_analysis_run_pk: ClaimedJob | UUID | str,
        processing_run_pk: UUID | str | None = None,
    ) -> bool:
        """Mark a live attempt succeeded; stale tokens are rejected."""

        return self._finish(
            claim_or_analysis_run_pk,
            processing_run_pk,
            status="succeeded",
            error_code=None,
            error_message=None,
        )

    def fail(
        self,
        claim_or_analysis_run_pk: ClaimedJob | UUID | str,
        processing_run_pk: UUID | str | None = None,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> bool:
        """Mark a live attempt failed; stale tokens are rejected."""

        if not error_code.strip():
            raise ValueError("error_code must not be blank")
        return self._finish(
            claim_or_analysis_run_pk,
            processing_run_pk,
            status="failed",
            error_code=error_code,
            error_message=error_message,
        )

    def _finish(
        self,
        claim_or_analysis_run_pk: ClaimedJob | UUID | str,
        processing_run_pk: UUID | str | None,
        *,
        status: str,
        error_code: str | None,
        error_message: str | None,
    ) -> bool:
        analysis_run_pk, token = self._claim_ids(
            claim_or_analysis_run_pk, processing_run_pk
        )
        with self._engine.begin() as connection:
            now = _as_utc(connection.scalar(_DB_NOW))
            if now is None:  # pragma: no cover
                raise QueueInvariantError("database clock returned NULL")
            row = connection.execute(
                _LOCK_LIVE_JOB,
                {
                    "analysis_run_pk": analysis_run_pk,
                    "processing_run_pk": str(token),
                },
            ).mappings().first()
            if row is None:
                return False
            processing = connection.execute(
                _LOCK_PROCESSING_RUN,
                {"processing_run_pk": token},
            ).mappings().first()
            if processing is None or processing["status"] != "running":
                return False
            metadata = _finished_metadata(
                _metadata(row["run_metadata"]),
                finished_at=now,
                error_code=error_code,
                error_message=error_message,
            )
            workspace = connection.execute(
                _UPDATE_WORKSPACE_TERMINAL,
                {
                    "analysis_run_pk": analysis_run_pk,
                    "processing_run_pk": str(token),
                    "status": status,
                    "completed_at": now,
                    "updated_at": now,
                    "run_metadata": _json(metadata),
                },
            ).mappings().first()
            if workspace is None:
                return False
            updated = connection.execute(
                _UPDATE_PROCESSING_TERMINAL,
                {
                    "processing_run_pk": token,
                    "status": status,
                    "finished_at": now,
                    "run_metadata": _json(metadata),
                    "error_code": error_code,
                    "error_message": error_message,
                },
            ).mappings().first()
            if updated is None:
                raise QueueInvariantError("terminal token stopped during update")
            return True

    def _recover_stale_leases(
        self,
        connection: Connection,
        *,
        now: datetime,
    ) -> list[LeaseRecovery]:
        cutoff = now - self._lease_timeout
        recovered: list[LeaseRecovery] = []
        rows = connection.execute(_LOCK_RUNNING).mappings().all()
        for row in rows:
            metadata = _metadata(row["run_metadata"])
            lease_at = _lease_timestamp(metadata, row["started_at"])
            if lease_at is None or lease_at > cutoff:
                continue
            token_value = metadata.get("processing_run_pk")
            token: UUID | None
            try:
                token = _uuid(token_value) if token_value is not None else None
            except (TypeError, ValueError):
                token = None
                error_code = FENCE_TOKEN_INVALID_ERROR_CODE
                target_status = "failed"
            else:
                processing = None
                if token is not None:
                    processing = (
                        connection.execute(
                            _LOCK_PROCESSING_RUN,
                            {"processing_run_pk": token},
                        )
                        .mappings()
                        .first()
                    )
                if token is None:
                    error_code = FENCE_TOKEN_MISSING_ERROR_CODE
                    target_status = "failed"
                elif processing is None or processing["status"] != "running":
                    error_code = FENCE_TOKEN_NOT_LIVE_ERROR_CODE
                    target_status = "failed"
                elif _attempt_no(metadata) >= self._max_attempts:
                    error_code = MAX_ATTEMPTS_ERROR_CODE
                    target_status = "failed"
                else:
                    error_code = LEASE_EXPIRED_ERROR_CODE
                    target_status = "queued"

            attempt_no = _attempt_no(metadata)
            if token is None and error_code == FENCE_TOKEN_INVALID_ERROR_CODE:
                error_message = "running row has an invalid processing run token"
            elif token is None and error_code == FENCE_TOKEN_MISSING_ERROR_CODE:
                error_message = "running row has no processing run token"
            elif token is None:
                error_message = "running row has no usable processing run token"
            elif error_code == FENCE_TOKEN_NOT_LIVE_ERROR_CODE:
                error_message = "processing run token is no longer live"
            elif error_code == MAX_ATTEMPTS_ERROR_CODE:
                error_message = (
                    f"lease expired at attempt {attempt_no}; maximum is "
                    f"{self._max_attempts}"
                )
            else:
                error_message = "worker lease expired before heartbeat"

            finished_metadata = _finished_metadata(
                metadata,
                finished_at=now,
                error_code=error_code,
                error_message=error_message,
            )
            if target_status == "queued":
                finished_metadata.pop("finished_at", None)
                finished_metadata["last_error_code"] = error_code
                finished_metadata["last_error_message"] = error_message
            if token is not None:
                updated_processing = connection.execute(
                    _UPDATE_PROCESSING_RECOVERY,
                    {
                        "processing_run_pk": token,
                        "finished_at": now,
                        "run_metadata": _json(finished_metadata),
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                ).mappings().first()
                if updated_processing is None and target_status == "queued":
                    # Never requeue a row when the old attempt is not known to
                    # be fenced.  A terminal processing row means this row is
                    # already inconsistent and must stop safely.
                    target_status = "failed"
                    error_code = FENCE_TOKEN_NOT_LIVE_ERROR_CODE
                    error_message = "processing run token stopped during recovery"
                    finished_metadata = _finished_metadata(
                        metadata,
                        finished_at=now,
                        error_code=error_code,
                        error_message=error_message,
                    )
            updated_workspace = connection.execute(
                _UPDATE_WORKSPACE_RECOVERY,
                {
                    "analysis_run_pk": row["analysis_run_pk"],
                    "processing_run_pk": str(token or token_value or ""),
                    "status": target_status,
                    "completed_at": now,
                    "updated_at": now,
                    "run_metadata": _json(finished_metadata),
                },
            ).mappings().first()
            if updated_workspace is None:
                raise QueueInvariantError("stale row changed while it was locked")
            recovered.append(
                LeaseRecovery(
                    analysis_run_pk=_uuid(row["analysis_run_pk"]),
                    processing_run_pk=token,
                    attempt_no=attempt_no,
                    status=target_status,
                    error_code=error_code,
                )
            )
        return recovered

    @staticmethod
    def _claim_ids(
        claim_or_analysis_run_pk: ClaimedJob | UUID | str,
        processing_run_pk: UUID | str | None,
    ) -> tuple[UUID, UUID]:
        if isinstance(claim_or_analysis_run_pk, ClaimedJob):
            if processing_run_pk is not None:
                raise TypeError("processing_run_pk is duplicated")
            return (
                claim_or_analysis_run_pk.analysis_run_pk,
                claim_or_analysis_run_pk.processing_run_pk,
            )
        if processing_run_pk is None:
            raise TypeError("processing_run_pk is required")
        return _uuid(claim_or_analysis_run_pk), _uuid(processing_run_pk)


# The alias matches the name used in early worker design notes and keeps the
# public surface boring for callers that call this an analysis queue.
WorkerQueue = AnalysisRunQueue


__all__ = [
    "AnalysisRunQueue",
    "ClaimedJob",
    "FENCE_TOKEN_INVALID_ERROR_CODE",
    "FENCE_TOKEN_MISSING_ERROR_CODE",
    "FENCE_TOKEN_NOT_LIVE_ERROR_CODE",
    "LEASE_EXPIRED_ERROR_CODE",
    "LeaseRecovery",
    "MAX_ATTEMPTS_ERROR_CODE",
    "QueueInvariantError",
    "WorkerQueue",
]
