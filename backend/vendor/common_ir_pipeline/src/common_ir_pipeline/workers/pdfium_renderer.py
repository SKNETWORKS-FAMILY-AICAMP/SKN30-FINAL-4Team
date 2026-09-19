"""Render one complete PDF into canonical, source-bound RGB PNG artifacts.

This is the EC2/CPU half of the visual PDF pipeline. It owns rendering, not
OCR: a remote worker may receive immutable pages only after local source,
page-tree metadata, bytes and coordinate manifests have been verified.

PDFium does not expose ``/UserUnit`` and its direct MediaBox/CropBox helpers
do not reliably inherit values from the page tree. The pinned ``pypdf``
metadata reader resolves those attributes from actual page/ancestor
dictionaries. No PDFium fallback geometry is accepted as an authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import argparse
import json
import math
import os
from pathlib import Path
import platform
import stat
import struct
import sys
import sysconfig
from typing import Any, Mapping

from common_ir_pipeline.pdf_fusion.coordinate_manifest import AffineTransform, PdfCoordinateManifest
from common_ir_pipeline.pdf_fusion.render_manifest import (
    MAX_PNG_DECOMPRESSED_BYTES,
    MAX_PNG_DIMENSION_PX,
    MAX_PNG_FILE_BYTES,
    MAX_PNG_TOTAL_PIXELS,
    PdfRenderManifest,
    PdfRenderManifestError,
    RenderedPageInput,
    assemble_render_manifest,
)


PINNED_PYPDFIUM2_VERSION = "5.13.0"
PINNED_PYPDF_VERSION = "6.18.1"
PINNED_PDFIUM_ENGINE_VERSION = "153.0.7999.0"
PINNED_PILLOW_VERSION = "12.3.0"
RENDERER_NAME = "pypdfium2"
RENDER_DPI = 200
RENDER_SCALE_PX_PER_POINT = RENDER_DPI / 72.0
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_PAGES = 256


class PdfiumRenderError(ValueError):
    """Raised when a PDF cannot be rendered under the canonical contract."""


@dataclass(frozen=True, slots=True)
class PdfiumRenderLimits:
    """Hard limits checked before PDFium bitmap allocation/publication."""

    max_source_bytes: int = _MAX_SOURCE_BYTES
    max_pages: int = _MAX_PAGES
    max_png_file_bytes: int = MAX_PNG_FILE_BYTES
    max_png_dimension_px: int = MAX_PNG_DIMENSION_PX
    max_png_total_pixels: int = MAX_PNG_TOTAL_PIXELS
    max_png_decompressed_bytes: int = MAX_PNG_DECOMPRESSED_BYTES
    # Bounds the whole document even though pages render sequentially: output
    # upload/remote-OCR costs otherwise scale without a hard ceiling.
    max_document_total_pixels: int = 250_000_000

    def __post_init__(self) -> None:
        for field in (
            "max_source_bytes", "max_pages", "max_png_file_bytes",
            "max_png_dimension_px", "max_png_total_pixels",
            "max_png_decompressed_bytes", "max_document_total_pixels",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise PdfiumRenderError(f"{field} must be a positive integer")


DEFAULT_LIMITS = PdfiumRenderLimits()


@dataclass(frozen=True, slots=True)
class ResolvedPdfPageGeometry:
    """Effective inherited PDF geometry in raw PDF user-space units."""

    page: int
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    user_unit: float


def _canonical_json_sha256(value: Mapping[str, Any]) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _pillow_runtime_identity() -> dict[str, str | None]:
    """Verify the PNG encoder package and expose its relevant codec identity.

    Pillow's PNG encoder is implemented by Pillow itself and its zlib binding;
    it does not use a separately exposed libpng runtime. ``None`` therefore
    records that no libpng version was reported rather than inventing one.
    """
    try:
        import PIL
        import PIL._imaging as imaging
    except ImportError as error:
        raise PdfiumRenderError(f"canonical renderer requires Pillow=={PINNED_PILLOW_VERSION}") from error
    if getattr(PIL, "__version__", None) != PINNED_PILLOW_VERSION:
        raise PdfiumRenderError(f"canonical renderer requires Pillow=={PINNED_PILLOW_VERSION}")
    zlib_version = getattr(imaging, "zlib_version", None)
    if not isinstance(zlib_version, str) or not zlib_version:
        raise PdfiumRenderError("Pillow PNG encoder does not expose a zlib runtime version")
    zlib_ng_version = getattr(imaging, "zlib_ng_version", None)
    if zlib_ng_version is not None and not isinstance(zlib_ng_version, str):
        raise PdfiumRenderError("Pillow PNG encoder exposed an invalid zlib-ng runtime version")
    return {
        "pillow_version": PINNED_PILLOW_VERSION,
        "zlib_version": zlib_version,
        "zlib_ng_version": zlib_ng_version,
        "libpng_version": None,
    }


def _sha256_regular_file(path: Path, *, label: str) -> str:
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size < 1 or file_stat.st_size > 128 * 1024 * 1024:
            raise PdfiumRenderError(f"invalid {label} runtime binary")
        digest = sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except PdfiumRenderError:
        raise
    except OSError as error:
        raise PdfiumRenderError(f"failed to identify {label} runtime binary") from error


def _pdfium_runtime_identity() -> dict[str, str | None]:
    """Bind the actual PDFium shared object and ABI, not only wheel versions."""
    try:
        import pypdfium2_raw
        raw_root = Path(pypdfium2_raw.__file__).resolve(strict=True).parent
    except (ImportError, OSError) as error:
        raise PdfiumRenderError("canonical renderer requires pypdfium2_raw runtime files") from error
    binary = next(
        (raw_root / name for name in ("libpdfium.so", "libpdfium.dylib", "pdfium.dll") if (raw_root / name).exists()),
        None,
    )
    if binary is None:
        raise PdfiumRenderError("canonical renderer could not locate the PDFium shared binary")
    return {
        "shared_binary_name": binary.name,
        "shared_binary_sha256": _sha256_regular_file(binary, label="PDFium"),
        "os": platform.system(),
        "arch": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_cache_tag": sys.implementation.cache_tag,
        "python_soabi": sysconfig.get_config_var("SOABI"),
    }


def renderer_config_sha256() -> str:
    """Hash every explicit setting that affects canonical rendered pixels."""
    return _canonical_json_sha256({
        "renderer": RENDERER_NAME,
        "pypdfium2_version": PINNED_PYPDFIUM2_VERSION,
        "pdfium_engine_version": PINNED_PDFIUM_ENGINE_VERSION,
        "metadata_parser": {"name": "pypdf", "version": PINNED_PYPDF_VERSION},
        "png_runtime": _pillow_runtime_identity(),
        "pdfium_runtime": _pdfium_runtime_identity(),
        "dpi": RENDER_DPI,
        "render_scale_px_per_point": RENDER_SCALE_PX_PER_POINT,
        "additional_rotation_degrees": 0,
        "render_crop": [0, 0, 0, 0],
        "may_draw_forms": False,
        "draw_annots": False,
        "fill_color_rgba": [255, 255, 255, 255],
        "rev_byteorder": True,
        "maybe_alpha": False,
        "required_bitmap_mode": "RGB",
        "png": {
            "encoder": "Pillow.Image.save",
            "format": "PNG",
            "save_kwargs": {},
            "bit_depth": 8,
            "color_type": "RGB",
            "interlace": "none",
        },
        # PDFium may consult a host font fallback for PDFs that omit embedded
        # fonts. We deliberately do not walk/hash a host font tree (it can be
        # unbounded and is not portable). The image SHA in each page manifest
        # remains the terminal byte-level binding for the actual outcome.
        "font_fallback": {
            "policy": "host_font_fallback_unpinned",
            "limitation": "non_embedded_font_glyphs_can_vary_by_host",
            "terminal_binding": "page_image_sha256",
        },
    })


def _load_pypdf() -> Any:
    try:
        import pypdf
    except ImportError as error:
        raise PdfiumRenderError(f"canonical renderer requires pypdf=={PINNED_PYPDF_VERSION}") from error
    if getattr(pypdf, "__version__", None) != PINNED_PYPDF_VERSION:
        raise PdfiumRenderError(f"canonical renderer requires pypdf=={PINNED_PYPDF_VERSION}")
    return pypdf


def _deref(value: Any) -> Any:
    get_object = getattr(value, "get_object", None)
    return get_object() if callable(get_object) else value


def _page_tree_value(page: Any, key: str) -> Any:
    """Resolve an inheritable page-tree attribute.

    PDF only makes a small set of page attributes inheritable.  Callers must
    not use this helper for leaf-only entries such as ``/UserUnit``.
    """
    current = page
    visited: set[tuple[int, int] | int] = set()
    while current is not None:
        reference = getattr(current, "indirect_reference", None)
        identity = (
            (int(getattr(reference, "idnum")), int(getattr(reference, "generation", 0)))
            if reference is not None and hasattr(reference, "idnum") else id(current)
        )
        if identity in visited:
            raise PdfiumRenderError(f"PDF page-tree cycle while resolving {key}")
        visited.add(identity)
        if key in current:
            return _deref(current[key])
        parent = current.get("/Parent") if hasattr(current, "get") else None
        current = _deref(parent) if parent is not None else None
    return None


def _finite_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PdfiumRenderError(f"PDF {label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise PdfiumRenderError(f"PDF {label} must be a finite number")
    return 0.0 if number == 0.0 else number


def _box(value: Any, *, label: str) -> tuple[float, float, float, float]:
    value = _deref(value)
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise PdfiumRenderError(f"PDF {label} must resolve to exactly four coordinates")
    box = tuple(_finite_number(item, label=label) for item in value)
    if not box[0] < box[2] or not box[1] < box[3]:
        raise PdfiumRenderError(f"PDF {label} bounds must be strictly increasing")
    return box  # type: ignore[return-value]


def _intersection(media: tuple[float, float, float, float], crop: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    visible = (max(media[0], crop[0]), max(media[1], crop[1]), min(media[2], crop[2]), min(media[3], crop[3]))
    if not visible[0] < visible[2] or not visible[1] < visible[3]:
        raise PdfiumRenderError("PDF MediaBox and CropBox must have a non-empty intersection")
    return visible


def _rotation(value: Any) -> int:
    if value is None:
        return 0
    number = _finite_number(_deref(value), label="/Rotate")
    if int(number) != number or int(number) % 90:
        raise PdfiumRenderError("PDF /Rotate must be an integer multiple of 90")
    return int(number) % 360


def _user_unit(value: Any) -> float:
    # PDF 1.6 defines absent /UserUnit as exactly 1. The value is taken only
    # from the leaf /Page dictionary because /UserUnit is not inheritable.
    if value is None:
        return 1.0
    number = _finite_number(_deref(value), label="/UserUnit")
    if number <= 0:
        raise PdfiumRenderError("PDF /UserUnit must be positive")
    return number


def _page_tree_maximum_depth(max_pages: int) -> int:
    """Return a modest, deterministic parser depth bound for this document."""
    # A balanced tree for ``max_pages`` leaves needs log2(N) levels.  The
    # allowance tolerates ordinary producer quirks while remaining much lower
    # than pypdf's permissive process-global default (100).
    return min(64, max(8, math.ceil(math.log2(max_pages)) + 8))


def _page_tree_maximum_entries(max_pages: int) -> int:
    """Bound internal /Pages nodes while preserving a true leaf-page cap."""
    # pypdf counts both /Page leaves and internal /Pages nodes.  The explicit
    # loop below owns the actual max-page policy; this bounded headroom keeps a
    # valid 256-page tree from failing merely because it has intermediary nodes.
    return max_pages * 4 + 64


def resolve_pdf_page_geometries(
    source: bytes,
    *,
    max_pages: int = _MAX_PAGES,
) -> tuple[ResolvedPdfPageGeometry, ...]:
    """Resolve inherited page geometry and each leaf page's direct UserUnit."""
    if not isinstance(source, bytes) or not source.startswith(b"%PDF-"):
        raise PdfiumRenderError("source must be PDF bytes with a %PDF- header")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise PdfiumRenderError("max_pages must be a positive integer")
    pypdf = _load_pypdf()
    try:
        # ``reader.pages`` materializes the entire page tree.  Bound it before
        # accessing that property so a hostile /Pages tree cannot consume the
        # old 100k-entry / 100-depth pypdf defaults in this process.
        with pypdf.apply_configuration(
            page_tree_maximum_entries=_page_tree_maximum_entries(max_pages),
            page_tree_maximum_depth=_page_tree_maximum_depth(max_pages),
        ):
            reader = pypdf.PdfReader(BytesIO(source), strict=True)
            if reader.is_encrypted:
                raise PdfiumRenderError("encrypted PDFs are not supported by the canonical renderer")
            pages: list[Any] = []
            for index, page in enumerate(reader.pages, start=1):
                # Keep an explicit guard even though pypdf is configured too:
                # it gives a stable, renderer-owned error should that library
                # ever relax its limit semantics.
                if index > max_pages:
                    raise PdfiumRenderError("PDF page count exceeds the renderer safety cap")
                pages.append(page)
    except PdfiumRenderError:
        raise
    except Exception as error:
        if "page tree" in str(error).lower() and "limit" in str(error).lower():
            raise PdfiumRenderError("PDF page count exceeds the renderer safety cap") from error
        raise PdfiumRenderError("pypdf failed to parse PDF page metadata") from error
    if not pages:
        raise PdfiumRenderError("PDF contains no pages")
    result: list[ResolvedPdfPageGeometry] = []
    for index, page in enumerate(pages, start=1):
        media = _box(_page_tree_value(page, "/MediaBox"), label="/MediaBox")
        raw_crop_value = _page_tree_value(page, "/CropBox")
        raw_crop = _box(raw_crop_value, label="/CropBox") if raw_crop_value is not None else media
        result.append(ResolvedPdfPageGeometry(
            page=index, media_box=media, crop_box=_intersection(media, raw_crop),
            rotation=_rotation(_page_tree_value(page, "/Rotate")),
            user_unit=_user_unit(_deref(page.get("/UserUnit"))),
        ))
    return tuple(result)


