"""Strict, textless Surya layout artifact contract for Existing PDF shadowing.

``surya_layout_artifact/v1`` is deliberately a *layout-only* boundary.  It
contains geometry and labels for already-rendered page PNGs, never a PDF,
recognized OCR string, HTML, table cell value, or Common IR content.  The
consumer must bind it to a locally trusted :class:`PdfRenderManifest` before
using even its geometry.

The module is dependency-free so the same portable validation runs before and
after a remote accelerator round trip.  It does not contact RunPod, Storage,
or a database and is not an authorization mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .coordinate_manifest import (
    CoordinateManifestError,
    PdfCoordinateManifest,
    build_sidecar_binding,
    pixel_to_user_bbox,
    user_to_pixel_bbox,
    validate_sidecar_binding,
)
from .render_manifest import PdfRenderManifest


SCHEMA_VERSION = "surya_layout_artifact/v1"
REGION_LABELS_V1 = (
    "caption",
    "footnote",
    "equation",
    "list_group",
    "page_header",
    "page_footer",
    "picture",
    "section_header",
    "table",
    "text",
    "figure",
    "code",
    "form",
    "table_of_contents",
    "chemical_block",
    "diagram",
    "bibliography",
    "blank_page",
)
_REGION_LABEL_SET = frozenset(REGION_LABELS_V1)
_REGION_LABEL_INDEX = {label: index for index, label in enumerate(REGION_LABELS_V1)}
_SHA256_LENGTH = 64
_BOUND_EPSILON = 1e-6
_ROUND_TRIP_EPSILON = 1e-6
# The remote result is untrusted before this module accepts it.  These caps
# are intentionally independent of an eventual provider HTTP body cap.  The
# default 200 regions/page is the current Surya prompt budget; changing it is
# a contract revision, not a caller knob.
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 32
# Count before json.loads builds Python dict/list nodes.  This cap is also a
# practical guard against a compact array of millions of empty objects.
MAX_JSON_NODES = 50_000
MAX_REQUESTED_PAGES = 256
MAX_REGIONS_PER_PAGE = 200
MAX_TOTAL_REGIONS = 10_000
MAX_STRING_BYTES = 4 * 1024
MAX_IDENTITY_STRING_CHARS = 256
# Surya emits binary-float tails such as ``383.26800000000003``.  v1 stores
# layout coordinates at a fixed milli-pixel precision: this is far tighter
# than raster/OCR geometry while avoiding non-deterministic JSON float tails.
PIXEL_COORDINATE_DECIMALS = 3
_PIXEL_QUANTUM = Decimal("0.001")

_ARTIFACT_KEYS = frozenset({
    "schema_version",
    "logical_compute_key",
    "source_sha256",
    "page_count",
    "render_manifest_schema_version",
    "render_manifest_sha256",
    "producer",
    "requested_pages",
    "pages",
})
_PRODUCER_KEYS = frozenset({
    "engine_id",
    "engine_version",
    "model_id",
    "model_revision",
    "model_weights_sha256",
    "pipeline_revision",
    "config_sha256",
    "worker_image_digest",
})
_PAGE_KEYS = frozenset({
    "page",
    "sidecar_binding",
    "pixel_width",
    "pixel_height",
    "rendered_page_px",
    "regions",
})
_REGION_KEYS = frozenset({
    "region_id",
    "label",
    "bbox_px",
    "polygon_px",
    "confidence",
    "reading_order",
})


class SuryaLayoutArtifactError(ValueError):
    """Raised when an untrusted Surya layout result fails closed."""


def _assert_exact_keys(value: Mapping[str, Any], expected: frozenset[str], *, name: str) -> None:
    keys = frozenset(value.keys())
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append("missing keys: " + ", ".join(missing))
        if extra:
            parts.append("unexpected keys: " + ", ".join(extra))
        raise SuryaLayoutArtifactError(f"{name} keys are invalid ({'; '.join(parts)})")


def _require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in value):
        raise SuryaLayoutArtifactError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _require_nonempty_string(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > MAX_IDENTITY_STRING_CHARS
    ):
        raise SuryaLayoutArtifactError(f"{name} must be a non-empty string")
    return value


def _require_worker_image_digest(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise SuryaLayoutArtifactError("producer.worker_image_digest must be sha256:<64 lowercase hex characters>")
    _require_sha256("producer.worker_image_digest", value[len("sha256:"):])
    return value


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SuryaLayoutArtifactError(f"{name} must be a positive integer")
    return value


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SuryaLayoutArtifactError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise SuryaLayoutArtifactError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise SuryaLayoutArtifactError(f"{name} must be a finite number")
    return 0.0 if result == 0.0 else result


def _pixel_coordinate(name: str, value: object) -> float:
    """Normalize a JSON pixel coordinate to the v1 milli-pixel wire policy."""
    number = _finite_number(name, value)
    try:
        quantized = Decimal(str(number)).quantize(_PIXEL_QUANTUM, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError) as error:
        raise SuryaLayoutArtifactError(f"{name} cannot be normalized to milli-pixel precision") from error
    result = float(quantized)
    if not math.isfinite(result):
        raise SuryaLayoutArtifactError(f"{name} must be a finite number")
    return 0.0 if result == 0.0 else result


def _bbox(name: str, value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise SuryaLayoutArtifactError(f"{name} must contain exactly four coordinates")
    x0, y0, x1, y1 = tuple(_finite_number(f"{name}[{index}]", item) for index, item in enumerate(value))
    if not x0 < x1 or not y0 < y1:
        raise SuryaLayoutArtifactError(f"{name} must have strictly increasing x/y bounds")
    return x0, y0, x1, y1


def _pixel_bbox(name: str, value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise SuryaLayoutArtifactError(f"{name} must contain exactly four coordinates")
    x0, y0, x1, y1 = tuple(_pixel_coordinate(f"{name}[{index}]", item) for index, item in enumerate(value))
    if not x0 < x1 or not y0 < y1:
        raise SuryaLayoutArtifactError(f"{name} must have strictly increasing x/y bounds")
    return x0, y0, x1, y1


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise SuryaLayoutArtifactError("artifact is not canonically serializable JSON") from error


def _assert_json_limits(value: object, *, depth: int = 1) -> None:
    """Bound decoded JSON before semantic construction can recurse or allocate.

    ``json.loads`` is deliberately followed by this check rather than trusting
    the provider's Content-Length.  It also protects direct ``from_dict``
    callers, which do not pass through raw-byte parsing.
    """
    if depth > MAX_JSON_DEPTH:
        raise SuryaLayoutArtifactError("artifact JSON exceeds the maximum nesting depth")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise SuryaLayoutArtifactError("artifact JSON string contains a surrogate code point")
        try:
            encoded_length = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise SuryaLayoutArtifactError("artifact JSON string is not valid UTF-8 text") from error
        if encoded_length > MAX_STRING_BYTES:
            raise SuryaLayoutArtifactError("artifact JSON string exceeds the byte cap")
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise SuryaLayoutArtifactError("artifact JSON object keys must be strings")
            _assert_json_limits(key, depth=depth + 1)
            _assert_json_limits(nested, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_json_limits(nested, depth=depth + 1)


def _assert_artifact_collection_limits(value: Mapping[str, Any]) -> None:
    """Reject oversized page/region arrays before dataclass construction."""
    requested = value.get("requested_pages")
    pages = value.get("pages")
    if isinstance(requested, list) and len(requested) > MAX_REQUESTED_PAGES:
        raise SuryaLayoutArtifactError("requested_pages exceeds the page cap")
    if isinstance(pages, list) and len(pages) > MAX_REQUESTED_PAGES:
        raise SuryaLayoutArtifactError("pages exceeds the page cap")
    if not isinstance(pages, list):
        return
    total_regions = 0
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        regions = page.get("regions")
        if not isinstance(regions, list):
            continue
        if len(regions) > MAX_REGIONS_PER_PAGE:
            raise SuryaLayoutArtifactError("page.regions exceeds the per-page region cap")
        total_regions += len(regions)
        if total_regions > MAX_TOTAL_REGIONS:
            raise SuryaLayoutArtifactError("artifact exceeds the total region cap")


def _close(left: float, right: float, *, tolerance: float = _BOUND_EPSILON) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance)


def _contains(bounds: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    return (
        bounds[0] - _BOUND_EPSILON <= point[0] <= bounds[2] + _BOUND_EPSILON
        and bounds[1] - _BOUND_EPSILON <= point[1] <= bounds[3] + _BOUND_EPSILON
    )


def _polygon_area(points: Sequence[tuple[float, float]]) -> float:
    return 0.5 * abs(sum(
        point[0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * point[1]
        for index, point in enumerate(points)
    ))


def _segments_intersect(
    first_start: tuple[float, float], first_end: tuple[float, float],
    second_start: tuple[float, float], second_end: tuple[float, float],
) -> bool:
    def orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def sign(value: float) -> int:
        return 1 if value > _BOUND_EPSILON else -1 if value < -_BOUND_EPSILON else 0

    def on_segment(a: tuple[float, float], b: tuple[float, float], point: tuple[float, float]) -> bool:
        return (
            min(a[0], b[0]) - _BOUND_EPSILON <= point[0] <= max(a[0], b[0]) + _BOUND_EPSILON
            and min(a[1], b[1]) - _BOUND_EPSILON <= point[1] <= max(a[1], b[1]) + _BOUND_EPSILON
        )

    first, second = orientation(first_start, first_end, second_start), orientation(first_start, first_end, second_end)
    third, fourth = orientation(second_start, second_end, first_start), orientation(second_start, second_end, first_end)
    first_sign, second_sign, third_sign, fourth_sign = sign(first), sign(second), sign(third), sign(fourth)
    if first_sign * second_sign < 0 and third_sign * fourth_sign < 0:
        return True
    return (
        (first_sign == 0 and on_segment(first_start, first_end, second_start))
        or (second_sign == 0 and on_segment(first_start, first_end, second_end))
        or (third_sign == 0 and on_segment(second_start, second_end, first_start))
        or (fourth_sign == 0 and on_segment(second_start, second_end, first_end))
    )


@dataclass(frozen=True, slots=True)
class SuryaProducerIdentity:
    """Pinned producer identity repeated by every accepted remote artifact.

    ``model_weights_sha256`` is the digest of the immutable aggregate weights
    manifest, not a claim about one arbitrary downloaded model file.  Its name
    intentionally matches the existing accelerator contract.
    """

    engine_id: str
    engine_version: str
    model_id: str
    model_revision: str
    model_weights_sha256: str
    pipeline_revision: str
    config_sha256: str
    worker_image_digest: str

    def __post_init__(self) -> None:
        for name in ("engine_id", "engine_version", "model_id", "model_revision", "pipeline_revision"):
            object.__setattr__(self, name, _require_nonempty_string(f"producer.{name}", getattr(self, name)))
        if self.engine_id != "surya":
            raise SuryaLayoutArtifactError("producer.engine_id must be the pinned value 'surya'")
        object.__setattr__(self, "model_weights_sha256", _require_sha256("producer.model_weights_sha256", self.model_weights_sha256))
        object.__setattr__(self, "config_sha256", _require_sha256("producer.config_sha256", self.config_sha256))
        object.__setattr__(self, "worker_image_digest", _require_worker_image_digest(self.worker_image_digest))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SuryaProducerIdentity":
        if not isinstance(value, Mapping):
            raise SuryaLayoutArtifactError("producer must be an object")
        _assert_exact_keys(value, _PRODUCER_KEYS, name="producer")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_weights_sha256": self.model_weights_sha256,
            "pipeline_revision": self.pipeline_revision,
            "config_sha256": self.config_sha256,
            "worker_image_digest": self.worker_image_digest,
        }


@dataclass(frozen=True, slots=True)
class SuryaLayoutRegion:
    """One textless layout region in the canonical rendered-page pixel space."""

    region_id: str
    label: str
    bbox_px: tuple[float, float, float, float]
    polygon_px: tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
    confidence: float | None
    reading_order: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "region_id", _require_nonempty_string("region.region_id", self.region_id))
        label = _require_nonempty_string("region.label", self.label)
        if label not in _REGION_LABEL_SET:
            raise SuryaLayoutArtifactError("region.label is not in the pinned v1 vocabulary")
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "bbox_px", _pixel_bbox("region.bbox_px", self.bbox_px))
        if not isinstance(self.polygon_px, (tuple, list)) or len(self.polygon_px) != 4:
            raise SuryaLayoutArtifactError("region.polygon_px must contain exactly four points")
        points: list[tuple[float, float]] = []
        for index, point in enumerate(self.polygon_px):
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise SuryaLayoutArtifactError(f"region.polygon_px[{index}] must contain exactly two coordinates")
            points.append((_pixel_coordinate(f"region.polygon_px[{index}][0]", point[0]), _pixel_coordinate(f"region.polygon_px[{index}][1]", point[1])))
        if len(set(points)) != 4:
            raise SuryaLayoutArtifactError("region.polygon_px points must be unique")
        if _polygon_area(points) <= _BOUND_EPSILON:
            raise SuryaLayoutArtifactError("region.polygon_px must enclose a non-zero area")
        if _segments_intersect(points[0], points[1], points[2], points[3]) or _segments_intersect(points[1], points[2], points[3], points[0]):
            raise SuryaLayoutArtifactError("region.polygon_px must not self-intersect")
        envelope = (min(point[0] for point in points), min(point[1] for point in points), max(point[0] for point in points), max(point[1] for point in points))
        if not all(_close(left, right) for left, right in zip(envelope, self.bbox_px)):
            raise SuryaLayoutArtifactError("region.bbox_px must equal the polygon_px envelope")
        variants = tuple(
            tuple(sequence[offset:] + sequence[:offset])
            for sequence in (points, list(reversed(points)))
            for offset in range(4)
        )
        object.__setattr__(self, "polygon_px", min(variants))
        if self.confidence is None:
            object.__setattr__(self, "confidence", None)
        else:
            confidence = _finite_number("region.confidence", self.confidence)
            if not 0.0 <= confidence <= 1.0:
                raise SuryaLayoutArtifactError("region.confidence must be between 0 and 1")
            object.__setattr__(self, "confidence", confidence)
        if isinstance(self.reading_order, bool) or not isinstance(self.reading_order, int) or self.reading_order < 0:
            raise SuryaLayoutArtifactError("region.reading_order must be an integer greater than or equal to zero")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SuryaLayoutRegion":
        if not isinstance(value, Mapping):
            raise SuryaLayoutArtifactError("region must be an object")
        _assert_exact_keys(value, _REGION_KEYS, name="region")
        return cls(
            region_id=value["region_id"], label=value["label"], bbox_px=_pixel_bbox("region.bbox_px", value["bbox_px"]),
            polygon_px=value["polygon_px"], confidence=value["confidence"], reading_order=value["reading_order"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "label": self.label,
            "bbox_px": list(self.bbox_px),
            "polygon_px": [list(point) for point in self.polygon_px],
            "confidence": self.confidence,
            "reading_order": self.reading_order,
        }


def _region_sort_key(region: SuryaLayoutRegion) -> tuple[Any, ...]:
    return (region.reading_order, _REGION_LABEL_INDEX[region.label], *region.bbox_px, *(coordinate for point in region.polygon_px for coordinate in point))


def _expected_region_id(page: int, region: SuryaLayoutRegion) -> str:
    return f"p{page:04d}-{region.label}-{region.reading_order + 1:04d}"


@dataclass(frozen=True, slots=True)
class SuryaLayoutPage:
    """All textless regions for exactly one canonical rendered PDF page."""

    page: int
    sidecar_binding: Mapping[str, Any]
    pixel_width: int
    pixel_height: int
    rendered_page_px: tuple[float, float, float, float]
    regions: tuple[SuryaLayoutRegion, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "page", _positive_int("page.page", self.page))
        object.__setattr__(self, "pixel_width", _positive_int("page.pixel_width", self.pixel_width))
        object.__setattr__(self, "pixel_height", _positive_int("page.pixel_height", self.pixel_height))
        page_bounds = _bbox("page.rendered_page_px", self.rendered_page_px)
        expected_bounds = (0.0, 0.0, float(self.pixel_width), float(self.pixel_height))
        if page_bounds != expected_bounds:
            raise SuryaLayoutArtifactError("page.rendered_page_px must exactly cover the rendered pixel canvas")
        object.__setattr__(self, "rendered_page_px", page_bounds)
        if not isinstance(self.sidecar_binding, Mapping):
            raise SuryaLayoutArtifactError("page.sidecar_binding must be an object")
        # Frozen dataclasses do not freeze nested dictionaries.  Keep the
        # binding immutable after construction so a caller cannot change what
        # later local validation observes.
        object.__setattr__(self, "sidecar_binding", MappingProxyType(dict(self.sidecar_binding)))
        if not isinstance(self.regions, tuple) or any(not isinstance(region, SuryaLayoutRegion) for region in self.regions):
            raise SuryaLayoutArtifactError("page.regions must be a tuple of SuryaLayoutRegion values")
        if len(self.regions) > MAX_REGIONS_PER_PAGE:
            raise SuryaLayoutArtifactError("page.regions exceeds the per-page region cap")
        if tuple(sorted(self.regions, key=_region_sort_key)) != self.regions:
            raise SuryaLayoutArtifactError("page.regions must use canonical v1 region order")
        identities: set[str] = set()
        geometries: set[tuple[Any, ...]] = set()
        reading_orders: list[int] = []
        for region in self.regions:
            if region.region_id != _expected_region_id(self.page, region):
                raise SuryaLayoutArtifactError("region.region_id must be the deterministic canonical v1 identifier")
            if region.region_id in identities:
                raise SuryaLayoutArtifactError("page.regions must not repeat a region_id")
            identities.add(region.region_id)
            geometry = (region.bbox_px, region.polygon_px)
            if geometry in geometries:
                raise SuryaLayoutArtifactError("page.regions must not repeat an identical geometry")
            geometries.add(geometry)
            reading_orders.append(region.reading_order)
            if not all(_contains(page_bounds, point) for point in region.polygon_px):
                raise SuryaLayoutArtifactError("region.polygon_px lies outside rendered_page_px")
            if not (_contains(page_bounds, (region.bbox_px[0], region.bbox_px[1])) and _contains(page_bounds, (region.bbox_px[2], region.bbox_px[3]))):
                raise SuryaLayoutArtifactError("region.bbox_px lies outside rendered_page_px")
        if tuple(reading_orders) != tuple(range(len(self.regions))):
            raise SuryaLayoutArtifactError("page region reading_order values must be unique, contiguous, and start at zero")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SuryaLayoutPage":
        if not isinstance(value, Mapping):
            raise SuryaLayoutArtifactError("page must be an object")
        _assert_exact_keys(value, _PAGE_KEYS, name="page")
        if not isinstance(value["regions"], list):
            raise SuryaLayoutArtifactError("page.regions must be an array")
        return cls(
            page=value["page"], sidecar_binding=value["sidecar_binding"], pixel_width=value["pixel_width"],
            pixel_height=value["pixel_height"], rendered_page_px=_bbox("page.rendered_page_px", value["rendered_page_px"]),
            regions=tuple(SuryaLayoutRegion.from_dict(region) for region in value["regions"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "sidecar_binding": dict(self.sidecar_binding),
            "pixel_width": self.pixel_width,
            "pixel_height": self.pixel_height,
            "rendered_page_px": list(self.rendered_page_px),
            "regions": [region.to_dict() for region in self.regions],
        }


@dataclass(frozen=True, slots=True)
class SuryaLayoutArtifact:
    """Portable, canonical, textless output from one Surya layout computation."""

    logical_compute_key: str
    source_sha256: str
    page_count: int
    render_manifest_schema_version: str
    render_manifest_sha256: str
    producer: SuryaProducerIdentity
    requested_pages: tuple[int, ...]
    pages: tuple[SuryaLayoutPage, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SuryaLayoutArtifactError(f"schema_version must be {SCHEMA_VERSION!r}")
        object.__setattr__(self, "logical_compute_key", _require_sha256("logical_compute_key", self.logical_compute_key))
        object.__setattr__(self, "source_sha256", _require_sha256("source_sha256", self.source_sha256))
        object.__setattr__(self, "page_count", _positive_int("page_count", self.page_count))
        object.__setattr__(self, "render_manifest_schema_version", _require_nonempty_string("render_manifest_schema_version", self.render_manifest_schema_version))
        object.__setattr__(self, "render_manifest_sha256", _require_sha256("render_manifest_sha256", self.render_manifest_sha256))
        if not isinstance(self.producer, SuryaProducerIdentity):
            raise SuryaLayoutArtifactError("producer must be a SuryaProducerIdentity")
        if not isinstance(self.requested_pages, tuple) or not self.requested_pages:
            raise SuryaLayoutArtifactError("requested_pages must be a non-empty tuple")
        if len(self.requested_pages) > MAX_REQUESTED_PAGES:
            raise SuryaLayoutArtifactError("requested_pages exceeds the page cap")
        requested = tuple(_positive_int(f"requested_pages[{index}]", page) for index, page in enumerate(self.requested_pages))
        if tuple(sorted(requested)) != requested or len(set(requested)) != len(requested):
            raise SuryaLayoutArtifactError("requested_pages must be strictly ascending and unique")
        if requested[-1] > self.page_count:
            raise SuryaLayoutArtifactError("requested_pages must not exceed page_count")
        object.__setattr__(self, "requested_pages", requested)
        if not isinstance(self.pages, tuple) or any(not isinstance(page, SuryaLayoutPage) for page in self.pages):
            raise SuryaLayoutArtifactError("pages must be a tuple of SuryaLayoutPage values")
        if tuple(page.page for page in self.pages) != requested:
            raise SuryaLayoutArtifactError("pages must cover requested_pages exactly once and in order")
        if sum(len(page.regions) for page in self.pages) > MAX_TOTAL_REGIONS:
            raise SuryaLayoutArtifactError("artifact exceeds the total region cap")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SuryaLayoutArtifact":
        if not isinstance(value, Mapping):
            raise SuryaLayoutArtifactError("artifact must be an object")
        _assert_json_limits(value)
        _assert_artifact_collection_limits(value)
        _assert_exact_keys(value, _ARTIFACT_KEYS, name="artifact")
        if not isinstance(value["requested_pages"], list) or not isinstance(value["pages"], list):
            raise SuryaLayoutArtifactError("requested_pages and pages must be arrays")
        return cls(
            schema_version=value["schema_version"], logical_compute_key=value["logical_compute_key"], source_sha256=value["source_sha256"],
            page_count=value["page_count"], render_manifest_schema_version=value["render_manifest_schema_version"],
            render_manifest_sha256=value["render_manifest_sha256"], producer=SuryaProducerIdentity.from_dict(value["producer"]),
            requested_pages=tuple(value["requested_pages"]), pages=tuple(SuryaLayoutPage.from_dict(page) for page in value["pages"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "logical_compute_key": self.logical_compute_key,
            "source_sha256": self.source_sha256,
            "page_count": self.page_count,
            "render_manifest_schema_version": self.render_manifest_schema_version,
            "render_manifest_sha256": self.render_manifest_sha256,
            "producer": self.producer.to_dict(),
            "requested_pages": list(self.requested_pages),
            "pages": [page.to_dict() for page in self.pages],
        }

    def canonical_json(self) -> bytes:
        return _canonical_json(self.to_dict())

    def artifact_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()


def validate_surya_layout_artifact(
    artifact: SuryaLayoutArtifact | Mapping[str, Any],
    *,
    render_manifest: PdfRenderManifest,
    expected_logical_compute_key: str,
    expected_producer: SuryaProducerIdentity,
    expected_requested_pages: Sequence[int],
) -> SuryaLayoutArtifact:
    """Bind a parsed artifact to trusted local render metadata and identity.

    The remote artifact cannot select a PDF, page image, coordinate transform,
    model revision, or logical computation.  All are compared against local
    values at this acceptance boundary.  Both expected values are mandatory:
    unbound JSON can be inspected with :meth:`SuryaLayoutArtifact.from_dict`,
    but it is not accepted as an artifact.
    """
    if isinstance(artifact, Mapping):
        artifact = SuryaLayoutArtifact.from_dict(artifact)
    if not isinstance(artifact, SuryaLayoutArtifact):
        raise SuryaLayoutArtifactError("artifact must be a SuryaLayoutArtifact or object")
    if not isinstance(render_manifest, PdfRenderManifest):
        raise SuryaLayoutArtifactError("render_manifest must be a PdfRenderManifest")
    if artifact.source_sha256 != render_manifest.source_pdf_sha256:
        raise SuryaLayoutArtifactError("source_sha256 does not match trusted render_manifest")
    if artifact.page_count != render_manifest.page_count:
        raise SuryaLayoutArtifactError("page_count does not match trusted render_manifest")
    if artifact.render_manifest_schema_version != render_manifest.schema_version:
        raise SuryaLayoutArtifactError("render_manifest_schema_version does not match trusted render_manifest")
    if artifact.render_manifest_sha256 != render_manifest.manifest_sha256():
        raise SuryaLayoutArtifactError("render_manifest_sha256 does not match trusted render_manifest")
    if artifact.logical_compute_key != _require_sha256("expected_logical_compute_key", expected_logical_compute_key):
        raise SuryaLayoutArtifactError("logical_compute_key does not match requested computation")
    if not isinstance(expected_producer, SuryaProducerIdentity):
        raise SuryaLayoutArtifactError("expected_producer must be a SuryaProducerIdentity")
    if artifact.producer != expected_producer:
        raise SuryaLayoutArtifactError("producer identity does not match requested computation")
    if isinstance(expected_requested_pages, (str, bytes)):
        raise SuryaLayoutArtifactError("expected_requested_pages must be a page sequence")
    expected_pages = tuple(
        _positive_int(f"expected_requested_pages[{index}]", page)
        for index, page in enumerate(expected_requested_pages)
    )
    if (
        not expected_pages
        or len(expected_pages) > MAX_REQUESTED_PAGES
        or tuple(sorted(expected_pages)) != expected_pages
        or len(set(expected_pages)) != len(expected_pages)
    ):
        raise SuryaLayoutArtifactError("expected_requested_pages must be non-empty, strictly ascending, unique, and within caps")
    if artifact.requested_pages != expected_pages:
        raise SuryaLayoutArtifactError("requested_pages does not match requested computation")
    coordinates_by_page = {rendered.page: rendered.coordinate_manifest for rendered in render_manifest.pages}
    for page in artifact.pages:
        coordinate = coordinates_by_page.get(page.page)
        if coordinate is None:
            raise SuryaLayoutArtifactError("artifact page is absent from trusted render_manifest")
        _validate_page_binding(page, coordinate)
    return artifact


def _validate_page_binding(page: SuryaLayoutPage, coordinate: PdfCoordinateManifest) -> None:
    try:
        validate_sidecar_binding(page.sidecar_binding, coordinate)
    except CoordinateManifestError as error:
        raise SuryaLayoutArtifactError(str(error)) from error
    if page.pixel_width != coordinate.rendered_width_px or page.pixel_height != coordinate.rendered_height_px:
        raise SuryaLayoutArtifactError("page pixel dimensions do not match trusted coordinate_manifest")
    expected_bounds = coordinate.page_bounds_pixel
    if page.rendered_page_px != expected_bounds:
        raise SuryaLayoutArtifactError("page rendered_page_px does not match trusted coordinate_manifest")
    for region in page.regions:
        try:
            user_bbox = pixel_to_user_bbox(coordinate, region.bbox_px)
            round_trip = user_to_pixel_bbox(coordinate, user_bbox)
        except CoordinateManifestError as error:
            raise SuryaLayoutArtifactError(f"region {region.region_id} coordinate binding failed: {error}") from error
        if not all(_close(left, right, tolerance=_ROUND_TRIP_EPSILON) for left, right in zip(round_trip, region.bbox_px)):
            raise SuryaLayoutArtifactError(f"region {region.region_id} pixel/user bbox round-trip failed")


def _reject_json_constant(token: str) -> Any:
    raise SuryaLayoutArtifactError(f"non-finite JSON constant is forbidden: {token}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SuryaLayoutArtifactError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _lexical_json_preflight(decoded: str) -> None:
    """Bound structural/value tokens before ``json.loads`` allocates objects.

    This is intentionally not a second JSON parser.  It only scans JSON
    lexical boundaries in deterministic O(n) time, correctly skipping quoted
    braces/brackets and escaped quotes.  Syntax validity remains the job of
    ``json.loads`` below.  Counting every container and quoted/unquoted token
    safely over-counts object keys, which is desirable at this untrusted
    boundary.
    """
    node_count = 0
    depth = 0
    index = 0
    length = len(decoded)

    def count_node() -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_JSON_NODES:
            raise SuryaLayoutArtifactError("artifact JSON exceeds the node/token cap")

    while index < length:
        character = decoded[index]
        if character in " \t\r\n,:":
            index += 1
            continue
        if character == '"':
            count_node()
            index += 1
            while index < length:
                if decoded[index] == "\\":
                    index += 2
                    continue
                if decoded[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if character in "[{":
            count_node()
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise SuryaLayoutArtifactError("artifact JSON exceeds the maximum nesting depth")
            index += 1
            continue
        if character in "]}":
            # Keep malformed closing delimiters for json.loads to diagnose;
            # never let them turn this preflight into a recursive parser.
            if depth > 0:
                depth -= 1
            index += 1
            continue
        count_node()
        index += 1
        while index < length and decoded[index] not in " \t\r\n,:[]{}":
            index += 1


def parse_surya_layout_artifact_bytes(
    raw: bytes,
    *,
    render_manifest: PdfRenderManifest,
    expected_logical_compute_key: str,
    expected_producer: SuryaProducerIdentity,
    expected_requested_pages: Sequence[int],
) -> SuryaLayoutArtifact:
    """Parse only exact canonical UTF-8 JSON then apply local trusted bindings."""
    if not isinstance(raw, bytes) or not raw:
        raise SuryaLayoutArtifactError("artifact raw bytes must be a non-empty bytes value")
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise SuryaLayoutArtifactError("artifact raw bytes exceed the byte cap")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SuryaLayoutArtifactError("artifact raw bytes must be UTF-8 JSON") from error
    if decoded.startswith("\ufeff"):
        raise SuryaLayoutArtifactError("artifact raw bytes must not include a UTF-8 BOM")
    try:
        _lexical_json_preflight(decoded)
    except SuryaLayoutArtifactError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise SuryaLayoutArtifactError("artifact JSON lexical preflight failed") from error
    try:
        value = json.loads(decoded, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    except SuryaLayoutArtifactError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        raise SuryaLayoutArtifactError("artifact raw bytes are not valid JSON") from error
    _assert_json_limits(value)
    if isinstance(value, Mapping):
        _assert_artifact_collection_limits(value)
    artifact = SuryaLayoutArtifact.from_dict(value)
    if raw != artifact.canonical_json():
        raise SuryaLayoutArtifactError("artifact raw bytes are not canonical JSON")
    return validate_surya_layout_artifact(
        artifact,
        render_manifest=render_manifest,
        expected_logical_compute_key=expected_logical_compute_key,
        expected_producer=expected_producer,
        expected_requested_pages=expected_requested_pages,
    )
