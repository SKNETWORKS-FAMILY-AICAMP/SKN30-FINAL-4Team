"""SHA-256 helpers. Digests are lowercase 64-character hex."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.models.outcomes import ErrorCode

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1024 * 1024


class SourceHashError(ValueError):
    def __init__(self, message: str, *, error_code: ErrorCode = ErrorCode.SOURCE_HASH_MISMATCH) -> None:
        super().__init__(message)
        self.error_code = error_code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sha256(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if not SHA256_RE.fullmatch(text):
        raise SourceHashError(
            "source SHA-256 must be a lowercase 64-character hex digest",
            error_code=ErrorCode.SOURCE_HASH_MISMATCH,
        )
    return text


def verify_source_sha256(path: str | Path, expected: str) -> str:
    actual = sha256_file(path)
    wanted = normalize_sha256(expected)
    if actual != wanted:
        raise SourceHashError("source_sha256 does not match file contents")
    return actual
