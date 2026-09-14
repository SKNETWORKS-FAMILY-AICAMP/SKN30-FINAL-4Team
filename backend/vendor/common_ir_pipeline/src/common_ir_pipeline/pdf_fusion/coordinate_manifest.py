"""Fail-closed coordinate manifests for one rendered PDF page.

PDF extractors report geometry in PDF user space while renderers, OCR and
layout tools generally use pixels.  A ``pdf_coordinate_manifest/v1`` binds
one such rendering to its source PDF and records the *complete* affine
conversion.  Consumers must not infer a transform from DPI or page size:
crop boxes, page rotation and renderer choices make that unsafe.

The module has no PDF/OCR dependency.  It is intended as the small,
deterministic boundary shared by native PDF, OpenDataLoader and visual OCR
adapters before a later Common IR version chooses how to promote evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "pdf_coordinate_manifest/v1"
_SHA256_LENGTH = 64
_EPSILON = 1e-9
_BBOX_EPSILON = 1e-7
_PIXEL_MAPPING_TOLERANCE = 0.51
_USER_ROUND_TRIP_TOLERANCE = 1e-6
_MANIFEST_KEYS = frozenset({
    "schema_version",
    "source_sha256",
    "page",
    "page_count",
    "media_box",
    "crop_box",
    "rotation",
    "user_unit",
    "canonical_width_pt",
    "canonical_height_pt",
    "render_scale_px_per_point",
    "rendered_width_px",
    "rendered_height_px",
    "pdf_origin",
    "pdf_x_axis",
    "pdf_y_axis",
    "pixel_origin",
    "pixel_x_axis",
    "pixel_y_axis",
    "user_to_pixel",
    "pixel_to_user",
    "renderer",
    "renderer_version",
    "renderer_config_sha256",
    "page_image_sha256",
})
_BINDING_KEYS = frozenset({
    "coordinate_manifest_schema_version",
    "coordinate_manifest_sha256",
    "source_sha256",
    "page",
    "page_image_sha256",
})


class CoordinateManifestError(ValueError):
    """Raised when coordinate lineage is incomplete, invalid or tampered."""


def _require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        raise CoordinateManifestError(f"{name} must be a lowercase SHA-256 hex digest")
    if any(character not in "0123456789abcdef" for character in value):
        raise CoordinateManifestError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _require_nonempty_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CoordinateManifestError(f"{name} must be a non-empty string")
    return value


def _finite_number(name: str, value: object) -> float:
    # bool is an int subclass but never a valid coordinate.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CoordinateManifestError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise CoordinateManifestError(f"{name} must be a finite number")
    # Normalise signed zero so semantically identical manifests cannot acquire
    # different canonical hashes solely through ``-0.0`` serialization.
    return 0.0 if result == 0.0 else result


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CoordinateManifestError(f"{name} must be a positive integer")
    return value


def _bbox(name: str, value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise CoordinateManifestError(f"{name} must contain exactly four coordinates")
    x0, y0, x1, y1 = tuple(_finite_number(f"{name}[{index}]", item) for index, item in enumerate(value))
    if not x0 < x1 or not y0 < y1:
        raise CoordinateManifestError(f"{name} must have strictly increasing x/y bounds")
    return (x0, y0, x1, y1)


def _contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float], *, tolerance: float = _BBOX_EPSILON) -> bool:
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    """Canonical bytes used for stable content hashes and sidecar binding.

    ``allow_nan=False`` is intentional: a JSON representation containing
    NaN/Infinity cannot be a trustworthy coordinate contract.
    """
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CoordinateManifestError("manifest is not canonically serializable JSON") from error


@dataclass(frozen=True, slots=True)
class AffineTransform:
    """Two-dimensional affine transform using PDF/SVG six-coefficient order.

    ``(a, b, c, d, e, f)`` maps ``(x, y)`` to
    ``(a*x + c*y + e, b*x + d*y + f)``.
    """

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    def __post_init__(self) -> None:
        for name in ("a", "b", "c", "d", "e", "f"):
            object.__setattr__(self, name, _finite_number(f"affine.{name}", getattr(self, name)))
        if abs(self.determinant) <= _EPSILON:
            raise CoordinateManifestError("affine matrix must be non-singular")

    @classmethod
    def from_sequence(cls, value: object, *, name: str = "affine") -> "AffineTransform":
        if not isinstance(value, (list, tuple)) or len(value) != 6:
            raise CoordinateManifestError(f"{name} must contain exactly six affine coefficients")
        return cls(*(_finite_number(f"{name}[{index}]", item) for index, item in enumerate(value)))

    @property
    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    def apply_point(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.a * x + self.c * y + self.e,
            self.b * x + self.d * y + self.f,
        )

    def apply_bbox(self, bbox: Sequence[float]) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = _bbox("bbox", bbox)
        points = (
            self.apply_point(x0, y0),
            self.apply_point(x0, y1),
            self.apply_point(x1, y0),
            self.apply_point(x1, y1),
        )
        return (
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )

    def inverse(self) -> "AffineTransform":
        determinant = self.determinant
        if abs(determinant) <= _EPSILON:
            raise CoordinateManifestError("affine matrix must be non-singular")
        return AffineTransform(
            self.d / determinant,
            -self.b / determinant,
            -self.c / determinant,
            self.a / determinant,
            (self.c * self.f - self.d * self.e) / determinant,
            (self.b * self.e - self.a * self.f) / determinant,
        )

    def as_list(self) -> list[float]:
        return [self.a, self.b, self.c, self.d, self.e, self.f]


def _assert_inverse_pair(forward: AffineTransform, inverse: AffineTransform) -> None:
    expected = forward.inverse()
    actual = inverse.as_list()
    wanted = expected.as_list()
    if not all(
        math.isclose(left, right, rel_tol=0.0, abs_tol=_USER_ROUND_TRIP_TOLERANCE)
        for left, right in zip(actual, wanted)
    ):
        raise CoordinateManifestError("pixel_to_user must be the inverse of user_to_pixel")


def _assert_rectilinear_transform(transform: AffineTransform) -> None:
    """Reject shears/diagonal rotations that cannot round-trip an axis bbox.

    PDF page rotation is constrained to right angles.  Keeping this invariant
    means converting an axis-aligned user bbox to an axis-aligned pixel bbox
    and back is lossless; accepting a 15-degree affine would silently expand
    the box on every round trip.
    """
    no_swap = abs(transform.b) <= _EPSILON and abs(transform.c) <= _EPSILON
    swapped = abs(transform.a) <= _EPSILON and abs(transform.d) <= _EPSILON
    if not (no_swap or swapped):
        raise CoordinateManifestError("user_to_pixel must be rectilinear for right-angle PDF rotation")


def _assert_point_round_trip(
    forward: AffineTransform,
    inverse: AffineTransform,
    bbox: tuple[float, float, float, float],
) -> None:
    for x, y in ((bbox[0], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[1]), (bbox[2], bbox[3])):
        round_tripped = inverse.apply_point(*forward.apply_point(x, y))
        if not (
            math.isclose(x, round_tripped[0], rel_tol=0.0, abs_tol=_USER_ROUND_TRIP_TOLERANCE)
            and math.isclose(y, round_tripped[1], rel_tol=0.0, abs_tol=_USER_ROUND_TRIP_TOLERANCE)
        ):
            raise CoordinateManifestError("affine matrices fail crop_box point round-trip validation")


def _assert_render_mapping(
    transform: AffineTransform,
    crop_box: tuple[float, float, float, float],
    *,
    rotation: int,
    rendered_width_px: int,
    rendered_height_px: int,
    pixel_y_axis: str,
) -> None:
    """Bind PDF rotation and the complete visible crop to renderer pixels.

    Merely requiring the transformed crop to fit inside the image admits a
    stale scale, padding, or a rotation field that disagrees with the affine
    matrix.  Canonical page renders have no implicit padding: the four crop
    corners must map to the four image corners in the order dictated by the
    PDF page rotation.  A half-pixel tolerance accommodates integer raster
    rounding without weakening the coordinate contract.
    """

    x0, y0, x1, y1 = crop_box
    width = float(rendered_width_px)
    height = float(rendered_height_px)
    pdf_corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    top_left_targets = {
        0: ((0.0, height), (width, height), (0.0, 0.0), (width, 0.0)),
        90: ((0.0, 0.0), (0.0, height), (width, 0.0), (width, height)),
        180: ((width, 0.0), (0.0, 0.0), (width, height), (0.0, height)),
        270: ((width, height), (width, 0.0), (0.0, height), (0.0, 0.0)),
    }
    expected = top_left_targets[rotation]
    if pixel_y_axis == "up":
        expected = tuple((pixel_x, height - pixel_y) for pixel_x, pixel_y in expected)

    for source, target in zip(pdf_corners, expected):
        actual = transform.apply_point(*source)
        if not (
            math.isclose(actual[0], target[0], rel_tol=0.0, abs_tol=_PIXEL_MAPPING_TOLERANCE)
            and math.isclose(actual[1], target[1], rel_tol=0.0, abs_tol=_PIXEL_MAPPING_TOLERANCE)
        ):
            raise CoordinateManifestError(
                "user_to_pixel does not map the complete crop_box with the declared rotation"
            )


@dataclass(frozen=True, slots=True)
class PdfCoordinateManifest:
    """Validated immutable manifest for one PDF page render.

    The supplied affine matrices, rather than a guessed DPI formula, are the
    sole conversion authority.  ``crop_box`` is the admissible native-user
    region because it is the visible page region passed to renderers.
    """

    source_sha256: str
    page: int
    page_count: int
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    user_unit: float
    canonical_width_pt: float
    canonical_height_pt: float
    render_scale_px_per_point: float
    rendered_width_px: int
    rendered_height_px: int
    pdf_origin: str
    pdf_x_axis: str
    pdf_y_axis: str
    pixel_origin: str
    pixel_x_axis: str
    pixel_y_axis: str
    user_to_pixel: AffineTransform
    pixel_to_user: AffineTransform
    renderer: str
    renderer_version: str
    renderer_config_sha256: str
    page_image_sha256: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise CoordinateManifestError(f"schema_version must be {SCHEMA_VERSION!r}")
        object.__setattr__(self, "source_sha256", _require_sha256("source_sha256", self.source_sha256))
        object.__setattr__(self, "page", _positive_int("page", self.page))
        object.__setattr__(self, "page_count", _positive_int("page_count", self.page_count))
        if self.page > self.page_count:
            raise CoordinateManifestError("page must not exceed page_count")
        object.__setattr__(self, "media_box", _bbox("media_box", self.media_box))
        object.__setattr__(self, "crop_box", _bbox("crop_box", self.crop_box))
        if not _contains(self.media_box, self.crop_box):
            raise CoordinateManifestError("crop_box must be contained by media_box")
        if isinstance(self.rotation, bool) or not isinstance(self.rotation, int) or self.rotation not in {0, 90, 180, 270}:
            raise CoordinateManifestError("rotation must be one of 0, 90, 180, 270")
        unit = _finite_number("user_unit", self.user_unit)
        if unit <= 0:
            raise CoordinateManifestError("user_unit must be positive")
        object.__setattr__(self, "user_unit", unit)
        unrotated_width_pt = (self.crop_box[2] - self.crop_box[0]) * unit
        unrotated_height_pt = (self.crop_box[3] - self.crop_box[1]) * unit
        expected_width_pt, expected_height_pt = (
            (unrotated_height_pt, unrotated_width_pt)
            if self.rotation in {90, 270}
            else (unrotated_width_pt, unrotated_height_pt)
        )
        canonical_width_pt = _finite_number("canonical_width_pt", self.canonical_width_pt)
        canonical_height_pt = _finite_number("canonical_height_pt", self.canonical_height_pt)
        if not (
            math.isclose(canonical_width_pt, expected_width_pt, rel_tol=0.0, abs_tol=_USER_ROUND_TRIP_TOLERANCE)
            and math.isclose(canonical_height_pt, expected_height_pt, rel_tol=0.0, abs_tol=_USER_ROUND_TRIP_TOLERANCE)
        ):
            raise CoordinateManifestError(
                "canonical page dimensions must match crop_box, rotation, and user_unit"
            )
        object.__setattr__(self, "canonical_width_pt", canonical_width_pt)
        object.__setattr__(self, "canonical_height_pt", canonical_height_pt)
        render_scale = _finite_number("render_scale_px_per_point", self.render_scale_px_per_point)
        if render_scale <= 0:
            raise CoordinateManifestError("render_scale_px_per_point must be positive")
        object.__setattr__(self, "render_scale_px_per_point", render_scale)
        object.__setattr__(self, "rendered_width_px", _positive_int("rendered_width_px", self.rendered_width_px))
        object.__setattr__(self, "rendered_height_px", _positive_int("rendered_height_px", self.rendered_height_px))
        if self.pdf_origin != "bottom_left" or self.pdf_x_axis != "right" or self.pdf_y_axis != "up":
            raise CoordinateManifestError("PDF user space must declare bottom_left origin, right x-axis, and up y-axis")
        if self.pixel_origin not in {"top_left", "bottom_left"}:
            raise CoordinateManifestError("pixel_origin must be top_left or bottom_left")
        if self.pixel_x_axis != "right":
            raise CoordinateManifestError("pixel_x_axis must be right")
        if self.pixel_y_axis not in {"down", "up"}:
            raise CoordinateManifestError("pixel_y_axis must be down or up")
        if (self.pixel_origin == "top_left") != (self.pixel_y_axis == "down"):
            raise CoordinateManifestError("pixel_origin and pixel_y_axis must describe the same orientation")
        if not isinstance(self.user_to_pixel, AffineTransform) or not isinstance(self.pixel_to_user, AffineTransform):
            raise CoordinateManifestError("user_to_pixel and pixel_to_user must be AffineTransform values")
        _assert_inverse_pair(self.user_to_pixel, self.pixel_to_user)
        _assert_rectilinear_transform(self.user_to_pixel)
        _assert_point_round_trip(self.user_to_pixel, self.pixel_to_user, self.crop_box)
        object.__setattr__(self, "renderer", _require_nonempty_string("renderer", self.renderer))
        object.__setattr__(self, "renderer_version", _require_nonempty_string("renderer_version", self.renderer_version))
        object.__setattr__(self, "renderer_config_sha256", _require_sha256("renderer_config_sha256", self.renderer_config_sha256))
        object.__setattr__(self, "page_image_sha256", _require_sha256("page_image_sha256", self.page_image_sha256))
        # The render must cover the full visible crop.  This catches a stale
        # transform paired with the wrong page image before its sidecar is used.
        rendered_crop = self.user_to_pixel.apply_bbox(self.crop_box)
        pixel_page = (0.0, 0.0, float(self.rendered_width_px), float(self.rendered_height_px))
        if not _contains(pixel_page, rendered_crop):
            raise CoordinateManifestError("user_to_pixel maps crop_box outside rendered page bounds")
        if not (
            abs(self.rendered_width_px - canonical_width_pt * render_scale) <= 1.01
            and abs(self.rendered_height_px - canonical_height_pt * render_scale) <= 1.01
        ):
            raise CoordinateManifestError(
                "rendered dimensions must match canonical page dimensions and render scale"
            )
        _assert_render_mapping(
            self.user_to_pixel,
            self.crop_box,
            rotation=self.rotation,
            rendered_width_px=self.rendered_width_px,
            rendered_height_px=self.rendered_height_px,
            pixel_y_axis=self.pixel_y_axis,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PdfCoordinateManifest":
        if not isinstance(value, Mapping):
            raise CoordinateManifestError("manifest must be an object")
        keys = frozenset(value.keys())
        missing = sorted(_MANIFEST_KEYS - keys)
        extra = sorted(keys - _MANIFEST_KEYS)
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if extra:
                details.append(f"unexpected keys: {', '.join(extra)}")
            raise CoordinateManifestError("manifest keys are invalid (" + "; ".join(details) + ")")
        return cls(
            schema_version=value["schema_version"],
            source_sha256=value["source_sha256"],
            page=value["page"],
            page_count=value["page_count"],
            media_box=_bbox("media_box", value["media_box"]),
            crop_box=_bbox("crop_box", value["crop_box"]),
            rotation=value["rotation"],
            user_unit=value["user_unit"],
            canonical_width_pt=value["canonical_width_pt"],
            canonical_height_pt=value["canonical_height_pt"],
            render_scale_px_per_point=value["render_scale_px_per_point"],
            rendered_width_px=value["rendered_width_px"],
            rendered_height_px=value["rendered_height_px"],
            pdf_origin=value["pdf_origin"],
            pdf_x_axis=value["pdf_x_axis"],
            pdf_y_axis=value["pdf_y_axis"],
            pixel_origin=value["pixel_origin"],
            pixel_x_axis=value["pixel_x_axis"],
            pixel_y_axis=value["pixel_y_axis"],
            user_to_pixel=AffineTransform.from_sequence(value["user_to_pixel"], name="user_to_pixel"),
            pixel_to_user=AffineTransform.from_sequence(value["pixel_to_user"], name="pixel_to_user"),
            renderer=value["renderer"],
            renderer_version=value["renderer_version"],
            renderer_config_sha256=value["renderer_config_sha256"],
            page_image_sha256=value["page_image_sha256"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the exact v1 JSON object in deterministic field/value form."""
        return {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "page": self.page,
            "page_count": self.page_count,
            "media_box": list(self.media_box),
            "crop_box": list(self.crop_box),
            "rotation": self.rotation,
            "user_unit": self.user_unit,
            "canonical_width_pt": self.canonical_width_pt,
            "canonical_height_pt": self.canonical_height_pt,
            "render_scale_px_per_point": self.render_scale_px_per_point,
            "rendered_width_px": self.rendered_width_px,
            "rendered_height_px": self.rendered_height_px,
            "pdf_origin": self.pdf_origin,
            "pdf_x_axis": self.pdf_x_axis,
            "pdf_y_axis": self.pdf_y_axis,
            "pixel_origin": self.pixel_origin,
            "pixel_x_axis": self.pixel_x_axis,
            "pixel_y_axis": self.pixel_y_axis,
            "user_to_pixel": self.user_to_pixel.as_list(),
            "pixel_to_user": self.pixel_to_user.as_list(),
            "renderer": self.renderer,
            "renderer_version": self.renderer_version,
            "renderer_config_sha256": self.renderer_config_sha256,
            "page_image_sha256": self.page_image_sha256,
        }

    def canonical_json(self) -> bytes:
        return _canonical_json(self.to_dict())

    def manifest_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()

    @property
    def crop_bounds_user(self) -> tuple[float, float, float, float]:
        return self.crop_box

    @property
    def page_bounds_pixel(self) -> tuple[float, float, float, float]:
        return (0.0, 0.0, float(self.rendered_width_px), float(self.rendered_height_px))


