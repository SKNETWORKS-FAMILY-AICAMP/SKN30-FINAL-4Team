"""Offline-only geometry projection for *legacy* cached Surya JSON.

This module is intentionally not an importer for ``surya_layout_artifact/v1``.
Legacy output predates that contract and can only produce an explicitly
non-promotable evaluation envelope.  In particular, OCR strings, HTML, table
grids, polygons, confidences, and producer claims are never exposed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "legacy_surya_layout_evaluation/v1"
COORDINATE_STATUS = "legacy_surya_unverified"
ORDERING_STATUS = "legacy_surya_traversal_unverified"
LEGACY_ALL_PAGES_MODE = "all_page_layout_only_table_diagram_candidates"
MAX_LEGACY_JSON_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000
MAX_JSON_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_REGIONS_PER_PAGE = 200
MAX_TOTAL_REGIONS = 10_000
_SHA256_LENGTH = 64

# This mapping is deliberately local to the evaluation projection.  It must
# never be treated as a claim that the legacy producer emitted v1 labels.
LEGACY_PASCALCASE_LABEL_TO_CANONICAL = {
    "Caption": "caption", "Footnote": "footnote", "Equation": "equation",
    "ListGroup": "list_group", "PageHeader": "page_header", "PageFooter": "page_footer",
    "Picture": "picture", "SectionHeader": "section_header", "Table": "table",
    "Text": "text", "Figure": "figure", "Code": "code", "Form": "form",
    "TableOfContents": "table_of_contents", "ChemicalBlock": "chemical_block",
    "Diagram": "diagram", "Bibliography": "bibliography", "BlankPage": "blank_page",
}


class LegacySuryaEvaluationError(ValueError):
    """Raised when a cached legacy result cannot safely be evaluated."""


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in value):
        raise LegacySuryaEvaluationError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _string(name: str, value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise LegacySuryaEvaluationError(f"{name} must be a non-empty trimmed string")
    return value


def _positive_int(name: str, value: object, *, maximum: int | None = None) -> int:
    if (isinstance(value, bool) or not isinstance(value, int) or value < 1
            or (maximum is not None and value > maximum)):
        suffix = f" no greater than {maximum}" if maximum is not None else ""
        raise LegacySuryaEvaluationError(f"{name} must be a positive integer{suffix}")
    return value


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LegacySuryaEvaluationError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise LegacySuryaEvaluationError(f"{name} must be a finite number")
    return 0.0 if result == 0.0 else result


def _bbox(value: object, *, width: int, height: int) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise LegacySuryaEvaluationError("legacy source_bbox must contain exactly four coordinates")
    x0, y0, x1, y1 = tuple(_finite(f"legacy source_bbox[{i}]", item) for i, item in enumerate(value))
    if not x0 < x1 or not y0 < y1:
        raise LegacySuryaEvaluationError("legacy source_bbox must have strictly increasing bounds")
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        raise LegacySuryaEvaluationError("legacy source_bbox lies outside declared page image size")
    return x0, y0, x1, y1


def _assert_limits(value: object, *, depth: int = 1, counter: list[int] | None = None) -> None:
    if depth > MAX_JSON_DEPTH:
        raise LegacySuryaEvaluationError("legacy JSON exceeds the maximum nesting depth")
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise LegacySuryaEvaluationError("legacy JSON exceeds the node cap")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        # ``json.loads`` accepts exponent overflows such as ``1e999`` as
        # ``inf``.  They must be rejected even when located in a field this
        # geometry-only projection does not otherwise inspect.
        if isinstance(value, float) and not math.isfinite(value):
            raise LegacySuryaEvaluationError("legacy JSON contains a non-finite number")
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise LegacySuryaEvaluationError("legacy JSON contains a surrogate code point")
        if len(value.encode("utf-8")) > MAX_JSON_STRING_BYTES:
            raise LegacySuryaEvaluationError("legacy JSON string exceeds the byte cap")
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise LegacySuryaEvaluationError("legacy JSON object key must be a string")
            _assert_limits(key, depth=depth + 1, counter=counter)
            _assert_limits(nested, depth=depth + 1, counter=counter)
    elif isinstance(value, list):
        for nested in value:
            _assert_limits(nested, depth=depth + 1, counter=counter)
    else:
        # A decoded JSON document cannot contain this, but retaining the
        # check makes this boundary fail closed if the decoder changes.
        raise LegacySuryaEvaluationError("legacy JSON contains a non-JSON primitive")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LegacySuryaEvaluationError(f"legacy JSON contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LegacySuryaEvaluationError(f"legacy JSON contains non-finite constant {value!r}")


def _no_symlink_components(path: Path) -> Path:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as error:
            raise LegacySuryaEvaluationError("legacy JSON file does not exist") from error
        if stat.S_ISLNK(info.st_mode):
            raise LegacySuryaEvaluationError("legacy JSON file path must not traverse a symlink")
    return absolute


def read_legacy_surya_json(path: str | Path) -> bytes:
    """Read one immutable regular file without following links or path swaps."""
    candidate = _no_symlink_components(Path(path))
    before = os.lstat(candidate)
    if not stat.S_ISREG(before.st_mode):
        raise LegacySuryaEvaluationError("legacy JSON input must be a regular file")
    if before.st_size > MAX_LEGACY_JSON_BYTES:
        raise LegacySuryaEvaluationError("legacy JSON exceeds the byte cap")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise LegacySuryaEvaluationError("legacy JSON input cannot be safely opened") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise LegacySuryaEvaluationError("legacy JSON input changed while opening")
        chunks: list[bytes] = []
        remaining = MAX_LEGACY_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_LEGACY_JSON_BYTES:
            raise LegacySuryaEvaluationError("legacy JSON exceeds the byte cap")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (opened.st_dev, opened.st_ino, opened.st_size):
            raise LegacySuryaEvaluationError("legacy JSON input changed while reading")
        return raw
    finally:
        os.close(descriptor)


def _decode(raw: bytes) -> Mapping[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > MAX_LEGACY_JSON_BYTES:
        raise LegacySuryaEvaluationError("legacy JSON exceeds the byte cap")
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise LegacySuryaEvaluationError("legacy JSON is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise LegacySuryaEvaluationError("legacy JSON root must be an object")
    _assert_limits(value)
    return value


def _scope(page_scope: Sequence[int], *, page_count: int) -> tuple[int, ...]:
    if not isinstance(page_scope, (tuple, list)) or not page_scope:
        raise LegacySuryaEvaluationError("page_scope must be a non-empty page sequence")
    pages = tuple(_positive_int(f"page_scope[{i}]", page, maximum=MAX_PAGES) for i, page in enumerate(page_scope))
    if tuple(sorted(pages)) != pages or len(set(pages)) != len(pages) or pages[-1] > page_count:
        raise LegacySuryaEvaluationError("page_scope must be ascending, unique, and within page_count")
    return pages


@dataclass(frozen=True, slots=True)
class LegacySuryaGeometryProposal:
    """One unverified geometry proposal; it deliberately has no text fields."""
    page: int
    legacy_block_index: int
    legacy_label: str
    canonical_label: str
    bbox_px: tuple[float, float, float, float]
    legacy_traversal_order: int

    def __post_init__(self) -> None:
        """Validate direct construction as strictly as parsed construction.

        ``legacy_block_index`` is a producer identity only.  It is *not* a
        verified reading-order claim.  The separately named traversal value
        records deterministic array traversal in this cached payload.
        """
        object.__setattr__(self, "page", _positive_int("proposal page", self.page, maximum=MAX_PAGES))
        if isinstance(self.legacy_block_index, bool) or not isinstance(self.legacy_block_index, int) or self.legacy_block_index < 0:
            raise LegacySuryaEvaluationError("proposal legacy_block_index must be a non-negative integer")
        if isinstance(self.legacy_traversal_order, bool) or not isinstance(self.legacy_traversal_order, int) or self.legacy_traversal_order < 0:
            raise LegacySuryaEvaluationError("proposal legacy_traversal_order must be a non-negative integer")
        if not isinstance(self.legacy_label, str) or self.legacy_label not in LEGACY_PASCALCASE_LABEL_TO_CANONICAL:
            raise LegacySuryaEvaluationError("proposal legacy_label is not a supported evaluation projection")
        expected = LEGACY_PASCALCASE_LABEL_TO_CANONICAL[self.legacy_label]
        if not isinstance(self.canonical_label, str) or self.canonical_label != expected:
            raise LegacySuryaEvaluationError("proposal canonical_label must exactly map from legacy_label")
        if not isinstance(self.bbox_px, tuple) or len(self.bbox_px) != 4:
            raise LegacySuryaEvaluationError("proposal bbox_px must be a four-coordinate tuple")
        bbox = tuple(_finite(f"proposal bbox_px[{index}]", coordinate) for index, coordinate in enumerate(self.bbox_px))
        if not bbox[0] < bbox[2] or not bbox[1] < bbox[3] or bbox[0] < 0 or bbox[1] < 0:
            raise LegacySuryaEvaluationError("proposal bbox_px must be finite, non-negative, and strictly increasing")
        object.__setattr__(self, "bbox_px", bbox)

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page, "legacy_block_index": self.legacy_block_index,
                "legacy_label": self.legacy_label, "canonical_label": self.canonical_label,
                "bbox_px": list(self.bbox_px), "legacy_traversal_order": self.legacy_traversal_order}


@dataclass(frozen=True, slots=True)
class LegacySuryaLayoutEvaluation:
    """Canonical non-promotable envelope for an offline-only comparison run."""
    notice_id: str
    source_pdf_sha256: str
    raw_json_sha256: str
    stage_record_sha256: str
    legacy_render_manifest_sha256: str
    page_scope: tuple[int, ...]
    page_count: int
    format_variant: str
    pages: tuple[tuple[int, int, int, tuple[LegacySuryaGeometryProposal, ...]], ...]
    schema_version: str = SCHEMA_VERSION
    coordinate_status: str = COORDINATE_STATUS
    ordering_status: str = ORDERING_STATUS
    evaluation_only: bool = True
    non_promotable: bool = True

    def __post_init__(self) -> None:
        if (self.schema_version != SCHEMA_VERSION or self.coordinate_status != COORDINATE_STATUS
                or self.ordering_status != ORDERING_STATUS):
            raise LegacySuryaEvaluationError("legacy evaluation constants are immutable")
        if self.evaluation_only is not True or self.non_promotable is not True:
            raise LegacySuryaEvaluationError("legacy evaluation envelope must remain non-promotable and evaluation-only")
        for name in ("source_pdf_sha256", "raw_json_sha256", "stage_record_sha256", "legacy_render_manifest_sha256"):
            object.__setattr__(self, name, _sha(name, getattr(self, name)))
        object.__setattr__(self, "notice_id", _string("notice_id", self.notice_id))
        object.__setattr__(self, "page_count", _positive_int("page_count", self.page_count, maximum=MAX_PAGES))
        object.__setattr__(self, "page_scope", _scope(self.page_scope, page_count=self.page_count))
        if self.format_variant != "all_pages":
            raise LegacySuryaEvaluationError("format_variant must be all_pages")
        if not isinstance(self.pages, tuple) or len(self.pages) > MAX_PAGES:
            raise LegacySuryaEvaluationError("pages must be a bounded tuple")
        envelope_pages: list[int] = []
        total = 0
        for entry in self.pages:
            if not isinstance(entry, tuple) or len(entry) != 4:
                raise LegacySuryaEvaluationError("pages must contain page geometry entries")
            page, width, height, proposals = entry
            page = _positive_int("envelope page", page, maximum=MAX_PAGES)
            _positive_int("envelope pixel_width", width)
            _positive_int("envelope pixel_height", height)
            if page not in self.page_scope or page in envelope_pages or not isinstance(proposals, tuple):
                raise LegacySuryaEvaluationError("pages must uniquely remain within page_scope")
            envelope_pages.append(page)
            identities: set[int] = set()
            traversals: list[int] = []
            for proposal in proposals:
                if not isinstance(proposal, LegacySuryaGeometryProposal) or proposal.page != page:
                    raise LegacySuryaEvaluationError("geometry proposal has an invalid page identity")
                if proposal.legacy_block_index in identities:
                    raise LegacySuryaEvaluationError("geometry proposals must not repeat legacy block identities")
                identities.add(proposal.legacy_block_index)
                traversals.append(proposal.legacy_traversal_order)
                _bbox(list(proposal.bbox_px), width=width, height=height)
            if tuple(traversals) != tuple(range(len(proposals))):
                raise LegacySuryaEvaluationError("geometry proposals must preserve dense deterministic legacy traversal order")
            total += len(proposals)
            if len(proposals) > MAX_REGIONS_PER_PAGE or total > MAX_TOTAL_REGIONS:
                raise LegacySuryaEvaluationError("geometry proposals exceed evaluation caps")
        if tuple(envelope_pages) != self.page_scope:
            raise LegacySuryaEvaluationError("pages must cover page_scope exactly once in deterministic page order")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "evaluation_only": True, "non_promotable": True,
                "coordinate_status": self.coordinate_status, "ordering_status": self.ordering_status,
                "notice_id": self.notice_id,
                "source_pdf_sha256": self.source_pdf_sha256, "raw_json_sha256": self.raw_json_sha256,
                "stage_record_sha256": self.stage_record_sha256,
                "legacy_render_manifest_sha256": self.legacy_render_manifest_sha256,
                "page_scope": list(self.page_scope), "page_count": self.page_count,
                "format_variant": self.format_variant,
                "pages": [{"page": page, "pixel_width": width, "pixel_height": height,
                           "geometry_proposals": [proposal.to_dict() for proposal in proposals]}
                          for page, width, height, proposals in self.pages]}

    def canonical_json(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def parse_legacy_surya_layout_evaluation_bytes(
    raw: bytes, *, notice_id: str, source_pdf_sha256: str, raw_json_sha256: str,
    stage_record_sha256: str, legacy_render_manifest_sha256: str, page_scope: Sequence[int],
    page_count: int, format_variant: str,
) -> LegacySuryaLayoutEvaluation:
    """Build an evaluation envelope; only legacy ``all_pages`` is projectable.

    ``targeted`` is retained as an explicit envelope variant but intentionally
    has no parser: its historical shape/table crops do not form a trustworthy
    page-layout input.
    """
    if format_variant != "all_pages":
        raise LegacySuryaEvaluationError("only legacy all_pages JSON may produce geometry proposals")
    if sha256(raw).hexdigest() != _sha("raw_json_sha256", raw_json_sha256):
        raise LegacySuryaEvaluationError("raw_json_sha256 does not bind the supplied legacy bytes")
    checked_page_count = _positive_int("page_count", page_count, maximum=MAX_PAGES)
    scope = _scope(page_scope, page_count=checked_page_count)
    if scope != tuple(range(1, checked_page_count + 1)):
        raise LegacySuryaEvaluationError("all_pages page_scope must cover every page exactly once")
    value = _decode(raw)
    if value.get("status") != "ok" or value.get("mode") != LEGACY_ALL_PAGES_MODE:
        raise LegacySuryaEvaluationError("legacy JSON is not a successful all-pages layout result")
    if value.get("notice_id") != notice_id:
        raise LegacySuryaEvaluationError("legacy JSON notice_id does not match caller binding")
    pages = value.get("pages")
    if not isinstance(pages, list) or not pages or len(pages) > MAX_PAGES:
        raise LegacySuryaEvaluationError("legacy pages must be a non-empty bounded array")
    if len(pages) != checked_page_count:
        raise LegacySuryaEvaluationError("legacy pages do not match caller page_count")
    output: list[tuple[int, int, int, tuple[LegacySuryaGeometryProposal, ...]]] = []
    seen_pages: set[int] = set()
    total = 0
    for page_value in pages:
        if not isinstance(page_value, Mapping):
            raise LegacySuryaEvaluationError("legacy page must be an object")
        page = _positive_int("legacy page.page", page_value.get("page"), maximum=MAX_PAGES)
        if page in seen_pages or page not in scope:
            raise LegacySuryaEvaluationError("legacy pages must have unique identities within page_scope")
        seen_pages.add(page)
        image_size = page_value.get("image_size")
        if not isinstance(image_size, Mapping):
            raise LegacySuryaEvaluationError("legacy page.image_size must be an object")
        width, height = _positive_int("legacy image_size.width", image_size.get("width")), _positive_int("legacy image_size.height", image_size.get("height"))
        blocks = page_value.get("blocks")
        if not isinstance(blocks, list) or len(blocks) > MAX_REGIONS_PER_PAGE:
            raise LegacySuryaEvaluationError("legacy page.blocks exceeds the per-page region cap")
        if page_value.get("block_count") != len(blocks):
            raise LegacySuryaEvaluationError("legacy page.block_count does not match page.blocks")
        proposals: list[LegacySuryaGeometryProposal] = []
        identities: set[int] = set()
        for traversal_order, block in enumerate(blocks):
            if not isinstance(block, Mapping):
                raise LegacySuryaEvaluationError("legacy block must be an object")
            index = block.get("block_index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index in identities:
                raise LegacySuryaEvaluationError("legacy blocks must have unique non-negative block_index identities")
            identities.add(index)
            label = block.get("label")
            if not isinstance(label, str) or label not in LEGACY_PASCALCASE_LABEL_TO_CANONICAL:
                raise LegacySuryaEvaluationError("legacy block label is not a supported evaluation projection")
            proposals.append(LegacySuryaGeometryProposal(page=page, legacy_block_index=index, legacy_label=label,
                canonical_label=LEGACY_PASCALCASE_LABEL_TO_CANONICAL[label], bbox_px=_bbox(block.get("source_bbox"), width=width, height=height), legacy_traversal_order=traversal_order))
        total += len(proposals)
        if total > MAX_TOTAL_REGIONS:
            raise LegacySuryaEvaluationError("legacy JSON exceeds the total region cap")
        output.append((page, width, height, tuple(proposals)))
    if tuple(sorted(seen_pages)) != scope:
        raise LegacySuryaEvaluationError("legacy pages must cover caller page_scope exactly once")
    output.sort(key=lambda entry: entry[0])
    return LegacySuryaLayoutEvaluation(notice_id=notice_id, source_pdf_sha256=source_pdf_sha256,
        raw_json_sha256=raw_json_sha256, stage_record_sha256=stage_record_sha256,
        legacy_render_manifest_sha256=legacy_render_manifest_sha256, page_scope=scope,
        page_count=checked_page_count, format_variant=format_variant, pages=tuple(output))


def parse_legacy_surya_layout_evaluation_file(path: str | Path, **bindings: Any) -> LegacySuryaLayoutEvaluation:
    """Safely read a legacy cache file, then apply caller-supplied bindings."""
    return parse_legacy_surya_layout_evaluation_bytes(read_legacy_surya_json(path), **bindings)
