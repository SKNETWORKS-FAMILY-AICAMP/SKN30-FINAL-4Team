"""Direct trusted-Postgres adapter for FastAPI analysis-run commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    AnalysisRunFinalizationRejected,
    AnalysisRunFinalizationUncertain,
    AnalysisRunPersistenceUnavailable,
    AnalysisRunRecord,
    SourceObject,
    UploadCleanupObject,
    UploadReservation,
)


_UPLOAD_RESERVATION_TTL_SECONDS = 15 * 60
_QUEUED_SOURCE_TTL_SECONDS = 60 * 60
_STALE_CLEANUP_BATCH_SIZE = 25

_CLAIM_STALE_UPLOAD_CLEANUP_SQL = """
WITH candidate_runs AS MATERIALIZED (
    SELECT analysis_run.analysis_run_pk
      FROM workspace.analysis_run AS analysis_run
     WHERE (
           (
               analysis_run.status = 'uploading'
               AND COALESCE(
                   analysis_run.expires_at,
                   analysis_run.created_at + interval '15 minutes'
               ) <= clock_timestamp()
           )
           OR (
            analysis_run.status = 'cleanup_pending'
            AND analysis_run.error_code IN (
                'UPLOAD_RESERVATION_EXPIRED',
                'SOURCE_UPLOAD_FAILED',
                'UPLOAD_FINALIZE_FAILED'
            )
           )
       )
       AND NOT EXISTS (
           SELECT 1
             FROM workspace.analysis_run_dispatch AS active_dispatch
            WHERE active_dispatch.analysis_run_pk = analysis_run.analysis_run_pk
              AND active_dispatch.processing_run_pk IS NOT NULL
       )
     ORDER BY COALESCE(analysis_run.expires_at, analysis_run.updated_at),
              analysis_run.analysis_run_pk
     LIMIT %s
     FOR UPDATE OF analysis_run SKIP LOCKED
), locked_dispatch AS MATERIALIZED (
    SELECT dispatch.analysis_run_pk, dispatch.source_bucket,
           dispatch.source_object_key, dispatch.processing_run_pk
      FROM workspace.analysis_run_dispatch AS dispatch
      JOIN candidate_runs
        ON candidate_runs.analysis_run_pk = dispatch.analysis_run_pk
     FOR UPDATE OF dispatch
), candidates AS (
    SELECT candidate_runs.analysis_run_pk,
           locked_dispatch.analysis_run_pk IS NOT NULL AS has_cleanup_object
      FROM candidate_runs
      LEFT JOIN locked_dispatch
        ON locked_dispatch.analysis_run_pk = candidate_runs.analysis_run_pk
     WHERE locked_dispatch.analysis_run_pk IS NULL
        OR locked_dispatch.processing_run_pk IS NULL
), marked AS (
    UPDATE workspace.analysis_run AS analysis_run
       SET status = CASE
               WHEN candidates.has_cleanup_object THEN 'cleanup_pending'
               ELSE 'failed'
           END,
           completed_at = COALESCE(analysis_run.completed_at, clock_timestamp()),
           error_code = CASE
               WHEN analysis_run.status = 'uploading'
                   THEN 'UPLOAD_RESERVATION_EXPIRED'
               ELSE analysis_run.error_code
           END,
           error_message = CASE
               WHEN analysis_run.status = 'uploading'
                   THEN '파일 업로드 시간이 만료되었습니다. 다시 시도해 주세요.'
               ELSE analysis_run.error_message
           END
      FROM candidates
     WHERE analysis_run.analysis_run_pk = candidates.analysis_run_pk
    RETURNING analysis_run.analysis_run_pk
)
SELECT marked.analysis_run_pk, dispatch.source_bucket, dispatch.source_object_key
  FROM marked
  JOIN locked_dispatch AS dispatch
    ON dispatch.analysis_run_pk = marked.analysis_run_pk
"""

_RESERVE_RUN_SQL = """
INSERT INTO workspace.analysis_run (
    analysis_run_pk,
    user_id,
    status,
    original_filename,
    declared_mime_type,
    declared_size_bytes,
    expires_at
) VALUES (
    %s, %s, 'uploading', %s, %s, %s,
    clock_timestamp() + make_interval(secs => %s)
)
RETURNING analysis_run_pk, status, analysis_case_pk, error_code, error_message,
          created_at, updated_at
"""

_RESERVE_DISPATCH_SQL = """
INSERT INTO workspace.analysis_run_dispatch (
    analysis_run_pk,
    source_bucket,
    source_object_key,
    source_content_sha256
) VALUES (%s, %s, %s, %s)
"""

_LOCK_FINALIZATION_SQL = """
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
 WHERE analysis_run.analysis_run_pk = %s
   AND analysis_run.user_id = %s
 FOR UPDATE OF analysis_run, dispatch
"""

_INSERT_SOURCE_ARTIFACT_SQL = """
INSERT INTO workspace.source_artifact (
    analysis_run_pk,
    artifact_type,
    artifact_logical_id,
    storage_bucket,
    storage_object_key,
    content_sha256,
    mime_type,
    size_bytes
) VALUES (%s, 'source', %s, %s, %s, %s, %s, %s)
RETURNING artifact_pk
"""

_QUEUE_RUN_SQL = """
UPDATE workspace.analysis_run
   SET status = 'queued',
       expires_at = clock_timestamp() + make_interval(secs => %s),
       completed_at = NULL,
       error_code = NULL,
       error_message = NULL
 WHERE analysis_run_pk = %s
   AND user_id = %s
   AND status = 'uploading'
