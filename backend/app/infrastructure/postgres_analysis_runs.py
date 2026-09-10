"""Direct trusted-Postgres adapter for FastAPI analysis-run commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    AnalysisRunPersistenceUnavailable,
    AnalysisRunRecord,
    SourceObject,
)


_CREATE_RUN_SQL = """
INSERT INTO workspace.analysis_run (
    analysis_run_pk,
    user_id,
    status,
    original_filename,
    declared_mime_type,
    declared_size_bytes
) VALUES (%s, %s, 'queued', %s, %s, %s)
RETURNING analysis_run_pk, status, analysis_case_pk, error_code, error_message,
          created_at, updated_at
"""

_CREATE_DISPATCH_SQL = """
INSERT INTO workspace.analysis_run_dispatch (
    analysis_run_pk,
    source_bucket,
    source_object_key,
    source_content_sha256
) VALUES (%s, %s, %s, %s)
"""

_CREATE_SOURCE_ARTIFACT_SQL = """
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
"""

_GET_RUN_SQL = """
SELECT analysis_run_pk, status, analysis_case_pk, error_code, error_message,
       created_at, updated_at
  FROM workspace.analysis_run
 WHERE analysis_run_pk = %s
   AND user_id = %s
"""


class PostgresAnalysisRunRepository:
    """Persist one queue item and its immutable source in one transaction."""

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 10) -> None:
        self._database_url = database_url
        self._connect_timeout = connect_timeout_seconds

    def _ensure_configured(self) -> None:
        if not self._database_url:
            raise AnalysisRunPersistenceUnavailable("Analysis database is not configured")

    async def create_queued(
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
                        _CREATE_RUN_SQL,
                        (
                            analysis_run_id,
                            owner_id,
                            source.filename,
                            source.declared_mime_type or source.mime_type,
                            source.size_bytes,
                        ),
                    )
                    row = await cursor.fetchone()
                    await cursor.execute(
                        _CREATE_DISPATCH_SQL,
                        (
                            analysis_run_id,
                            source.bucket,
                            source.object_key,
                            source.content_sha256,
                        ),
                    )
                    await cursor.execute(
                        _CREATE_SOURCE_ARTIFACT_SQL,
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
                await connection.commit()
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "uq_workspace_analysis_run_one_active_per_user":
                raise ActiveAnalysisRunExists("An active analysis run already exists") from exc
            raise AnalysisRunPersistenceUnavailable("Analysis run could not be persisted") from exc
        except (psycopg.Error, OSError) as exc:
            raise AnalysisRunPersistenceUnavailable("Analysis database is unavailable") from exc
        if row is None:
            raise AnalysisRunPersistenceUnavailable("Analysis run was not returned after creation")
        return _record(row)

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