def _sha256_bound_pdf(path: str | Path, *, limits: PdfiumRenderLimits) -> tuple[Path, bytes, str]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise PdfiumRenderError("source PDF must not be a symlink")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PdfiumRenderError("source PDF must be a regular file")
            if before.st_size < 1 or before.st_size > limits.max_source_bytes:
                raise PdfiumRenderError("source PDF exceeds the source size safety cap")
            source = handle.read(limits.max_source_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise PdfiumRenderError("failed to read source PDF") from error
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_identity != after_identity or len(source) != before.st_size:
        raise PdfiumRenderError("source PDF changed while it was being read")
    if not source.startswith(b"%PDF-"):
        raise PdfiumRenderError("source PDF must have a %PDF- header")
    return candidate.resolve(strict=True), source, sha256(source).hexdigest()


def _artifact_root(path: str | Path) -> Path:
    root = Path(path)
    if root.is_symlink():
        raise PdfiumRenderError("artifact_root must not be a symlink")
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise PdfiumRenderError("artifact_root must already exist") from error
    if not root.is_dir():
        raise PdfiumRenderError("artifact_root must be a directory")
    return root


def _require_below(root: Path, path: Path, *, name: str) -> Path:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise PdfiumRenderError(f"{name} must be inside artifact_root") from error
    return path


def _float32(value: float) -> float:
    """Match the coordinate precision exposed by PDFium's public Python API."""
    try:
        result = struct.unpack("!f", struct.pack("!f", value))[0]
    except (OverflowError, struct.error) as error:
        raise PdfiumRenderError("PDF geometry is outside PDFium float32 range") from error
    if not math.isfinite(result):
        raise PdfiumRenderError("PDF geometry is outside PDFium float32 range")
    return result


def _pdfium_bbox_for_geometry(geometry: ResolvedPdfPageGeometry) -> tuple[float, float, float, float]:
    """The effective CropBox as PDFium exposes it (single precision)."""
    return tuple(_float32(value) for value in geometry.crop_box)  # type: ignore[return-value]


def _expected_dimensions(
    geometry: ResolvedPdfPageGeometry,
    *,
    engine_bbox: tuple[float, float, float, float],
) -> tuple[int, int]:
    # PDFium receives/stores page geometry as float32.  In particular, derive
    # subtraction in float32 too: raw pypdf arithmetic around a ceil boundary
    # can otherwise expect a bitmap one pixel larger than PDFium renders.
    width = _float32(_float32(engine_bbox[2]) - _float32(engine_bbox[0]))
    height = _float32(_float32(engine_bbox[3]) - _float32(engine_bbox[1]))
    if width <= 0 or height <= 0:
        raise PdfiumRenderError(f"PDFium returned invalid visible bbox for page {geometry.page}")
    scale = RENDER_SCALE_PX_PER_POINT * geometry.user_unit
    unrotated_width = math.ceil(width * scale)
    unrotated_height = math.ceil(height * scale)
    return (unrotated_height, unrotated_width) if geometry.rotation in {90, 270} else (unrotated_width, unrotated_height)


def _assert_pixel_cap(width: int, height: int, *, limits: PdfiumRenderLimits) -> None:
    if width < 1 or height < 1 or width > limits.max_png_dimension_px or height > limits.max_png_dimension_px:
        raise PdfiumRenderError("rendered PNG dimensions exceed the renderer safety cap")
    if width * height > limits.max_png_total_pixels:
        raise PdfiumRenderError("rendered PNG pixel count exceeds the renderer safety cap")
    if height * (width * 3 + 1) > limits.max_png_decompressed_bytes:
        raise PdfiumRenderError("rendered PNG scanlines exceed the decompressed-byte safety cap")


def _affine_for_page(geometry: ResolvedPdfPageGeometry, *, width: int, height: int) -> AffineTransform:
    x0, y0, x1, y1 = geometry.crop_box
    crop_width, crop_height = x1 - x0, y1 - y0
    # Actual ceil-rounded bitmap dimensions form the effective scale; nominal
    # DPI alone would miss image corners by almost a pixel.
    if geometry.rotation == 0:
        values = (width / crop_width, 0, 0, -height / crop_height, -width * x0 / crop_width, height * y1 / crop_height)
    elif geometry.rotation == 90:
        values = (0, height / crop_width, width / crop_height, 0, -width * y0 / crop_height, -height * x0 / crop_width)
    elif geometry.rotation == 180:
        values = (-width / crop_width, 0, 0, height / crop_height, width * x1 / crop_width, -height * y0 / crop_height)
    elif geometry.rotation == 270:
        values = (0, -height / crop_width, -width / crop_height, 0, width * y1 / crop_height, height * x1 / crop_width)
    else:
        raise PdfiumRenderError("unsupported PDF page rotation")
    return AffineTransform(*values)


def _assert_pdfium_page_matches_geometry(
    page: Any,
    geometry: ResolvedPdfPageGeometry,
    *,
    page_count: int,
) -> tuple[float, float, float, float]:
    try:
        engine_bbox = tuple(float(value) for value in page.get_bbox())
        engine_rotation = int(page.get_rotation())
    except Exception as error:
        raise PdfiumRenderError(f"PDFium failed to expose geometry for page {geometry.page}") from error
    if len(engine_bbox) != 4 or any(not math.isfinite(value) for value in engine_bbox):
        raise PdfiumRenderError(f"PDFium returned invalid bbox for page {geometry.page}")
    expected_bbox = _pdfium_bbox_for_geometry(geometry)
    for expected, actual in zip(expected_bbox, engine_bbox):
        # ``get_bbox()`` is a float32 API.  The pypdf source remains raw in
        # manifests/affines; this comparison only verifies the PDFium bridge.
        tolerance = max(math.ulp(expected), math.ulp(actual))
        if not math.isclose(expected, actual, rel_tol=0.0, abs_tol=tolerance):
            raise PdfiumRenderError(f"PDFium bbox disagrees with resolved MediaBox/CropBox for page {geometry.page}")
    if engine_rotation != geometry.rotation:
        raise PdfiumRenderError(f"PDFium rotation disagrees with resolved /Rotate for page {geometry.page}")
    if geometry.page < 1 or geometry.page > page_count:
        raise PdfiumRenderError("resolved PDF page number is outside document bounds")
    return engine_bbox  # type: ignore[return-value]


def _png_rgb_bytes(bitmap: Any) -> bytes:
    if getattr(bitmap, "mode", None) != "RGB" or getattr(bitmap, "n_channels", None) != 3:
        raise PdfiumRenderError("PDFium did not produce the required packed RGB bitmap")
    try:
        # Keep the recovered compatibility renderer's visual conversion: the
        # exact pinned PDFium bitmap becomes a Pillow RGB image, then Pillow
        # writes its default non-interlaced PNG. The manifest validator checks
        # the resulting bytes/PNG structure before publication.
        image = bitmap.to_pil().convert("RGB")
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception as error:
        raise PdfiumRenderError("failed to encode PDFium RGB bitmap as PNG") from error


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise PdfiumRenderError(f"refusing to overwrite existing artifact: {path.name}") from error
    except OSError as error:
        raise PdfiumRenderError(f"failed to create artifact: {path.name}") from error


def _assert_posconv(bitmap: Any, page: Any, geometry: ResolvedPdfPageGeometry, affine: AffineTransform) -> None:
    """Cross-check the derived affine against PDFium's own coordinate helper."""
    try:
        converter = bitmap.get_posconv(page)
        x0, y0, x1, y1 = geometry.crop_box
        for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            actual_x, actual_y = converter.to_bitmap(x, y)
            expected_x, expected_y = affine.apply_point(x, y)
            if abs(actual_x - expected_x) > 1.0 or abs(actual_y - expected_y) > 1.0:
                raise PdfiumRenderError(f"PDFium coordinate conversion disagrees with canonical affine for page {geometry.page}")
    except PdfiumRenderError:
        raise
    except Exception as error:
        raise PdfiumRenderError(f"PDFium coordinate conversion failed for page {geometry.page}") from error


def _load_pdfium() -> Any:
    try:
        import pypdfium2
    except ImportError as error:
        raise PdfiumRenderError(f"canonical renderer requires pypdfium2=={PINNED_PYPDFIUM2_VERSION}") from error
    version = getattr(getattr(pypdfium2, "version", None), "PYPDFIUM_INFO", None)
    if getattr(version, "version", None) != PINNED_PYPDFIUM2_VERSION:
        raise PdfiumRenderError(f"canonical renderer requires pypdfium2=={PINNED_PYPDFIUM2_VERSION}")
    engine = getattr(getattr(pypdfium2, "version", None), "PDFIUM_INFO", None)
    if getattr(engine, "version", None) != PINNED_PDFIUM_ENGINE_VERSION:
        raise PdfiumRenderError(f"canonical renderer requires PDFium engine {PINNED_PDFIUM_ENGINE_VERSION}")
    return pypdfium2


def _document_page(document: Any, *, index: int, geometry: ResolvedPdfPageGeometry) -> Any:
    try:
        return document[index]
    except Exception as error:
        raise PdfiumRenderError(f"PDFium failed to open page {geometry.page}") from error


def _preflight_pdfium_document(
    document: Any,
    geometry: tuple[ResolvedPdfPageGeometry, ...],
    *,
    limits: PdfiumRenderLimits,
) -> tuple[tuple[tuple[float, float, float, float], int, int], ...]:
    """Validate every PDFium page before creating visible output artifacts."""
    try:
        page_count = len(document)
    except Exception as error:
        raise PdfiumRenderError("PDFium failed to count source PDF pages") from error
    if page_count != len(geometry):
        raise PdfiumRenderError("PDFium page count disagrees with pypdf page tree")

    total_pixels = 0
    preflight: list[tuple[tuple[float, float, float, float], int, int]] = []
    for index, page_geometry in enumerate(geometry):
        page = _document_page(document, index=index, geometry=page_geometry)
        try:
            engine_bbox = _assert_pdfium_page_matches_geometry(
                page,
                page_geometry,
                page_count=page_count,
            )
            width, height = _expected_dimensions(page_geometry, engine_bbox=engine_bbox)
            _assert_pixel_cap(width, height, limits=limits)
            total_pixels += width * height
            if total_pixels > limits.max_document_total_pixels:
                raise PdfiumRenderError("PDF document pixel count exceeds the renderer safety cap")
            preflight.append((engine_bbox, width, height))
        finally:
            try:
                page.close()
            except Exception:
                pass
    return tuple(preflight)


def render_canonical_pdf(*, artifact_root: str | Path, source_pdf_path: str | Path, output_directory: str = "rendered", manifest_filename: str = "render_manifest.json", limits: PdfiumRenderLimits = DEFAULT_LIMITS) -> PdfRenderManifest:
    """Render all source pages and publish a no-overwrite render manifest.

    The source must already be a regular file inside ``artifact_root``. On
    failure, only incomplete files without the terminal manifest may remain;
    callers must discard that workspace rather than retry into it.
    """
    if not isinstance(limits, PdfiumRenderLimits):
        raise PdfiumRenderError("limits must be PdfiumRenderLimits")
    root = _artifact_root(artifact_root)
    source_path = Path(source_pdf_path)
    if not source_path.is_absolute():
        source_path = root / source_path
    source_path, source_bytes, source_sha256 = _sha256_bound_pdf(source_path, limits=limits)
    _require_below(root, source_path, name="source_pdf_path")
    if not output_directory or Path(output_directory).is_absolute() or "/" in output_directory or "\\" in output_directory or output_directory in {".", ".."}:
        raise PdfiumRenderError("output_directory must be one safe directory name")
    if not manifest_filename or Path(manifest_filename).is_absolute() or "/" in manifest_filename or "\\" in manifest_filename or Path(manifest_filename).suffix.lower() != ".json":
        raise PdfiumRenderError("manifest_filename must be one safe .json filename")
    geometry = resolve_pdf_page_geometries(source_bytes, max_pages=limits.max_pages)
    render_directory, manifest_path = root / output_directory, root / manifest_filename
    if render_directory.exists() or manifest_path.exists():
        raise PdfiumRenderError("render output already exists; refusing to overwrite artifacts")

    pdfium = _load_pdfium()
    try:
        document = pdfium.PdfDocument(source_bytes)
    except Exception as error:
        raise PdfiumRenderError("PDFium failed to open source PDF bytes") from error
    pages: list[RenderedPageInput] = []
    try:
        # Do not create a published render directory until PDFium itself has
        # accepted every page geometry and all bitmap/document caps.  pypdf
        # supplies the raw manifest geometry; PDFium's float32 bbox determines
        # its actual ceil-rounded bitmap dimensions.
        preflight = _preflight_pdfium_document(document, geometry, limits=limits)
        config_sha256 = renderer_config_sha256()
        try:
            os.mkdir(render_directory, 0o700)
        except OSError as error:
            raise PdfiumRenderError("failed to create exclusive render output directory") from error
        for index, page_geometry in enumerate(geometry):
            _engine_bbox, expected_width, expected_height = preflight[index]
            page = _document_page(document, index=index, geometry=page_geometry)
            bitmap = None
            try:
                bitmap = page.render(scale=RENDER_SCALE_PX_PER_POINT * page_geometry.user_unit, rotation=0, crop=(0, 0, 0, 0), may_draw_forms=False, draw_annots=False, fill_color=(255, 255, 255, 255), rev_byteorder=True, maybe_alpha=False)
                if (bitmap.width, bitmap.height) != (expected_width, expected_height):
                    raise PdfiumRenderError(f"PDFium bitmap dimensions disagree with canonical geometry for page {page_geometry.page}")
                _assert_pixel_cap(bitmap.width, bitmap.height, limits=limits)
                affine = _affine_for_page(page_geometry, width=bitmap.width, height=bitmap.height)
                _assert_posconv(bitmap, page, page_geometry, affine)
                png = _png_rgb_bytes(bitmap)
                if len(png) > limits.max_png_file_bytes:
                    raise PdfiumRenderError(f"rendered PNG exceeds the compressed-file safety cap for page {page_geometry.page}")
                image_path = render_directory / f"page-{page_geometry.page:04d}.png"
                _write_exclusive(image_path, png)
                canonical_width = (page_geometry.crop_box[2] - page_geometry.crop_box[0]) * page_geometry.user_unit
                canonical_height = (page_geometry.crop_box[3] - page_geometry.crop_box[1]) * page_geometry.user_unit
                if page_geometry.rotation in {90, 270}:
                    canonical_width, canonical_height = canonical_height, canonical_width
                coordinate = PdfCoordinateManifest(
                    source_sha256=source_sha256, page=page_geometry.page, page_count=len(geometry), media_box=page_geometry.media_box, crop_box=page_geometry.crop_box, rotation=page_geometry.rotation, user_unit=page_geometry.user_unit, canonical_width_pt=canonical_width, canonical_height_pt=canonical_height, render_scale_px_per_point=RENDER_SCALE_PX_PER_POINT, rendered_width_px=bitmap.width, rendered_height_px=bitmap.height, pdf_origin="bottom_left", pdf_x_axis="right", pdf_y_axis="up", pixel_origin="top_left", pixel_x_axis="right", pixel_y_axis="down", user_to_pixel=affine, pixel_to_user=affine.inverse(), renderer=RENDERER_NAME, renderer_version=PINNED_PYPDFIUM2_VERSION, renderer_config_sha256=config_sha256, page_image_sha256=sha256(png).hexdigest(),
                )
                pages.append(RenderedPageInput(image_path=image_path, coordinate_manifest=coordinate))
            except PdfiumRenderError:
                raise
            except Exception as error:
                raise PdfiumRenderError(f"PDFium failed to render page {page_geometry.page}") from error
            finally:
                if bitmap is not None:
                    try:
                        bitmap.close()
                    except Exception:
                        pass
                try:
                    page.close()
                except Exception:
                    pass
    finally:
        try:
            document.close()
        except Exception:
            pass

    _, _, current_source_sha256 = _sha256_bound_pdf(source_path, limits=limits)
    if current_source_sha256 != source_sha256:
        raise PdfiumRenderError("source PDF changed during canonical rendering")
    try:
        manifest = assemble_render_manifest(artifact_root=root, source_pdf_path=source_path, pages=pages)
    except PdfRenderManifestError as error:
        raise PdfiumRenderError("rendered artifacts failed manifest validation") from error
    _write_exclusive(manifest_path, manifest.canonical_json())
    return manifest


def main() -> None:
    """Small argv-only entry point for a single private artifact workspace."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True, help="regular source PDF below --artifact-root")
    parser.add_argument("--output-directory", default="rendered")
    parser.add_argument("--manifest-filename", default="render_manifest.json")
    args = parser.parse_args()
    try:
        manifest = render_canonical_pdf(
            artifact_root=args.artifact_root,
            source_pdf_path=args.pdf,
            output_directory=args.output_directory,
            manifest_filename=args.manifest_filename,
        )
    except PdfiumRenderError as error:
        parser.error(str(error))
    print(json.dumps({
        "manifest": args.manifest_filename,
        "manifest_sha256": manifest.manifest_sha256(),
        "page_count": manifest.page_count,
        "source_pdf_sha256": manifest.source_pdf_sha256,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
