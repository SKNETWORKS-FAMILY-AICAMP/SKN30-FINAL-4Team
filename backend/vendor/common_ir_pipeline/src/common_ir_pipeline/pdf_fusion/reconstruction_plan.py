"""Native-first, offline PDF reconstruction planning.

The v1 plan is deliberately a shadow artifact.  Native PDF text occurrences
remain the only text authority and each valid native occurrence owns one
atomic evidence unit.  OpenDataLoader candidates may reference those atoms as
structure context, but can never own them or create composite evidence here.

This separation is important for legacy parser caches: a calibrated parser
coordinate convention can make geometry useful for evaluation without
pretending that an old, producer-unbound artifact became production evidence.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .legacy_opendataloader_evaluation import SCHEMA_VERSION as LEGACY_ODL_SCHEMA_VERSION
from .native_capture import (
    CAPTURE_SCHEMA_VERSION as NATIVE_CAPTURE_SCHEMA_VERSION,
    NativeCaptureError,
    NativeCaptureLimits,
    canonical_json_bytes as canonical_native_json_bytes,
    validate_native_capture,
)
from .opendataloader_coordinate_calibration import (
    PROOF_SCHEMA_VERSION,
    REVIEWED_PROOF_CANONICAL_SHA256,
    OpenDataLoaderCoordinateCalibrationError,
    calibration_proof_sha256,
    project_odl_bbox_with_validated_convention,
    validate_calibration_proof_for_projection,
)
from .render_manifest import (
    SCHEMA_VERSION as RENDER_MANIFEST_SCHEMA_VERSION,
    PdfRenderManifest,
    PdfRenderManifestError,
)
from .structure_candidates import (
    SCHEMA_VERSION as STRUCTURE_CANDIDATES_SCHEMA_VERSION,
    PdfStructureCandidatesError,
    MAX_CANDIDATES,
    canonical_structure_candidates_json,
    validate_structure_candidates,
)


SCHEMA_VERSION = "pdf_reconstruction_plan/v1"
COORDINATE_PROJECTION_STATUS = "calibrated_shadow_only"
MAX_CONTEXT_REFERENCES_PER_UNIT = 5_000
MAX_CONTEXT_MEMBERSHIPS_PER_OCCURRENCE = 256
MAX_ALIGNMENT_COMPARISONS = 5_000_000
MAX_NATIVE_OCCURRENCES = NativeCaptureLimits().max_text_items
MAX_PLAN_UNITS = MAX_NATIVE_OCCURRENCES + MAX_CANDIDATES
MIN_NATIVE_COVERAGE = 0.95
MAX_SAME_LINE_BASELINE_DELTA_PT = 1.0
MAX_BULLET_TO_TEXT_GAP_PT = 12.0
_SHA256_HEX = frozenset("0123456789abcdef")
_OCCURRENCE_ID_RE = re.compile(r"^occ:inspector:p([1-9][0-9]*):t([0-9]+)$")
_CANDIDATE_ID_RE = re.compile(r"^odl-[0-9a-f]{64}$")
_LEAF_KINDS = frozenset({"heading", "paragraph", "list_item", "table_cell", "text_block", "caption"})
_CONTAINER_KINDS = frozenset({"list", "table", "table_row"})
_ODL_KINDS = _LEAF_KINDS | _CONTAINER_KINDS | {"image"}
_BULLET_MARKERS = frozenset({"□", "■", "○", "●", "◇", "◆", "▪", "▫", "•", "◦", "-"})
_ALIGNMENT_STATUSES = frozenset({"accepted", "partial", "rejected"})
_USE_POLICIES = frozenset({"evidence_atomic", "context_only", "diagnostic_only"})
_REJECTION_REASON_CODES = frozenset(
    {
        "no_candidate_bbox",
        "invalid_candidate_bbox_projection",
        "no_native_occurrence",
        "cross_column_or_row_merge",
        "multi_occurrence_leaf_unverified",
        "competing_leaf_context",
        "unsupported_candidate_kind",
    }
)
_REASON_CODES = frozenset(
    {
        "legacy_odl_non_promotable",
        "container_reference_only",
        "no_candidate_bbox",
        "invalid_candidate_bbox_projection",
        "no_native_occurrence",
        "cross_column_or_row_merge",
        "multi_occurrence_leaf_unverified",
        "competing_leaf_context",
        "split_bullet_marker",
        "calibration_kind_unverified",
        "unsupported_candidate_kind",
        "invalid_native_geometry",
        "non_substantive_native",
    }
)
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "native_text_status",
        "notice_id",
        "source_pdf_sha256",
        "page_scope",
        "input_artifacts",
        "coordinate_projection",
        "units",
        "native_occurrence_ledger",
        "rejected_candidates",
        "metrics",
    }
)
_INPUT_KEYS = frozenset(
    {
        "native_capture_schema_version",
        "native_capture_sha256",
        "render_manifest_schema_version",
        "render_manifest_sha256",
        "structure_candidates_schema_version",
        "structure_candidates_sha256",
        "structure_input_binding_schema_version",
        "structure_input_binding_sha256",
        "coordinate_calibration_schema_version",
        "coordinate_calibration_sha256",
    }
)
_COORDINATE_KEYS = frozenset(
    {"status", "selected_convention", "proof_sha256", "parser_binding_status"}
)
_UNIT_KEYS = frozenset(
    {
        "unit_id",
        "origin",
        "source_candidate_id",
        "kind",
        "page",
        "bbox_pdf_user_space",
        "alignment_status",
        "use_policy",
        "primary_occurrence_ids",
        "reference_occurrence_ids",
        "parent_unit_id",
        "header_unit_ids",
        "joiners",
        "reason_codes",
    }
)
_LEDGER_KEYS = frozenset(
    {
        "occurrence_id",
        "page",
        "source_item_index",
        "bbox_pdf_user_space",
        "substantive_status",
        "primary_owner_unit_id",
        "context_unit_ids",
        "disposition",
        "reason_codes",
    }
)
_REJECTED_KEYS = frozenset({"source_candidate_id", "unit_id", "reason_codes"})
_METRIC_KEYS = frozenset(
    {
        "native_ownership_gate_status",
        "scoped_native_occurrence_count",
        "substantive_native_occurrence_count",
        "atomic_evidence_unit_count",
        "unowned_substantive_occurrence_count",
        "duplicate_primary_owner_count",
        "structure_candidate_count",
        "candidate_alignment_counts",
        "candidate_use_policy_counts",
        "rejected_candidate_count",
        "evidence_composite_unit_count",
        "max_context_reference_count",
    }
)


class PdfReconstructionPlanError(ValueError):
    """Raised when a reconstruction plan would be ambiguous or unsafe."""


def _exact_keys(value: object, expected: frozenset[str], *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfReconstructionPlanError(f"{name} must be an object")
    keys = frozenset(value.keys())
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise PdfReconstructionPlanError(f"{name} keys are invalid ({'; '.join(details)})")
    return value


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in _SHA256_HEX for character in value):
        raise PdfReconstructionPlanError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _trimmed(name: str, value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise PdfReconstructionPlanError(f"{name} must be a bounded non-empty trimmed string")
    return value


def _positive_int(name: str, value: object, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (maximum is not None and value > maximum):
        raise PdfReconstructionPlanError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PdfReconstructionPlanError(f"{name} must be a non-negative integer")
    return value


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise PdfReconstructionPlanError(f"{name} must be a finite number")
    result = float(value)
    return 0.0 if result == 0.0 else result


def _bbox(value: object, *, name: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise PdfReconstructionPlanError(f"{name} must contain exactly four coordinates")
    x0, y0, x1, y1 = (_finite(f"{name}[{index}]", item) for index, item in enumerate(value))
    if not x0 < x1 or not y0 < y1:
        raise PdfReconstructionPlanError(f"{name} must have strictly increasing bounds")
    return (x0, y0, x1, y1)


def _contains(outer: Sequence[float], inner: Sequence[float], *, tolerance: float = 1e-5) -> bool:
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection_area(left: Sequence[float], right: Sequence[float]) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _center_inside(outer: Sequence[float], inner: Sequence[float], *, tolerance: float = 1e-5) -> bool:
    center_x = (inner[0] + inner[2]) / 2.0
    center_y = (inner[1] + inner[3]) / 2.0
    return (
        outer[0] - tolerance <= center_x <= outer[2] + tolerance
        and outer[1] - tolerance <= center_y <= outer[3] + tolerance
    )


def _unit_id(origin: str, identity: str, *, source_sha256: str) -> str:
    digest = sha256(f"{source_sha256}:{origin}:{identity}".encode("utf-8")).hexdigest()
    return f"unit-{origin}-{digest}"


def _occurrence_id(page: int, item_index: int) -> str:
    return f"occ:inspector:p{page}:t{item_index}"


def _is_substantive(item: Mapping[str, Any]) -> bool:
    text = item.get("text")
    return isinstance(text, str) and bool(text.strip()) and item.get("item_type") == "text"


def _native_bbox(item: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        x = _finite("native x", item.get("x"))
        y = _finite("native y", item.get("y"))
        width = _finite("native width", item.get("width"))
        height = _finite("native height", item.get("height"))
    except PdfReconstructionPlanError:
        return None
    if width <= 0 or height <= 0:
        return None
    return (x, y, x + width, y + height)


def _matches(candidate_bbox: Sequence[float], native_bbox: Sequence[float]) -> bool:
    native_area = (native_bbox[2] - native_bbox[0]) * (native_bbox[3] - native_bbox[1])
    return (
        native_area > 0
        and _center_inside(candidate_bbox, native_bbox)
        and _intersection_area(candidate_bbox, native_bbox) / native_area >= MIN_NATIVE_COVERAGE
    )


def _is_lossless_bullet_split(matches: Sequence[Mapping[str, Any]]) -> bool:
    if len(matches) != 2:
        return False
    left, right = matches
    if right["source_item_index"] != left["source_item_index"] + 1:
        return False
    left_text = left["text"].strip()
    if left_text not in _BULLET_MARKERS or not right["text"].strip():
        return False
    left_bbox, right_bbox = left["bbox"], right["bbox"]
    left_center_y = (left_bbox[1] + left_bbox[3]) / 2.0
    right_center_y = (right_bbox[1] + right_bbox[3]) / 2.0
    if abs(left_center_y - right_center_y) > MAX_SAME_LINE_BASELINE_DELTA_PT:
        return False
    gap = right_bbox[0] - left_bbox[2]
    return 0.0 <= gap <= MAX_BULLET_TO_TEXT_GAP_PT


def _is_clear_cross_column_merge(matches: Sequence[Mapping[str, Any]]) -> bool:
    """Return true only for a wide same-line separation between native atoms.

    Other multi-occurrence leaves remain rejected, but use the neutral
    ``multi_occurrence_leaf_unverified`` reason instead of pretending that a
    row/column boundary was proven.
    """

    for left_index, left in enumerate(matches):
        left_bbox = left["bbox"]
        left_center_y = (left_bbox[1] + left_bbox[3]) / 2.0
        for right in matches[left_index + 1 :]:
            right_bbox = right["bbox"]
            right_center_y = (right_bbox[1] + right_bbox[3]) / 2.0
            if abs(left_center_y - right_center_y) > MAX_SAME_LINE_BASELINE_DELTA_PT:
                continue
            first, second = sorted((left_bbox, right_bbox), key=lambda bbox: bbox[0])
            if second[0] - first[2] > MAX_BULLET_TO_TEXT_GAP_PT:
                return True
    return False


def _canonical_sha(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PdfReconstructionPlanError("value is not canonical JSON") from error
    return sha256(encoded).hexdigest()


def build_pdf_reconstruction_plan(
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    structure_candidates: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    calibration_proof: Mapping[str, Any],
    expected_calibration_proof_sha256: str,
) -> dict[str, Any]:
    """Build a deterministic native-first evaluation plan.

    The source PDF is mandatory so the native capture is rebound to the exact
    bytes at this boundary.  The caller should separately use
    ``validate_render_manifest_files`` before this function when page images
    came from an untrusted directory; this function consumes only the
    manifest's validated affine coordinates and never opens a PNG.
    """

    try:
        native = validate_native_capture(native_capture, source_pdf=source_pdf)
    except NativeCaptureError as error:
        raise PdfReconstructionPlanError(f"native capture is invalid: {error}") from error
    try:
        candidates = validate_structure_candidates(structure_candidates)
        candidates_sha256 = sha256(canonical_structure_candidates_json(candidates)).hexdigest()
    except PdfStructureCandidatesError as error:
        raise PdfReconstructionPlanError(f"structure candidates are invalid: {error}") from error
    try:
        manifest = render_manifest if isinstance(render_manifest, PdfRenderManifest) else PdfRenderManifest.from_dict(render_manifest)
    except PdfRenderManifestError as error:
        raise PdfReconstructionPlanError(f"render manifest is invalid: {error}") from error
    try:
        if expected_calibration_proof_sha256 != REVIEWED_PROOF_CANONICAL_SHA256:
            raise PdfReconstructionPlanError(
                "expected calibration proof hash is not the reviewed v1 digest"
            )
        selected_convention = validate_calibration_proof_for_projection(
            calibration_proof,
            expected_proof_sha256=expected_calibration_proof_sha256,
        )
        proof_sha256 = calibration_proof_sha256(calibration_proof)
    except OpenDataLoaderCoordinateCalibrationError as error:
        raise PdfReconstructionPlanError(f"coordinate calibration proof is invalid: {error}") from error

    if native["notice_id"] != candidates["notice_id"]:
        raise PdfReconstructionPlanError("native and structure notice identities disagree")
    source_sha256 = native["source_sha256"]
    if candidates["source_pdf_sha256"] != source_sha256 or manifest.source_pdf_sha256 != source_sha256:
        raise PdfReconstructionPlanError("input artifacts do not bind the same source PDF")
    if manifest.source_pdf_size_bytes != native["source_size_bytes"]:
        raise PdfReconstructionPlanError("native capture and render manifest source sizes disagree")
    page_count = native["process_result"]["page_count"]
    if candidates["source_page_count"] != page_count or manifest.page_count != page_count:
        raise PdfReconstructionPlanError("input artifacts do not agree on source page count")
    page_scope = tuple(candidates["page_scope"])
    coordinates = {page.page: page.coordinate_manifest for page in manifest.pages}
    if any(page not in coordinates for page in page_scope):
        raise PdfReconstructionPlanError("render manifest does not cover candidate page_scope")

    native_capture_sha256 = sha256(canonical_native_json_bytes(native)).hexdigest()
    render_manifest_sha256 = sha256(manifest.canonical_json()).hexdigest()
    binding_schema = candidates["input_binding_schema_version"]
    # v1 receives only a projected candidate sidecar.  It cannot revalidate a
    # strict parser artifact's JAR/config/source lineage from that sidecar, so
    # accepting a claimed strict schema here would make provenance forgeable.
    # Keep this boundary legacy-evaluation-only until the underlying binding
    # artifact is a mandatory, revalidated input.
    if binding_schema != LEGACY_ODL_SCHEMA_VERSION:
        raise PdfReconstructionPlanError(
            "v1 reconstruction requires a legacy evaluation binding; "
            "strict parser claims require the underlying artifact"
        )

    scoped_native: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    owner_ids: set[str] = set()
    for item_index, item in enumerate(native["text_items"]):
        page = item["page"]
        if page not in page_scope:
            continue
        occurrence_id = _occurrence_id(page, item_index)
        substantive = _is_substantive(item)
        bbox = _native_bbox(item)
        valid_geometry = bbox is not None and _contains(coordinates[page].crop_box, bbox)
        primary_owner: str | None = None
        disposition: str
        reasons: list[str] = []
        substantive_status = "substantive" if substantive else "non_substantive"
        if not substantive:
            disposition = "non_substantive"
            reasons.append("non_substantive_native")
        elif not valid_geometry:
            disposition = "rejected_invalid_geometry"
            substantive_status = "invalid_geometry"
            reasons.append("invalid_native_geometry")
        else:
            primary_owner = _unit_id("native", occurrence_id, source_sha256=source_sha256)
            if primary_owner in owner_ids:
                raise PdfReconstructionPlanError("native atomic unit identity collision")
            owner_ids.add(primary_owner)
            disposition = "owned_atomic"
            units.append(
                {
                    "unit_id": primary_owner,
                    "origin": "native",
                    "source_candidate_id": None,
                    "kind": "native_text",
                    "page": page,
                    "bbox_pdf_user_space": [round(value, 6) for value in bbox],
                    "alignment_status": "accepted",
                    "use_policy": "evidence_atomic",
                    "primary_occurrence_ids": [occurrence_id],
                    "reference_occurrence_ids": [],
                    "parent_unit_id": None,
                    "header_unit_ids": [],
                    "joiners": [],
                    "reason_codes": [],
                }
            )
        entry = {
            "occurrence_id": occurrence_id,
            "page": page,
            "source_item_index": item_index,
            "bbox_pdf_user_space": None if bbox is None else [round(value, 6) for value in bbox],
            "substantive_status": substantive_status,
            "primary_owner_unit_id": primary_owner,
            "context_unit_ids": [],
            "disposition": disposition,
            "reason_codes": reasons,
        }
        ledger.append(entry)
        scoped_native.append(
            {
                "occurrence_id": occurrence_id,
                "page": page,
                "source_item_index": item_index,
                "text": item["text"],
                "bbox": bbox,
                "substantive": substantive,
                "valid_geometry": valid_geometry,
                "ledger": entry,
            }
        )

    candidate_unit_ids = {
        candidate["candidate_id"]: _unit_id("odl", candidate["candidate_id"], source_sha256=source_sha256)
        for candidate in candidates["candidates"]
    }
    rejected_candidates: list[dict[str, Any]] = []
    alignment_counts = {"accepted": 0, "partial": 0, "rejected": 0}
    policy_counts = {"context_only": 0, "diagnostic_only": 0}
    ledger_by_occurrence = {entry["occurrence_id"]: entry for entry in ledger}
    native_by_page: dict[int, list[dict[str, Any]]] = {page: [] for page in page_scope}
    for native_item in scoped_native:
        if native_item["substantive"] and native_item["valid_geometry"]:
            native_by_page[native_item["page"]].append(native_item)
    candidate_counts_by_page: dict[int, int] = {page: 0 for page in page_scope}
    for candidate in candidates["candidates"]:
        candidate_counts_by_page[candidate["source_page"]] += 1
    comparison_count = sum(
        candidate_counts_by_page[page] * len(native_by_page[page]) for page in page_scope
    )
    if comparison_count > MAX_ALIGNMENT_COMPARISONS:
        raise PdfReconstructionPlanError("native/candidate alignment exceeds the comparison safety cap")
    for candidate in candidates["candidates"]:
        candidate_id = candidate["candidate_id"]
        unit_id = candidate_unit_ids[candidate_id]
        kind = candidate["normalized_kind"]
        parent_id = candidate["parent_candidate_id"]
        parent_unit_id = candidate_unit_ids.get(parent_id) if parent_id is not None else None
        reasons: list[str] = []
        reference_occurrences: list[str] = []
        mapped_bbox: tuple[float, float, float, float] | None = None
        raw_bbox = candidate.get("bbox_odl_pdf_points_unverified")
        if raw_bbox is None:
            alignment_status, use_policy = "rejected", "diagnostic_only"
            reasons.append("no_candidate_bbox")
        elif kind not in _LEAF_KINDS | _CONTAINER_KINDS:
            alignment_status, use_policy = "rejected", "diagnostic_only"
            reasons.append("unsupported_candidate_kind")
        else:
            coordinate = coordinates[candidate["source_page"]]
            try:
                mapped_bbox = project_odl_bbox_with_validated_convention(
                    raw_bbox,
                    coordinate=coordinate,
                    validated_convention=selected_convention,
                )
            except OpenDataLoaderCoordinateCalibrationError:
                # A proof/hash failure is global and was checked above.  A
                # candidate-local bad bbox remains visible as a rejected unit.
                alignment_status, use_policy = "rejected", "diagnostic_only"
                reasons.append("invalid_candidate_bbox_projection")
            else:
                if kind != "paragraph":
                    # The reviewed proof used paragraph anchors.  Geometry for
                    # other parser node kinds remains useful for shadow
                    # comparison, but the scope limitation must stay visible.
                    reasons.append("calibration_kind_unverified")
                matched = [
                    native_item
                    for native_item in native_by_page[candidate["source_page"]]
                    if _matches(mapped_bbox, native_item["bbox"])
                ]
                reference_occurrences = [item["occurrence_id"] for item in matched]
                if not matched:
                    alignment_status, use_policy = "rejected", "diagnostic_only"
                    reasons.append("no_native_occurrence")
                elif kind in _CONTAINER_KINDS:
                    alignment_status, use_policy = "partial", "context_only"
                    reasons.append("container_reference_only")
                    reasons.append("legacy_odl_non_promotable")
                elif len(matched) == 1:
                    alignment_status, use_policy = "partial", "context_only"
                    reasons.append("legacy_odl_non_promotable")
                elif _is_lossless_bullet_split(matched):
                    alignment_status, use_policy = "partial", "context_only"
                    reasons.extend(["split_bullet_marker", "legacy_odl_non_promotable"])
                else:
                    alignment_status, use_policy = "rejected", "diagnostic_only"
                    reasons.append(
                        "cross_column_or_row_merge"
                        if _is_clear_cross_column_merge(matched)
                        else "multi_occurrence_leaf_unverified"
                    )

        if len(reference_occurrences) > MAX_CONTEXT_REFERENCES_PER_UNIT:
            raise PdfReconstructionPlanError("candidate context reference count exceeds the safety cap")
        reasons = sorted(set(reasons))
        unit = {
            "unit_id": unit_id,
            "origin": "opendataloader",
            "source_candidate_id": candidate_id,
            "kind": kind,
            "page": candidate["source_page"],
            "bbox_pdf_user_space": None if mapped_bbox is None else [round(value, 6) for value in mapped_bbox],
            "alignment_status": alignment_status,
            "use_policy": use_policy,
            "primary_occurrence_ids": [],
            "reference_occurrence_ids": reference_occurrences,
            "parent_unit_id": parent_unit_id,
            "header_unit_ids": [],
            "joiners": [],
            "reason_codes": reasons,
        }
        units.append(unit)
        alignment_counts[alignment_status] += 1
        policy_counts[use_policy] += 1
        if use_policy == "context_only":
            for occurrence_id in reference_occurrences:
                memberships = ledger_by_occurrence[occurrence_id]["context_unit_ids"]
                if len(memberships) >= MAX_CONTEXT_MEMBERSHIPS_PER_OCCURRENCE:
                    raise PdfReconstructionPlanError("native occurrence exceeds the context membership safety cap")
                memberships.append(unit_id)
        else:
            rejected_candidates.append(
                {"source_candidate_id": candidate_id, "unit_id": unit_id, "reason_codes": reasons}
            )

    # Context references may overlap for nested containers, but two leaf
    # candidates must not silently propose the same native atom as independent
    # semantic leaves.  Neither owns evidence in v1; both become diagnostic so
    # a later context projector cannot choose one by traversal accident.
    candidate_units = [unit for unit in units if unit["origin"] == "opendataloader"]
    competing_unit_ids: set[str] = set()
    context_leaf_by_occurrence: dict[str, list[str]] = {}
    for unit in candidate_units:
        if unit["kind"] not in _LEAF_KINDS or unit["use_policy"] != "context_only":
            continue
        for occurrence_id in unit["reference_occurrence_ids"]:
            context_leaf_by_occurrence.setdefault(occurrence_id, []).append(unit["unit_id"])
    for unit_ids in context_leaf_by_occurrence.values():
        if len(unit_ids) > 1:
            competing_unit_ids.update(unit_ids)
    if competing_unit_ids:
        for unit in candidate_units:
            if unit["unit_id"] not in competing_unit_ids:
                continue
            alignment_counts[unit["alignment_status"]] -= 1
            policy_counts[unit["use_policy"]] -= 1
            unit["alignment_status"] = "rejected"
            unit["use_policy"] = "diagnostic_only"
            unit["reason_codes"] = sorted(set(unit["reason_codes"] + ["competing_leaf_context"]))
            alignment_counts["rejected"] += 1
            policy_counts["diagnostic_only"] += 1
            for occurrence_id in unit["reference_occurrence_ids"]:
                ledger_by_occurrence[occurrence_id]["context_unit_ids"].remove(unit["unit_id"])
            rejected_candidates.append(
                {
                    "source_candidate_id": unit["source_candidate_id"],
                    "unit_id": unit["unit_id"],
                    "reason_codes": unit["reason_codes"],
                }
            )

    substantive_count = sum(entry["substantive_status"] != "non_substantive" for entry in ledger)
    unowned_count = sum(
        entry["substantive_status"] == "invalid_geometry" or (
            entry["substantive_status"] == "substantive" and entry["primary_owner_unit_id"] is None
        )
        for entry in ledger
    )
    owner_occurrences = [
        occurrence_id
        for unit in units
        for occurrence_id in unit["primary_occurrence_ids"]
    ]
    duplicate_owner_count = len(owner_occurrences) - len(set(owner_occurrences))
    max_context_refs = max((len(entry["context_unit_ids"]) for entry in ledger), default=0)
    metrics = {
        "native_ownership_gate_status": (
            "passed"
            if substantive_count > 0 and unowned_count == 0 and duplicate_owner_count == 0
            else "failed"
        ),
        "scoped_native_occurrence_count": len(ledger),
        "substantive_native_occurrence_count": substantive_count,
        "atomic_evidence_unit_count": sum(unit["use_policy"] == "evidence_atomic" for unit in units),
        "unowned_substantive_occurrence_count": unowned_count,
        "duplicate_primary_owner_count": duplicate_owner_count,
        "structure_candidate_count": len(candidates["candidates"]),
        "candidate_alignment_counts": alignment_counts,
        "candidate_use_policy_counts": policy_counts,
        "rejected_candidate_count": len(rejected_candidates),
        "evidence_composite_unit_count": 0,
        "max_context_reference_count": max_context_refs,
    }
    plan = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "native_text_status": (
            "native_text_available" if substantive_count > 0 else "requires_ocr_semantic_v2"
        ),
        "notice_id": native["notice_id"],
        "source_pdf_sha256": source_sha256,
        "page_scope": list(page_scope),
        "input_artifacts": {
            "native_capture_schema_version": native["capture_schema_version"],
            "native_capture_sha256": native_capture_sha256,
            "render_manifest_schema_version": manifest.schema_version,
            "render_manifest_sha256": render_manifest_sha256,
            "structure_candidates_schema_version": candidates["schema_version"],
            "structure_candidates_sha256": candidates_sha256,
            "structure_input_binding_schema_version": binding_schema,
            "structure_input_binding_sha256": candidates["input_binding_sha256"],
            "coordinate_calibration_schema_version": PROOF_SCHEMA_VERSION,
            "coordinate_calibration_sha256": proof_sha256,
        },
        "coordinate_projection": {
            "status": COORDINATE_PROJECTION_STATUS,
            "selected_convention": selected_convention,
            "proof_sha256": proof_sha256,
            "parser_binding_status": "legacy_claim_unbound",
        },
        "units": units,
        "native_occurrence_ledger": ledger,
        "rejected_candidates": rejected_candidates,
        "metrics": metrics,
    }
    validate_reconstruction_plan(plan)
    return plan


def validate_reconstruction_plan(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate internal v1 schema, ledger and reciprocal ownership only.

    This function cannot authenticate the external artifacts named by their
    hashes.  A consumer crossing a trust boundary must additionally call
    :func:`validate_reconstruction_plan_against_inputs` with the source-bound
    artifacts.  Canonical serialization is likewise not authentication.
    """

    root = _exact_keys(value, _ROOT_KEYS, name="reconstruction plan")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfReconstructionPlanError("reconstruction plan schema_version is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfReconstructionPlanError("reconstruction plan must remain evaluation-only and non-promotable")
    if root["standalone_validation_scope"] != "internal_consistency_only":
        raise PdfReconstructionPlanError("standalone validation scope is invalid")
    if root["native_text_status"] not in {"native_text_available", "requires_ocr_semantic_v2"}:
        raise PdfReconstructionPlanError("native text status is invalid")
    _trimmed("notice_id", root["notice_id"])
    _sha("source_pdf_sha256", root["source_pdf_sha256"])
    if not isinstance(root["page_scope"], list) or not root["page_scope"]:
        raise PdfReconstructionPlanError("page_scope must be a non-empty array")
    pages = tuple(_positive_int(f"page_scope[{index}]", page, maximum=256) for index, page in enumerate(root["page_scope"]))
    if pages != tuple(sorted(set(pages))):
        raise PdfReconstructionPlanError("page_scope must be sorted and unique")

    inputs = _exact_keys(root["input_artifacts"], _INPUT_KEYS, name="input_artifacts")
    for name, item in inputs.items():
        if name.endswith("_sha256"):
            _sha(f"input_artifacts.{name}", item)
        else:
            _trimmed(f"input_artifacts.{name}", item)
    if inputs["native_capture_schema_version"] != NATIVE_CAPTURE_SCHEMA_VERSION:
        raise PdfReconstructionPlanError("native capture schema version is invalid")
    if inputs["render_manifest_schema_version"] != RENDER_MANIFEST_SCHEMA_VERSION:
        raise PdfReconstructionPlanError("render manifest schema version is invalid")
    if inputs["structure_candidates_schema_version"] != STRUCTURE_CANDIDATES_SCHEMA_VERSION:
        raise PdfReconstructionPlanError("structure candidates schema version is invalid")
    if inputs["coordinate_calibration_schema_version"] != PROOF_SCHEMA_VERSION:
        raise PdfReconstructionPlanError("coordinate calibration schema version is invalid")
    if inputs["coordinate_calibration_sha256"] != REVIEWED_PROOF_CANONICAL_SHA256:
        raise PdfReconstructionPlanError("coordinate calibration hash is not the reviewed v1 proof")
    if inputs["structure_input_binding_schema_version"] != LEGACY_ODL_SCHEMA_VERSION:
        raise PdfReconstructionPlanError(
            "v1 reconstruction plan cannot attest a strict parser binding without its underlying artifact"
        )
    coordinate = _exact_keys(root["coordinate_projection"], _COORDINATE_KEYS, name="coordinate_projection")
    if coordinate["status"] != COORDINATE_PROJECTION_STATUS:
        raise PdfReconstructionPlanError("coordinate projection status is invalid")
    if coordinate["selected_convention"] != "rotated_crop_relative_bottom_left_raw_units":
        raise PdfReconstructionPlanError("coordinate projection convention is invalid")
    if _sha("coordinate_projection.proof_sha256", coordinate["proof_sha256"]) != inputs["coordinate_calibration_sha256"]:
        raise PdfReconstructionPlanError("coordinate projection proof hash disagrees with input artifacts")
    if coordinate["parser_binding_status"] != "legacy_claim_unbound":
        raise PdfReconstructionPlanError("coordinate parser binding status disagrees with the structure input")

    if not isinstance(root["units"], list) or len(root["units"]) > MAX_PLAN_UNITS:
        raise PdfReconstructionPlanError("units must be an array")
    units: dict[str, Mapping[str, Any]] = {}
    candidates: dict[str, str] = {}
    primary_owners: dict[str, str] = {}
    for index, value_unit in enumerate(root["units"]):
        unit = _exact_keys(value_unit, _UNIT_KEYS, name=f"units[{index}]")
        unit_id = _trimmed(f"units[{index}].unit_id", unit["unit_id"])
        if (
            unit_id in units
            or not unit_id.startswith(("unit-native-", "unit-odl-"))
            or len(unit_id.rsplit("-", 1)[-1]) != 64
            or any(character not in _SHA256_HEX for character in unit_id.rsplit("-", 1)[-1])
        ):
            raise PdfReconstructionPlanError("unit identity is invalid or duplicated")
        units[unit_id] = unit
        origin = unit["origin"]
        if origin not in {"native", "opendataloader"}:
            raise PdfReconstructionPlanError("unit origin is invalid")
        candidate_id = unit["source_candidate_id"]
        if origin == "native":
            if candidate_id is not None or unit["kind"] != "native_text":
                raise PdfReconstructionPlanError("native unit must be an atomic native_text")
        else:
            candidate_id = _trimmed("source_candidate_id", candidate_id)
            if _CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
                raise PdfReconstructionPlanError("source_candidate_id is invalid")
            if candidate_id in candidates:
                raise PdfReconstructionPlanError("structure candidate appears in more than one unit")
            candidates[candidate_id] = unit_id
            if unit["kind"] not in _ODL_KINDS:
                raise PdfReconstructionPlanError("parser unit kind is invalid")
            if unit_id != _unit_id("odl", candidate_id, source_sha256=root["source_pdf_sha256"]):
                raise PdfReconstructionPlanError("parser unit identity does not bind its candidate")
        _positive_int(f"units[{index}].page", unit["page"], maximum=256)
        if unit["page"] not in pages:
            raise PdfReconstructionPlanError("unit page is outside page_scope")
        if unit["bbox_pdf_user_space"] is not None:
            _bbox(unit["bbox_pdf_user_space"], name=f"units[{index}].bbox_pdf_user_space")
        if unit["alignment_status"] not in _ALIGNMENT_STATUSES or unit["use_policy"] not in _USE_POLICIES:
            raise PdfReconstructionPlanError("unit status or use policy is invalid")
        for field in ("primary_occurrence_ids", "reference_occurrence_ids", "header_unit_ids", "joiners", "reason_codes"):
            if (
                not isinstance(unit[field], list)
                or any(not isinstance(item, str) for item in unit[field])
                or len(unit[field]) != len(set(unit[field]))
            ):
                raise PdfReconstructionPlanError(f"units[{index}].{field} must be a unique array")
        if len(unit["reference_occurrence_ids"]) > MAX_CONTEXT_REFERENCES_PER_UNIT:
            raise PdfReconstructionPlanError("unit context reference count exceeds the safety cap")
        if any(reason not in _REASON_CODES for reason in unit["reason_codes"]):
            raise PdfReconstructionPlanError("unit contains an unsupported reason code")
        if unit["joiners"]:
            raise PdfReconstructionPlanError("v1 reconstruction plan forbids composite evidence joiners")
        if origin == "native":
            if unit["alignment_status"] != "accepted" or unit["use_policy"] != "evidence_atomic":
                raise PdfReconstructionPlanError("native unit must be accepted atomic evidence")
            if len(unit["primary_occurrence_ids"]) != 1 or unit["reference_occurrence_ids"]:
                raise PdfReconstructionPlanError("native unit must own exactly one occurrence")
            if unit_id != _unit_id(
                "native",
                unit["primary_occurrence_ids"][0],
                source_sha256=root["source_pdf_sha256"],
            ):
                raise PdfReconstructionPlanError("native unit identity does not bind its occurrence")
            if unit["kind"] != "native_text" or unit["bbox_pdf_user_space"] is None or unit["reason_codes"]:
                raise PdfReconstructionPlanError("native owner shape is invalid")
            if unit["parent_unit_id"] is not None:
                raise PdfReconstructionPlanError("native atomic evidence cannot have a parser parent")
        else:
            if unit["primary_occurrence_ids"] or unit["use_policy"] == "evidence_atomic":
                raise PdfReconstructionPlanError("parser unit must never own native evidence")
            if unit["alignment_status"] == "accepted":
                raise PdfReconstructionPlanError("v1 parser unit cannot be accepted")
            expected_policy = "context_only" if unit["alignment_status"] == "partial" else "diagnostic_only"
            if unit["use_policy"] != expected_policy:
                raise PdfReconstructionPlanError("parser alignment status and use policy disagree")
            reasons = set(unit["reason_codes"])
            if unit["use_policy"] == "context_only":
                if not unit["reference_occurrence_ids"] or unit["bbox_pdf_user_space"] is None:
                    raise PdfReconstructionPlanError("context unit must carry projected occurrence references")
                if "legacy_odl_non_promotable" not in reasons or reasons & _REJECTION_REASON_CODES:
                    raise PdfReconstructionPlanError("context unit reason codes are invalid")
                if unit["kind"] in _CONTAINER_KINDS and "container_reference_only" not in reasons:
                    raise PdfReconstructionPlanError("container context must remain reference-only")
                if unit["kind"] in _LEAF_KINDS:
                    if len(unit["reference_occurrence_ids"]) > 1 and "split_bullet_marker" not in reasons:
                        raise PdfReconstructionPlanError("multi-occurrence leaf requires an explicit lossless split reason")
                    if "split_bullet_marker" in reasons and len(unit["reference_occurrence_ids"]) != 2:
                        raise PdfReconstructionPlanError("split bullet context must reference exactly two occurrences")
            elif not reasons & _REJECTION_REASON_CODES:
                raise PdfReconstructionPlanError("diagnostic unit lacks a rejection reason")
            if unit["bbox_pdf_user_space"] is not None and unit["kind"] != "paragraph":
                if "calibration_kind_unverified" not in reasons:
                    raise PdfReconstructionPlanError("non-paragraph projection must retain its calibration scope warning")
            elif "calibration_kind_unverified" in reasons:
                raise PdfReconstructionPlanError("calibration scope warning is inconsistent with the unit")
        for occurrence_id in unit["primary_occurrence_ids"]:
            if occurrence_id in primary_owners:
                raise PdfReconstructionPlanError("native occurrence has duplicate primary owners")
            primary_owners[occurrence_id] = unit_id
        parent = unit["parent_unit_id"]
        if parent is not None and (
            parent == unit_id
            or parent not in units
            or units[parent]["origin"] != "opendataloader"
            or units[parent]["page"] != unit["page"]
        ):
            raise PdfReconstructionPlanError("parent_unit_id must refer to an earlier same-page parser unit")
        if unit["header_unit_ids"]:
            raise PdfReconstructionPlanError("v1 reconstruction plan does not infer header relationships")

    if (
        not isinstance(root["native_occurrence_ledger"], list)
        or len(root["native_occurrence_ledger"]) > MAX_NATIVE_OCCURRENCES
    ):
        raise PdfReconstructionPlanError("native_occurrence_ledger must be an array")
    ledger: dict[str, Mapping[str, Any]] = {}
    for index, value_entry in enumerate(root["native_occurrence_ledger"]):
        entry = _exact_keys(value_entry, _LEDGER_KEYS, name=f"native_occurrence_ledger[{index}]")
        occurrence_id = _trimmed("occurrence_id", entry["occurrence_id"])
        match = _OCCURRENCE_ID_RE.fullmatch(occurrence_id)
        if match is None or int(match.group(1)) != entry["page"] or int(match.group(2)) != entry["source_item_index"]:
            raise PdfReconstructionPlanError("occurrence_id must bind ledger page and source item index")
        if occurrence_id in ledger:
            raise PdfReconstructionPlanError("native occurrence ledger identity is duplicated")
        ledger[occurrence_id] = entry
        if _nonnegative_int("source_item_index", entry["source_item_index"]) < 0:
            raise PdfReconstructionPlanError("source item index is invalid")
        if _positive_int("ledger page", entry["page"], maximum=256) not in pages:
            raise PdfReconstructionPlanError("ledger page is outside page_scope")
        if entry["bbox_pdf_user_space"] is not None:
            _bbox(entry["bbox_pdf_user_space"], name="ledger bbox")
        if entry["substantive_status"] not in {"substantive", "non_substantive", "invalid_geometry"}:
            raise PdfReconstructionPlanError("ledger substantive status is invalid")
        if entry["disposition"] not in {"owned_atomic", "non_substantive", "rejected_invalid_geometry"}:
            raise PdfReconstructionPlanError("ledger disposition is invalid")
        if (
            not isinstance(entry["reason_codes"], list)
            or any(not isinstance(reason, str) for reason in entry["reason_codes"])
            or len(entry["reason_codes"]) != len(set(entry["reason_codes"]))
        ):
            raise PdfReconstructionPlanError("ledger reason codes must be a unique string array")
        owner = entry["primary_owner_unit_id"]
        if owner is not None and (owner not in units or primary_owners.get(occurrence_id) != owner):
            raise PdfReconstructionPlanError("ledger primary owner is not reciprocal")
        if owner is None and occurrence_id in primary_owners:
            raise PdfReconstructionPlanError("unit primary ownership is missing from ledger")
        if owner is not None:
            owner_unit = units[owner]
            if (
                entry["substantive_status"] != "substantive"
                or entry["disposition"] != "owned_atomic"
                or entry["reason_codes"]
                or entry["bbox_pdf_user_space"] is None
                or owner_unit["origin"] != "native"
                or owner_unit["page"] != entry["page"]
                or _bbox(owner_unit["bbox_pdf_user_space"], name="native owner bbox")
                != _bbox(entry["bbox_pdf_user_space"], name="owned ledger bbox")
            ):
                raise PdfReconstructionPlanError("native owner does not match its ledger geometry and disposition")
        elif entry["substantive_status"] == "non_substantive":
            if entry["disposition"] != "non_substantive" or entry["reason_codes"] != ["non_substantive_native"]:
                raise PdfReconstructionPlanError("non-substantive ledger disposition is invalid")
        elif entry["substantive_status"] == "invalid_geometry":
            if (
                entry["disposition"] != "rejected_invalid_geometry"
                or entry["reason_codes"] != ["invalid_native_geometry"]
            ):
                raise PdfReconstructionPlanError("invalid-geometry ledger disposition is invalid")
        else:
            raise PdfReconstructionPlanError("substantive native occurrence must have one atomic owner")
        if (
            not isinstance(entry["context_unit_ids"], list)
            or any(not isinstance(context_id, str) for context_id in entry["context_unit_ids"])
            or len(entry["context_unit_ids"]) != len(set(entry["context_unit_ids"]))
        ):
            raise PdfReconstructionPlanError("ledger context unit ids must be unique")
        if len(entry["context_unit_ids"]) > MAX_CONTEXT_MEMBERSHIPS_PER_OCCURRENCE:
            raise PdfReconstructionPlanError("ledger context membership count exceeds the safety cap")
        for context_id in entry["context_unit_ids"]:
            context = units.get(context_id)
            if context is None or context["use_policy"] != "context_only" or occurrence_id not in context["reference_occurrence_ids"]:
                raise PdfReconstructionPlanError("ledger context reference is not reciprocal")
        if any(reason not in _REASON_CODES for reason in entry["reason_codes"]):
            raise PdfReconstructionPlanError("ledger contains an unsupported reason code")
    for occurrence_id in primary_owners:
        if occurrence_id not in ledger:
            raise PdfReconstructionPlanError("unit owns an occurrence absent from the ledger")
    for unit in units.values():
        for occurrence_id in unit["reference_occurrence_ids"]:
            if occurrence_id not in ledger:
                raise PdfReconstructionPlanError("unit references an occurrence absent from the ledger")
            if ledger[occurrence_id]["page"] != unit["page"]:
                raise PdfReconstructionPlanError("parser unit cannot reference a native occurrence on another page")
            if (
                unit["bbox_pdf_user_space"] is None
                or ledger[occurrence_id]["bbox_pdf_user_space"] is None
                or not _matches(unit["bbox_pdf_user_space"], ledger[occurrence_id]["bbox_pdf_user_space"])
            ):
                raise PdfReconstructionPlanError("parser bbox does not geometrically bind its native reference")
            present = unit["unit_id"] in ledger[occurrence_id]["context_unit_ids"]
            if unit["use_policy"] == "context_only" and not present:
                raise PdfReconstructionPlanError("context unit reference is missing from the reciprocal ledger")
            if unit["use_policy"] != "context_only" and present:
                raise PdfReconstructionPlanError("diagnostic reference must not become a context membership")
        if unit["origin"] != "opendataloader":
            continue
        reasons = set(unit["reason_codes"])
        references = unit["reference_occurrence_ids"]
        if reasons & {"no_candidate_bbox", "invalid_candidate_bbox_projection", "unsupported_candidate_kind"}:
            if references or unit["bbox_pdf_user_space"] is not None:
                raise PdfReconstructionPlanError("unprojectable parser diagnostic must not carry geometry references")
        if "no_native_occurrence" in reasons and references:
            raise PdfReconstructionPlanError("no-native parser diagnostic must not carry native references")
        if "cross_column_or_row_merge" in reasons and len(references) < 2:
            raise PdfReconstructionPlanError("cross-row/column diagnostic must retain all matched references")
        if "multi_occurrence_leaf_unverified" in reasons and len(references) < 2:
            raise PdfReconstructionPlanError("multi-occurrence diagnostic must retain all matched references")
        if "competing_leaf_context" in reasons and not references:
            raise PdfReconstructionPlanError("competing-leaf diagnostic must retain its matched references")

    partial_leaf_by_occurrence: dict[str, list[str]] = {}
    for unit in units.values():
        if unit["origin"] != "opendataloader" or unit["kind"] not in _LEAF_KINDS:
            continue
        if unit["use_policy"] != "context_only":
            continue
        for occurrence_id in unit["reference_occurrence_ids"]:
            partial_leaf_by_occurrence.setdefault(occurrence_id, []).append(unit["unit_id"])
    if any(len(unit_ids) > 1 for unit_ids in partial_leaf_by_occurrence.values()):
        raise PdfReconstructionPlanError("competing parser leaves must remain rejected diagnostics")

    if (
        not isinstance(root["rejected_candidates"], list)
        or len(root["rejected_candidates"]) > MAX_CANDIDATES
    ):
        raise PdfReconstructionPlanError("rejected_candidates must be an array")
    rejected_ids: set[str] = set()
    for index, value_rejected in enumerate(root["rejected_candidates"]):
        rejected = _exact_keys(value_rejected, _REJECTED_KEYS, name=f"rejected_candidates[{index}]")
        candidate_id = _trimmed("rejected source_candidate_id", rejected["source_candidate_id"])
        unit_id = _trimmed("rejected unit_id", rejected["unit_id"])
        if candidate_id in rejected_ids or candidates.get(candidate_id) != unit_id:
            raise PdfReconstructionPlanError("rejected candidate identity is invalid or duplicated")
        rejected_ids.add(candidate_id)
        if units[unit_id]["use_policy"] != "diagnostic_only" or rejected["reason_codes"] != units[unit_id]["reason_codes"]:
            raise PdfReconstructionPlanError("rejected candidate does not match its diagnostic unit")
    expected_rejected = {
        candidate_id for candidate_id, unit_id in candidates.items() if units[unit_id]["use_policy"] == "diagnostic_only"
    }
    if rejected_ids != expected_rejected:
        raise PdfReconstructionPlanError("rejected candidate ledger is incomplete")

    metrics = _exact_keys(root["metrics"], _METRIC_KEYS, name="metrics")
    if metrics["native_ownership_gate_status"] not in {"passed", "failed"}:
        raise PdfReconstructionPlanError("metrics.native_ownership_gate_status is invalid")
    for name, item in metrics.items():
        if name in {"native_ownership_gate_status", "candidate_alignment_counts", "candidate_use_policy_counts"}:
            continue
        _nonnegative_int(f"metrics.{name}", item)
    alignment_metrics = _exact_keys(
        metrics["candidate_alignment_counts"],
        frozenset({"accepted", "partial", "rejected"}),
        name="metrics.candidate_alignment_counts",
    )
    policy_metrics = _exact_keys(
        metrics["candidate_use_policy_counts"],
        frozenset({"context_only", "diagnostic_only"}),
        name="metrics.candidate_use_policy_counts",
    )
    for name, item in alignment_metrics.items():
        _nonnegative_int(f"metrics.candidate_alignment_counts.{name}", item)
    for name, item in policy_metrics.items():
        _nonnegative_int(f"metrics.candidate_use_policy_counts.{name}", item)
    if metrics["evidence_composite_unit_count"] != 0:
        raise PdfReconstructionPlanError("v1 reconstruction plan forbids composite evidence")
    if metrics["scoped_native_occurrence_count"] != len(ledger):
        raise PdfReconstructionPlanError("native occurrence metric disagrees with ledger")
    if metrics["structure_candidate_count"] != len(candidates):
        raise PdfReconstructionPlanError("candidate metric disagrees with units")
    if metrics["rejected_candidate_count"] != len(rejected_ids):
        raise PdfReconstructionPlanError("rejected metric disagrees with ledger")
    expected_substantive = sum(
        entry["substantive_status"] in {"substantive", "invalid_geometry"} for entry in ledger.values()
    )
    expected_native_text_status = (
        "native_text_available" if expected_substantive > 0 else "requires_ocr_semantic_v2"
    )
    if root["native_text_status"] != expected_native_text_status:
        raise PdfReconstructionPlanError("native text status disagrees with the occurrence ledger")
    expected_atomic = sum(unit["use_policy"] == "evidence_atomic" for unit in units.values())
    expected_unowned = sum(
        entry["substantive_status"] in {"substantive", "invalid_geometry"}
        and entry["primary_owner_unit_id"] is None
        for entry in ledger.values()
    )
    expected_max_context = max((len(entry["context_unit_ids"]) for entry in ledger.values()), default=0)
    if metrics["substantive_native_occurrence_count"] != expected_substantive:
        raise PdfReconstructionPlanError("substantive native metric disagrees with ledger")
    if metrics["atomic_evidence_unit_count"] != expected_atomic:
        raise PdfReconstructionPlanError("atomic evidence metric disagrees with units")
    if metrics["unowned_substantive_occurrence_count"] != expected_unowned:
        raise PdfReconstructionPlanError("unowned native metric disagrees with ledger")
    if metrics["duplicate_primary_owner_count"] != 0:
        raise PdfReconstructionPlanError("duplicate primary owner metric must be zero after validation")
    if metrics["max_context_reference_count"] != expected_max_context:
        raise PdfReconstructionPlanError("context reference metric disagrees with ledger")
    expected_gate = "passed" if expected_substantive > 0 and expected_unowned == 0 else "failed"
    if metrics["native_ownership_gate_status"] != expected_gate:
        raise PdfReconstructionPlanError("native ownership gate status disagrees with ownership metrics")
    expected_alignment = {status: 0 for status in ("accepted", "partial", "rejected")}
    expected_policies = {policy: 0 for policy in ("context_only", "diagnostic_only")}
    for unit_id in candidates.values():
        unit = units[unit_id]
        expected_alignment[unit["alignment_status"]] += 1
        expected_policies[unit["use_policy"]] += 1
    if alignment_metrics != expected_alignment or policy_metrics != expected_policies:
        raise PdfReconstructionPlanError("candidate count metrics disagree with units")
    return root


def validate_reconstruction_plan_against_inputs(
    value: Mapping[str, Any],
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    structure_candidates: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    calibration_proof: Mapping[str, Any],
    expected_calibration_proof_sha256: str,
) -> Mapping[str, Any]:
    """Authenticate a persisted plan by deterministic replay of every input.

    The standalone validator intentionally proves only internal consistency.
    This boundary revalidates the source-bound artifacts through the builder,
    rebuilds every unit and ledger link, and then requires canonical byte
    identity.  Callers handling an untrusted render directory must first run
    ``validate_render_manifest_files`` so page-image bytes are also rebound.
    """

    validated = validate_reconstruction_plan(value)
    expected = build_pdf_reconstruction_plan(
        source_pdf=source_pdf,
        native_capture=native_capture,
        structure_candidates=structure_candidates,
        render_manifest=render_manifest,
        calibration_proof=calibration_proof,
        expected_calibration_proof_sha256=expected_calibration_proof_sha256,
    )
    if canonical_reconstruction_plan_json(validated) != canonical_reconstruction_plan_json(expected):
        raise PdfReconstructionPlanError(
            "reconstruction plan does not match deterministic replay of its input artifacts"
        )
    return validated


def canonical_reconstruction_plan_json(value: Mapping[str, Any]) -> bytes:
    """Structurally validate and serialize canonical JSON; this is not authentication."""

    validated = validate_reconstruction_plan(value)
    try:
        return json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PdfReconstructionPlanError("reconstruction plan is not canonical JSON") from error


__all__ = [
    "COORDINATE_PROJECTION_STATUS",
    "PdfReconstructionPlanError",
    "SCHEMA_VERSION",
    "build_pdf_reconstruction_plan",
    "canonical_reconstruction_plan_json",
    "validate_reconstruction_plan",
    "validate_reconstruction_plan_against_inputs",
]
