"""Document preflight: format gate, SHA-256, and immutable artifact metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.models.artifacts import ArtifactMetadata, ArtifactType, StorageBucket
from app.models.pipeline import PipelineKind
from app.pipelines.artifacts import (
    build_artifact_metadata,
    existing_object_key,
    request_object_key,
)
from app.pipelines.formats import FormatDecision, validate_format
from app.pipelines.hashing import sha256_file, verify_source_sha256


@dataclass(frozen=True, slots=True)
class SourcePreflight:
    kind: PipelineKind
    format: FormatDecision
    source_sha256: str
    artifact: ArtifactMetadata


def preflight_source(
    kind: PipelineKind,
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    analysis_run_pk: str | None = None,
    notice_id: str | None = None,
    source_profile_id: str | None = None,
    content: bytes | None = None,
) -> SourcePreflight:
    source = Path(path)
    decision = validate_format(kind, source, content=content)
    digest = verify_source_sha256(source, expected_sha256) if expected_sha256 else sha256_file(source)
    if kind is PipelineKind.REQUEST:
        if not analysis_run_pk:
            raise ValueError("analysis_run_pk is required for request artifacts")
        key = request_object_key(
            analysis_run_pk=analysis_run_pk,
            artifact_type=ArtifactType.SOURCE,
            content_sha256=digest,
            ext=decision.suffix,
        )
        bucket = StorageBucket.REQUEST_TEMP
    else:
        if not notice_id or not source_profile_id:
            raise ValueError("notice_id and source_profile_id are required for existing artifacts")
        key = existing_object_key(
            notice_id=notice_id,
            source_profile_id=source_profile_id,
            source_sha256=digest,
            artifact_type=ArtifactType.SOURCE,
            content_sha256=digest,
            ext=decision.suffix,
        )
        bucket = StorageBucket.EXISTING_KB
    metadata = build_artifact_metadata(
        bucket=bucket,
        object_key=key,
        path=source,
        artifact_type=ArtifactType.SOURCE,
        mime_type=decision.mime_type,
        schema_version=None,
        content_sha256=digest,
    )
    return SourcePreflight(
        kind=kind,
        format=decision,
        source_sha256=digest,
        artifact=metadata,
    )
