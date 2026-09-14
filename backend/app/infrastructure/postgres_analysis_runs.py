"""Direct trusted-Postgres adapter for FastAPI analysis-run commands.

The v0.2 reservation RPC owns user-level serialization, exact idempotency,
stale-run reconciliation, and canonical run-ID allocation.  FastAPI supplies
only the verified owner and immutable source identity, then uses the returned
run ID for every later transition.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    ActiveResultSessionExists,
    AnalysisRunFinalizationExpired,
    AnalysisRunFinalizationRejected,
    AnalysisRunFinalizationUncertain,
    AnalysisQueueCapacityExceeded,
    AnalysisRunPersistenceUnavailable,
    AnalysisRunRecord,
    IdempotencyKeyConflict,
    SourceObject,
    UploadCleanupObject,
    UploadReservation,
)


_UPLOAD_RESERVATION_TTL_SECONDS = 15 * 60
_QUEUED_SOURCE_TTL_SECONDS = 60 * 60
DEFAULT_GLOBAL_QUEUE_MAX = 25
MAX_GLOBAL_QUEUE_MAX = 10_000

_SET_GLOBAL_QUEUE_LIMIT_SQL = """
SELECT set_config('prereview.global_queue_max', %s, TRUE)
"""

_RESERVE_RUN_SQL = """
SELECT analysis_run_id AS analysis_run_pk, status,
       NULL::uuid AS analysis_case_pk, replayed, error_code, error_message,
       created_at, updated_at, cleanup_objects
  FROM workspace.reserve_analysis_upload_v2(
      %s, %s, %s, %s, %s, %s, %s, %s, %s
  )
"""

_FINALIZE_RUN_SQL = """
SELECT analysis_run_id AS analysis_run_pk, status, analysis_case_id AS analysis_case_pk,
       error_code, error_message, created_at, updated_at, outcome, cleanup_objects
  FROM workspace.finalize_analysis_upload_v2(
      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
  )
"""

_FINALIZATION_STATE_SQL = """
SELECT analysis_run.analysis_run_pk, analysis_run.status,
       analysis_run.analysis_case_pk, analysis_run.error_code,
       analysis_run.error_message, analysis_run.created_at,
       analysis_run.updated_at, analysis_run.original_filename,
       analysis_run.declared_mime_type, analysis_run.declared_size_bytes,
       dispatch.source_bucket,
       dispatch.source_object_key, dispatch.source_content_sha256,
       EXISTS (
           SELECT 1
             FROM workspace.source_artifact AS artifact
            WHERE artifact.analysis_run_pk = analysis_run.analysis_run_pk
              AND artifact.artifact_type = 'source'
              AND artifact.storage_bucket = %s
              AND artifact.storage_object_key = %s
              AND lower(artifact.content_sha256) = lower(%s)
              AND artifact.mime_type = %s
              AND artifact.size_bytes = %s
       ) AS source_artifact_matches
  FROM workspace.analysis_run AS analysis_run
  JOIN workspace.analysis_run_dispatch AS dispatch
    ON dispatch.analysis_run_pk = analysis_run.analysis_run_pk
 WHERE analysis_run.analysis_run_pk = %s
   AND analysis_run.user_id = %s
"""

_MARK_UPLOAD_CLEANUP_PENDING_SQL = """
UPDATE workspace.analysis_run AS analysis_run
   SET status = 'cleanup_pending',
       completed_at = clock_timestamp(),
       error_code = %s,
       error_message = %s
  FROM workspace.analysis_run_dispatch AS dispatch
 WHERE analysis_run.analysis_run_pk = %s
   AND analysis_run.user_id = %s
   AND analysis_run.status = 'uploading'
   AND dispatch.analysis_run_pk = analysis_run.analysis_run_pk
   AND dispatch.source_bucket = %s
   AND dispatch.source_object_key = %s
   AND lower(dispatch.source_content_sha256) = lower(%s)
   AND dispatch.processing_run_pk IS NULL
RETURNING analysis_run.analysis_run_pk, dispatch.source_bucket,
          dispatch.source_object_key