RETURNING analysis_run_pk, status, analysis_case_pk, error_code, error_message,
          created_at, updated_at
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
 WHERE analysis_run.analysis_run_pk = %s
   AND analysis_run.user_id = %s
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

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 10) -> None:
        self._database_url = database_url
        self._connect_timeout = connect_timeout_seconds

    def _ensure_configured(self) -> None:
        if not self._database_url:
            raise AnalysisRunPersistenceUnavailable("Analysis database is not configured")

    async def reserve_uploading(
        self,
        *,
        analysis_run_id: str,
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
                    await cursor.execute(
                        _CLAIM_STALE_UPLOAD_CLEANUP_SQL,
                        (_STALE_CLEANUP_BATCH_SIZE,),
                    )
                    cleanup_rows = await cursor.fetchall()
                    await cursor.execute(
                        _RESERVE_RUN_SQL,
                        (
                            analysis_run_id,
                            owner_id,
                            source.filename,
                            source.declared_mime_type or source.mime_type,
                            source.size_bytes,
                            _UPLOAD_RESERVATION_TTL_SECONDS,
                        ),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise AnalysisRunPersistenceUnavailable(
                            "Analysis upload reservation was not returned"
                        )
                    await cursor.execute(
                        _RESERVE_DISPATCH_SQL,
                        (
                            analysis_run_id,
                            source.bucket,
                            source.object_key,
                            source.content_sha256,
                        ),
                    )
                await connection.commit()
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name in {
                "analysis_run_pkey",
                "uq_workspace_analysis_run_one_active_per_user",
            }:
                confirmed = await self._read_exact_run(
                    analysis_run_id=analysis_run_id,
                    owner_id=owner_id,
                    source=source,
                )
                if confirmed is not None:
                    return UploadReservation(
                        record=confirmed,
                        replayed=confirmed.status != "uploading",
                    )
            if exc.diag.constraint_name == "uq_workspace_analysis_run_one_active_per_user":
                raise ActiveAnalysisRunExists("An active analysis run already exists") from exc
            if exc.diag.constraint_name == "analysis_run_pkey":
                raise ActiveAnalysisRunExists(
                    "The idempotency key is already in use"
                ) from exc
            raise AnalysisRunPersistenceUnavailable("Analysis upload could not be reserved") from exc
        except (psycopg.Error, OSError) as exc:
            confirmed = await self._read_exact_run(
                analysis_run_id=analysis_run_id,
                owner_id=owner_id,
                source=source,
            )
            if confirmed is not None:
                return UploadReservation(
                    record=confirmed,
                    replayed=confirmed.status != "uploading",
                )
            raise AnalysisRunPersistenceUnavailable("Analysis database is unavailable") from exc
        return UploadReservation(
            record=_record(row),
            cleanup_objects=tuple(_cleanup_object(item) for item in cleanup_rows),
        )

    async def finalize_queued(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
    ) -> AnalysisRunRecord:
        self._ensure_configured()
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        _LOCK_FINALIZATION_SQL,
                        (analysis_run_id, owner_id),
                    )
                    state = await cursor.fetchone()
                    if state is None or not _dispatch_matches(state, source):
                        raise AnalysisRunFinalizationUncertain(
                            "Analysis upload reservation does not match its source"
                        )
                    if state["status"] == "queued":
                        await cursor.execute(
                            _FINALIZATION_STATE_SQL,
                            _finalization_params(analysis_run_id, owner_id, source),
                        )
                        finalized = await cursor.fetchone()
                        if finalized and finalized["source_artifact_matches"]:
                            return _record(finalized)
                        raise AnalysisRunFinalizationUncertain(
                            "Queued analysis source could not be verified"
                        )
                    if state["status"] != "uploading":
                        raise AnalysisRunFinalizationUncertain(
                            "Analysis upload is no longer finalizable"
                        )

                    await cursor.execute(
                        _INSERT_SOURCE_ARTIFACT_SQL,
                        (
                            analysis_run_id,
                            source.filename,
                            source.bucket,
                            source.object_key,
                            source.content_sha256,
                            source.mime_type,
                            source.size_bytes,
                        ),
                    )
                    artifact = await cursor.fetchone()
                    if artifact is None:
                        raise AnalysisRunFinalizationRejected(
                            "Analysis source artifact was not returned"
                        )
                    await cursor.execute(
                        _QUEUE_RUN_SQL,
                        (
                            _QUEUED_SOURCE_TTL_SECONDS,
                            analysis_run_id,
                            owner_id,
                        ),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise AnalysisRunFinalizationRejected(
                            "Analysis upload could not transition to queued"
                        )
                await connection.commit()
        except (AnalysisRunFinalizationRejected, AnalysisRunFinalizationUncertain):
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
            raise AnalysisRunFinalizationUncertain(
                "Analysis upload finalization could not be confirmed"
            ) from exc
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
        analysis_run_id: str,
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
                        (analysis_run_id, owner_id),
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
                        _finalization_params(analysis_run_id, owner_id, source),
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


def _cleanup_object(row: Mapping[str, Any]) -> UploadCleanupObject:
    return UploadCleanupObject(
        analysis_run_id=str(row["analysis_run_pk"]),
        bucket=str(row["source_bucket"]),
        object_key=str(row["source_object_key"]),
    )


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
        source.bucket,
        source.object_key,
        source.content_sha256,
        source.mime_type,
        source.size_bytes,
        analysis_run_id,
        owner_id,
    )
