"""Source format gates for Request and Existing ingest.

Request production ingest in this layer accepts .hwp and .hwpx.
Existing ingest accepts .hwp, .hwpx, and .pdf. Markdown fixtures are
test-only in the vendor package and are not a production Existing format.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.models.artifacts import MIME_BY_SUFFIX
from app.models.outcomes import ErrorCode
from app.models.pipeline import PipelineKind

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK"
PDF_MAGIC = b"%PDF"

REQUEST_SUFFIXES = frozenset({".hwp", ".hwpx"})
EXISTING_SUFFIXES = frozenset({".hwp", ".hwpx", ".pdf"})


class FormatError(ValueError):
    def __init__(self, message: str, *, error_code: ErrorCode = ErrorCode.UNSUPPORTED_FORMAT) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class FormatDecision:
    kind: PipelineKind
    suffix: str
    source_kind: str
    mime_type: str
    filename: str


def allowed_suffixes(kind: PipelineKind) -> frozenset[str]:
    if kind is PipelineKind.REQUEST:
        return REQUEST_SUFFIXES
    if kind is PipelineKind.EXISTING:
        return EXISTING_SUFFIXES
    raise FormatError(f"unknown document kind: {kind}")


def validate_format(
    kind: PipelineKind,
    path: str | Path,
    *,
    content: bytes | None = None,
) -> FormatDecision:
    filename = Path(path).name
    if not filename or filename in {".", ".."}:
        raise FormatError("source filename is empty")
    suffix = Path(filename).suffix.lower()
    allowed = allowed_suffixes(kind)
    if suffix not in allowed:
        raise FormatError(
            f"{kind} source must be one of {sorted(allowed)}; got {suffix or '<none>'}"
        )
    if content is not None:
        _assert_magic(suffix, content)
    return FormatDecision(
        kind=kind,
        suffix=suffix,
        source_kind=suffix.lstrip("."),
        mime_type=MIME_BY_SUFFIX[suffix],
        filename=filename,
    )


def _assert_magic(suffix: str, content: bytes) -> None:
    head = content[:8]
    if suffix == ".pdf":
        if not content.startswith(PDF_MAGIC):
            raise FormatError("PDF source does not start with %PDF")
        return
    if suffix == ".hwp":
        if not head.startswith(OLE_MAGIC):
            raise FormatError("HWP source is not an OLE compound document")
        return
    if suffix == ".hwpx":
        if not head.startswith(ZIP_MAGIC):
            raise FormatError("HWPX source is not a ZIP container")
        return
    raise FormatError(f"no magic-byte rule for {suffix}")