"""

_COMPLETE_UPLOAD_CLEANUP_SQL = """
UPDATE workspace.analysis_run
   SET status = 'failed'
 WHERE analysis_run_pk = %s
   AND status = 'cleanup_pending'
   AND error_code IN (
       'UPLOAD_RESERVATION_EXPIRED',
       'SOURCE_UPLOAD_FAILED',
       'UPLOAD_FINALIZE_FAILED'
   )
RETURNING analysis_run_pk
"""

_CONFIRM_RESERVATION_SQL = """
SELECT analysis_run.analysis_run_pk, analysis_run.status,
       analysis_run.analysis_case_pk, analysis_run.error_code,
       analysis_run.error_message, analysis_run.created_at,
       analysis_run.updated_at, analysis_run.original_filename,
       analysis_run.declared_mime_type, analysis_run.declared_size_bytes,
       dispatch.source_bucket,
       dispatch.source_object_key, dispatch.source_content_sha256
  FROM workspace.analysis_run AS analysis_run
  JOIN workspace.analysis_run_dispatch AS dispatch
    ON dispatch.analysis_run_pk = analysis_run.analysis_run_pk
 WHERE analysis_run.user_id = %s
   AND analysis_run.idempotency_key = %s
"""

_GET_RUN_SQL = """
SELECT analysis_run_pk, status, analysis_case_pk, error_code, error_message,
       created_at, updated_at
  FROM workspace.analysis_run
 WHERE analysis_run_pk = %s
   AND user_id = %s
