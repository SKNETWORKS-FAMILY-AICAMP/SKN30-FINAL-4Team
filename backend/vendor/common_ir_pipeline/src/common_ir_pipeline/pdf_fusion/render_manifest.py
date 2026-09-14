"""Document-level, fail-closed bindings for rendered PDF page images.

The coordinate manifest describes a *single* rendered page.  This module
binds those page manifests to the original PDF and the immutable PNG files
that an EC2 renderer produced.  It deliberately does not render PDFs: a
renderer integration must hand it already-created files and the exact
per-page :class:`PdfCoordinateManifest` values.

The resulting ``pdf_render_manifest/v1`` is an artifact boundary, not a
best-effort inventory.  Unknown fields, missing files, hash/size mismatches,
page gaps, and symlinks are all rejected.  In particular, a consumer must use
``validate_render_manifest_files`` before it sends page images to a remote
layout worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import struct
import zlib
from typing import Any, Mapping, Sequence

from .coordinate_manifest import CoordinateManifestError, PdfCoordinateManifest


SCHEMA_VERSION = "pdf_render_manifest/v1"
PNG_MIME_TYPE = "image/png"
_SHA256_LENGTH = 64
# Initial safety limits only.  Tune these from trusted renderer-corpus p99s
# before production rollout.
MAX_PNG_FILE_BYTES = 64 * 1024 * 1024
MAX_PNG_DIMENSION_PX = 20_000
MAX_PNG_TOTAL_PIXELS = 100_000_000
# A render-manifest page is deliberately narrower than the whole PNG format:
# only non-interlaced, 8-bit RGB/RGBA output from the pinned renderer is
# accepted.  This permits a deterministic scanline size before decompression.
MAX_PNG_DECOMPRESSED_BYTES = 128 * 1024 * 1024
_MAX_PNG_DECOMPRESS_OUTPUT_CHUNK_BYTES = 64 * 1024
_DOCUMENT_KEYS = frozenset({
    "schema_version",
    "source_pdf_relative_path",
    "source_pdf_sha256",
    "source_pdf_size_bytes",
    "page_count",
    "renderer",
    "renderer_version",
    "renderer_config_sha256",
    "pages",
})
_PAGE_KEYS = frozenset({
    "page",
    "image_relative_path",
    "image_mime_type",
    "image_sha256",
    "image_size_bytes",
    "coordinate_manifest",
    "coordinate_manifest_sha256",
})


class PdfRenderManifestError(ValueError):
    """Raised when a rendered-page artifact cannot be trusted."""


def _require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        raise PdfRenderManifestError(f"{name} must be a lowercase SHA-256 hex digest")
    if any(character not in "0123456789abcdef" for character in value):
        raise PdfRenderManifestError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _require_nonempty_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PdfRenderManifestError(f"{name} must be a non-empty string")
    return value


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PdfRenderManifestError(f"{name} must be a positive integer")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PdfRenderManifestError("manifest is not canonically serializable JSON") from error


def _assert_exact_keys(value: Mapping[str, Any], expected: frozenset[str], *, name: str) -> None:
    keys = frozenset(value.keys())
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected keys: {', '.join(extra)}")
        raise PdfRenderManifestError(f"{name} keys are invalid ({'; '.join(details)})")


def _artifact_root(root: str | Path) -> Path:
    candidate = Path(root)
    if candidate.is_symlink():
        raise PdfRenderManifestError("artifact_root must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise PdfRenderManifestError("artifact_root must exist") from error
    if not resolved.is_dir():
        raise PdfRenderManifestError("artifact_root must be a directory")
    return resolved


def _safe_relative_path(value: object, *, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PdfRenderManifestError(f"{name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PdfRenderManifestError(f"{name} must be a safe relative path")
    # Windows separators are not path separators on POSIX; reject them so the
    # serialized storage key has one portable interpretation.
    if "\\" in value:
        raise PdfRenderManifestError(f"{name} must use POSIX path separators")
    return path


def _bound_file(root: Path, relative_path: object, *, name: str) -> tuple[Path, str]:
    relative = _safe_relative_path(relative_path, name=name)
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise PdfRenderManifestError(f"{name} must not traverse a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise PdfRenderManifestError(f"{name} does not exist") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise PdfRenderManifestError(f"{name} escapes artifact_root") from error
    if not resolved.is_file():
        raise PdfRenderManifestError(f"{name} must refer to a regular file")
    return resolved, relative.as_posix()


def _relative_bound_file(root: Path, path: str | Path, *, name: str) -> tuple[Path, str]:
    supplied = Path(path)
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise PdfRenderManifestError(f"{name} must be inside artifact_root") from error
    return _bound_file(root, relative.as_posix(), name=name)


def _sha256_size_and_prefix(path: Path, *, prefix_size: int) -> tuple[str, int, bytes]:
    digest = sha256()
    size = 0
    prefix = b""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PdfRenderManifestError("bound artifact must be a regular file")
            prefix = handle.read(prefix_size)
            digest.update(prefix)
            size += len(prefix)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise PdfRenderManifestError("failed to read bound artifact") from error
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_identity != after_identity:
        raise PdfRenderManifestError("bound artifact changed while it was being read")
    return digest.hexdigest(), size, prefix


def _inspect_pdf(path: Path) -> tuple[str, int]:
    digest, size, prefix = _sha256_size_and_prefix(path, prefix_size=8)
    if not prefix.startswith(b"%PDF-"):
        raise PdfRenderManifestError("source artifact must have a PDF header")
    return digest, size


def _inspect_png(path: Path) -> tuple[str, int, int, int]:
    """Hash and structurally validate one bounded PNG from its opened bytes.

    The initial renderer contract accepts only non-interlaced 8-bit RGB/RGBA
    PNGs.  It validates the complete concatenated IDAT zlib stream without
    materializing decompressed pixels: output is bounded by the expected
    filtered scanline size and consumed in small chunks.  It deliberately
    does not apply PNG filters or decode pixels into an image buffer.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PdfRenderManifestError("bound artifact must be a regular file")
            if before.st_size > MAX_PNG_FILE_BYTES:
                raise PdfRenderManifestError("rendered PNG exceeds the compressed-file safety cap")
            data = handle.read(MAX_PNG_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise PdfRenderManifestError("failed to read rendered PNG") from error
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_identity != after_identity:
        raise PdfRenderManifestError("bound artifact changed while it was being read")
    if len(data) != before.st_size or len(data) > MAX_PNG_FILE_BYTES:
        raise PdfRenderManifestError("rendered PNG exceeds the compressed-file safety cap")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PdfRenderManifestError("rendered page image must have a PNG signature")
    offset = len(b"\x89PNG\r\n\x1a\n")
    seen_ihdr = seen_iend = False
    seen_idat = False
    idat_closed = False
    width = height = 0
    decoder: zlib.Decompress | None = None
    expected_decompressed_bytes = 0
    decoded_bytes = 0
    scanline_payload_bytes = 0
    rows_started = 0
    remaining_scanline_payload_bytes = 0

    def consume_decoded_bytes(decoded: bytes) -> None:
        """Check output size and PNG filter bytes without retaining pixels."""
        nonlocal decoded_bytes, rows_started, remaining_scanline_payload_bytes
        if decoded_bytes + len(decoded) > expected_decompressed_bytes:
            raise PdfRenderManifestError("rendered PNG IDAT expands beyond expected scanline bytes")
        decoded_bytes += len(decoded)
        offset_in_output = 0
        while offset_in_output < len(decoded):
            if remaining_scanline_payload_bytes == 0:
                if rows_started >= height:
                    raise PdfRenderManifestError("rendered PNG IDAT expands beyond declared rows")
                filter_byte = decoded[offset_in_output]
                offset_in_output += 1
                if filter_byte > 4:
                    raise PdfRenderManifestError("rendered PNG contains an invalid scanline filter byte")
                rows_started += 1
                remaining_scanline_payload_bytes = scanline_payload_bytes
            available = len(decoded) - offset_in_output
            take = min(available, remaining_scanline_payload_bytes)
            offset_in_output += take
            remaining_scanline_payload_bytes -= take

    def consume_idat_bytes(chunk_data: bytes) -> None:
        """Feed one contiguous IDAT segment using a bounded DEFLATE output."""
        if decoder is None:  # pragma: no cover - IHDR setup is mandatory
            raise PdfRenderManifestError("rendered PNG IDAT appears before IHDR")
        if decoder.eof:
            raise PdfRenderManifestError("rendered PNG IDAT contains bytes after the zlib stream")
        pending = chunk_data
        while pending:
            try:
                decoded = decoder.decompress(pending, _MAX_PNG_DECOMPRESS_OUTPUT_CHUNK_BYTES)
            except zlib.error as error:
                raise PdfRenderManifestError("rendered PNG IDAT DEFLATE stream is invalid") from error
            consume_decoded_bytes(decoded)
            if decoder.unused_data:
                raise PdfRenderManifestError("rendered PNG IDAT contains bytes after the zlib stream")
            next_pending = decoder.unconsumed_tail
            if next_pending and not decoded and len(next_pending) >= len(pending):
                raise PdfRenderManifestError("rendered PNG IDAT DEFLATE stream made no progress")
            pending = next_pending
    while offset < len(data):
        if len(data) - offset < 12:
            raise PdfRenderManifestError("rendered PNG has a truncated chunk")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        if any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in chunk_type):
            raise PdfRenderManifestError("rendered PNG has an invalid chunk type")
        end = offset + 12 + length
        if end > len(data):
            raise PdfRenderManifestError("rendered PNG has a truncated chunk")
        chunk_data = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length:end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            raise PdfRenderManifestError("rendered PNG chunk checksum is invalid")
        if not seen_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                raise PdfRenderManifestError("rendered page image must start with exactly one PNG IHDR chunk")
            seen_ihdr = True
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", chunk_data)
            if width < 1 or height < 1 or width > MAX_PNG_DIMENSION_PX or height > MAX_PNG_DIMENSION_PX or width * height > MAX_PNG_TOTAL_PIXELS:
                raise PdfRenderManifestError("rendered PNG dimensions exceed safety caps")
            if bit_depth != 8 or color_type not in {2, 6} or compression != 0 or filter_method != 0 or interlace != 0:
                raise PdfRenderManifestError(
                    "rendered PNG must be non-interlaced 8-bit RGB or RGBA with standard compression/filter methods"
                )
            channels = 3 if color_type == 2 else 4
            scanline_payload_bytes = width * channels
            expected_decompressed_bytes = height * (scanline_payload_bytes + 1)
            if expected_decompressed_bytes > MAX_PNG_DECOMPRESSED_BYTES:
                raise PdfRenderManifestError("rendered PNG expected decompressed scanlines exceed safety cap")
            decoder = zlib.decompressobj()
        else:
            if chunk_type == b"IHDR":
                raise PdfRenderManifestError("rendered PNG must contain exactly one IHDR chunk")
            if chunk_type == b"IDAT":
                if idat_closed:
                    raise PdfRenderManifestError("rendered PNG IDAT chunks must be contiguous")
                seen_idat = True
                consume_idat_bytes(chunk_data)
            else:
                if seen_idat:
                    idat_closed = True
                if chunk_type == b"IEND":
                    if seen_iend or length != 0:
                        raise PdfRenderManifestError("rendered PNG IEND chunk is invalid")
                    seen_iend = True
                    if end != len(data):
                        raise PdfRenderManifestError("rendered PNG has trailing bytes after IEND")
                elif 65 <= chunk_type[0] <= 90:
                    raise PdfRenderManifestError("rendered PNG contains an unknown critical chunk")
        offset = end
    if not seen_ihdr or not seen_idat or not seen_iend:
        raise PdfRenderManifestError("rendered PNG must contain IHDR, contiguous IDAT, and final IEND chunks")
    if decoder is None or not decoder.eof:
        raise PdfRenderManifestError("rendered PNG IDAT DEFLATE stream is truncated")
    if decoder.unused_data:
        raise PdfRenderManifestError("rendered PNG IDAT contains bytes after the zlib stream")
    if decoded_bytes != expected_decompressed_bytes or rows_started != height or remaining_scanline_payload_bytes != 0:
        raise PdfRenderManifestError("rendered PNG IDAT size does not match declared scanlines")
    return sha256(data).hexdigest(), len(data), width, height


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """One immutable PNG plus its complete coordinate-manifest binding."""

    page: int
    image_relative_path: str
    image_sha256: str
    image_size_bytes: int
    coordinate_manifest: PdfCoordinateManifest
    coordinate_manifest_sha256: str
    image_mime_type: str = PNG_MIME_TYPE

    def __post_init__(self) -> None:
        object.__setattr__(self, "page", _positive_int("page", self.page))
        object.__setattr__(
            self,
            "image_relative_path",
            _safe_relative_path(self.image_relative_path, name="image_relative_path").as_posix(),
        )
        if Path(self.image_relative_path).suffix.lower() != ".png":
            raise PdfRenderManifestError("image_relative_path must use a .png extension")
        if self.image_mime_type != PNG_MIME_TYPE:
            raise PdfRenderManifestError(f"image_mime_type must be {PNG_MIME_TYPE!r}")
        object.__setattr__(self, "image_sha256", _require_sha256("image_sha256", self.image_sha256))
        object.__setattr__(self, "image_size_bytes", _positive_int("image_size_bytes", self.image_size_bytes))
        if not isinstance(self.coordinate_manifest, PdfCoordinateManifest):
            raise PdfRenderManifestError("coordinate_manifest must be a PdfCoordinateManifest")
        expected_manifest_sha256 = self.coordinate_manifest.manifest_sha256()
        object.__setattr__(
            self,
            "coordinate_manifest_sha256",
            _require_sha256("coordinate_manifest_sha256", self.coordinate_manifest_sha256),
        )
        if self.coordinate_manifest_sha256 != expected_manifest_sha256:
            raise PdfRenderManifestError("coordinate_manifest_sha256 does not bind coordinate_manifest")
        if self.coordinate_manifest.page != self.page:
            raise PdfRenderManifestError("page does not match coordinate_manifest.page")
        if self.coordinate_manifest.page_image_sha256 != self.image_sha256:
            raise PdfRenderManifestError("image_sha256 does not match coordinate_manifest.page_image_sha256")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RenderedPage":
        if not isinstance(value, Mapping):
            raise PdfRenderManifestError("page entry must be an object")
        _assert_exact_keys(value, _PAGE_KEYS, name="page entry")
        try:
            coordinate_manifest = PdfCoordinateManifest.from_dict(value["coordinate_manifest"])
        except CoordinateManifestError as error:
            raise PdfRenderManifestError("coordinate_manifest is invalid") from error
        return cls(
            page=value["page"],
            image_relative_path=value["image_relative_path"],
            image_mime_type=value["image_mime_type"],
            image_sha256=value["image_sha256"],
            image_size_bytes=value["image_size_bytes"],
            coordinate_manifest=coordinate_manifest,
            coordinate_manifest_sha256=value["coordinate_manifest_sha256"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "image_relative_path": self.image_relative_path,
            "image_mime_type": self.image_mime_type,
            "image_sha256": self.image_sha256,
            "image_size_bytes": self.image_size_bytes,
            "coordinate_manifest": self.coordinate_manifest.to_dict(),
            "coordinate_manifest_sha256": self.coordinate_manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class PdfRenderManifest:
    """Validated document-level binding for a deterministic PDF rendering."""

    source_pdf_relative_path: str
    source_pdf_sha256: str
    source_pdf_size_bytes: int
    page_count: int
    renderer: str
    renderer_version: str
    renderer_config_sha256: str
    pages: tuple[RenderedPage, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise PdfRenderManifestError(f"schema_version must be {SCHEMA_VERSION!r}")
        object.__setattr__(
            self,
            "source_pdf_relative_path",
            _safe_relative_path(self.source_pdf_relative_path, name="source_pdf_relative_path").as_posix(),
        )
        if Path(self.source_pdf_relative_path).suffix.lower() != ".pdf":
            raise PdfRenderManifestError("source_pdf_relative_path must use a .pdf extension")
        object.__setattr__(self, "source_pdf_sha256", _require_sha256("source_pdf_sha256", self.source_pdf_sha256))
        object.__setattr__(self, "source_pdf_size_bytes", _positive_int("source_pdf_size_bytes", self.source_pdf_size_bytes))
        object.__setattr__(self, "page_count", _positive_int("page_count", self.page_count))
        object.__setattr__(self, "renderer", _require_nonempty_string("renderer", self.renderer))
        object.__setattr__(self, "renderer_version", _require_nonempty_string("renderer_version", self.renderer_version))
        object.__setattr__(self, "renderer_config_sha256", _require_sha256("renderer_config_sha256", self.renderer_config_sha256))
        if not isinstance(self.pages, tuple) or not self.pages:
            raise PdfRenderManifestError("pages must be a non-empty tuple")
        for index, rendered_page in enumerate(self.pages, start=1):
            if not isinstance(rendered_page, RenderedPage):
                raise PdfRenderManifestError("pages must contain RenderedPage values")
            if rendered_page.page != index:
                raise PdfRenderManifestError("pages must be 1-based, contiguous, and unique")
            coordinate = rendered_page.coordinate_manifest
            if coordinate.page_count != self.page_count:
                raise PdfRenderManifestError("coordinate_manifest page_count does not match document page_count")
            if coordinate.source_sha256 != self.source_pdf_sha256:
                raise PdfRenderManifestError("coordinate_manifest source_sha256 does not match source PDF")
            if coordinate.renderer != self.renderer:
                raise PdfRenderManifestError("coordinate_manifest renderer does not match document renderer")
            if coordinate.renderer_version != self.renderer_version:
                raise PdfRenderManifestError("coordinate_manifest renderer_version does not match document renderer")
            if coordinate.renderer_config_sha256 != self.renderer_config_sha256:
                raise PdfRenderManifestError("coordinate_manifest renderer_config_sha256 does not match document renderer")
        image_paths = [page.image_relative_path for page in self.pages]
        if len(image_paths) != len(set(image_paths)):
            raise PdfRenderManifestError("pages must not reuse an image_relative_path")
        if len(self.pages) != self.page_count:
            raise PdfRenderManifestError("pages must contain every page declared by page_count")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PdfRenderManifest":
        if not isinstance(value, Mapping):
            raise PdfRenderManifestError("render manifest must be an object")
        _assert_exact_keys(value, _DOCUMENT_KEYS, name="render manifest")
        pages_value = value["pages"]
        if not isinstance(pages_value, list):
            raise PdfRenderManifestError("pages must be an array")
        return cls(
            schema_version=value["schema_version"],
            source_pdf_relative_path=value["source_pdf_relative_path"],
            source_pdf_sha256=value["source_pdf_sha256"],
            source_pdf_size_bytes=value["source_pdf_size_bytes"],
            page_count=value["page_count"],
            renderer=value["renderer"],
            renderer_version=value["renderer_version"],
            renderer_config_sha256=value["renderer_config_sha256"],
            pages=tuple(RenderedPage.from_dict(item) for item in pages_value),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_pdf_relative_path": self.source_pdf_relative_path,
            "source_pdf_sha256": self.source_pdf_sha256,
            "source_pdf_size_bytes": self.source_pdf_size_bytes,
            "page_count": self.page_count,
            "renderer": self.renderer,
            "renderer_version": self.renderer_version,
            "renderer_config_sha256": self.renderer_config_sha256,
            "pages": [page.to_dict() for page in self.pages],
        }

    def canonical_json(self) -> bytes:
        return _canonical_json(self.to_dict())

    def manifest_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()


@dataclass(frozen=True, slots=True)
class RenderedPageInput:
    """Input supplied by an actual renderer after it has created one PNG."""

    image_path: str | Path
    coordinate_manifest: PdfCoordinateManifest


def assemble_render_manifest(
    *,
    artifact_root: str | Path,
    source_pdf_path: str | Path,
    pages: Sequence[RenderedPageInput],
) -> PdfRenderManifest:
    """Build and immediately verify a manifest from immutable local artifacts.

    All page metadata is derived from the checked coordinate manifests.  No
    caller-provided source hash, renderer identity, image hash, or size is
    trusted.  The source PDF and every PNG must be regular, non-symlink files
    below ``artifact_root``.
    """
    root = _artifact_root(artifact_root)
    source_path, source_relative_path = _relative_bound_file(root, source_pdf_path, name="source_pdf_path")
    source_sha256, source_size_bytes = _inspect_pdf(source_path)
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)) or not pages:
        raise PdfRenderManifestError("pages must be a non-empty sequence")

    rendered_pages: list[RenderedPage] = []
    supplied_image_paths: set[str] = set()
    for input_page in pages:
        if not isinstance(input_page, RenderedPageInput):
            raise PdfRenderManifestError("pages must contain RenderedPageInput values")
        coordinate = input_page.coordinate_manifest
        if not isinstance(coordinate, PdfCoordinateManifest):
            raise PdfRenderManifestError("page coordinate_manifest must be a PdfCoordinateManifest")
        image_path, image_relative_path = _relative_bound_file(root, input_page.image_path, name="page image")
        if image_relative_path in supplied_image_paths:
            raise PdfRenderManifestError("pages must not reuse a page image path")
        supplied_image_paths.add(image_relative_path)
        image_sha256, image_size_bytes, image_width, image_height = _inspect_png(image_path)
        if (
            image_width != coordinate.rendered_width_px
            or image_height != coordinate.rendered_height_px
        ):
            raise PdfRenderManifestError(
                f"page {coordinate.page} PNG dimensions do not match coordinate_manifest"
            )
        rendered_pages.append(
            RenderedPage(
                page=coordinate.page,
                image_relative_path=image_relative_path,
                image_sha256=image_sha256,
                image_size_bytes=image_size_bytes,
                coordinate_manifest=coordinate,
                coordinate_manifest_sha256=coordinate.manifest_sha256(),
            )
        )

    if not rendered_pages:
        raise PdfRenderManifestError("pages must be non-empty")
    # Input traversal order is not a lineage property.  Canonicalize by PDF
    # page number, then let ``PdfRenderManifest`` reject a duplicate or gap.
    rendered_pages.sort(key=lambda rendered_page: rendered_page.page)
    first_coordinate = rendered_pages[0].coordinate_manifest
    manifest = PdfRenderManifest(
        source_pdf_relative_path=source_relative_path,
        source_pdf_sha256=source_sha256,
        source_pdf_size_bytes=source_size_bytes,
        page_count=first_coordinate.page_count,
        renderer=first_coordinate.renderer,
        renderer_version=first_coordinate.renderer_version,
        renderer_config_sha256=first_coordinate.renderer_config_sha256,
        pages=tuple(rendered_pages),
    )
    validate_render_manifest_files(manifest, artifact_root=root)
    return manifest


def validate_render_manifest_files(
    manifest: PdfRenderManifest | Mapping[str, Any],
    *,
    artifact_root: str | Path,
) -> PdfRenderManifest:
    """Validate bindings and the exact local bytes it references.

    The return value is the normalized object, allowing consumers that read
    JSON to validate and then pass the same immutable object forward.  This is
    safe only for trusted, single-owner artifact roots: it does not eliminate
    pathname TOCTOU if a later consumer reopens paths.  Production must upload
    these same verified FD/bytes or an immutable content-addressed object.
    Multi-tenant path reopening is NO-GO until a dirfd/openat2 boundary exists.
    """
    if isinstance(manifest, Mapping):
        manifest = PdfRenderManifest.from_dict(manifest)
    if not isinstance(manifest, PdfRenderManifest):
        raise PdfRenderManifestError("manifest must be a PdfRenderManifest or object")
    root = _artifact_root(artifact_root)
    source_path, _ = _bound_file(root, manifest.source_pdf_relative_path, name="source_pdf_relative_path")
    source_sha256, source_size_bytes = _inspect_pdf(source_path)
    if source_sha256 != manifest.source_pdf_sha256:
        raise PdfRenderManifestError("source PDF SHA-256 does not match manifest")
    if source_size_bytes != manifest.source_pdf_size_bytes:
        raise PdfRenderManifestError("source PDF size does not match manifest")
    for rendered_page in manifest.pages:
        image_path, _ = _bound_file(root, rendered_page.image_relative_path, name=f"page {rendered_page.page} image")
        image_sha256, image_size_bytes, image_width, image_height = _inspect_png(image_path)
        if image_sha256 != rendered_page.image_sha256:
            raise PdfRenderManifestError(f"page {rendered_page.page} image SHA-256 does not match manifest")
        if image_size_bytes != rendered_page.image_size_bytes:
            raise PdfRenderManifestError(f"page {rendered_page.page} image size does not match manifest")
        coordinate = rendered_page.coordinate_manifest
        if image_width != coordinate.rendered_width_px or image_height != coordinate.rendered_height_px:
            raise PdfRenderManifestError(
                f"page {rendered_page.page} PNG dimensions do not match coordinate_manifest"
            )
    return manifest
