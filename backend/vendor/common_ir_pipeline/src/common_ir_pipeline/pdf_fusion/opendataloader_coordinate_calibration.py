"""Offline coordinate calibration for pinned OpenDataLoader JSON output.

The production OpenDataLoader contract deliberately labels every bounding box
``odl_pdf_points_unverified``.  This module does not weaken that boundary.  It
creates deterministic, synthetic PDFs and evaluates parser output in a
separate, non-promotable proof artifact.  A later reviewed contract version may
use a passing proof; v1 artifacts remain unverified forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping, Sequence

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    CoordinateManifestError,
    PdfCoordinateManifest,
    pixel_to_user_bbox,
)
from common_ir_pipeline.pdf_fusion.opendataloader_artifact import (
    OpenDataLoaderArtifactError,
    canonical_json_bytes as canonical_opendataloader_json_bytes,
    decode_opendataloader_json_bytes,
)
from common_ir_pipeline.workers.pdfium_renderer import render_canonical_pdf


FIXTURE_SCHEMA_VERSION = "opendataloader_coordinate_fixture_suite/v1"
PROOF_SCHEMA_VERSION = "opendataloader_coordinate_calibration/v1"
RUN_MANIFEST_SCHEMA_VERSION = "opendataloader_coordinate_parser_run/v1"
GENERATOR_VERSION = "opendataloader-coordinate-fixture-generator/v1"
PARSER_NAME = "opendataloader-pdf"
PINNED_PARSER_VERSION = "2.5.7"
CONFIG = {"format": "json", "hybrid": "off", "ocr_enabled": False}
CONFIG_SHA256 = sha256(
    json.dumps(CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
NORMALIZED_PARSER_COMMAND = (
    "<java-17>",
    "-Djava.awt.headless=true",
    "-Dapple.awt.UIElement=true",
    "-jar",
    "<opendataloader-pdf-2.5.7.jar>",
    "--hybrid",
    "off",
    "--format",
    "json",
    "--output-dir",
    "<output-dir>",
    "<source-pdf>",
)
NORMALIZED_PARSER_COMMAND_SHA256 = sha256(
    json.dumps(NORMALIZED_PARSER_COMMAND, separators=(",", ":")).encode("utf-8")
).hexdigest()
MAX_CENTER_ERROR_PT = 1.0
MAX_EDGE_ERROR_PT = 1.0
MIN_BBOX_IOU = 0.95
MIN_ANCHORS_PER_CASE = 5
MAX_OCCURRENCES_PER_ANCHOR = 2
MAX_SOURCE_PDF_BYTES = 1024 * 1024
MAX_ABS_COORDINATE = 1_000_000.0
SELECTED_COORDINATE_CONVENTION = "rotated_crop_relative_bottom_left_raw_units"
# Canonical JSON digest of the reviewed 2026-09-19 proof in
# ``backend/baselines/pdf_reconstruction/opendataloader_coordinate_calibration_v1``.
# Updating the proof requires an explicit code review rather than accepting a
# digest derived from the same untrusted payload at runtime.
REVIEWED_PROOF_CANONICAL_SHA256 = "f8c041e14cad1637150165050dc12ff87ef9a752d8192e46e73bda12edf003be"

_SHA256_HEX = frozenset("0123456789abcdef")
_CASE_IDS = frozenset(
    {
        "baseline-r0-u1",
        "crop-r0-u1",
        "crop-r90-u1",
        "crop-r180-u1",
        "crop-r270-u1",
        "crop-r0-u2",
        "combined-r270-u2",
    }
)
_CONVENTIONS = (
    "raw_pdf_user_space_bottom_left",
    "raw_pdf_user_space_top_left_media",
    "crop_relative_bottom_left_raw_units",
    "crop_relative_top_left_raw_units",
    "crop_relative_bottom_left_physical_points",
    "crop_relative_top_left_physical_points",
    "rotated_crop_relative_top_left_raw_units",
    "rotated_crop_relative_bottom_left_raw_units",
    "canonical_rotated_top_left_physical_points",
    "canonical_rotated_bottom_left_physical_points",
)
_SUITE_KEYS = frozenset({"schema_version", "suite_id", "generator_version", "parser_target", "cases"})
_PARSER_TARGET_KEYS = frozenset({"name", "version", "config", "config_sha256"})
_CASE_KEYS = frozenset(
    {
        "case_id",
        "source_pdf_relative_path",
        "source_pdf_sha256",
        "source_pdf_size_bytes",
        "render_manifest_relative_path",
        "render_manifest_sha256",
        "coordinate_manifest_sha256",
        "media_box",
        "crop_box",
        "rotation",
        "user_unit",
        "anchors",
    }
)
_ANCHOR_IDS = frozenset({"north_west", "north_east", "center", "south_west", "south_east"})
_ANCHOR_KEYS = frozenset(
    {"anchor_id", "label", "marker_bbox_user", "expected_text_bbox_user", "expected_text_center_user"}
)
_RUN_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "case_id",
        "fixture_suite_sha256",
        "source_pdf_sha256",
        "source_pdf_size_bytes",
        "coordinate_manifest_sha256",
        "parser_jar_sha256",
        "parser_package_metadata_sha256",
        "parser_config_sha256",
        "java_executable_sha256",
        "java_provided_package_file_sha256",
        "java_version_output_sha256",
        "normalized_command",
        "normalized_command_sha256",
        "raw_output_relative_path",
        "raw_output_sha256",
        "raw_output_size_bytes",
    }
)
_PROOF_KEYS = frozenset(
    {
        "schema_version",
        "fixture_suite_sha256",
        "producer_identity_verified",
        "parser",
        "runtime",
        "gate",
        "status",
        "selected_global_convention",
        "production_coordinate_contract_changed",
        "case_results",
    }
)
_PROOF_PARSER_KEYS = frozenset(
    {
        "name",
        "version",
        "package_metadata_sha256",
        "jar_sha256",
        "config",
        "config_sha256",
        "ocr_enabled",
    }
)
_PROOF_GATE_KEYS = frozenset(
    {
        "max_center_error_pt",
        "max_edge_error_pt",
        "min_bbox_iou",
        "required_anchor_count_per_case",
        "max_occurrences_per_anchor",
        "requires_raw_and_canonical_repeat_identity",
        "requires_verified_run_manifests",
        "requires_one_global_convention",
    }
)


class OpenDataLoaderCoordinateCalibrationError(ValueError):
    """Raised when a fixture or calibration proof is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class CalibrationCaseSpec:
    case_id: str
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    user_unit: float

    def __post_init__(self) -> None:
        if self.case_id not in _CASE_IDS:
            raise OpenDataLoaderCoordinateCalibrationError("unknown calibration case_id")
        for name, box in (("media_box", self.media_box), ("crop_box", self.crop_box)):
            if len(box) != 4 or not all(_is_number(value) for value in box):
                raise OpenDataLoaderCoordinateCalibrationError(f"{name} must contain four finite numbers")
            if not box[0] < box[2] or not box[1] < box[3]:
                raise OpenDataLoaderCoordinateCalibrationError(f"{name} bounds must be increasing")
        if not _contains(self.media_box, self.crop_box):
            raise OpenDataLoaderCoordinateCalibrationError("crop_box must lie within media_box")
        if self.rotation not in {0, 90, 180, 270}:
            raise OpenDataLoaderCoordinateCalibrationError("rotation must be 0/90/180/270")
        if not _is_number(self.user_unit) or self.user_unit <= 0:
            raise OpenDataLoaderCoordinateCalibrationError("user_unit must be positive")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _contains(outer: Sequence[float], inner: Sequence[float], *, tolerance: float = 1e-7) -> bool:
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