"""


class PostgresAnalysisRunRepository:
    """Persist reservation, immutable source, and queue transition safely."""

    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 10,
        global_queue_max: int | None = None,
    ) -> None:
        if global_queue_max is not None and (
            isinstance(global_queue_max, bool)
            or not isinstance(global_queue_max, int)
            or not 1 <= global_queue_max <= MAX_GLOBAL_QUEUE_MAX
        ):
            raise ValueError(
                f"global_queue_max must be between 1 and {MAX_GLOBAL_QUEUE_MAX}"
            )
        self._database_url = database_url
        self._connect_timeout = connect_timeout_seconds
        self._global_queue_max = global_queue_max

    def _ensure_configured(self) -> None:
        if not self._database_url:
            raise AnalysisRunPersistenceUnavailable("Analysis database is not configured")

    async def reserve_uploading(
        self,
        *,
        idempotency_key: str,
        owner_id: str,
        source: SourceObject,
    ) -> UploadReservation:
        self._ensure_configured()
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.cursor() as cursor:
                    if self._global_queue_max is not None:
                        await cursor.execute(
                            _SET_GLOBAL_QUEUE_LIMIT_SQL,
                            (str(self._global_queue_max),),
                        )
                    await cursor.execute(
                        _RESERVE_RUN_SQL,
                        (
                            owner_id,
                            idempotency_key,
                            source.filename,
                            source.declared_mime_type or source.mime_type,
                            source.size_bytes,
                            source.bucket,
                            source.object_key,
                            source.content_sha256,
                            _UPLOAD_RESERVATION_TTL_SECONDS,
                        ),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise AnalysisRunPersistenceUnavailable(
                            "Analysis upload reservation was not returned"
                        )
                await connection.commit()
        except (psycopg.Error, OSError) as exc:
            message = _database_error_message(exc)
            if "GLOBAL_QUEUE_CAPACITY_EXCEEDED" in message:
                raise AnalysisQueueCapacityExceeded(
                    "Analysis queue is temporarily full"
                ) from exc
            if "IDEMPOTENCY_KEY_CONFLICT" in message:
                raise IdempotencyKeyConflict(
                    "Idempotency-Key was reused for a different source"
                ) from exc
            if "ACTIVE_RESULT_SESSION" in message:
                raise ActiveResultSessionExists(
                    "An active result session must be closed first"
                ) from exc
            if "ANALYSIS_RUN_ACTIVE" in message:
                raise ActiveAnalysisRunExists(
                    "An active analysis run already exists"
                ) from exc
            confirmed = await self._read_exact_run(
                idempotency_key=idempotency_key,
                owner_id=owner_id,
                source=source,
            )
            if confirmed is not None:
                return UploadReservation(
                    record=confirmed,
                    replayed=True,
                )
            raise AnalysisRunPersistenceUnavailable("Analysis database is unavailable") from exc
        return UploadReservation(
            record=_record(row),
            cleanup_objects=tuple(
                _cleanup_object(item) for item in (row.get("cleanup_objects") or [])
            ),
            replayed=bool(row.get("replayed")),
        )

    async def finalize_queued(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
    ) -> AnalysisRunRecord:
        self._ensure_configured()
        expired_cleanup: UploadCleanupObject | None = None
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.cursor() as cursor:
                    if self._global_queue_max is not None:
                        await cursor.execute(
                            _SET_GLOBAL_QUEUE_LIMIT_SQL,
                            (str(self._global_queue_max),),
                        )
                    await cursor.execute(
                        _FINALIZE_RUN_SQL,
                        _finalization_params(analysis_run_id, owner_id, source),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise AnalysisRunFinalizationUncertain(
                            "Analysis upload finalization was not returned"
                        )
                    if row.get("outcome") == "expired_capacity":
                        cleanup = _finalization_cleanup_object(row)
                        if cleanup is None:
                            raise AnalysisRunFinalizationUncertain(
                                "Expired analysis upload cleanup was not returned"
                            )
                        # Commit the cleanup_pending fence before signalling a
                        # typed capacity result to the service.
                        expired_cleanup = cleanup
                    elif row.get("outcome") not in {"queued", "queued_replay"}:
                        raise AnalysisRunFinalizationUncertain(
                            "Analysis upload finalization returned an invalid outcome"
                        )
                await connection.commit()
        except (
            AnalysisRunFinalizationExpired,
            AnalysisRunFinalizationRejected,
            AnalysisRunFinalizationUncertain,
        ):
            raise
        except (psycopg.Error, OSError) as exc:
            state = await self._read_finalization_state(
                analysis_run_id=analysis_run_id,
                owner_id=owner_id,
                source=source,
            )
            if state is not None:
                if (
                    state["status"] == "queued"
                    and _dispatch_matches(state, source)
                    and state["source_artifact_matches"]
                ):
                    return _record(state)
                if (
                    state["status"] == "uploading"
                    and _dispatch_matches(state, source)
                    and not state["source_artifact_matches"]
                ):
                    raise AnalysisRunFinalizationRejected(
                        "Analysis upload finalization did not commit"
                    ) from exc
                if (
                    state["status"] == "cleanup_pending"
                    and _dispatch_matches(state, source)
                    and state["error_code"] == "UPLOAD_RESERVATION_EXPIRED"
                ):
                    raise AnalysisRunFinalizationExpired(
                        "Analysis queue is temporarily full",
                        UploadCleanupObject(
                            analysis_run_id=analysis_run_id,
                            bucket=source.bucket,
                            object_key=source.object_key,
                        ),
                    ) from None
            raise AnalysisRunFinalizationUncertain(
                "Analysis upload finalization could not be confirmed"
            ) from exc
        if expired_cleanup is not None:
            raise AnalysisRunFinalizationExpired(
                "Analysis queue is temporarily full", expired_cleanup
            )
        return _record(row)

    async def mark_upload_cleanup_pending(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
        error_code: str,
        error_message: str,
    ) -> UploadCleanupObject | None:
        self._ensure_configured()
        if error_code not in {"SOURCE_UPLOAD_FAILED", "UPLOAD_FINALIZE_FAILED"}:
            raise ValueError("unsupported upload cleanup error code")
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        _MARK_UPLOAD_CLEANUP_PENDING_SQL,
                        (
                            error_code,
                            error_message,
                            analysis_run_id,
                            owner_id,
                            source.bucket,
                            source.object_key,
                            source.content_sha256,
                        ),
                    )
                    row = await cursor.fetchone()
                await connection.commit()
        except (psycopg.Error, OSError) as exc:
            raise AnalysisRunPersistenceUnavailable("Analysis database is unavailable") from exc
        return _cleanup_object(row) if row is not None else None

    async def complete_upload_cleanup(
        self,
        *,
        analysis_run_id: str,
    ) -> bool:
        self._ensure_configured()
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        _COMPLETE_UPLOAD_CLEANUP_SQL,
                        (analysis_run_id,),
                    )
                    row = await cursor.fetchone()
                await connection.commit()
        except (psycopg.Error, OSError) as exc:
            raise AnalysisRunPersistenceUnavailable("Analysis database is unavailable") from exc
        return row is not None

    async def get_for_owner(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
    ) -> AnalysisRunRecord | None:
        self._ensure_configured()
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(_GET_RUN_SQL, (analysis_run_id, owner_id))
                    row = await cursor.fetchone()
        except (psycopg.Error, OSError) as exc:
            raise AnalysisRunPersistenceUnavailable("Analysis database is unavailable") from exc
        return _record(row) if row is not None else None

    async def _read_exact_run(
        self,
        *,
        idempotency_key: str,
        owner_id: str,
        source: SourceObject,
    ) -> AnalysisRunRecord | None:
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        _CONFIRM_RESERVATION_SQL,
                        (owner_id, idempotency_key),
                    )
                    row = await cursor.fetchone()
        except (psycopg.Error, OSError):
            return None
        if row is None:
            return None
        return _record(row) if _reservation_matches(row, source) else None

    async def _read_finalization_state(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
    ) -> Mapping[str, Any] | None:
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        _FINALIZATION_STATE_SQL,
                        _finalization_readback_params(analysis_run_id, owner_id, source),
                    )
                    return await cursor.fetchone()
        except (psycopg.Error, OSError):
            return None


def _record(row: Mapping[str, Any]) -> AnalysisRunRecord:
    return AnalysisRunRecord(
        analysis_run_id=str(row["analysis_run_pk"]),
        status=str(row["status"]),
        analysis_case_id=(
            str(row["analysis_case_pk"])
            if row.get("analysis_case_pk") is not None
            else None
        ),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _database_error_message(exc: BaseException) -> str:
    if not isinstance(exc, psycopg.Error):
        return ""
    diagnostic = getattr(exc, "diag", None)
    primary = getattr(diagnostic, "message_primary", None)
    return str(primary or exc).strip()


def _cleanup_object(row: Mapping[str, Any]) -> UploadCleanupObject:
    return UploadCleanupObject(
        analysis_run_id=str(row["analysis_run_pk"]),
        bucket=str(row["source_bucket"]),
        object_key=str(row["source_object_key"]),
    )


def _finalization_cleanup_object(
    row: Mapping[str, Any],
) -> UploadCleanupObject | None:
    objects = row.get("cleanup_objects")
    if not isinstance(objects, list) or len(objects) != 1:
        return None
    item = objects[0]
    if not isinstance(item, Mapping):
        return None
    try:
        return _cleanup_object(item)
    except (KeyError, TypeError, ValueError):
        return None


def _dispatch_matches(row: Mapping[str, Any], source: SourceObject) -> bool:
    return (
        row.get("source_bucket") == source.bucket
        and row.get("source_object_key") == source.object_key
        and str(row.get("source_content_sha256") or "").lower()
        == source.content_sha256.lower()
    )


def _reservation_matches(row: Mapping[str, Any], source: SourceObject) -> bool:
    return (
        _dispatch_matches(row, source)
        and row.get("original_filename") == source.filename
        and row.get("declared_mime_type")
        == (source.declared_mime_type or source.mime_type)
        and row.get("declared_size_bytes") == source.size_bytes
    )


def _finalization_params(
    analysis_run_id: str,
    owner_id: str,
    source: SourceObject,
) -> tuple[Any, ...]:
    return (
        analysis_run_id,
        owner_id,
        source.filename,
        source.declared_mime_type or source.mime_type,
        source.size_bytes,
        source.bucket,
        source.object_key,
        source.content_sha256,
        source.mime_type,
        source.size_bytes,
        _QUEUED_SOURCE_TTL_SECONDS,
    )


def _finalization_readback_params(
    analysis_run_id: str,
    owner_id: str,
    source: SourceObject,
) -> tuple[Any, ...]:
    return (
        source.bucket,
        source.object_key,
        source.content_sha256,
        source.mime_type,
        source.size_bytes,
        analysis_run_id,
        owner_id,
    )
