"""Immutable artifact metadata and Storage object-key policy.

Object keys are paths inside a bucket. The bucket name is not repeated in
the key. Keys are content-addressed so a different payload cannot reuse a
previous object key.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from app.models.artifacts import (
    ARTIFACT_EXTENSIONS,
    MIME_BY_SUFFIX,
    ArtifactMetadata,
    ArtifactType,
    StorageBucket,
)
from app.models.outcomes import ErrorCode
from app.models.pipeline import utc_now
from app.pipelines.hashing import normalize_sha256, sha256_bytes, sha256_file

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ObjectKeyError(ValueError):
    def __init__(self, message: str, *, error_code: ErrorCode = ErrorCode.INVALID_OBJECT_KEY) -> None:
        super().__init__(message)
        self.error_code = error_code


def sanitize_segment(value: str, *, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ObjectKeyError(f"{field} is empty")
    if _CONTROL.search(text):
        raise ObjectKeyError(f"{field} contains control characters")
    if "/" in text or "\\" in text or ".." in text:
        raise ObjectKeyError(f"{field} contains path separators or '..'")
    if text in {".", ".."}:
        raise ObjectKeyError(f"{field} is not a usable path segment")
    cleaned = text.replace("/", "-")
    if not _SAFE_SEGMENT.fullmatch(cleaned):
        raise ObjectKeyError(f"{field} contains characters that are not safe in an object key")
    if len(cleaned) > 128:
        raise ObjectKeyError(f"{field} exceeds 128 characters")
    return cleaned


def _digest(value: str, *, field: str) -> str:
    digest = normalize_sha256(value)
    return sanitize_segment(digest, field=field)


def _suffix(ext: str) -> str:
    text = ext.strip().lower().lstrip(".")
    if not text or not re.fullmatch(r"[a-z0-9]+", text):
        raise ObjectKeyError("artifact extension is invalid")
    return text


def existing_object_key(
    *,
    notice_id: str,
    source_profile_id: str,
    source_sha256: str,
    artifact_type: ArtifactType,
    content_sha256: str,
    ext: str,
) -> str:
    if artifact_type not in ArtifactType:
        raise ObjectKeyError(f"unsupported existing artifact_type: {artifact_type}")
    return "/".join(
        (
            sanitize_segment(notice_id, field="notice_id"),
            sanitize_segment(source_profile_id, field="source_profile_id"),
            _digest(source_sha256, field="source_sha256"),
            artifact_type.value,
            f"{_digest(content_sha256, field='content_sha256')}.{_suffix(ext)}",
        )
    )


def request_object_key(
    *,
    analysis_run_pk: str,
    artifact_type: ArtifactType,
    content_sha256: str,
    ext: str,
) -> str:
    if artifact_type not in ArtifactType:
        raise ObjectKeyError(f"unsupported request artifact_type: {artifact_type}")
    return "/".join(
        (
            sanitize_segment(analysis_run_pk, field="analysis_run_pk"),
            artifact_type.value,
            f"{_digest(content_sha256, field='content_sha256')}.{_suffix(ext)}",
        )
    )


def report_object_key(
    *,
    user_id: str,
    analysis_case_pk: str,
    report_type: str,
    content_sha256: str,
    ext: str,
) -> str:
    return "/".join(
        (
            sanitize_segment(user_id, field="user_id"),
            sanitize_segment(analysis_case_pk, field="analysis_case_pk"),
            sanitize_segment(report_type, field="report_type"),
            f"{_digest(content_sha256, field='content_sha256')}.{_suffix(ext)}",
        )
    )


def request_cleanup_prefix(analysis_run_pk: str) -> str:
    return f"{sanitize_segment(analysis_run_pk, field='analysis_run_pk')}/"


def build_artifact_metadata(
    *,
    bucket: StorageBucket,
    object_key: str,
    content: bytes | None = None,
    path: str | Path | None = None,
    artifact_type: ArtifactType,
    mime_type: str | None = None,
    schema_version: str | None = None,
    created_at: datetime | None = None,
    content_sha256: str | None = None,
) -> ArtifactMetadata:
    if content is None and path is None:
        raise ObjectKeyError("content or path is required")
    if content is not None:
        digest = sha256_bytes(content)
        size = len(content)
        if path is not None and sha256_file(path) != digest:
            raise ObjectKeyError("content and path digests do not match")
    else:
        source = Path(path)  # type: ignore[arg-type]
        digest = sha256_file(source)
        size = source.stat().st_size

    if content_sha256 is not None and normalize_sha256(content_sha256) != digest:
        raise ObjectKeyError("declared content_sha256 does not match payload")

    ext = Path(object_key).suffix.lower()
    inferred_mime = MIME_BY_SUFFIX.get(ext, "application/octet-stream")
    metadata = ArtifactMetadata(
        storage_bucket=bucket,
        storage_object_key=object_key,
        content_sha256=digest,
        mime_type=mime_type or inferred_mime,
        size_bytes=size,
        schema_version=schema_version,
        created_at=created_at or utc_now(),
        artifact_type=artifact_type,
    )
    _assert_immutable_key_matches(metadata, digest, ext)
    return metadata


def _assert_immutable_key_matches(metadata: ArtifactMetadata, digest: str, ext: str) -> None:
    filename = Path(metadata.storage_object_key).name
    expected = f"{digest}.{ext.lstrip('.')}" if ext else digest
    if filename != expected and not filename.startswith(digest):
        raise ObjectKeyError("object key filename must be content-addressed by SHA-256")


def replace_metadata(**_kwargs: object) -> ArtifactMetadata:
    raise TypeError("artifact metadata is immutable")


def derived_artifact_ext(artifact_type: ArtifactType, *, source_ext: str | None = None) -> str:
    if artifact_type is ArtifactType.SOURCE:
        if not source_ext:
            raise ObjectKeyError("source artifacts require the original extension")
        return _suffix(source_ext)
    return ARTIFACT_EXTENSIONS[artifact_type]
