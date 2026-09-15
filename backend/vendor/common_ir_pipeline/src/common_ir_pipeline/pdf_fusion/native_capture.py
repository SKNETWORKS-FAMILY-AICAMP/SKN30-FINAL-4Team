"""Deterministic, fail-closed capture contract for native PDF evidence.

This module is the narrow boundary between the pinned ``pdf-inspector``
runtime and the PDF Common IR adapter.  It deliberately captures the whole
document from one immutable byte string: callers cannot select pages, and no
absolute host path is written into the artifact.

Only fields documented by ``pdf-inspector==1.17.0`` are projected.  In
particular, this module never walks ``dir()`` or serialises arbitrary object
attributes.  That keeps runtime internals, credentials and nondeterministic
timing fields out of the archived artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping, Sequence


CAPTURE_SCHEMA_VERSION = "pdf_inspector_native_capture/v1"
PINNED_PDF_INSPECTOR_VERSION = "1.17.0"

_CAPTURE_KEYS = frozenset({
    "capture_schema_version",
    "notice_id",
    "source_kind",
    "artifact_role",
    "method",
    "version",
    "extraction_scope",
    "source_path",
    "source_sha256",
    "source_size_bytes",
    "process_result",
    "pages_markdown_result",
    "text_items",
    "structure_elements",
})
_PROCESS_KEYS = frozenset({
    "pdf_type",
    "markdown",
    "page_count",
    "pages_needing_ocr",
    "ocr_reasons_by_page",
    "title",
    "confidence",
    "is_complex_layout",
    "pages_with_tables",
    "pages_with_columns",
    "has_encoding_issues",
})
_PAGES_RESULT_KEYS = frozenset({
    "pages",
    "pages_with_tables",
    "pages_with_columns",
    "pages_needing_ocr",
    "ocr_reasons_by_page",
    "is_complex",
})
_PAGE_MARKDOWN_KEYS = frozenset({"page", "markdown", "needs_ocr", "ocr_reason"})
_OCR_REASON_KEYS = frozenset({"page", "reasons"})
_TEXT_ITEM_KEYS = frozenset({
    "text",
    "x",
    "y",
    "width",
    "height",
    "font",
    "font_tag",
    "font_size",
    "page",
    "is_bold",
    "is_italic",
    "is_underline",
    "is_strikeout",
    "item_type",
    "mcid",
})
_STRUCTURE_ELEMENT_KEYS = frozenset({"page", "mcid", "role"})
_PDF_TYPES = frozenset({"text_based", "scanned", "image_based", "mixed"})
_SHA256_LENGTH = 64
_JSON_MAX_DEPTH = 20


class NativeCaptureError(ValueError):
    """Raised when native PDF evidence is incomplete, unsafe or unbound."""


@dataclass(frozen=True, slots=True)
class NativeCaptureLimits:
    """Resource limits applied before and after native extraction.

    The defaults are intentionally generous for public-notice PDFs while
    bounding memory and artifact amplification.  A caller may tighten them,
    but must not disable them.
    """

    max_source_bytes: int = 64 * 1024 * 1024
    max_pages: int = 256
    max_text_items: int = 500_000
    max_structure_elements: int = 500_000
    max_markdown_characters: int = 16 * 1024 * 1024
    max_text_characters: int = 16 * 1024 * 1024
    max_capture_bytes: int = 64 * 1024 * 1024
    # PDF user-space points are normally in the hundreds.  One million still
    # permits unusually large engineering/poster pages while rejecting
    # adversarial magnitudes before downstream bbox arithmetic.
    max_absolute_coordinate: int = 1_000_000

    def __post_init__(self) -> None:
        for name in (
            "max_source_bytes",
            "max_pages",
            "max_text_items",
            "max_structure_elements",
            "max_markdown_characters",
            "max_text_characters",
            "max_capture_bytes",
            "max_absolute_coordinate",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise NativeCaptureError(f"{name} must be a positive integer")


DEFAULT_LIMITS = NativeCaptureLimits()


def _assert_json_value(value: Any, *, path: str = "$", depth: int = 0) -> None:
    if depth > _JSON_MAX_DEPTH:
        raise NativeCaptureError(f"{path} exceeds the JSON nesting limit")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NativeCaptureError(f"{path} must not contain NaN or Infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NativeCaptureError(f"{path} contains a non-string object key")
            _assert_json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise NativeCaptureError(f"{path} contains unsupported value type {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes, rejecting non-JSON and non-finite data."""

    _assert_json_value(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise NativeCaptureError("capture is not canonically serializable JSON") from error


def load_native_capture_file(
    path: str | Path,
    *,
    limits: NativeCaptureLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Boundedly load one regular native-capture JSON file without following links."""

    if not isinstance(limits, NativeCaptureLimits):
        raise NativeCaptureError("limits must be NativeCaptureLimits")
    capture_path = Path(path)
    descriptor: int | None = None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise NativeCaptureError(f"native capture has duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise NativeCaptureError(f"native capture contains non-finite JSON number {value!r}")

    try:
        descriptor = os.open(
            capture_path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise NativeCaptureError("native capture must be a regular file")
            if before.st_size > limits.max_capture_bytes:
                raise NativeCaptureError("native capture exceeds the artifact safety cap")
            encoded = stream.read(limits.max_capture_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(encoded) > limits.max_capture_bytes:
            raise NativeCaptureError("native capture exceeds the artifact safety cap")
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_identity != after_identity or len(encoded) != before.st_size:
            raise NativeCaptureError("native capture changed while it was being read")
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except NativeCaptureError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeCaptureError("failed to read native capture JSON") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise NativeCaptureError("native capture JSON root must be an object")
    return payload


def _exact_keys(value: object, expected: frozenset[str], *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NativeCaptureError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise NativeCaptureError(f"{name} keys must all be strings")
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected keys: {', '.join(str(item) for item in extra)}")
        raise NativeCaptureError(f"{name} keys are invalid ({'; '.join(details)})")
    return value


def _field(value: object, name: str, *, owner: str) -> Any:
    """Read one explicitly approved field from a PyO3 object or test mapping."""

    if isinstance(value, Mapping):
        if name not in value:
            raise NativeCaptureError(f"{owner}.{name} is missing")
        return value[name]
    try:
        return getattr(value, name)
    except (AttributeError, RuntimeError) as error:
        raise NativeCaptureError(f"{owner}.{name} is unavailable") from error


def _project_fields(value: object, fields: Sequence[str], *, owner: str) -> dict[str, Any]:
    return {field: _field(value, field, owner=owner) for field in fields}


def _serialise_ocr_reasons(
    values: object,
    *,
    owner: str,
    max_items: int,
) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        raise NativeCaptureError(f"{owner} must be a list")
    if len(values) > max_items:
        raise NativeCaptureError(f"{owner} exceeds the page safety cap")
    return [
        _project_fields(item, ("page", "reasons"), owner=f"{owner}[{index}]")
        for index, item in enumerate(values)
    ]


def _serialise_process_result(value: object, *, max_pages: int) -> dict[str, Any]:
    projected = _project_fields(
        value,
        (
            "pdf_type",
            "markdown",
            "page_count",
            "pages_needing_ocr",
            "ocr_reasons_by_page",
            "title",
            "confidence",
            "is_complex_layout",
            "pages_with_tables",
            "pages_with_columns",
            "has_encoding_issues",
        ),
        owner="process_result",
    )
    projected["ocr_reasons_by_page"] = _serialise_ocr_reasons(
        projected["ocr_reasons_by_page"],
        owner="process_result.ocr_reasons_by_page",
        max_items=max_pages,
    )
    # processing_time_ms is intentionally excluded: it has no evidentiary
    # value and would make byte-for-byte replay nondeterministic.
    return projected


def _serialise_pages_result(
    value: object,
    *,
    limits: NativeCaptureLimits,
    expected_page_count: int,
) -> dict[str, Any]:
    projected = _project_fields(
        value,
        (
            "pages",
            "pages_with_tables",
            "pages_with_columns",
            "pages_needing_ocr",
            "ocr_reasons_by_page",
            "is_complex",
        ),
        owner="pages_markdown_result",
    )
    pages = projected["pages"]
    if not isinstance(pages, (list, tuple)):
        raise NativeCaptureError("pages_markdown_result.pages must be a list")
    if len(pages) != expected_page_count or len(pages) > limits.max_pages:
        raise NativeCaptureError("pages_markdown_result exceeds or disagrees with the page limit")
    projected["pages"] = [
        _project_fields(
            page,
            ("page", "markdown", "needs_ocr", "ocr_reason"),
            owner=f"pages_markdown_result.pages[{index}]",
        )
        for index, page in enumerate(pages)
    ]
    projected["ocr_reasons_by_page"] = _serialise_ocr_reasons(
        projected["ocr_reasons_by_page"],
        owner="pages_markdown_result.ocr_reasons_by_page",
        max_items=limits.max_pages,
    )
    return projected


def _serialise_text_items(values: object, *, limits: NativeCaptureLimits) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        raise NativeCaptureError("text_items must be a list")
    if len(values) > limits.max_text_items:
        raise NativeCaptureError("text_items exceed the safety cap")
    fields = (
        "text", "x", "y", "width", "height", "font", "font_tag", "font_size", "page",
        "is_bold", "is_italic", "is_underline", "is_strikeout", "item_type", "mcid",
    )
    return [
        _project_fields(item, fields, owner=f"text_items[{index}]")
        for index, item in enumerate(values)
    ]


def _serialise_structure_elements(
    values: object,
    *,
    limits: NativeCaptureLimits,
) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        raise NativeCaptureError("structure_elements must be a list")
    if len(values) > limits.max_structure_elements:
        raise NativeCaptureError("structure_elements exceed the safety cap")
    return [
        _project_fields(item, ("page", "mcid", "role"), owner=f"structure_elements[{index}]")
        for index, item in enumerate(values)
    ]


def _safe_relative_path(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or "\\" in value:
        raise NativeCaptureError(f"{name} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    windows_drive_like = bool(path.parts and len(path.parts[0]) == 2 and path.parts[0][0].isalpha() and path.parts[0][1] == ":")
    if path.is_absolute() or windows_drive_like or any(part in {"", ".", ".."} for part in path.parts):
        raise NativeCaptureError(f"{name} must be a safe POSIX relative path")
    return path.as_posix()


def _nonempty_string(name: str, value: object, *, max_characters: int = 16_384) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_characters:
        raise NativeCaptureError(f"{name} must be a bounded non-empty string")
    return value


def _optional_string(name: str, value: object, *, max_characters: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_characters:
        raise NativeCaptureError(f"{name} must be null or a bounded string")
    return value


def _boolean(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise NativeCaptureError(f"{name} must be a boolean")
    return value


def _integer(name: str, value: object, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NativeCaptureError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise NativeCaptureError(f"{name} exceeds its safety cap")
    return value


def _finite(name: str, value: object, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeCaptureError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise NativeCaptureError(f"{name} must be a finite number")
    if minimum is not None and result < minimum:
        raise NativeCaptureError(f"{name} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise NativeCaptureError(f"{name} must be <= {maximum}")
    return 0.0 if result == 0.0 else result


def _page_list(name: str, value: object, *, page_count: int) -> list[int]:
    if not isinstance(value, list):
        raise NativeCaptureError(f"{name} must be a list")
    pages = [_integer(f"{name}[{index}]", page, minimum=1, maximum=page_count) for index, page in enumerate(value)]
    if pages != sorted(set(pages)):
        raise NativeCaptureError(f"{name} must contain sorted unique pages")
    return pages


def _validate_ocr_reasons(name: str, value: object, *, page_count: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise NativeCaptureError(f"{name} must be a list")
    pages: list[int] = []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item = _exact_keys(raw, _OCR_REASON_KEYS, name=f"{name}[{index}]")
        page = _integer(f"{name}[{index}].page", item["page"], minimum=1, maximum=page_count)
        reasons = item["reasons"]
        if not isinstance(reasons, list) or not reasons:
            raise NativeCaptureError(f"{name}[{index}].reasons must be a non-empty list")
        checked_reasons = [
            _nonempty_string(f"{name}[{index}].reasons[{reason_index}]", reason, max_characters=256)
            for reason_index, reason in enumerate(reasons)
        ]
        if checked_reasons != sorted(set(checked_reasons)):
            raise NativeCaptureError(f"{name}[{index}].reasons must be sorted and unique")
        pages.append(page)
        result.append({"page": page, "reasons": checked_reasons})
    if pages != sorted(set(pages)):
        raise NativeCaptureError(f"{name} must be sorted by unique page")
    return result


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _read_source_pdf(source_pdf: Path, limits: NativeCaptureLimits) -> bytes:
    if source_pdf.is_symlink():
        raise NativeCaptureError("source_pdf must not be a symlink")
    try:
        descriptor = os.open(source_pdf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise NativeCaptureError("source_pdf must be a regular file")
            if before.st_size < 5 or before.st_size > limits.max_source_bytes:
                raise NativeCaptureError("source_pdf size is outside the safety bounds")
            data = handle.read(limits.max_source_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise NativeCaptureError("failed to read source_pdf") from error
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after or len(data) != before.st_size:
        raise NativeCaptureError("source_pdf changed while it was being read")
    if len(data) > limits.max_source_bytes:
        raise NativeCaptureError("source_pdf exceeds the safety cap")
    if not data.startswith(b"%PDF-"):
        raise NativeCaptureError("source_pdf must begin with a PDF header")
    return data


def _resolve_inspector(inspector_module: object | None, inspector_version: str | None) -> tuple[object, str]:
    if inspector_module is None:
        try:
            inspector_module = import_module("pdf_inspector")
        except ImportError as error:
            raise NativeCaptureError("install the optional pinned PDF dependency before capture") from error
    if inspector_version is None:
        try:
            inspector_version = package_version("pdf-inspector")
        except PackageNotFoundError as error:
            raise NativeCaptureError("cannot verify the installed pdf-inspector version") from error
    if inspector_version != PINNED_PDF_INSPECTOR_VERSION:
        raise NativeCaptureError(
            f"pdf-inspector version must be exactly {PINNED_PDF_INSPECTOR_VERSION}; got {inspector_version!r}"
        )
    return inspector_module, inspector_version


def capture_pdf_to_native(
    source_pdf: str | Path,
    *,
    notice_id: str,
    source_relative_path: str,
    limits: NativeCaptureLimits = DEFAULT_LIMITS,
    inspector_module: object | None = None,
    inspector_version: str | None = None,
) -> dict[str, Any]:
    """Capture a complete native artifact from one immutable PDF byte string.

    ``inspector_module`` and ``inspector_version`` are injection seams for
    isolated tests.  Production callers omit both, causing an import and an
    installed-distribution version check.  All four byte APIs are invoked
    without a page selector, which is the only accepted extraction scope.
    """

    if not isinstance(limits, NativeCaptureLimits):
        raise NativeCaptureError("limits must be NativeCaptureLimits")
    checked_notice_id = _nonempty_string("notice_id", notice_id, max_characters=256)
    checked_relative_path = _safe_relative_path(source_relative_path, name="source_relative_path")
    source_bytes = _read_source_pdf(Path(source_pdf), limits)
    inspector, checked_version = _resolve_inspector(inspector_module, inspector_version)
    functions = {}
    for name in (
        "process_pdf_bytes",
        "extract_pages_markdown_bytes",
        "extract_text_with_positions_bytes",
        "extract_structure_elements_bytes",
    ):
        function = getattr(inspector, name, None)
        if not callable(function):
            raise NativeCaptureError(f"pinned pdf-inspector is missing {name}()")
        functions[name] = function
    try:
        # Check the cheapest document-wide result before asking the parser for
        # the larger page/text/structure collections. RLIMIT protects the
        # child while each native call is running; these inter-stage checks
        # prevent a known-over-limit document from doing more work or being
        # copied into Python dictionaries.
        raw_process_result = functions["process_pdf_bytes"](source_bytes)
        page_count = _integer(
            "process_result.page_count",
            _field(raw_process_result, "page_count", owner="process_result"),
            minimum=1,
            maximum=limits.max_pages,
        )
        process_result = _serialise_process_result(
            raw_process_result,
            max_pages=limits.max_pages,
        )
        _optional_string(
            "process_result.markdown",
            process_result["markdown"],
            max_characters=limits.max_markdown_characters,
        )

        pages_result = _serialise_pages_result(
            functions["extract_pages_markdown_bytes"](source_bytes),
            limits=limits,
            expected_page_count=page_count,
        )
        text_items = _serialise_text_items(
            functions["extract_text_with_positions_bytes"](source_bytes),
            limits=limits,
        )
        structure_elements = _serialise_structure_elements(
            functions["extract_structure_elements_bytes"](source_bytes),
            limits=limits,
        )
    except NativeCaptureError:
        raise
    except Exception as error:  # library exceptions are normalised at the artifact boundary
        raise NativeCaptureError("pdf-inspector failed to capture the complete source PDF") from error

    payload: dict[str, Any] = {
        "capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "notice_id": checked_notice_id,
        "source_kind": "pdf",
        "artifact_role": "production",
        "method": "pdf_inspector",
        "version": checked_version,
        "extraction_scope": "full_document",
        "source_path": checked_relative_path,
        "source_sha256": _sha256_bytes(source_bytes),
        "source_size_bytes": len(source_bytes),
        "process_result": process_result,
        "pages_markdown_result": pages_result,
        "text_items": text_items,
        "structure_elements": structure_elements,
    }
    return validate_native_capture(
        payload,
        source_pdf=source_pdf,
        expected_notice_id=checked_notice_id,
        expected_source_relative_path=checked_relative_path,
        limits=limits,
    )


def validate_native_capture(
    payload: object,
    *,
    source_pdf: str | Path | None = None,
    expected_notice_id: str | None = None,
    expected_source_relative_path: str | None = None,
    limits: NativeCaptureLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Validate and return a canonical-value copy of a native capture.

    Passing ``source_pdf`` rebinds the capture to the local immutable bytes
    by both SHA-256 and size.  Replay/import code should always pass it;
    schema-only readers may omit it when the original is unavailable.
    """

    if not isinstance(limits, NativeCaptureLimits):
        raise NativeCaptureError("limits must be NativeCaptureLimits")
    capture = _exact_keys(payload, _CAPTURE_KEYS, name="capture")
    if capture["capture_schema_version"] != CAPTURE_SCHEMA_VERSION:
        raise NativeCaptureError("unsupported native capture schema version")
    notice_id = _nonempty_string("capture.notice_id", capture["notice_id"], max_characters=256)
    if expected_notice_id is not None and notice_id != expected_notice_id:
        raise NativeCaptureError("capture notice_id does not match the expected notice")
    if capture["source_kind"] != "pdf" or capture["artifact_role"] != "production":
        raise NativeCaptureError("capture must be production PDF evidence")
    if capture["method"] != "pdf_inspector" or capture["version"] != PINNED_PDF_INSPECTOR_VERSION:
        raise NativeCaptureError("capture parser identity is not the pinned pdf-inspector release")
    if capture["extraction_scope"] != "full_document":
        raise NativeCaptureError("partial-page native captures are forbidden")
    source_path = _safe_relative_path(capture["source_path"], name="capture.source_path")
    if expected_source_relative_path is not None:
        expected = _safe_relative_path(expected_source_relative_path, name="expected_source_relative_path")
        if source_path != expected:
            raise NativeCaptureError("capture source_path does not match the expected relative path")
    digest = capture["source_sha256"]
    if not isinstance(digest, str) or len(digest) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in digest):
        raise NativeCaptureError("capture.source_sha256 must be a lowercase SHA-256 digest")
    source_size = _integer(
        "capture.source_size_bytes", capture["source_size_bytes"], minimum=5, maximum=limits.max_source_bytes,
    )
    if source_pdf is not None:
        source_bytes = _read_source_pdf(Path(source_pdf), limits)
        if len(source_bytes) != source_size or _sha256_bytes(source_bytes) != digest:
            raise NativeCaptureError("capture source hash/size does not match source_pdf")

    process = _exact_keys(capture["process_result"], _PROCESS_KEYS, name="capture.process_result")
    pdf_type = _nonempty_string("capture.process_result.pdf_type", process["pdf_type"], max_characters=32)
    if pdf_type not in _PDF_TYPES:
        raise NativeCaptureError("capture.process_result.pdf_type is unsupported")
    page_count = _integer(
        "capture.process_result.page_count", process["page_count"], minimum=1, maximum=limits.max_pages,
    )
    process_markdown = _optional_string(
        "capture.process_result.markdown", process["markdown"], max_characters=limits.max_markdown_characters,
    )
    title = _optional_string("capture.process_result.title", process["title"], max_characters=16_384)
    confidence = _finite("capture.process_result.confidence", process["confidence"], minimum=0.0, maximum=1.0)
    process_pages_needing_ocr = _page_list(
        "capture.process_result.pages_needing_ocr", process["pages_needing_ocr"], page_count=page_count,
    )
    process_pages_with_tables = _page_list(
        "capture.process_result.pages_with_tables", process["pages_with_tables"], page_count=page_count,
    )
    process_pages_with_columns = _page_list(
        "capture.process_result.pages_with_columns", process["pages_with_columns"], page_count=page_count,
    )
    process_ocr_reasons = _validate_ocr_reasons(
        "capture.process_result.ocr_reasons_by_page", process["ocr_reasons_by_page"], page_count=page_count,
    )

    pages_result = _exact_keys(
        capture["pages_markdown_result"], _PAGES_RESULT_KEYS, name="capture.pages_markdown_result",
    )
    pages = pages_result["pages"]
    if not isinstance(pages, list) or len(pages) != page_count:
        raise NativeCaptureError("pages_markdown_result must cover every source page exactly once")
    checked_pages: list[dict[str, Any]] = []
    markdown_characters = len(process_markdown or "")
    for index, raw_page in enumerate(pages):
        page = _exact_keys(raw_page, _PAGE_MARKDOWN_KEYS, name=f"capture.pages_markdown_result.pages[{index}]")
        page_number = _integer(f"capture.pages_markdown_result.pages[{index}].page", page["page"])
        if page_number != index:
            raise NativeCaptureError("pages_markdown_result.pages must be complete, ordered and 0-indexed")
        markdown = _optional_string(
            f"capture.pages_markdown_result.pages[{index}].markdown",
            page["markdown"],
            max_characters=limits.max_markdown_characters,
        )
        if markdown is None:
            raise NativeCaptureError("page markdown must be a string")
        markdown_characters += len(markdown)
        if markdown_characters > limits.max_markdown_characters:
            raise NativeCaptureError("native markdown exceeds the aggregate safety cap")
        needs_ocr = _boolean(f"capture.pages_markdown_result.pages[{index}].needs_ocr", page["needs_ocr"])
        ocr_reason = _optional_string(
            f"capture.pages_markdown_result.pages[{index}].ocr_reason", page["ocr_reason"], max_characters=256,
        )
        checked_pages.append({
            "page": page_number,
            "markdown": markdown,
            "needs_ocr": needs_ocr,
            "ocr_reason": ocr_reason,
        })
    pages_needing_ocr = _page_list(
        "capture.pages_markdown_result.pages_needing_ocr", pages_result["pages_needing_ocr"], page_count=page_count,
    )
    pages_with_tables = _page_list(
        "capture.pages_markdown_result.pages_with_tables", pages_result["pages_with_tables"], page_count=page_count,
    )
    pages_with_columns = _page_list(
        "capture.pages_markdown_result.pages_with_columns", pages_result["pages_with_columns"], page_count=page_count,
    )
    pages_ocr_reasons = _validate_ocr_reasons(
        "capture.pages_markdown_result.ocr_reasons_by_page",
        pages_result["ocr_reasons_by_page"],
        page_count=page_count,
    )
    if pages_needing_ocr != process_pages_needing_ocr:
        raise NativeCaptureError("process and per-page OCR routing disagree")
    if pages_with_tables != process_pages_with_tables or pages_with_columns != process_pages_with_columns:
        raise NativeCaptureError("process and per-page layout classifications disagree")
    if pages_ocr_reasons != process_ocr_reasons:
        raise NativeCaptureError("process and per-page OCR reasons disagree")
    if pages_result["is_complex"] != process["is_complex_layout"]:
        raise NativeCaptureError("process and per-page layout complexity disagree")
    _boolean("capture.process_result.is_complex_layout", process["is_complex_layout"])
    _boolean("capture.process_result.has_encoding_issues", process["has_encoding_issues"])
    _boolean("capture.pages_markdown_result.is_complex", pages_result["is_complex"])

    raw_text_items = capture["text_items"]
    if not isinstance(raw_text_items, list) or len(raw_text_items) > limits.max_text_items:
        raise NativeCaptureError("text_items must be a list within the safety cap")
    checked_text_items: list[dict[str, Any]] = []
    text_characters = 0
    for index, raw_item in enumerate(raw_text_items):
        item = _exact_keys(raw_item, _TEXT_ITEM_KEYS, name=f"capture.text_items[{index}]")
        text = _optional_string(f"capture.text_items[{index}].text", item["text"], max_characters=limits.max_text_characters)
        if text is None:
            raise NativeCaptureError("text_items[].text must be a string")
        text_characters += len(text)
        if text_characters > limits.max_text_characters:
            raise NativeCaptureError("native text exceeds the aggregate safety cap")
        checked = {
            "text": text,
            "x": _finite(
                f"capture.text_items[{index}].x", item["x"],
                minimum=-float(limits.max_absolute_coordinate),
                maximum=float(limits.max_absolute_coordinate),
            ),
            "y": _finite(
                f"capture.text_items[{index}].y", item["y"],
                minimum=-float(limits.max_absolute_coordinate),
                maximum=float(limits.max_absolute_coordinate),
            ),
            # Link/form annotations may retain a signed rectangle dimension
            # in pdf-inspector 1.17.0.  It is source geometry, not a length to
            # normalise: preserve the sign while rejecting non-finite or
            # absurdly large coordinates.
            "width": _finite(
                f"capture.text_items[{index}].width", item["width"],
                minimum=-float(limits.max_absolute_coordinate),
                maximum=float(limits.max_absolute_coordinate),
            ),
            "height": _finite(
                f"capture.text_items[{index}].height", item["height"],
                minimum=-float(limits.max_absolute_coordinate),
                maximum=float(limits.max_absolute_coordinate),
            ),
            "font": _optional_string(f"capture.text_items[{index}].font", item["font"], max_characters=4_096),
            "font_tag": _optional_string(f"capture.text_items[{index}].font_tag", item["font_tag"], max_characters=4_096),
            "font_size": _finite(
                f"capture.text_items[{index}].font_size", item["font_size"],
                minimum=0.0,
                maximum=float(limits.max_absolute_coordinate),
            ),
            "page": _integer(f"capture.text_items[{index}].page", item["page"], minimum=1, maximum=page_count),
            "is_bold": _boolean(f"capture.text_items[{index}].is_bold", item["is_bold"]),
            "is_italic": _boolean(f"capture.text_items[{index}].is_italic", item["is_italic"]),
            "is_underline": _boolean(f"capture.text_items[{index}].is_underline", item["is_underline"]),
            "is_strikeout": _boolean(f"capture.text_items[{index}].is_strikeout", item["is_strikeout"]),
            "item_type": _nonempty_string(
                f"capture.text_items[{index}].item_type", item["item_type"], max_characters=16_384,
            ),
            "mcid": None if item["mcid"] is None else _integer(
                f"capture.text_items[{index}].mcid", item["mcid"], minimum=0,
            ),
        }
        if checked["font"] is None or checked["font_tag"] is None:
            raise NativeCaptureError("text_items font and font_tag must be strings")
        checked_text_items.append(checked)

    raw_structure = capture["structure_elements"]
    if not isinstance(raw_structure, list) or len(raw_structure) > limits.max_structure_elements:
        raise NativeCaptureError("structure_elements must be a list within the safety cap")
    checked_structure: list[dict[str, Any]] = []
    previous_structure_key: tuple[int, int] | None = None
    for index, raw_item in enumerate(raw_structure):
        item = _exact_keys(raw_item, _STRUCTURE_ELEMENT_KEYS, name=f"capture.structure_elements[{index}]")
        checked = {
            "page": _integer(f"capture.structure_elements[{index}].page", item["page"], minimum=1, maximum=page_count),
            "mcid": _integer(f"capture.structure_elements[{index}].mcid", item["mcid"], minimum=0),
            "role": _nonempty_string(f"capture.structure_elements[{index}].role", item["role"], max_characters=4_096),
        }
        structure_key = (checked["page"], checked["mcid"])
        if previous_structure_key is not None and structure_key < previous_structure_key:
            raise NativeCaptureError("structure_elements must be sorted by (page, mcid)")
        previous_structure_key = structure_key
        checked_structure.append(checked)

    canonical: dict[str, Any] = {
        "capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "notice_id": notice_id,
        "source_kind": "pdf",
        "artifact_role": "production",
        "method": "pdf_inspector",
        "version": PINNED_PDF_INSPECTOR_VERSION,
        "extraction_scope": "full_document",
        "source_path": source_path,
        "source_sha256": digest,
        "source_size_bytes": source_size,
        "process_result": {
            "pdf_type": pdf_type,
            "markdown": process_markdown,
            "page_count": page_count,
            "pages_needing_ocr": process_pages_needing_ocr,
            "ocr_reasons_by_page": process_ocr_reasons,
            "title": title,
            "confidence": confidence,
            "is_complex_layout": process["is_complex_layout"],
            "pages_with_tables": process_pages_with_tables,
            "pages_with_columns": process_pages_with_columns,
            "has_encoding_issues": process["has_encoding_issues"],
        },
        "pages_markdown_result": {
            "pages": checked_pages,
            "pages_with_tables": pages_with_tables,
            "pages_with_columns": pages_with_columns,
            "pages_needing_ocr": pages_needing_ocr,
            "ocr_reasons_by_page": pages_ocr_reasons,
            "is_complex": pages_result["is_complex"],
        },
        "text_items": checked_text_items,
        "structure_elements": checked_structure,
    }
    encoded = canonical_json_bytes(canonical)
    if len(encoded) > limits.max_capture_bytes:
        raise NativeCaptureError("canonical native capture exceeds the artifact safety cap")
    return canonical


__all__ = [
    "CAPTURE_SCHEMA_VERSION",
    "DEFAULT_LIMITS",
    "NativeCaptureError",
    "NativeCaptureLimits",
    "PINNED_PDF_INSPECTOR_VERSION",
    "canonical_json_bytes",
    "capture_pdf_to_native",
    "load_native_capture_file",
    "validate_native_capture",
]
