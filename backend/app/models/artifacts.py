"""Immutable artifact metadata aligned with Storage Object Key Policy v0.1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class StorageBucket(StrEnum):
    EXISTING_KB = "existing-kb"
    REQUEST_TEMP = "request-temp"
    ANALYSIS_REPORTS = "analysis-reports"


class ArtifactType(StrEnum):
    SOURCE = "source"
    PARSER_RAW = "parser_raw"
    FORMAT_IR = "format_ir"
    COMMON_IR = "common_ir"
    CANDIDATE_PACK = "candidate_pack"
    STRUCTURED_PROFILE = "structured_profile"


EXISTING_ARTIFACT_TYPES: frozenset[ArtifactType] = frozenset(ArtifactType)
REQUEST_ARTIFACT_TYPES: frozenset[ArtifactType] = frozenset(ArtifactType)

ARTIFACT_EXTENSIONS: dict[ArtifactType, str] = {
    ArtifactType.SOURCE: "",  # actual source suffix
    ArtifactType.PARSER_RAW: "json",
    ArtifactType.FORMAT_IR: "json",
    ArtifactType.COMMON_IR: "json",
    ArtifactType.CANDIDATE_PACK: "json",
    ArtifactType.STRUCTURED_PROFILE: "json",
}

MIME_BY_SUFFIX: dict[str, str] = {
    ".hwp": "application/x-hwp",
    ".hwpx": "application/vnd.hancom.hwpx",
    ".pdf": "application/pdf",
    ".json": "application/json",
}


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    storage_bucket: StorageBucket
    storage_object_key: str
    content_sha256: str
    mime_type: str
    size_bytes: int
    schema_version: str | None
    created_at: datetime
    artifact_type: ArtifactType