def user_to_pixel_bbox(manifest: PdfCoordinateManifest, bbox: Sequence[float]) -> tuple[float, float, float, float]:
    """Convert a visible PDF-user-space bbox into a renderer pixel bbox.

    Inputs and outputs are checked against the manifest's crop/render bounds;
    calls therefore fail before a cross-page or wrong-render sidecar can be
    silently aligned.
    """
    if not isinstance(manifest, PdfCoordinateManifest):
        raise CoordinateManifestError("manifest must be a PdfCoordinateManifest")
    source = _bbox("user bbox", bbox)
    if not _contains(manifest.crop_bounds_user, source):
        raise CoordinateManifestError("user bbox lies outside crop_box")
    converted = manifest.user_to_pixel.apply_bbox(source)
    if not _contains(manifest.page_bounds_pixel, converted):
        raise CoordinateManifestError("converted pixel bbox lies outside rendered page")
    return converted


def pixel_to_user_bbox(manifest: PdfCoordinateManifest, bbox: Sequence[float]) -> tuple[float, float, float, float]:
    """Convert a renderer pixel bbox back into visible PDF user space."""
    if not isinstance(manifest, PdfCoordinateManifest):
        raise CoordinateManifestError("manifest must be a PdfCoordinateManifest")
    source = _bbox("pixel bbox", bbox)
    if not _contains(manifest.page_bounds_pixel, source):
        raise CoordinateManifestError("pixel bbox lies outside rendered page")
    converted = manifest.pixel_to_user.apply_bbox(source)
    if not _contains(manifest.crop_bounds_user, converted):
        raise CoordinateManifestError("converted user bbox lies outside crop_box")
    return converted


