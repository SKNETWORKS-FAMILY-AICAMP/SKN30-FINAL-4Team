"""Trusted PostgreSQL adapter for worker profile artifacts and retrieval."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import math
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .analysis_job import (
    AnalysisJobContractError,
    AnalysisJobUnavailable,
    ArtifactRef,
    CachedRequestProfile,
    EmbeddingConfiguration,
    ExistingCandidate,
)


_CACHE_SQL = """
SELECT
    common.storage_bucket AS common_bucket,
    common.storage_object_key AS common_key,
    common.content_sha256 AS common_sha256,
    common.mime_type AS common_mime,
    common.size_bytes AS common_size,
    common.schema_version AS common_schema,
    common.artifact_logical_id AS common_logical_id,
    structured.storage_bucket AS profile_bucket,
    structured.storage_object_key AS profile_key,
    structured.content_sha256 AS profile_sha256,
    structured.mime_type AS profile_mime,
    structured.size_bytes AS profile_size,
    structured.schema_version AS profile_schema,
    structured.artifact_logical_id AS profile_logical_id
FROM workspace.request_profile profile
JOIN workspace.source_artifact structured
  ON structured.artifact_pk = profile.structured_artifact_pk
JOIN workspace.artifact_lineage lineage
  ON lineage.child_artifact_pk = structured.artifact_pk
 AND lineage.relation_type = 'input_to'
JOIN workspace.source_artifact common
  ON common.artifact_pk = lineage.parent_artifact_pk
 AND common.artifact_type = 'common_ir'
WHERE profile.analysis_run_pk = %s
ORDER BY profile.created_at DESC, common.created_at DESC
LIMIT 1
"""

_FENCE_SQL = """
SELECT 1 AS is_live
FROM workspace.analysis_run run
JOIN workspace.analysis_run_dispatch dispatch
  ON dispatch.analysis_run_pk = run.analysis_run_pk
WHERE run.analysis_run_pk = %s
  AND run.status = 'running'
  AND dispatch.processing_run_pk = %s
  AND dispatch.lease_expires_at > clock_timestamp()
FOR UPDATE OF run, dispatch
"""

_SOURCE_SQL = """
SELECT artifact_pk
FROM workspace.source_artifact
WHERE analysis_run_pk = %s
  AND artifact_type = 'source'
  AND storage_bucket = %s
  AND storage_object_key = %s
