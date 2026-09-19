"""Conservative, textless heading-to-body relations for PDF A4.2.

The v1 sidecar is additive: it never changes ``pdf_primary_document_view/v1``
leaves or occurrence ownership.  A candidate is rebuilt from all A4.1 source
inputs, and the complete Surya layout artifact is independently rebound to the
same trusted render manifest.  Persisted output contains only artifact
bindings and relations between existing leaf IDs.

This module deliberately does not import Gold or inspect OpenDataLoader
proposals.  Native strings and geometry are bounded decision inputs only; they
are never copied into the sidecar.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .coordinate_manifest import CoordinateManifestError, pixel_to_user_bbox
from .native_capture import NativeCaptureError, validate_native_capture
from .primary_document_view import (
    SCHEMA_VERSION as PRIMARY_VIEW_SCHEMA_VERSION,
    PdfPrimaryDocumentViewError,
    build_pdf_primary_document_view,
    canonical_pdf_primary_document_view_json,
)
from .reconstruction_plan import (
    PdfReconstructionPlanError,
    validate_reconstruction_plan_against_inputs,
)
from .render_manifest import PdfRenderManifest, PdfRenderManifestError
from .surya_layout_artifact import (
    SCHEMA_VERSION as SURYA_SCHEMA_VERSION,
    SuryaLayoutArtifactError,
    SuryaProducerIdentity,
    validate_surya_layout_artifact,
)


SCHEMA_VERSION = "pdf_primary_heading_relations/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"
POLICY_VERSION = "primary_numbered_heading_relation/v1"

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 2_000_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_RELATIONS = 10_000
MAX_HEADING_TEXT_BYTES = 128
MAX_NATIVE_REGION_COMPARISONS = 5_000_000

MIN_SURYA_HEADING_NATIVE_COVERAGE = 0.95
MIN_NATIVE_SURYA_REGION_COVERAGE = 0.70
MIN_HEADING_BODY_FONT_RATIO = 1.10
MAX_HEADING_BODY_FONT_RATIO = 2.00
MIN_HEADING_BODY_HEIGHT_RATIO = 1.10
MAX_HEADING_BODY_HEIGHT_RATIO = 2.00
MAX_HEADING_WIDTH_FRACTION = 0.45
MAX_HEADING_LEFT_FRACTION = 0.25
MAX_LEFT_EDGE_DELTA_IN_BODY_FONTS = 0.25
MIN_GAP_IN_BODY_HEIGHTS = 0.25
MAX_GAP_IN_MAX_LINE_HEIGHTS = 2.50
GEOMETRY_TOLERANCE_PT = 1e-5
MATERIAL_OVERLAP_FRACTION = 0.01
MIN_MATERIAL_OVERLAP_AREA = 1e-4

_SHA = frozenset("0123456789abcdef")
_LEAF_ID = re.compile(r"^leaf-[0-9a-f]{64}$")
_RELATION_ID = re.compile(r"^heading-relation-[0-9a-f]{64}$")
_SURYA_REGION_ID = re.compile(
    r"^p(?P<page>[0-9]{4})-section_header-(?P<ordinal>[0-9]{4})$"
)
_NUMBERED_HEADING = re.compile(
    r"\A[1-9][0-9]{0,2}\.[ \t]{1,4}\S(?:[^\r\n]{0,78}\S)?\Z"
)

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "policy",
        "notice_id",
        "source_pdf_sha256",
        "page_scope",
        "input_artifacts",
        "relations",
    }
)
_POLICY_KEYS = frozenset({"policy_version"})
_INPUT_KEYS = frozenset(
    {
        "primary_document_view_schema_version",
        "primary_document_view_sha256",
        "surya_layout_artifact_schema_version",
        "surya_layout_artifact_sha256",
    }
)
_RELATION_KEYS = frozenset(
    {
        "relation_id",
        "kind",
        "page",
        "heading_leaf_id",
        "body_leaf_id",
        "surya_region_id",
    }
)


class PdfPrimaryHeadingRelationsError(ValueError):
    """Raised when an A4.2 relation sidecar is unsafe or inconsistent."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfPrimaryHeadingRelationsError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PdfPrimaryHeadingRelationsError(f"{name} keys must be strings")
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise PdfPrimaryHeadingRelationsError(
            f"{name} keys are invalid ({'; '.join(details)})"
        )
    return value