def build_sidecar_binding(manifest: PdfCoordinateManifest) -> dict[str, Any]:
    """Return the mandatory immutable binding each page-level sidecar carries."""
    if not isinstance(manifest, PdfCoordinateManifest):
        raise CoordinateManifestError("manifest must be a PdfCoordinateManifest")
    return {
        "coordinate_manifest_schema_version": manifest.schema_version,
        "coordinate_manifest_sha256": manifest.manifest_sha256(),
        "source_sha256": manifest.source_sha256,
        "page": manifest.page,
        "page_image_sha256": manifest.page_image_sha256,
    }


def validate_sidecar_binding(binding: Mapping[str, Any], manifest: PdfCoordinateManifest) -> None:
    """Fail closed unless a sidecar names this exact source, page and render.

    The binding intentionally has no optional fields.  A caller must validate
    it before reading sidecar regions; otherwise a page image or a manifest
    generated for a different PDF could be geometrically plausible yet wrong.
    """
    if not isinstance(manifest, PdfCoordinateManifest):
        raise CoordinateManifestError("manifest must be a PdfCoordinateManifest")
    if not isinstance(binding, Mapping):
        raise CoordinateManifestError("sidecar coordinate binding must be an object")
    keys = frozenset(binding.keys())
    missing = sorted(_BINDING_KEYS - keys)
    extra = sorted(keys - _BINDING_KEYS)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected keys: {', '.join(extra)}")
        raise CoordinateManifestError("sidecar coordinate binding keys are invalid (" + "; ".join(details) + ")")
    expected = build_sidecar_binding(manifest)
    for key, expected_value in expected.items():
        if binding[key] != expected_value:
            raise CoordinateManifestError(f"sidecar coordinate binding mismatch: {key}")