FOR UPDATE
"""

_INSERT_ARTIFACT_SQL = """
INSERT INTO workspace.source_artifact (
    analysis_run_pk, processing_run_pk, artifact_type, artifact_logical_id,
    storage_bucket, storage_object_key, content_sha256, mime_type,
    size_bytes, schema_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (storage_bucket, storage_object_key) DO NOTHING
RETURNING artifact_pk
"""

_EXISTING_ARTIFACT_SQL = """
SELECT artifact_pk, analysis_run_pk, processing_run_pk, artifact_type,
       artifact_logical_id, content_sha256, mime_type, size_bytes, schema_version
FROM workspace.source_artifact
WHERE storage_bucket = %s AND storage_object_key = %s
"""

_LINEAGE_SQL = """
INSERT INTO workspace.artifact_lineage (
    parent_artifact_pk, child_artifact_pk, relation_type
) VALUES (%s, %s, 'input_to')
ON CONFLICT DO NOTHING
"""

_EXISTING_PROFILE_SQL = """
SELECT profile.request_profile_pk, profile.profile_id, profile.schema_version,
       structured.content_sha256 AS structured_sha256
FROM workspace.request_profile profile
LEFT JOIN workspace.source_artifact structured
  ON structured.artifact_pk = profile.structured_artifact_pk
WHERE profile.analysis_run_pk = %s
FOR UPDATE OF profile
"""

_INGEST_SQL = """
SELECT workspace.ingest_request_profile_core(%s, %s) AS request_profile_pk
"""

_LINK_PROFILE_SQL = """
UPDATE workspace.request_profile
SET structured_artifact_pk = %s,
    processing_run_pk = %s,
    common_ir_document_id = COALESCE(
        NULLIF(%s, ''), common_ir_document_id
    ),
    candidate_pack_id = COALESCE(
        NULLIF(%s, ''), candidate_pack_id
    ),
    candidate_pack_generator = COALESCE(
        NULLIF(%s, ''), candidate_pack_generator
    ),
    candidate_pack_generator_version = COALESCE(
        NULLIF(%s, ''), candidate_pack_generator_version
    )
WHERE request_profile_pk = %s
"""

_ACTIVE_CONFIG_SQL = """
SELECT embedding_config_pk, provider, model_id, dimensions,
       max_input_tokens, assembly_version
FROM retrieval.embedding_configuration
WHERE is_active
"""

_MATCH_SQL = """
WITH matched AS (
    SELECT *
    FROM retrieval.match_existing_profiles_three_axis(
        %s::extensions.vector(1536),
        %s::extensions.vector(1536),
        %s::extensions.vector(1536),
        %s,
        %s::uuid
    )
)
SELECT
    matched.profile_version_pk,
    matched.average_cosine_similarity,
    matched.purpose_similarity,
    matched.target_similarity,
    matched.support_similarity,
    source_profile.source_profile_id,
    notice.notice_id,
    NULLIF(notice.portal_metadata ->> 'title', '') AS title,
    artifact.storage_bucket,
    artifact.storage_object_key,
    artifact.content_sha256,
    artifact.mime_type,
    artifact.size_bytes,
    profile.schema_version
FROM matched
JOIN kb.profile_version profile
  ON profile.profile_version_pk = matched.profile_version_pk
JOIN kb.source_version source_version
  ON source_version.source_version_pk = profile.source_version_pk
JOIN kb.source_profile source_profile
  ON source_profile.source_profile_pk = source_version.source_profile_pk
JOIN kb.notice notice ON notice.notice_pk = source_profile.notice_pk
JOIN kb.artifact artifact ON artifact.artifact_pk = profile.structured_artifact_pk
ORDER BY matched.average_cosine_similarity DESC, matched.profile_version_pk
"""


class PostgresAnalysisStore:
    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 10,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url must not be blank")
        self._database_url = database_url
        self._timeout = connect_timeout_seconds
        self._connect = connect or psycopg.connect

    def __repr__(self) -> str:
        return f"PostgresAnalysisStore(connect_timeout_seconds={self._timeout})"

    def cached_request_profile(self, *, analysis_run_id: str) -> CachedRequestProfile | None:
        row = self._read_one(_CACHE_SQL, (analysis_run_id,))
        if row is None:
            return None
        return CachedRequestProfile(
            common_ir=_artifact_from_row(row, "common", "common_ir"),
            structured_profile=_artifact_from_row(
                row, "profile", "structured_profile"
            ),
        )

    def register_request_profile(
        self,
        *,
        analysis_run_id: str,
        processing_run_id: str,
        source_bucket: str,
        source_object_key: str,
        common_ir: ArtifactRef,
        structured_profile: ArtifactRef,
        profile: Mapping[str, Any],
    ) -> None:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_FENCE_SQL, (analysis_run_id, processing_run_id))
                    if cursor.fetchone() is None:
                        raise AnalysisJobContractError(
                            "processing lease was lost before profile registration"
                        )
                    cursor.execute(
                        _SOURCE_SQL,
                        (analysis_run_id, source_bucket, source_object_key),
                    )
                    source = cursor.fetchone()
                    if source is None:
                        raise AnalysisJobContractError(
                            "claimed source artifact is not registered"
                        )
                    common_pk = self._insert_artifact(
                        cursor, analysis_run_id, processing_run_id, common_ir
                    )
                    profile_pk = self._insert_artifact(
                        cursor, analysis_run_id, processing_run_id, structured_profile
                    )
                    cursor.execute(_LINEAGE_SQL, (source["artifact_pk"], common_pk))
                    cursor.execute(_LINEAGE_SQL, (common_pk, profile_pk))

                    cursor.execute(_EXISTING_PROFILE_SQL, (analysis_run_id,))
                    existing = cursor.fetchone()
                    if existing is not None:
                        if (
                            existing["profile_id"] != profile.get("profile_id")
                            or existing["schema_version"] != profile.get("schema_version")
                            or existing["structured_sha256"]
                            != structured_profile.content_sha256
                        ):
                            raise AnalysisJobContractError(
                                "retry produced a different request profile artifact"
                            )
                        return

                    cursor.execute(
                        _INGEST_SQL,
                        (analysis_run_id, Jsonb(dict(profile))),
                    )
                    ingested = cursor.fetchone()
                    if ingested is None or ingested.get("request_profile_pk") is None:
                        raise AnalysisJobContractError(
                            "request profile materialisation returned no identity"
                        )
                    metadata = profile.get("processing_metadata")
                    metadata = metadata if isinstance(metadata, Mapping) else {}
                    candidate = metadata.get("candidate_pack")
                    candidate = candidate if isinstance(candidate, Mapping) else {}
                    cursor.execute(
                        _LINK_PROFILE_SQL,
                        (
                            profile_pk,
                            processing_run_id,
                            metadata.get("common_ir_document_id"),
                            candidate.get("candidate_pack_id") or candidate.get("id"),
                            candidate.get("candidate_pack_generator") or candidate.get("generator"),
                            candidate.get("candidate_pack_generator_version")
                            or candidate.get("generator_version"),
                            ingested["request_profile_pk"],
                        ),
                    )
        except AnalysisJobContractError:
            raise
        except (psycopg.Error, OSError):
            raise AnalysisJobUnavailable("analysis database is unavailable") from None

    def active_embedding_configuration(self) -> EmbeddingConfiguration:
        rows = self._read_all(_ACTIVE_CONFIG_SQL, ())
        if len(rows) != 1:
            raise AnalysisJobContractError(
                "exactly one active embedding configuration is required"
            )
        row = rows[0]
        return EmbeddingConfiguration(
            configuration_id=str(row["embedding_config_pk"]),
            provider=str(row["provider"]),
            model_id=str(row["model_id"]),
            dimensions=int(row["dimensions"]),
            max_input_tokens=int(row["max_input_tokens"]),
            assembly_version=str(row["assembly_version"]),
        )

    def match_existing_profiles(
        self,
        *,
        configuration_id: str,
        purpose: Sequence[float],
        target: Sequence[float],
        support: Sequence[float],
        limit: int,
    ) -> list[ExistingCandidate]:
        rows = self._read_all(
            _MATCH_SQL,
            (
                _vector_literal(purpose),
                _vector_literal(target),
                _vector_literal(support),
                limit,
                configuration_id,
            ),
        )
        return [
            ExistingCandidate(
                profile_version_id=str(row["profile_version_pk"]),
                source_profile_id=str(row["source_profile_id"]),
                notice_id=str(row["notice_id"]),
                average_similarity=_score(row["average_cosine_similarity"]),
                purpose_similarity=_score(row["purpose_similarity"]),
                target_similarity=_score(row["target_similarity"]),
                support_similarity=_score(row["support_similarity"]),
                title=row.get("title"),
                profile_artifact=ArtifactRef(
                    bucket=str(row["storage_bucket"]),
                    object_key=str(row["storage_object_key"]),
                    content_sha256=str(row["content_sha256"]).lower(),
                    artifact_type="structured_profile",
                    mime_type=str(row.get("mime_type") or "application/json"),
                    size_bytes=int(row.get("size_bytes") or 0),
                    schema_version=str(row["schema_version"]),
                    logical_id=str(row["source_profile_id"]),
                ),
            )
            for row in rows
        ]

    def _insert_artifact(
        self,
        cursor: Any,
        analysis_run_id: str,
        processing_run_id: str,
        artifact: ArtifactRef,
    ) -> UUID:
        cursor.execute(
            _INSERT_ARTIFACT_SQL,
            (
                analysis_run_id,
                processing_run_id,
                artifact.artifact_type,
                artifact.logical_id,
                artifact.bucket,
                artifact.object_key,
                artifact.content_sha256,
                artifact.mime_type,
                artifact.size_bytes,
                artifact.schema_version,
            ),
        )
        inserted = cursor.fetchone()
        if inserted is not None:
            return inserted["artifact_pk"]
        cursor.execute(
            _EXISTING_ARTIFACT_SQL, (artifact.bucket, artifact.object_key)
        )
        existing = cursor.fetchone()
        if existing is None or any(
            (
                existing["analysis_run_pk"] != UUID(analysis_run_id),
                existing["artifact_type"] != artifact.artifact_type,
                existing["artifact_logical_id"] != artifact.logical_id,
                str(existing["content_sha256"]).lower()
                != artifact.content_sha256.lower(),
                existing["mime_type"] != artifact.mime_type,
                int(existing["size_bytes"] or 0) != artifact.size_bytes,
                existing["schema_version"] != artifact.schema_version,
            )
        ):
            raise AnalysisJobContractError(
                "content-addressed artifact conflicts with different lineage"
            )
        return existing["artifact_pk"]

    def _connection(self):
        return self._connect(
            self._database_url,
            connect_timeout=self._timeout,
            row_factory=dict_row,
        )

    def _read_one(
        self, query: str, params: tuple[object, ...]
    ) -> Mapping[str, Any] | None:
        rows = self._read_all(query, params)
        return rows[0] if rows else None

    def _read_all(
        self, query: str, params: tuple[object, ...]
    ) -> list[Mapping[str, Any]]:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
                    return list(cursor.fetchall())
        except (psycopg.Error, OSError):
            raise AnalysisJobUnavailable("analysis database is unavailable") from None


def _artifact_from_row(
    row: Mapping[str, Any], prefix: str, artifact_type: str
) -> ArtifactRef:
    digest = str(row[f"{prefix}_sha256"]).lower()
    if len(digest) != 64:
        raise AnalysisJobContractError("cached artifact has invalid SHA-256")
    return ArtifactRef(
        bucket=str(row[f"{prefix}_bucket"]),
        object_key=str(row[f"{prefix}_key"]),
        content_sha256=digest,
        artifact_type=artifact_type,
        mime_type=str(row.get(f"{prefix}_mime") or "application/json"),
        size_bytes=int(row.get(f"{prefix}_size") or 0),
        schema_version=row.get(f"{prefix}_schema"),
        logical_id=row.get(f"{prefix}_logical_id"),
    )


def _vector_literal(values: Sequence[float]) -> str:
    if len(values) != 1536:
        raise AnalysisJobContractError("retrieval vector must have 1536 dimensions")
    converted = [float(value) for value in values]
    if any(not math.isfinite(value) for value in converted):
        raise AnalysisJobContractError("retrieval vector must be finite")
    return "[" + ",".join(format(value, ".17g") for value in converted) + "]"


def _score(value: object) -> float:
    score = float(value)
    if not math.isfinite(score):
        raise AnalysisJobContractError("retrieval score must be finite")
    return score