def _string(name: str, value: object, *, maximum_bytes: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise PdfPrimaryHeadingRelationsError(
            f"{name} must be a bounded non-empty trimmed string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PdfPrimaryHeadingRelationsError(
            f"{name} contains invalid Unicode"
        ) from error
    if len(encoded) > maximum_bytes:
        raise PdfPrimaryHeadingRelationsError(
            f"{name} must be a bounded non-empty trimmed string"
        )
    return value


def _sha(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA for character in value)
    ):
        raise PdfPrimaryHeadingRelationsError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return value


def _positive(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PdfPrimaryHeadingRelationsError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _leaf_id(name: str, value: object) -> str:
    if not isinstance(value, str) or _LEAF_ID.fullmatch(value) is None:
        raise PdfPrimaryHeadingRelationsError(f"{name} is not a primary leaf ID")
    return value


def _surya_region_id(name: str, value: object, *, page: int) -> str:
    if not isinstance(value, str):
        raise PdfPrimaryHeadingRelationsError(
            f"{name} is not a canonical Surya section-header region ID"
        )
    matched = _SURYA_REGION_ID.fullmatch(value)
    if matched is None:
        raise PdfPrimaryHeadingRelationsError(
            f"{name} is not a canonical Surya section-header region ID"
        )
    region_page = int(matched.group("page"))
    ordinal = int(matched.group("ordinal"))
    if region_page != page or not 1 <= ordinal <= 200:
        raise PdfPrimaryHeadingRelationsError(
            f"{name} is not a canonical Surya section-header region ID"
        )
    return value


def _assert_json_limits(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise PdfPrimaryHeadingRelationsError(
                "heading relations exceed the JSON node cap"
            )
        if depth > MAX_JSON_DEPTH:
            raise PdfPrimaryHeadingRelationsError(
                "heading relations exceed the JSON depth cap"
            )
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise PdfPrimaryHeadingRelationsError(
                    "heading relations contain a non-finite number"
                )
            continue
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise PdfPrimaryHeadingRelationsError(
                    "heading relations contain a surrogate code point"
                )
            try:
                encoded = current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PdfPrimaryHeadingRelationsError(
                    "heading relations contain invalid Unicode"
                ) from error
            if len(encoded) > MAX_STRING_BYTES:
                raise PdfPrimaryHeadingRelationsError(
                    "heading relations contain an oversized string"
                )
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise PdfPrimaryHeadingRelationsError(
                        "heading relation object keys must be strings"
                    )
                stack.append((key, depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
            continue
        raise PdfPrimaryHeadingRelationsError(
            f"heading relations contain unsupported type {type(current).__name__}"
        )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain(nested) for nested in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(nested) for nested in value)
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    _assert_json_limits(value)
    try:
        encoded = json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PdfPrimaryHeadingRelationsError(
            "heading relations are not canonical UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryHeadingRelationsError(
            "heading relations exceed the artifact byte cap"
        )
    return encoded


def _relation_id(
    *,
    source_pdf_sha256: str,
    policy_version: str,
    primary_document_view_sha256: str,
    surya_layout_artifact_sha256: str,
    kind: str,
    page: int,
    heading_leaf_id: str,
    body_leaf_id: str,
    surya_region_id: str,
) -> str:
    payload = {
        "body_leaf_id": body_leaf_id,
        "heading_leaf_id": heading_leaf_id,
        "kind": kind,
        "page": page,
        "policy_version": policy_version,
        "primary_document_view_sha256": primary_document_view_sha256,
        "source_pdf_sha256": source_pdf_sha256,
        "surya_layout_artifact_sha256": surya_layout_artifact_sha256,
        "surya_region_id": surya_region_id,
    }
    return "heading-relation-" + sha256(_canonical_bytes(payload)).hexdigest()


def validate_pdf_primary_heading_relations(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate standalone sidecar consistency and return a detached copy."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="heading relations")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfPrimaryHeadingRelationsError("schema_version is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfPrimaryHeadingRelationsError(
            "heading relations must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PdfPrimaryHeadingRelationsError(
            "standalone validation scope is invalid"
        )
    policy = _exact_keys(root["policy"], _POLICY_KEYS, name="policy")
    if policy["policy_version"] != POLICY_VERSION:
        raise PdfPrimaryHeadingRelationsError("policy.policy_version is invalid")
    notice_id = _string("notice_id", root["notice_id"])
    source_pdf_sha256 = _sha("source_pdf_sha256", root["source_pdf_sha256"])

    raw_pages = root["page_scope"]
    if not isinstance(raw_pages, list) or not raw_pages or len(raw_pages) > MAX_PAGES:
        raise PdfPrimaryHeadingRelationsError(
            "page_scope must be a bounded non-empty array"
        )
    pages = [
        _positive(f"page_scope[{index}]", page, maximum=MAX_PAGES)
        for index, page in enumerate(raw_pages)
    ]
    if pages != sorted(set(pages)):
        raise PdfPrimaryHeadingRelationsError(
            "page_scope must be sorted and unique"
        )

    inputs = _exact_keys(
        root["input_artifacts"], _INPUT_KEYS, name="input_artifacts"
    )
    if inputs["primary_document_view_schema_version"] != PRIMARY_VIEW_SCHEMA_VERSION:
        raise PdfPrimaryHeadingRelationsError(
            "primary document view schema version is invalid"
        )
    if inputs["surya_layout_artifact_schema_version"] != SURYA_SCHEMA_VERSION:
        raise PdfPrimaryHeadingRelationsError(
            "Surya layout artifact schema version is invalid"
        )
    checked_inputs = {
        "primary_document_view_schema_version": PRIMARY_VIEW_SCHEMA_VERSION,
        "primary_document_view_sha256": _sha(
            "input_artifacts.primary_document_view_sha256",
            inputs["primary_document_view_sha256"],
        ),
        "surya_layout_artifact_schema_version": SURYA_SCHEMA_VERSION,
        "surya_layout_artifact_sha256": _sha(
            "input_artifacts.surya_layout_artifact_sha256",
            inputs["surya_layout_artifact_sha256"],
        ),
    }

    raw_relations = root["relations"]
    if not isinstance(raw_relations, list) or len(raw_relations) > MAX_RELATIONS:
        raise PdfPrimaryHeadingRelationsError("relations must be a bounded array")
    relations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_headings: set[str] = set()
    seen_bodies: set[str] = set()
    seen_surya_regions: set[str] = set()
    previous_key: tuple[int, str, str, str] | None = None
    for index, raw_relation in enumerate(raw_relations):
        name = f"relations[{index}]"
        relation = _exact_keys(raw_relation, _RELATION_KEYS, name=name)
        relation_id = relation["relation_id"]
        if (
            not isinstance(relation_id, str)
            or _RELATION_ID.fullmatch(relation_id) is None
            or relation_id in seen_ids
        ):
            raise PdfPrimaryHeadingRelationsError(
                f"{name}.relation_id is invalid or duplicated"
            )
        if relation["kind"] != "heading_to_body":
            raise PdfPrimaryHeadingRelationsError(f"{name}.kind is invalid")
        page = _positive(f"{name}.page", relation["page"], maximum=MAX_PAGES)
        if page not in pages:
            raise PdfPrimaryHeadingRelationsError(
                f"{name}.page is outside page_scope"
            )
        heading_leaf_id = _leaf_id(
            f"{name}.heading_leaf_id", relation["heading_leaf_id"]
        )
        body_leaf_id = _leaf_id(f"{name}.body_leaf_id", relation["body_leaf_id"])
        surya_region_id = _surya_region_id(
            f"{name}.surya_region_id", relation["surya_region_id"], page=page
        )
        if heading_leaf_id == body_leaf_id:
            raise PdfPrimaryHeadingRelationsError(
                f"{name} must connect two distinct leaves"
            )
        if heading_leaf_id in seen_headings or body_leaf_id in seen_bodies:
            raise PdfPrimaryHeadingRelationsError(
                "v1 permits at most one relation per heading and body leaf"
            )
        if surya_region_id in seen_surya_regions:
            raise PdfPrimaryHeadingRelationsError(
                "v1 permits at most one relation per Surya region"
            )
        expected_id = _relation_id(
            source_pdf_sha256=source_pdf_sha256,
            policy_version=POLICY_VERSION,
            primary_document_view_sha256=checked_inputs[
                "primary_document_view_sha256"
            ],
            surya_layout_artifact_sha256=checked_inputs[
                "surya_layout_artifact_sha256"
            ],
            kind="heading_to_body",
            page=page,
            heading_leaf_id=heading_leaf_id,
            body_leaf_id=body_leaf_id,
            surya_region_id=surya_region_id,
        )
        if relation_id != expected_id:
            raise PdfPrimaryHeadingRelationsError(
                f"{name}.relation_id does not bind canonical relation content"
            )
        key = (page, surya_region_id, heading_leaf_id, body_leaf_id)
        if previous_key is not None and key <= previous_key:
            raise PdfPrimaryHeadingRelationsError(
                "relations must be in canonical order"
            )
        previous_key = key
        seen_ids.add(relation_id)
        seen_headings.add(heading_leaf_id)
        seen_bodies.add(body_leaf_id)
        seen_surya_regions.add(surya_region_id)
        relations.append(
            {
                "relation_id": relation_id,
                "kind": "heading_to_body",
                "page": page,
                "heading_leaf_id": heading_leaf_id,
                "body_leaf_id": body_leaf_id,
                "surya_region_id": surya_region_id,
            }
        )

    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "policy": {"policy_version": POLICY_VERSION},
        "notice_id": notice_id,
        "source_pdf_sha256": source_pdf_sha256,
        "page_scope": pages,
        "input_artifacts": checked_inputs,
        "relations": relations,
    }
    _canonical_bytes(result)
    return result


def canonical_pdf_primary_heading_relations_json(
    value: Mapping[str, Any],
) -> bytes:
    """Validate and serialize canonical UTF-8 JSON."""

    return _canonical_bytes(validate_pdf_primary_heading_relations(value))


def _area(bbox: Sequence[float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection_area(left: Sequence[float], right: Sequence[float]) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def _contains(outer: Sequence[float], inner: Sequence[float]) -> bool:
    return (
        outer[0] - GEOMETRY_TOLERANCE_PT <= inner[0]
        and outer[1] - GEOMETRY_TOLERANCE_PT <= inner[1]
        and inner[2] <= outer[2] + GEOMETRY_TOLERANCE_PT
        and inner[3] <= outer[3] + GEOMETRY_TOLERANCE_PT
    )


def _center_inside(outer: Sequence[float], inner: Sequence[float]) -> bool:
    center_x = (inner[0] + inner[2]) / 2.0
    center_y = (inner[1] + inner[3]) / 2.0
    return (
        outer[0] - GEOMETRY_TOLERANCE_PT
        <= center_x
        <= outer[2] + GEOMETRY_TOLERANCE_PT
        and outer[1] - GEOMETRY_TOLERANCE_PT
        <= center_y
        <= outer[3] + GEOMETRY_TOLERANCE_PT
    )


def _materially_overlaps(left: Sequence[float], right: Sequence[float]) -> bool:
    overlap = _intersection_area(left, right)
    smaller = min(_area(left), _area(right))
    return overlap >= max(
        MIN_MATERIAL_OVERLAP_AREA, smaller * MATERIAL_OVERLAP_FRACTION
    )


def _strict_rectangle(region: Any) -> bool:
    x0, y0, x1, y1 = region.bbox_px
    return region.polygon_px == ((x0, y0), (x0, y1), (x1, y1), (x1, y0))


def _bounded_numbered_heading(text: object) -> bool:
    if not isinstance(text, str):
        return False
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        0 < len(encoded) <= MAX_HEADING_TEXT_BYTES
        and text == text.strip()
        and _NUMBERED_HEADING.fullmatch(text) is not None
    )


def _native_bbox(item: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(item["x"]),
        float(item["y"]),
        float(item["x"]) + float(item["width"]),
        float(item["y"]) + float(item["height"]),
    )


def _passes_heading_body_gates(
    *,
    heading_item: Mapping[str, Any],
    body_item: Mapping[str, Any],
    crop_box: Sequence[float],
) -> bool:
    if heading_item["item_type"] != "text" or body_item["item_type"] != "text":
        return False
    if not _bounded_numbered_heading(heading_item["text"]):
        return False
    heading_font = float(heading_item["font_size"])
    body_font = float(body_item["font_size"])
    heading_height = float(heading_item["height"])
    body_height = float(body_item["height"])
    if min(heading_font, body_font, heading_height, body_height) <= 0.0:
        return False
    font_ratio = heading_font / body_font
    height_ratio = heading_height / body_height
    if not MIN_HEADING_BODY_FONT_RATIO <= font_ratio <= MAX_HEADING_BODY_FONT_RATIO:
        return False
    if not MIN_HEADING_BODY_HEIGHT_RATIO <= height_ratio <= MAX_HEADING_BODY_HEIGHT_RATIO:
        return False

    heading_bbox = _native_bbox(heading_item)
    body_bbox = _native_bbox(body_item)
    crop_width = float(crop_box[2]) - float(crop_box[0])
    if crop_width <= 0.0:
        return False
    if (heading_bbox[2] - heading_bbox[0]) / crop_width > MAX_HEADING_WIDTH_FRACTION:
        return False
    if (heading_bbox[0] - float(crop_box[0])) / crop_width > MAX_HEADING_LEFT_FRACTION:
        return False
    if abs(heading_bbox[0] - body_bbox[0]) > MAX_LEFT_EDGE_DELTA_IN_BODY_FONTS * body_font:
        return False

    vertical_gap = heading_bbox[1] - body_bbox[3]
    if vertical_gap < MIN_GAP_IN_BODY_HEIGHTS * body_height:
        return False
    if vertical_gap > MAX_GAP_IN_MAX_LINE_HEIGHTS * max(heading_height, body_height):
        return False
    return True


def build_pdf_primary_heading_relations(
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    structure_candidates: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    render_artifact_root: str | Path,
    calibration_proof: Mapping[str, Any],
    expected_calibration_proof_sha256: str,
    reconstruction_plan: Mapping[str, Any],
    surya_layout_artifact: Mapping[str, Any],
    expected_surya_producer: SuryaProducerIdentity,
    expected_surya_logical_compute_key: str,
    expected_surya_pages: Sequence[int],
) -> dict[str, Any]:
    """Rebuild all inputs and emit only conservative heading/body relations."""

    if not isinstance(surya_layout_artifact, Mapping):
        raise PdfPrimaryHeadingRelationsError(
            "Surya layout input must be the full raw artifact object"
        )
    try:
        primary_view = build_pdf_primary_document_view(
            source_pdf=source_pdf,
            native_capture=native_capture,
            structure_candidates=structure_candidates,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
            calibration_proof=calibration_proof,
            expected_calibration_proof_sha256=expected_calibration_proof_sha256,
            reconstruction_plan=reconstruction_plan,
        )
    except PdfPrimaryDocumentViewError as error:
        raise PdfPrimaryHeadingRelationsError(
            f"A4.1 primary document view replay failed: {error}"
        ) from error

    try:
        manifest = (
            PdfRenderManifest.from_dict(render_manifest)
            if isinstance(render_manifest, Mapping)
            else render_manifest
        )
        if not isinstance(manifest, PdfRenderManifest):
            raise PdfRenderManifestError("render manifest type is invalid")
        plan = validate_reconstruction_plan_against_inputs(
            reconstruction_plan,
            source_pdf=source_pdf,
            native_capture=native_capture,
            structure_candidates=structure_candidates,
            render_manifest=manifest,
            calibration_proof=calibration_proof,
            expected_calibration_proof_sha256=expected_calibration_proof_sha256,
        )
        native = validate_native_capture(
            native_capture,
            source_pdf=source_pdf,
            expected_notice_id=primary_view["notice_id"],
        )
    except (PdfRenderManifestError, PdfReconstructionPlanError, NativeCaptureError) as error:
        raise PdfPrimaryHeadingRelationsError(
            f"A4.1 source replay failed: {error}"
        ) from error

    page_scope = tuple(primary_view["page_scope"])
    primary_view_sha256 = sha256(
        canonical_pdf_primary_document_view_json(primary_view)
    ).hexdigest()
    if isinstance(expected_surya_pages, (str, bytes)):
        raise PdfPrimaryHeadingRelationsError(
            "expected Surya pages must equal A4.1 page_scope"
        )
    try:
        expected_pages = tuple(expected_surya_pages)
    except TypeError as error:
        raise PdfPrimaryHeadingRelationsError(
            "expected Surya pages must be a sequence"
        ) from error
    if expected_pages != page_scope:
        raise PdfPrimaryHeadingRelationsError(
            "expected Surya pages must equal A4.1 page_scope"
        )
    try:
        surya = validate_surya_layout_artifact(
            surya_layout_artifact,
            render_manifest=manifest,
            expected_logical_compute_key=expected_surya_logical_compute_key,
            expected_producer=expected_surya_producer,
            expected_requested_pages=expected_pages,
        )
    except SuryaLayoutArtifactError as error:
        raise PdfPrimaryHeadingRelationsError(
            f"Surya layout artifact replay failed: {error}"
        ) from error
    surya_artifact_sha256 = surya.artifact_sha256()

    authoritative = [
        entry
        for entry in plan["native_occurrence_ledger"]
        if entry["substantive_status"] == "substantive"
        and entry["disposition"] == "owned_atomic"
        and entry["primary_owner_unit_id"] is not None
    ]
    authoritative.sort(key=lambda entry: (entry["page"], entry["source_item_index"]))
    by_occurrence = {entry["occurrence_id"]: entry for entry in authoritative}
    native_items = native["text_items"]
    coordinates = {page.page: page.coordinate_manifest for page in manifest.pages}
    crop_boxes = {page: coordinate.crop_box for page, coordinate in coordinates.items()}

    authoritative_by_page: dict[int, list[Mapping[str, Any]]] = {}
    for entry in authoritative:
        authoritative_by_page.setdefault(entry["page"], []).append(entry)
    comparison_count = sum(
        len(page.regions) * len(authoritative_by_page.get(page.page, ()))
        for page in surya.pages
    )
    if comparison_count > MAX_NATIVE_REGION_COMPARISONS:
        raise PdfPrimaryHeadingRelationsError(
            "Surya/native comparison count exceeds the safety cap"
        )

    valid_headers_by_occurrence: dict[str, list[Any]] = {}
    competing_header_occurrences: set[str] = set()
    non_heading_veto_occurrences: set[str] = set()
    for page in surya.pages:
        coordinate = coordinates.get(page.page)
        if coordinate is None:
            raise PdfPrimaryHeadingRelationsError(
                "Surya page is absent from the trusted render manifest"
            )
        page_entries = authoritative_by_page.get(page.page, [])
        for region in page.regions:
            try:
                region_bbox = pixel_to_user_bbox(coordinate, region.bbox_px)
            except CoordinateManifestError as error:
                raise PdfPrimaryHeadingRelationsError(
                    f"Surya coordinate conversion failed: {error}"
                ) from error
            members = {
                entry["occurrence_id"]
                for entry in page_entries
                if entry["bbox_pdf_user_space"] is not None
                and _contains(region_bbox, entry["bbox_pdf_user_space"])
            }
            touched = [
                entry
                for entry in page_entries
                if entry["bbox_pdf_user_space"] is not None
                and (
                    _center_inside(region_bbox, entry["bbox_pdf_user_space"])
                    or _materially_overlaps(
                        region_bbox, entry["bbox_pdf_user_space"]
                    )
                )
            ]
            if region.label != "section_header":
                non_heading_veto_occurrences.update(
                    entry["occurrence_id"] for entry in touched
                )
                continue
            strict_rectangle = _strict_rectangle(region)
            touched_ids = {entry["occurrence_id"] for entry in touched}
            for entry in touched:
                occurrence_id = entry["occurrence_id"]
                native_bbox = entry["bbox_pdf_user_space"]
                assert native_bbox is not None
                overlap = _intersection_area(region_bbox, native_bbox)
                native_coverage = overlap / _area(native_bbox)
                region_coverage = overlap / _area(region_bbox)
                if (
                    strict_rectangle
                    and members == {occurrence_id}
                    and touched_ids == {occurrence_id}
                    and native_coverage >= MIN_SURYA_HEADING_NATIVE_COVERAGE
                    and region_coverage >= MIN_NATIVE_SURYA_REGION_COVERAGE
                ):
                    valid_headers_by_occurrence.setdefault(occurrence_id, []).append(
                        region
                    )
                else:
                    competing_header_occurrences.add(occurrence_id)

    leaves = primary_view["leaves"]
    relations: list[dict[str, Any]] = []
    for index, heading_leaf in enumerate(leaves[:-1]):
        if (
            heading_leaf["kind"] != "unclassified_text"
            or heading_leaf["placement_method"] != "atomic_fallback"
            or len(heading_leaf["occurrence_ids"]) != 1
        ):
            continue
        body_leaf = leaves[index + 1]
        if (
            body_leaf["kind"] != "paragraph"
            or body_leaf["page"] != heading_leaf["page"]
        ):
            continue
        heading_occurrence = heading_leaf["occurrence_ids"][0]
        heading_entry = by_occurrence.get(heading_occurrence)
        body_entries = [by_occurrence.get(item) for item in body_leaf["occurrence_ids"]]
        if heading_entry is None or any(entry is None for entry in body_entries):
            raise PdfPrimaryHeadingRelationsError(
                "A4.1 leaf is not bound to the replayed authoritative ledger"
            )
        heading_item = native_items[heading_entry["source_item_index"]]
        first_body_entry = body_entries[0]
        assert first_body_entry is not None
        body_item = native_items[first_body_entry["source_item_index"]]
        page = heading_leaf["page"]
        if not _passes_heading_body_gates(
            heading_item=heading_item,
            body_item=body_item,
            crop_box=crop_boxes[page],
        ):
            continue

        if heading_entry["bbox_pdf_user_space"] is None:
            continue
        matching_headers = valid_headers_by_occurrence.get(heading_occurrence, [])
        if (
            heading_occurrence in competing_header_occurrences
            or heading_occurrence in non_heading_veto_occurrences
            or len(matching_headers) != 1
        ):
            continue

        surya_region_id = matching_headers[0].region_id
        relation = {
            "kind": "heading_to_body",
            "page": page,
            "heading_leaf_id": heading_leaf["leaf_id"],
            "body_leaf_id": body_leaf["leaf_id"],
            "surya_region_id": surya_region_id,
        }
        relations.append(
            {
                "relation_id": _relation_id(
                    source_pdf_sha256=primary_view["source"]["source_pdf_sha256"],
                    policy_version=POLICY_VERSION,
                    primary_document_view_sha256=primary_view_sha256,
                    surya_layout_artifact_sha256=surya_artifact_sha256,
                    kind="heading_to_body",
                    page=page,
                    heading_leaf_id=heading_leaf["leaf_id"],
                    body_leaf_id=body_leaf["leaf_id"],
                    surya_region_id=surya_region_id,
                ),
                **relation,
            }
        )

    relations.sort(
        key=lambda relation: (
            relation["page"],
            relation["surya_region_id"],
            relation["heading_leaf_id"],
            relation["body_leaf_id"],
        )
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "policy": {"policy_version": POLICY_VERSION},
        "notice_id": primary_view["notice_id"],
        "source_pdf_sha256": primary_view["source"]["source_pdf_sha256"],
        "page_scope": list(page_scope),
        "input_artifacts": {
            "primary_document_view_schema_version": PRIMARY_VIEW_SCHEMA_VERSION,
            "primary_document_view_sha256": primary_view_sha256,
            "surya_layout_artifact_schema_version": SURYA_SCHEMA_VERSION,
            "surya_layout_artifact_sha256": surya_artifact_sha256,
        },
        "relations": relations,
    }
    return validate_pdf_primary_heading_relations(result)


def validate_pdf_primary_heading_relations_against_inputs(
    value: Mapping[str, Any] | "PrimaryHeadingRelationsFixture", **kwargs: Any
) -> dict[str, Any]:
    """Replay every raw input and require exact canonical sidecar identity."""

    fixture = (
        value
        if type(value) is PrimaryHeadingRelationsFixture
        else PrimaryHeadingRelationsFixture.from_dict(value)
    )
    expected = build_pdf_primary_heading_relations(**kwargs)
    if fixture.canonical_json() != canonical_pdf_primary_heading_relations_json(expected):
        raise PdfPrimaryHeadingRelationsError(
            "heading relations do not match deterministic replay of raw inputs"
        )
    return fixture.to_dict()


@dataclass(frozen=True, slots=True)
class PrimaryHeadingRelationsFixture:
    """Immutable standalone sidecar; never evidence of input replay."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise PdfPrimaryHeadingRelationsError(
                "heading relations root must be an object"
            )
        canonical = validate_pdf_primary_heading_relations(self.payload)
        object.__setattr__(self, "payload", _freeze(canonical))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "PrimaryHeadingRelationsFixture":
        return cls(value)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self.payload)

    def canonical_json(self) -> bytes:
        return _canonical_bytes(self.payload)

    @property
    def canonical_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PdfPrimaryHeadingRelationsError(
                f"heading relations contain duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PdfPrimaryHeadingRelationsError(
        f"heading relations contain non-finite JSON number {value!r}"
    )


def _bounded_integer(value: str) -> int:
    if len(value.lstrip("-")) > 6:
        raise PdfPrimaryHeadingRelationsError(
            "heading relations contain an oversized integer"
        )
    return int(value)


def parse_pdf_primary_heading_relations_bytes(
    raw: bytes,
) -> PrimaryHeadingRelationsFixture:
    """Parse bounded, duplicate-free, exact canonical UTF-8 JSON."""

    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryHeadingRelationsError(
            "heading relations exceed the artifact byte cap"
        )
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PdfPrimaryHeadingRelationsError(
                "heading relations must not contain a UTF-8 BOM"
            )
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
            parse_int=_bounded_integer,
            parse_float=lambda _: (_ for _ in ()).throw(
                PdfPrimaryHeadingRelationsError(
                    "heading relations must not contain floating-point numbers"
                )
            ),
        )
    except PdfPrimaryHeadingRelationsError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PdfPrimaryHeadingRelationsError(
            "heading relations are not valid UTF-8 JSON"
        ) from error
    _assert_json_limits(value)
    if not isinstance(value, Mapping):
        raise PdfPrimaryHeadingRelationsError(
            "heading relations root must be an object"
        )
    fixture = PrimaryHeadingRelationsFixture.from_dict(value)
    if raw != fixture.canonical_json():
        raise PdfPrimaryHeadingRelationsError(
            "heading relation bytes are not canonical UTF-8 JSON"
        )
    return fixture


def load_pdf_primary_heading_relations_file(
    path: str | Path,
) -> PrimaryHeadingRelationsFixture:
    """Safely load one regular, non-symlink sidecar file."""

    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PdfPrimaryHeadingRelationsError(
                "heading relation input must be a regular non-symlink file"
            )
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise PdfPrimaryHeadingRelationsError(
                "heading relations exceed the artifact byte cap"
            )
        descriptor = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (before.st_dev, before.st_ino):
                raise PdfPrimaryHeadingRelationsError(
                    "heading relation input changed while opening"
                )
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(stream.fileno())
        before_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            len(raw) > MAX_ARTIFACT_BYTES
            or len(raw) != opened.st_size
            or before_identity != after_identity
        ):
            raise PdfPrimaryHeadingRelationsError(
                "heading relation input changed while reading"
            )
    except PdfPrimaryHeadingRelationsError:
        raise
    except OSError as error:
        raise PdfPrimaryHeadingRelationsError(
            "heading relation input cannot be safely opened"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_pdf_primary_heading_relations_bytes(raw)


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "PdfPrimaryHeadingRelationsError",
    "PrimaryHeadingRelationsFixture",
    "POLICY_VERSION",
    "SCHEMA_VERSION",
    "build_pdf_primary_heading_relations",
    "canonical_pdf_primary_heading_relations_json",
    "load_pdf_primary_heading_relations_file",
    "parse_pdf_primary_heading_relations_bytes",
    "validate_pdf_primary_heading_relations",
    "validate_pdf_primary_heading_relations_against_inputs",
]