CASE_SPECS = (
    CalibrationCaseSpec("baseline-r0-u1", (0, 0, 320, 240), (0, 0, 320, 240), 0, 1),
    CalibrationCaseSpec("crop-r0-u1", (-30, -20, 350, 280), (20, 30, 320, 250), 0, 1),
    CalibrationCaseSpec("crop-r90-u1", (-30, -20, 350, 280), (20, 30, 320, 250), 90, 1),
    CalibrationCaseSpec("crop-r180-u1", (-30, -20, 350, 280), (20, 30, 320, 250), 180, 1),
    CalibrationCaseSpec("crop-r270-u1", (-30, -20, 350, 280), (20, 30, 320, 250), 270, 1),
    CalibrationCaseSpec("crop-r0-u2", (-30, -20, 350, 280), (20, 30, 320, 250), 0, 2),
    CalibrationCaseSpec("combined-r270-u2", (-40, -30, 380, 300), (25, 35, 345, 275), 270, 2),
)


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
        raise OpenDataLoaderCoordinateCalibrationError("value is not canonical JSON") from error


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _require_sha(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise OpenDataLoaderCoordinateCalibrationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _exact_keys(value: object, expected: frozenset[str], *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenDataLoaderCoordinateCalibrationError(f"{name} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise OpenDataLoaderCoordinateCalibrationError(
            f"{name} keys are invalid (missing={missing}, extra={extra})"
        )
    return value


def _pdf(objects: Sequence[bytes]) -> bytes:
    chunks = [b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    for index, object_data in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii") + object_data + b"\nendobj\n")
    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    chunks.extend(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:])
    chunks.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return b"".join(chunks)


def _format_number(value: float) -> str:
    rendered = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered


def _anchor_specs(crop_box: Sequence[float]) -> tuple[dict[str, Any], ...]:
    x0, y0, x1, y1 = (float(value) for value in crop_box)
    width, height = 72.0, 28.0
    margin = 18.0
    placements = (
        ("north_west", "CAL_NW01", x0 + margin, y1 - margin - height),
        ("north_east", "CAL_NE02", x1 - margin - width, y1 - margin - height),
        ("center", "CAL_CT03", (x0 + x1 - width) / 2.0, (y0 + y1 - height) / 2.0),
        ("south_west", "CAL_SW04", x0 + margin, y0 + margin),
        ("south_east", "CAL_SE05", x1 - margin - width, y0 + margin),
    )
    anchors = []
    for anchor_id, label, left, bottom in placements:
        # Courier is fixed-width: 8 ASCII glyphs * 0.6em * 12pt = 57.6pt.
        # The expected box is intentionally a tolerant glyph envelope rather
        # than a claim about OpenDataLoader/PDFBox font metric internals.
        baseline_x, baseline_y = left + 6.0, bottom + 9.0
        text_bbox = [baseline_x, baseline_y - 3.0, baseline_x + 57.6, baseline_y + 9.66]
        anchors.append(
            {
                "anchor_id": anchor_id,
                "label": label,
                "marker_bbox_user": [left, bottom, left + width, bottom + height],
                "expected_text_bbox_user": text_bbox,
                "expected_text_center_user": [
                    (text_bbox[0] + text_bbox[2]) / 2.0,
                    (text_bbox[1] + text_bbox[3]) / 2.0,
                ],
            }
        )
    return tuple(anchors)


def build_calibration_pdf(spec: CalibrationCaseSpec) -> tuple[bytes, tuple[dict[str, Any], ...]]:
    """Build one deterministic single-page PDF with five labelled anchors."""
    if not isinstance(spec, CalibrationCaseSpec):
        raise OpenDataLoaderCoordinateCalibrationError("spec must be CalibrationCaseSpec")
    anchors = _anchor_specs(spec.crop_box)
    operations: list[str] = ["q 0.85 G 0.6 w"]
    for anchor in anchors:
        left, bottom, right, top = anchor["marker_bbox_user"]
        operations.append(
            " ".join(
                (
                    _format_number(left),
                    _format_number(bottom),
                    _format_number(right - left),
                    _format_number(top - bottom),
                    "re S",
                )
            )
        )
        expected_text_bbox = anchor["expected_text_bbox_user"]
        baseline_x = float(expected_text_bbox[0])
        baseline_y = float(expected_text_bbox[1]) + 3.0
        operations.append(
            f"BT /F1 12 Tf 1 0 0 1 {_format_number(baseline_x)} {_format_number(baseline_y)} Tm "
            f"({anchor['label']}) Tj ET"
        )
    operations.append("Q")
    content = "\n".join(operations).encode("ascii")
    media = " ".join(_format_number(value) for value in spec.media_box)
    crop = " ".join(_format_number(value) for value in spec.crop_box)
    pages = (
        f"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [{media}] /CropBox [{crop}] "
        f"/Rotate {spec.rotation} >>"
    ).encode("ascii")
    page = (
        b"<< /Type /Page /Parent 2 0 R "
        + f"/UserUnit {_format_number(spec.user_unit)} ".encode("ascii")
        + b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    stream = b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream"
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"
    return _pdf((b"<< /Type /Catalog /Pages 2 0 R >>", pages, page, stream, font)), anchors


def _write_exclusive(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise OpenDataLoaderCoordinateCalibrationError(f"failed to publish {path.name}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def generate_fixture_suite(output_root: str | Path) -> dict[str, Any]:
    """Generate PDFs, canonical renders and an immutable fixture manifest."""
    root = Path(output_root)
    try:
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError as error:
        raise OpenDataLoaderCoordinateCalibrationError("fixture output root must not already exist") from error
    case_rows: list[dict[str, Any]] = []
    for spec in CASE_SPECS:
        case_root = root / "cases" / spec.case_id
        (case_root / "source").mkdir(mode=0o700, parents=True)
        pdf_bytes, anchors = build_calibration_pdf(spec)
        pdf_path = case_root / "source" / "calibration.pdf"
        _write_exclusive(pdf_path, pdf_bytes)
        manifest = render_canonical_pdf(artifact_root=case_root, source_pdf_path=pdf_path)
        coordinate = manifest.pages[0].coordinate_manifest
        if (
            tuple(coordinate.media_box) != tuple(float(value) for value in spec.media_box)
            or tuple(coordinate.crop_box) != tuple(float(value) for value in spec.crop_box)
            or coordinate.rotation != spec.rotation
            or coordinate.user_unit != float(spec.user_unit)
        ):
            raise OpenDataLoaderCoordinateCalibrationError("renderer geometry disagrees with fixture specification")
        case_rows.append(
            {
                "case_id": spec.case_id,
                "source_pdf_relative_path": f"cases/{spec.case_id}/source/calibration.pdf",
                "source_pdf_sha256": _digest(pdf_bytes),
                "source_pdf_size_bytes": len(pdf_bytes),
                "render_manifest_relative_path": f"cases/{spec.case_id}/render_manifest.json",
                "render_manifest_sha256": manifest.manifest_sha256(),
                "coordinate_manifest_sha256": coordinate.manifest_sha256(),
                "media_box": list(spec.media_box),
                "crop_box": list(spec.crop_box),
                "rotation": spec.rotation,
                "user_unit": spec.user_unit,
                "anchors": list(anchors),
            }
        )
    suite = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "suite_id": "odl-coordinate-calibration-rotate-crop-userunit-v1",
        "generator_version": GENERATOR_VERSION,
        "parser_target": {
            "name": PARSER_NAME,
            "version": PINNED_PARSER_VERSION,
            "config": CONFIG,
            "config_sha256": CONFIG_SHA256,
        },
        "cases": case_rows,
    }
    manifest_bytes = _canonical_json(suite)
    _write_exclusive(root / "fixture_suite.json", manifest_bytes)
    return suite


def _bbox(value: object, *, name: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(_is_number(item) for item in value):
        raise OpenDataLoaderCoordinateCalibrationError(f"{name} must contain four finite coordinates")
    result = tuple(float(item) for item in value)
    if any(abs(item) > MAX_ABS_COORDINATE for item in result):
        raise OpenDataLoaderCoordinateCalibrationError(f"{name} exceeds the coordinate safety bound")
    if not result[0] < result[2] or not result[1] < result[3]:
        raise OpenDataLoaderCoordinateCalibrationError(f"{name} bounds must be increasing")
    return result  # type: ignore[return-value]


def _point(value: object, *, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(_is_number(item) for item in value):
        raise OpenDataLoaderCoordinateCalibrationError(f"{name} must contain two finite coordinates")
    return (float(value[0]), float(value[1]))


def _iter_nodes(value: object) -> Iterable[Mapping[str, Any]]:
    stack = [value]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen:
                raise OpenDataLoaderCoordinateCalibrationError("ODL object alias/cycle is not allowed")
            seen.add(identity)
            yield current
            stack.extend(reversed(tuple(current.values())))
        elif isinstance(current, (tuple, list)):
            stack.extend(reversed(current))


def _observed_anchors(
    document: Mapping[str, Any],
    anchors: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, tuple[float, float, float, float]], dict[str, int]]:
    wanted = {anchor["label"]: anchor["anchor_id"] for anchor in anchors}
    found: dict[str, tuple[float, float, float, float]] = {}
    occurrences: dict[str, int] = {str(anchor["anchor_id"]): 0 for anchor in anchors}
    for node in _iter_nodes(document):
        content = node.get("content")
        if content not in wanted:
            continue
        anchor_id = wanted[content]
        occurrences[anchor_id] += 1
        if occurrences[anchor_id] > MAX_OCCURRENCES_PER_ANCHOR:
            raise OpenDataLoaderCoordinateCalibrationError(f"ODL output over-duplicates anchor {anchor_id}")
        if node.get("page number") != 1:
            raise OpenDataLoaderCoordinateCalibrationError(f"ODL anchor {anchor_id} is on the wrong page")
        candidate = _bbox(node.get("bounding box"), name=f"ODL anchor {anchor_id} bbox")
        if anchor_id in found:
            # OpenDataLoader 2.5.7 may emit two structural paragraph objects
            # for one rotated text run.  Exact geometric duplicates carry no
            # extra coordinate information and are safe to collapse here;
            # conflicting duplicates remain fail-closed.
            if found[anchor_id] == candidate:
                continue
            raise OpenDataLoaderCoordinateCalibrationError(f"ODL output duplicates anchor {anchor_id}")
        found[anchor_id] = candidate
    if set(found) != {anchor["anchor_id"] for anchor in anchors}:
        missing = sorted({anchor["anchor_id"] for anchor in anchors} - set(found))
        raise OpenDataLoaderCoordinateCalibrationError(f"ODL output is missing calibration anchors: {missing}")
    return found, occurrences


def _bbox_iou(left: Sequence[float], right: Sequence[float]) -> float:
    intersection_width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    intersection_height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    intersection = intersection_width * intersection_height
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _map_bbox(
    convention: str,
    bbox: Sequence[float],
    *,
    coordinate: PdfCoordinateManifest,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = _bbox(bbox, name="ODL bbox")
    media_x0, media_y0, media_x1, media_y1 = coordinate.media_box
    crop_x0, crop_y0, crop_x1, crop_y1 = coordinate.crop_box
    unit = coordinate.user_unit
    if convention == "raw_pdf_user_space_bottom_left":
        return (x0, y0, x1, y1)
    if convention == "raw_pdf_user_space_top_left_media":
        return (x0, media_y0 + media_y1 - y1, x1, media_y0 + media_y1 - y0)
    if convention == "crop_relative_bottom_left_raw_units":
        return (crop_x0 + x0, crop_y0 + y0, crop_x0 + x1, crop_y0 + y1)
    if convention == "crop_relative_top_left_raw_units":
        return (crop_x0 + x0, crop_y1 - y1, crop_x0 + x1, crop_y1 - y0)
    if convention == "crop_relative_bottom_left_physical_points":
        return (crop_x0 + x0 / unit, crop_y0 + y0 / unit, crop_x0 + x1 / unit, crop_y0 + y1 / unit)
    if convention == "crop_relative_top_left_physical_points":
        return (crop_x0 + x0 / unit, crop_y1 - y1 / unit, crop_x0 + x1 / unit, crop_y1 - y0 / unit)
    if convention in {
        "rotated_crop_relative_top_left_raw_units",
        "rotated_crop_relative_bottom_left_raw_units",
        "canonical_rotated_top_left_physical_points",
        "canonical_rotated_bottom_left_physical_points",
    }:
        uses_raw_units = convention.endswith("raw_units")
        canonical_height = coordinate.canonical_height_pt / unit if uses_raw_units else coordinate.canonical_height_pt
        if "bottom_left" in convention:
            y0, y1 = canonical_height - y1, canonical_height - y0
        scale = coordinate.render_scale_px_per_point * (unit if uses_raw_units else 1.0)
        return pixel_to_user_bbox(coordinate, (x0 * scale, y0 * scale, x1 * scale, y1 * scale))
    raise OpenDataLoaderCoordinateCalibrationError("unsupported coordinate convention")


def calibration_proof_sha256(proof: Mapping[str, Any]) -> str:
    """Return the canonical digest used to bind one reviewed proof.

    The pretty-printed JSON file digest is intentionally not used here.  A
    proof is a JSON contract, so insignificant whitespace and key order must
    not alter its identity.
    """

    if not isinstance(proof, Mapping):
        raise OpenDataLoaderCoordinateCalibrationError("calibration proof must be an object")
    return _digest(_canonical_json(proof))


def validate_calibration_proof_for_projection(
    proof: Mapping[str, Any],
    *,
    expected_proof_sha256: str,
) -> str:
    """Validate a reviewed calibration proof for an offline projection.

    This does **not** promote ``opendataloader_artifact/v1`` coordinates to a
    production contract.  It only authorizes the selected conversion inside
    an explicitly evaluation-only reconstruction plan.  The caller must pin
    the canonical proof digest; accepting a digest computed from the same
    untrusted input would provide no trust boundary.
    """

    expected = _require_sha("expected_proof_sha256", expected_proof_sha256)
    proof = _exact_keys(proof, _PROOF_KEYS, name="calibration proof")
    observed = calibration_proof_sha256(proof)
    if observed != expected:
        raise OpenDataLoaderCoordinateCalibrationError(
            "calibration proof does not match the reviewed canonical digest"
        )
    if (
        proof.get("schema_version") != PROOF_SCHEMA_VERSION
        or proof.get("producer_identity_verified") is not True
        or proof.get("status") != "passed"
        or proof.get("selected_global_convention") != SELECTED_COORDINATE_CONVENTION
        or proof.get("production_coordinate_contract_changed") is not False
    ):
        raise OpenDataLoaderCoordinateCalibrationError(
            "calibration proof is not the passing shadow-only v1 contract"
        )

    parser = _exact_keys(proof.get("parser"), _PROOF_PARSER_KEYS, name="calibration proof parser")
    if (
        parser.get("name") != PARSER_NAME
        or parser.get("version") != PINNED_PARSER_VERSION
        or parser.get("config") != CONFIG
        or parser.get("config_sha256") != CONFIG_SHA256
        or parser.get("ocr_enabled") is not False
    ):
        raise OpenDataLoaderCoordinateCalibrationError("calibration proof parser identity is invalid")
    _require_sha("calibration proof parser package metadata", parser.get("package_metadata_sha256"))
    _require_sha("calibration proof parser jar", parser.get("jar_sha256"))

    expected_gate = {
        "max_center_error_pt": MAX_CENTER_ERROR_PT,
        "max_edge_error_pt": MAX_EDGE_ERROR_PT,
        "min_bbox_iou": MIN_BBOX_IOU,
        "required_anchor_count_per_case": MIN_ANCHORS_PER_CASE,
        "max_occurrences_per_anchor": MAX_OCCURRENCES_PER_ANCHOR,
        "requires_raw_and_canonical_repeat_identity": True,
        "requires_verified_run_manifests": True,
        "requires_one_global_convention": True,
    }
    gate = _exact_keys(proof.get("gate"), _PROOF_GATE_KEYS, name="calibration proof gate")
    if dict(gate) != expected_gate:
        raise OpenDataLoaderCoordinateCalibrationError("calibration proof gate is invalid")

    results = proof.get("case_results")
    if not isinstance(results, list) or len(results) != len(_CASE_IDS):
        raise OpenDataLoaderCoordinateCalibrationError("calibration proof must contain all seven cases")
    case_ids: set[str] = set()
    common_conventions = set(_CONVENTIONS)
    for result in results:
        if not isinstance(result, Mapping):
            raise OpenDataLoaderCoordinateCalibrationError("calibration proof case result must be an object")
        case_id = result.get("case_id")
        if case_id not in _CASE_IDS or case_id in case_ids:
            raise OpenDataLoaderCoordinateCalibrationError("calibration proof case identities are invalid")
        case_ids.add(str(case_id))
        passing_conventions = result.get("passing_conventions")
        if (
            result.get("status") != "passed"
            or result.get("deterministic_replay") is not True
            or result.get("matched_anchor_count") != MIN_ANCHORS_PER_CASE
            or not isinstance(passing_conventions, list)
            or SELECTED_COORDINATE_CONVENTION not in passing_conventions
            or result.get("passing_convention_count") != len(passing_conventions)
        ):
            raise OpenDataLoaderCoordinateCalibrationError("calibration proof case did not pass the selected convention")
        common_conventions.intersection_update(passing_conventions)
    if case_ids != _CASE_IDS:
        raise OpenDataLoaderCoordinateCalibrationError("calibration proof case set is incomplete")
    if common_conventions != {SELECTED_COORDINATE_CONVENTION}:
        raise OpenDataLoaderCoordinateCalibrationError("calibration proof does not select one global convention")
    return SELECTED_COORDINATE_CONVENTION


def project_odl_bbox_to_pdf_user_space(
    bbox: Sequence[float],
    *,
    coordinate: PdfCoordinateManifest,
    calibration_proof: Mapping[str, Any],
    expected_proof_sha256: str,
) -> tuple[float, float, float, float]:
    """Map an ODL bbox for an offline, non-promotable shadow evaluation.

    The proof is revalidated on every public call.  Callers processing many
    boxes may cache the returned convention internally only after invoking
    :func:`validate_calibration_proof_for_projection` at their boundary.
    """

    convention = validate_calibration_proof_for_projection(
        calibration_proof,
        expected_proof_sha256=expected_proof_sha256,
    )
    return project_odl_bbox_with_validated_convention(
        bbox,
        coordinate=coordinate,
        validated_convention=convention,
    )


def project_odl_bbox_with_validated_convention(
    bbox: Sequence[float],
    *,
    coordinate: PdfCoordinateManifest,
    validated_convention: str,
) -> tuple[float, float, float, float]:
    """Map one bbox after the caller validated the reviewed proof once.

    This is an optimization boundary for a bounded batch.  It accepts only
    the one convention selected by the v1 proof; callers must obtain that
    value from :func:`validate_calibration_proof_for_projection`, not from an
    ODL artifact field.
    """

    if validated_convention != SELECTED_COORDINATE_CONVENTION:
        raise OpenDataLoaderCoordinateCalibrationError("ODL coordinate convention is not the reviewed selection")
    try:
        mapped = _map_bbox(validated_convention, bbox, coordinate=coordinate)
    except CoordinateManifestError as error:
        raise OpenDataLoaderCoordinateCalibrationError("coordinate manifest rejected ODL bbox") from error
    if not _contains(coordinate.crop_box, mapped, tolerance=1e-5):
        raise OpenDataLoaderCoordinateCalibrationError("projected ODL bbox lies outside the visible CropBox")
    return mapped


def _score_convention(
    convention: str,
    observed: Mapping[str, Sequence[float]],
    anchors: Sequence[Mapping[str, Any]],
    *,
    coordinate: PdfCoordinateManifest,
) -> dict[str, Any]:
    center_errors: list[float] = []
    edge_errors: list[float] = []
    ious: list[float] = []
    mapped: list[dict[str, Any]] = []
    try:
        for anchor in anchors:
            anchor_id = str(anchor["anchor_id"])
            converted = _map_bbox(convention, observed[anchor_id], coordinate=coordinate)
            center = ((converted[0] + converted[2]) / 2.0, (converted[1] + converted[3]) / 2.0)
            expected = _point(anchor["expected_text_center_user"], name="expected_text_center_user")
            expected_bbox = _bbox(anchor["expected_text_bbox_user"], name="expected_text_bbox_user")
            # User-space coordinates are measured in default user units.
            # /UserUnit converts those distances to physical PDF points.
            center_error = math.hypot(center[0] - expected[0], center[1] - expected[1]) * coordinate.user_unit
            edge_error = max(abs(converted[index] - expected_bbox[index]) for index in range(4)) * coordinate.user_unit
            iou = _bbox_iou(converted, expected_bbox)
            center_errors.append(center_error)
            edge_errors.append(edge_error)
            ious.append(iou)
            mapped.append(
                {
                    "anchor_id": anchor_id,
                    "mapped_bbox_user": [round(value, 6) for value in converted],
                    "expected_bbox_user": [round(value, 6) for value in expected_bbox],
                    "center_error_pt": round(center_error, 6),
                    "max_edge_error_pt": round(edge_error, 6),
                    "bbox_iou": round(iou, 6),
                }
            )
    except (OpenDataLoaderCoordinateCalibrationError, CoordinateManifestError) as error:
        # CoordinateManifestError is intentionally rendered as a rejected
        # hypothesis rather than escaping and hiding all other candidates.
        return {
            "convention": convention,
            "status": "rejected",
            "reason": str(error),
            "mean_center_error_pt": None,
            "max_center_error_pt": None,
            "max_edge_error_pt": None,
            "min_bbox_iou": None,
            "mapped_anchors": [],
        }
    maximum_center = max(center_errors)
    maximum_edge = max(edge_errors)
    minimum_iou = min(ious)
    passed = (
        maximum_center <= MAX_CENTER_ERROR_PT
        and maximum_edge <= MAX_EDGE_ERROR_PT
        and minimum_iou >= MIN_BBOX_IOU
    )
    return {
        "convention": convention,
        "status": "passed" if passed else "rejected",
        "reason": None if passed else "anchor_bbox_error_exceeds_gate",
        "mean_center_error_pt": round(sum(center_errors) / len(center_errors), 6),
        "max_center_error_pt": round(maximum_center, 6),
        "max_edge_error_pt": round(maximum_edge, 6),
        "min_bbox_iou": round(minimum_iou, 6),
        "mapped_anchors": mapped,
    }


def evaluate_calibration_case(
    case: Mapping[str, Any],
    coordinate: PdfCoordinateManifest,
    raw_run_1: bytes,
    raw_run_2: bytes,
) -> dict[str, Any]:
    """Evaluate one case and require byte/canonical deterministic replay."""
    try:
        document_1 = decode_opendataloader_json_bytes(raw_run_1, role="ODL calibration run 1")
        document_2 = decode_opendataloader_json_bytes(raw_run_2, role="ODL calibration run 2")
    except OpenDataLoaderArtifactError as error:
        raise OpenDataLoaderCoordinateCalibrationError(str(error)) from error
    canonical_1 = canonical_opendataloader_json_bytes(document_1)
    canonical_2 = canonical_opendataloader_json_bytes(document_2)
    deterministic = raw_run_1 == raw_run_2 and canonical_1 == canonical_2
    anchors = case.get("anchors")
    if not isinstance(anchors, (list, tuple)) or len(anchors) < MIN_ANCHORS_PER_CASE:
        raise OpenDataLoaderCoordinateCalibrationError("fixture case has too few anchors")
    observed, occurrences = _observed_anchors(document_1, anchors)
    scores = [
        _score_convention(convention, observed, anchors, coordinate=coordinate)
        for convention in _CONVENTIONS
    ]
    passing = [score for score in scores if score["status"] == "passed"]
    passing.sort(key=lambda score: (score["max_center_error_pt"], score["mean_center_error_pt"], score["convention"]))
    selected = passing[0]["convention"] if len(passing) == 1 else None
    # A deliberately simple baseline case cannot distinguish crop-relative
    # from media-relative coordinates.  Ambiguity is resolved by the global
    # intersection across the entire Rotate/CropBox/UserUnit suite.
    status = "passed" if deterministic and passing else "failed"
    return {
        "case_id": case.get("case_id"),
        "status": status,
        "source_pdf_sha256": _require_sha("source_pdf_sha256", case.get("source_pdf_sha256")),
        "coordinate_manifest_sha256": coordinate.manifest_sha256(),
        "raw_run_1_sha256": _digest(raw_run_1),
        "raw_run_2_sha256": _digest(raw_run_2),
        "canonical_run_1_sha256": _digest(canonical_1),
        "canonical_run_2_sha256": _digest(canonical_2),
        "deterministic_replay": deterministic,
        "matched_anchor_count": len(observed),
        "observed_anchor_occurrence_count": sum(occurrences.values()),
        "duplicate_anchor_occurrence_count": sum(count - 1 for count in occurrences.values()),
        "selected_convention": selected,
        "passing_convention_count": len(passing),
        "passing_conventions": [score["convention"] for score in passing],
        "hypotheses": scores,
    }


def validate_fixture_suite(suite: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Validate the complete generated suite before any filesystem side effect.

    The calibration oracle is deliberately fixed, not user-extensible.  Every
    case, path, source PDF byte sequence and anchor must match this generator.
    That prevents a modified suite from proving a convention chosen by a
    modified oracle instead of by the PDF that OpenDataLoader parsed.
    """
    suite = _exact_keys(suite, _SUITE_KEYS, name="fixture suite")
    if suite.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise OpenDataLoaderCoordinateCalibrationError("fixture suite schema_version is unsupported")
    if suite.get("suite_id") != "odl-coordinate-calibration-rotate-crop-userunit-v1":
        raise OpenDataLoaderCoordinateCalibrationError("fixture suite_id is unsupported")
    if suite.get("generator_version") != GENERATOR_VERSION:
        raise OpenDataLoaderCoordinateCalibrationError("fixture generator_version is unsupported")
    parser_target = _exact_keys(suite.get("parser_target"), _PARSER_TARGET_KEYS, name="parser_target")
    if parser_target != {
        "name": PARSER_NAME,
        "version": PINNED_PARSER_VERSION,
        "config": CONFIG,
        "config_sha256": CONFIG_SHA256,
    }:
        raise OpenDataLoaderCoordinateCalibrationError("fixture parser_target is not the pinned OCR-off configuration")
    cases = suite.get("cases")
    if not isinstance(cases, (list, tuple)) or len(cases) != len(CASE_SPECS):
        raise OpenDataLoaderCoordinateCalibrationError("fixture suite does not contain the exact v1 case set")
    validated: list[Mapping[str, Any]] = []
    for case, spec in zip(cases, CASE_SPECS, strict=True):
        case = _exact_keys(case, _CASE_KEYS, name="fixture case")
        expected_pdf, expected_anchors = build_calibration_pdf(spec)
        expected_values = {
            "case_id": spec.case_id,
            "source_pdf_relative_path": f"cases/{spec.case_id}/source/calibration.pdf",
            "source_pdf_sha256": _digest(expected_pdf),
            "source_pdf_size_bytes": len(expected_pdf),
            "render_manifest_relative_path": f"cases/{spec.case_id}/render_manifest.json",
            "media_box": list(spec.media_box),
            "crop_box": list(spec.crop_box),
            "rotation": spec.rotation,
            "user_unit": spec.user_unit,
            "anchors": list(expected_anchors),
        }
        for key, expected in expected_values.items():
            if case.get(key) != expected:
                raise OpenDataLoaderCoordinateCalibrationError(
                    f"fixture {spec.case_id} {key} disagrees with the deterministic generator"
                )
        if len(expected_pdf) > MAX_SOURCE_PDF_BYTES:
            raise OpenDataLoaderCoordinateCalibrationError("generated source PDF exceeds the safety cap")
        _require_sha("case.render_manifest_sha256", case.get("render_manifest_sha256"))
        _require_sha("case.coordinate_manifest_sha256", case.get("coordinate_manifest_sha256"))
        for anchor in expected_anchors:
            if anchor["anchor_id"] not in _ANCHOR_IDS:
                raise OpenDataLoaderCoordinateCalibrationError("fixture anchor_id is unsupported")
            _bbox(anchor["marker_bbox_user"], name="anchor.marker_bbox_user")
            _bbox(anchor["expected_text_bbox_user"], name="anchor.expected_text_bbox_user")
            _point(anchor["expected_text_center_user"], name="anchor.expected_text_center_user")
        validated.append(case)
    return tuple(validated)


def build_parser_run_manifest(
    *,
    run_id: str,
    case: Mapping[str, Any],
    fixture_suite_sha256: str,
    raw_output: bytes,
    parser_jar_sha256: str,
    parser_package_metadata_sha256: str,
    java_executable_sha256: str,
    java_provided_package_file_sha256: str,
    java_version_output: str,
) -> dict[str, Any]:
    """Bind one parser output to its source, command and producer identity."""
    if run_id not in {"run-1", "run-2"}:
        raise OpenDataLoaderCoordinateCalibrationError("parser run_id is unsupported")
    case_id = case.get("case_id")
    if case_id not in _CASE_IDS:
        raise OpenDataLoaderCoordinateCalibrationError("parser run case_id is unsupported")
    if not isinstance(raw_output, bytes) or not raw_output:
        raise OpenDataLoaderCoordinateCalibrationError("parser raw output must be non-empty bytes")
    if not isinstance(java_version_output, str) or not java_version_output:
        raise OpenDataLoaderCoordinateCalibrationError("java version output is required")
    source_size = case.get("source_pdf_size_bytes")
    if isinstance(source_size, bool) or not isinstance(source_size, int):
        raise OpenDataLoaderCoordinateCalibrationError("parser run source size is invalid")
    return {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "fixture_suite_sha256": _require_sha("fixture_suite_sha256", fixture_suite_sha256),
        "source_pdf_sha256": _require_sha("source_pdf_sha256", case.get("source_pdf_sha256")),
        "source_pdf_size_bytes": source_size,
        "coordinate_manifest_sha256": _require_sha(
            "coordinate_manifest_sha256", case.get("coordinate_manifest_sha256")
        ),
        "parser_jar_sha256": _require_sha("parser_jar_sha256", parser_jar_sha256),
        "parser_package_metadata_sha256": _require_sha(
            "parser_package_metadata_sha256", parser_package_metadata_sha256
        ),
        "parser_config_sha256": CONFIG_SHA256,
        "java_executable_sha256": _require_sha("java_executable_sha256", java_executable_sha256),
        "java_provided_package_file_sha256": _require_sha(
            "java_provided_package_file_sha256", java_provided_package_file_sha256
        ),
        "java_version_output_sha256": _digest(java_version_output.encode("utf-8")),
        "normalized_command": list(NORMALIZED_PARSER_COMMAND),
        "normalized_command_sha256": NORMALIZED_PARSER_COMMAND_SHA256,
        "raw_output_relative_path": "parser-output/calibration.json",
        "raw_output_sha256": _digest(raw_output),
        "raw_output_size_bytes": len(raw_output),
    }


def validate_parser_run_manifest(
    manifest: Mapping[str, Any],
    *,
    run_id: str,
    case: Mapping[str, Any],
    fixture_suite_sha256: str,
    raw_output: bytes,
    parser_jar_sha256: str,
    parser_package_metadata_sha256: str,
    java_executable_sha256: str,
    java_provided_package_file_sha256: str,
    java_version_output: str,
) -> str:
    """Validate one immutable run manifest and return its content digest."""
    manifest = _exact_keys(manifest, _RUN_MANIFEST_KEYS, name="parser run manifest")
    expected = build_parser_run_manifest(
        run_id=run_id,
        case=case,
        fixture_suite_sha256=fixture_suite_sha256,
        raw_output=raw_output,
        parser_jar_sha256=parser_jar_sha256,
        parser_package_metadata_sha256=parser_package_metadata_sha256,
        java_executable_sha256=java_executable_sha256,
        java_provided_package_file_sha256=java_provided_package_file_sha256,
        java_version_output=java_version_output,
    )
    if manifest != expected:
        raise OpenDataLoaderCoordinateCalibrationError("parser run manifest binding is invalid")
    return _digest(_canonical_json(manifest))


def build_calibration_proof(
    suite: Mapping[str, Any],
    case_inputs: Mapping[
        str,
        tuple[PdfCoordinateManifest, bytes, bytes, Mapping[str, Any], Mapping[str, Any]],
    ],
    *,
    parser_jar_sha256: str,
    parser_package_metadata_sha256: str,
    java_executable_sha256: str,
    java_provided_package_file_sha256: str,
    java_version_output: str,
) -> dict[str, Any]:
    """Build a proof without changing the production ODL coordinate contract."""
    cases = validate_fixture_suite(suite)
    results = []
    seen_case_ids: set[str] = set()
    if set(case_inputs) != _CASE_IDS:
        raise OpenDataLoaderCoordinateCalibrationError("parser replay inputs must exactly cover the v1 case set")
    for case in cases:
        case = _exact_keys(case, _CASE_KEYS, name="fixture case")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id in seen_case_ids:
            raise OpenDataLoaderCoordinateCalibrationError("fixture case_id must be unique")
        seen_case_ids.add(case_id)
        if case_id not in case_inputs:
            raise OpenDataLoaderCoordinateCalibrationError(f"missing parser replay for {case_id}")
        coordinate, run_1, run_2, run_manifest_1, run_manifest_2 = case_inputs[case_id]
        if not isinstance(coordinate, PdfCoordinateManifest):
            raise OpenDataLoaderCoordinateCalibrationError("case input coordinate must be a PdfCoordinateManifest")
        expected_media = _bbox(case.get("media_box"), name="case.media_box")
        expected_crop = _bbox(case.get("crop_box"), name="case.crop_box")
        expected_source = _require_sha("case.source_pdf_sha256", case.get("source_pdf_sha256"))
        expected_coordinate = _require_sha(
            "case.coordinate_manifest_sha256",
            case.get("coordinate_manifest_sha256"),
        )
        _require_sha("case.render_manifest_sha256", case.get("render_manifest_sha256"))
        source_size = case.get("source_pdf_size_bytes")
        rotation = case.get("rotation")
        user_unit = case.get("user_unit")
        if (
            isinstance(source_size, bool)
            or not isinstance(source_size, int)
            or source_size < 1
            or source_size > MAX_SOURCE_PDF_BYTES
        ):
            raise OpenDataLoaderCoordinateCalibrationError("case.source_pdf_size_bytes must be positive")
        if rotation not in {0, 90, 180, 270}:
            raise OpenDataLoaderCoordinateCalibrationError("case.rotation is invalid")
        if user_unit not in {1, 2}:
            raise OpenDataLoaderCoordinateCalibrationError("case.user_unit is unsupported")
        if (
            coordinate.page != 1
            or coordinate.page_count != 1
            or coordinate.source_sha256 != expected_source
            or coordinate.manifest_sha256() != expected_coordinate
            or coordinate.media_box != expected_media
            or coordinate.crop_box != expected_crop
            or coordinate.rotation != rotation
            or coordinate.user_unit != float(user_unit)
        ):
            raise OpenDataLoaderCoordinateCalibrationError("fixture coordinate manifest disagrees with its case")
        anchors = case.get("anchors")
        if not isinstance(anchors, (list, tuple)) or len(anchors) != MIN_ANCHORS_PER_CASE:
            raise OpenDataLoaderCoordinateCalibrationError("fixture case must contain exactly five anchors")
        anchor_ids: set[object] = set()
        labels: set[object] = set()
        for anchor in anchors:
            anchor = _exact_keys(anchor, _ANCHOR_KEYS, name="fixture anchor")
            if anchor.get("anchor_id") not in _ANCHOR_IDS:
                raise OpenDataLoaderCoordinateCalibrationError("fixture anchor_id is unsupported")
            marker = _bbox(anchor.get("marker_bbox_user"), name="anchor.marker_bbox_user")
            expected_bbox = _bbox(anchor.get("expected_text_bbox_user"), name="anchor.expected_text_bbox_user")
            center = _point(anchor.get("expected_text_center_user"), name="anchor.expected_text_center_user")
            if not _contains(expected_crop, marker) or not _contains(marker, expected_bbox) or not (
                expected_bbox[0] <= center[0] <= expected_bbox[2]
                and expected_bbox[1] <= center[1] <= expected_bbox[3]
            ):
                raise OpenDataLoaderCoordinateCalibrationError("fixture anchor lies outside its marker/crop")
            anchor_ids.add(anchor.get("anchor_id"))
            labels.add(anchor.get("label"))
        if len(anchor_ids) != MIN_ANCHORS_PER_CASE or len(labels) != MIN_ANCHORS_PER_CASE:
            raise OpenDataLoaderCoordinateCalibrationError("fixture anchor identities and labels must be unique")
        fixture_suite_sha256 = _digest(_canonical_json(suite))
        run_manifest_1_sha256 = validate_parser_run_manifest(
            run_manifest_1,
            run_id="run-1",
            case=case,
            fixture_suite_sha256=fixture_suite_sha256,
            raw_output=run_1,
            parser_jar_sha256=parser_jar_sha256,
            parser_package_metadata_sha256=parser_package_metadata_sha256,
            java_executable_sha256=java_executable_sha256,
            java_provided_package_file_sha256=java_provided_package_file_sha256,
            java_version_output=java_version_output,
        )
        run_manifest_2_sha256 = validate_parser_run_manifest(
            run_manifest_2,
            run_id="run-2",
            case=case,
            fixture_suite_sha256=fixture_suite_sha256,
            raw_output=run_2,
            parser_jar_sha256=parser_jar_sha256,
            parser_package_metadata_sha256=parser_package_metadata_sha256,
            java_executable_sha256=java_executable_sha256,
            java_provided_package_file_sha256=java_provided_package_file_sha256,
            java_version_output=java_version_output,
        )
        result = evaluate_calibration_case(case, coordinate, run_1, run_2)
        result["run_manifest_1_sha256"] = run_manifest_1_sha256
        result["run_manifest_2_sha256"] = run_manifest_2_sha256
        results.append(result)
    common = set(_CONVENTIONS)
    for result in results:
        common.intersection_update(result["passing_conventions"])
    overall_status = "passed" if all(result["status"] == "passed" for result in results) and len(common) == 1 else "failed"
    if (
        not isinstance(java_version_output, str)
        or not java_version_output.strip()
        or java_version_output != java_version_output.strip()
        or len(java_version_output.encode("utf-8")) > 4096
        or 'version "17.' not in java_version_output
    ):
        raise OpenDataLoaderCoordinateCalibrationError("java_version_output must identify a bounded Java 17 runtime")
    proof = {
        "schema_version": PROOF_SCHEMA_VERSION,
        "fixture_suite_sha256": _digest(_canonical_json(suite)),
        "producer_identity_verified": True,
        "parser": {
            "name": PARSER_NAME,
            "version": PINNED_PARSER_VERSION,
            "package_metadata_sha256": _require_sha(
                "parser_package_metadata_sha256",
                parser_package_metadata_sha256,
            ),
            "jar_sha256": _require_sha("parser_jar_sha256", parser_jar_sha256),
            "config": CONFIG,
            "config_sha256": CONFIG_SHA256,
            "ocr_enabled": False,
        },
        "runtime": {
            "name": "OpenJDK",
            "major_version": 17,
            "version_output": java_version_output,
            "version_output_sha256": _digest(java_version_output.encode("utf-8")),
            "executable_sha256": _require_sha("java_executable_sha256", java_executable_sha256),
            "provided_package_file_sha256": _require_sha(
                "java_provided_package_file_sha256",
                java_provided_package_file_sha256,
            ),
        },
        "gate": {
            "max_center_error_pt": MAX_CENTER_ERROR_PT,
            "max_edge_error_pt": MAX_EDGE_ERROR_PT,
            "min_bbox_iou": MIN_BBOX_IOU,
            "required_anchor_count_per_case": MIN_ANCHORS_PER_CASE,
            "max_occurrences_per_anchor": MAX_OCCURRENCES_PER_ANCHOR,
            "requires_raw_and_canonical_repeat_identity": True,
            "requires_verified_run_manifests": True,
            "requires_one_global_convention": True,
        },
        "status": overall_status,
        "selected_global_convention": next(iter(common)) if overall_status == "passed" else None,
        "production_coordinate_contract_changed": False,
        "case_results": results,
    }
    return proof


def read_regular_bytes(path: str | Path, *, maximum_bytes: int = 64 * 1024 * 1024) -> bytes:
    """Read a bounded regular file without following symlinks."""
    target = Path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > maximum_bytes:
                raise OpenDataLoaderCoordinateCalibrationError("input must be a bounded non-empty regular file")
            content = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(content) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OpenDataLoaderCoordinateCalibrationError("input changed while being read")
        return content
    except OpenDataLoaderCoordinateCalibrationError:
        raise
    except OSError as error:
        raise OpenDataLoaderCoordinateCalibrationError("failed to read regular input file") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
