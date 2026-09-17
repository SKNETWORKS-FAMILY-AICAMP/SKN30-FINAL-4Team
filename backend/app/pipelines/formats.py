"""Source format gates for Request and Existing ingest.

Request production ingest in this layer accepts .hwp and .hwpx.
Existing ingest accepts .hwp, .hwpx, and .pdf. Markdown fixtures are
test-only in the vendor package and are not a production Existing format.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, LargeZipFile, ZipFile

from app.models.artifacts import MIME_BY_SUFFIX
from app.models.outcomes import ErrorCode
from app.models.pipeline import PipelineKind

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PDF_MAGIC = b"%PDF"
HWPX_MIMETYPE = b"application/hwp+zip"
HWPX_REQUIRED_MEMBERS = frozenset(
    {
        "mimetype",
        "META-INF/container.xml",
        "Contents/content.hpf",
        "Contents/header.xml",
        "Contents/section0.xml",
    }
)
HWPX_MAX_MEMBERS = 10_000
HWPX_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024

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
        _assert_hwpx(content)
        return
    raise FormatError(f"no magic-byte rule for {suffix}")


def _assert_hwpx(content: bytes) -> None:
    """Validate the bounded HWPX container without extracting it to disk."""

    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > HWPX_MAX_MEMBERS:
                raise FormatError("HWPX contains too many ZIP members")

            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise FormatError("HWPX contains duplicate ZIP members")

            if sum(info.file_size for info in infos) > HWPX_MAX_UNCOMPRESSED_BYTES:
                raise FormatError("HWPX declared uncompressed size exceeds limit")

            info_by_name = {info.filename: info for info in infos}
            if HWPX_REQUIRED_MEMBERS.difference(info_by_name):
                raise FormatError("HWPX is missing required members")

            for name in HWPX_REQUIRED_MEMBERS:
                info = info_by_name[name]
                if info.is_dir() or (info.flag_bits & 0x1):
                    raise FormatError("HWPX contains an invalid required member")

            mimetype = info_by_name["mimetype"]
            if mimetype.file_size != len(HWPX_MIMETYPE):
                raise FormatError("HWPX mimetype is invalid")
            if archive.read(mimetype) != HWPX_MIMETYPE:
                raise FormatError("HWPX mimetype is invalid")
    except FormatError:
        raise
    except (BadZipFile, LargeZipFile, OSError, RuntimeError, ValueError) as exc:
        raise FormatError("HWPX source is not a valid ZIP container") from exc
