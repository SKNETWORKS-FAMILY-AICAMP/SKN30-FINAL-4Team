"""Fail-closed, textless fragment grouping for native-first PDF shadowing.

This is deliberately not a paragraph reconstruction or a Common IR producer.
It records only an exceptionally narrow consensus: a rejected ODL paragraph
proposal and one strict Surya ``text`` region identify precisely the same,
contiguous set of native occurrences on one page.  Native strings, OCR
strings, geometry, headings, and hierarchy are intentionally absent.

The validators in this module accept an already decoded JSON-derived
``Mapping``.  They are semantic validators, not bounded raw-byte JSON parsers.
Any future persisted-artifact reader must add byte, depth, node, string, and
duplicate-key limits before calling them.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .coordinate_manifest import CoordinateManifestError, pixel_to_user_bbox
from .reconstruction_plan import (
    MAX_NATIVE_OCCURRENCES,
    PdfReconstructionPlanError,
    canonical_reconstruction_plan_json,
    validate_reconstruction_plan_against_inputs,
)
from .render_manifest import PdfRenderManifest, PdfRenderManifestError
from .surya_layout_artifact import (
    SCHEMA_VERSION as SURYA_SCHEMA_VERSION,
    SuryaLayoutArtifact,
    SuryaLayoutArtifactError,
    SuryaProducerIdentity,
    validate_surya_layout_artifact,
)


SCHEMA_VERSION = "pdf_fragment_groups/v1"
MAX_FRAGMENT_GROUPS = 25_000
MAX_OCCURRENCES_PER_GROUP = 5_000
MAX_REJECTIONS = 25_000
MAX_REGION_NATIVE_COMPARISONS = 5_000_000
# This is deliberately no larger than the bounded geometry scan above.  It
# also bounds a persisted sidecar before a replay can inspect every member.
MAX_AGGREGATE_MEMBERSHIPS = MAX_REGION_NATIVE_COMPARISONS
# Bounds candidate-specific broad-region veto work.  Geometry membership is
# precomputed above, but intersecting region memberships for every rejected
# ODL proposal is separately attacker-controlled and must not grow with the
# product of candidates, regions, and occurrences.
MAX_PROPOSAL_REGION_WORK = 5_000_000
# Output memberships are semantic references to the reconstruction ledger, so
# they cannot exceed the upstream bounded native occurrence universe.
MAX_OUTPUT_MEMBERSHIPS = MAX_NATIVE_OCCURRENCES
MATERIAL_OVERLAP_FRACTION = 0.01
MIN_MATERIAL_OVERLAP_AREA = 1e-4
_SHA = frozenset("0123456789abcdef")
_OCC = re.compile(r"^occ:inspector:p(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6]):t(?:0|[1-9][0-9]{0,4}|[1-4][0-9]{5})$")
_UNIT = re.compile(r"^unit-(?:native|odl)-[0-9a-f]{64}$")
_REGION = re.compile(r"^p[0-9]{4}-text-[0-9]{4}$")
_GROUP = re.compile(r"^fragment-[0-9a-f]{64}$")
_ROOT_KEYS = frozenset({"schema_version", "evaluation_only", "non_promotable", "standalone_validation_scope", "notice_id", "source_pdf_sha256", "page_scope", "input_artifacts", "fragment_groups", "rejected_proposals", "metrics"})
_INPUT_KEYS = frozenset({"reconstruction_plan_schema_version", "reconstruction_plan_sha256", "surya_layout_artifact_schema_version", "surya_layout_artifact_sha256"})
_GROUP_KEYS = frozenset({"fragment_group_id", "page", "occurrence_ids", "owner_unit_ids", "odl_unit_id", "surya_region_id"})
_REJECTED_KEYS = frozenset({"odl_unit_id", "reason_codes"})
_METRIC_KEYS = frozenset({"eligible_odl_paragraph_count", "accepted_fragment_group_count", "rejected_proposal_count"})
_REASONS = frozenset({"cross_page", "non_contiguous_substantive_source_order", "no_unique_surya_text_region", "surya_region_extra_native_occurrence", "surya_region_material_overlap", "competing_odl_paragraph", "wrong_native_owner"})


class PdfFragmentGroupsError(ValueError):
    """Raised when a decoded fragment-group sidecar is ambiguous or tampered."""


def _keys(value: object, expected: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value.keys()) != expected:
        raise PdfFragmentGroupsError(f"{name} keys are invalid")
    return value


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(item not in _SHA for item in value):
        raise PdfFragmentGroupsError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _positive(name: str, value: object, maximum: int = 256) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise PdfFragmentGroupsError(f"{name} must be a bounded positive integer")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PdfFragmentGroupsError("fragment groups are not canonical JSON") from error


def _native_owner(source_sha256: str, occurrence_id: str) -> str:
    return "unit-native-" + sha256(f"{source_sha256}:native:{occurrence_id}".encode("utf-8")).hexdigest()


def _group_id(source_sha256: str, page: int, occurrences: Sequence[str], odl_unit_id: str, region_id: str) -> str:
    payload = {"source_pdf_sha256": source_sha256, "page": page, "occurrence_ids": list(occurrences), "odl_unit_id": odl_unit_id, "surya_region_id": region_id}
    return "fragment-" + sha256(_canonical(payload)).hexdigest()


def _occurrence_parts(value: str) -> tuple[int, int] | None:
    match = _OCC.fullmatch(value)
    if match is None:
        return None
    # The regex bounds digit lengths before conversion; retain the numerical
    # upper bound too so runtime and the native ledger cap agree exactly.
    page_and_index = value.removeprefix("occ:inspector:p").split(":t", 1)
    page, source_index = int(page_and_index[0]), int(page_and_index[1])
    if source_index >= MAX_NATIVE_OCCURRENCES:
        return None
    return page, source_index


def _contains(outer: Sequence[float], inner: Sequence[float], tolerance: float = 1e-5) -> bool:
    return outer[0] - tolerance <= inner[0] and outer[1] - tolerance <= inner[1] and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance


def _center_inside(outer: Sequence[float], inner: Sequence[float], tolerance: float = 1e-5) -> bool:
    center_x = (inner[0] + inner[2]) / 2
    center_y = (inner[1] + inner[3]) / 2
    return outer[0] - tolerance <= center_x <= outer[2] + tolerance and outer[1] - tolerance <= center_y <= outer[3] + tolerance


def _materially_overlaps(left: Sequence[float], right: Sequence[float]) -> bool:
    """Use a fixed relative-and-absolute threshold, never a fuzzy heuristic."""
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    overlap = width * height
    smaller = min((left[2] - left[0]) * (left[3] - left[1]), (right[2] - right[0]) * (right[3] - right[1]))
    return overlap >= max(MIN_MATERIAL_OVERLAP_AREA, smaller * MATERIAL_OVERLAP_FRACTION)


def _strict_rectangle(region: Any) -> bool:
    x0, y0, x1, y1 = region.bbox_px
    # Surya canonicalizes polygon direction/rotation lexicographically.  The
    # only accepted representation therefore is its canonical rectangle.
    return region.polygon_px == ((x0, y0), (x0, y1), (x1, y1), (x1, y0))


def _add_rejection(items: dict[str, set[str]], unit_id: str, reason: str) -> None:
    items.setdefault(unit_id, set()).add(reason)


def build_pdf_fragment_groups(
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    structure_candidates: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    calibration_proof: Mapping[str, Any],
    expected_calibration_proof_sha256: str,
    reconstruction_plan: Mapping[str, Any],
    surya_layout_artifact: SuryaLayoutArtifact | Mapping[str, Any],
    expected_surya_producer: SuryaProducerIdentity,
    expected_surya_logical_compute_key: str,
    expected_surya_pages: Sequence[int],
) -> dict[str, Any]:
    """Build a deterministic, non-promotable consensus sidecar.

    The reconstruction replay is deliberately first: this function never
    treats a caller-provided plan as a source of occurrence ownership.
    """
    try:
        plan = validate_reconstruction_plan_against_inputs(
            reconstruction_plan, source_pdf=source_pdf, native_capture=native_capture,
            structure_candidates=structure_candidates, render_manifest=render_manifest,
            calibration_proof=calibration_proof,
            expected_calibration_proof_sha256=expected_calibration_proof_sha256,
        )
    except PdfReconstructionPlanError as error:
        raise PdfFragmentGroupsError(f"reconstruction plan is invalid: {error}") from error
    page_scope = tuple(plan["page_scope"])
    if isinstance(expected_surya_pages, (str, bytes)):
        raise PdfFragmentGroupsError("expected Surya pages must exactly equal reconstruction plan page_scope")
    try:
        expected_pages = tuple(expected_surya_pages)
    except TypeError as error:
        raise PdfFragmentGroupsError("expected Surya pages must be a page sequence") from error
    if expected_pages != page_scope:
        raise PdfFragmentGroupsError("expected Surya pages must exactly equal reconstruction plan page_scope")
    try:
        manifest = render_manifest if isinstance(render_manifest, PdfRenderManifest) else PdfRenderManifest.from_dict(render_manifest)
        surya = validate_surya_layout_artifact(
            surya_layout_artifact, render_manifest=manifest,
            expected_logical_compute_key=expected_surya_logical_compute_key,
            expected_producer=expected_surya_producer, expected_requested_pages=expected_pages,
        )
    except (PdfRenderManifestError, SuryaLayoutArtifactError) as error:
        raise PdfFragmentGroupsError(f"Surya layout artifact is invalid: {error}") from error

    source_sha = plan["source_pdf_sha256"]
    ledger = {entry["occurrence_id"]: entry for entry in plan["native_occurrence_ledger"]}
    coordinates = {entry.page: entry.coordinate_manifest for entry in manifest.pages}
    regions_by_page: dict[int, list[tuple[str, tuple[float, float, float, float]]]] = {}
    for page_entry in surya.pages:
        converted: list[tuple[str, tuple[float, float, float, float]]] = []
        for region in page_entry.regions:
            if region.label != "text":
                continue
            if not _strict_rectangle(region):
                raise PdfFragmentGroupsError("Surya text region polygon must exactly equal its axis-aligned bbox corners")
            try:
                converted.append((region.region_id, pixel_to_user_bbox(coordinates[page_entry.page], region.bbox_px)))
            except CoordinateManifestError as error:
                raise PdfFragmentGroupsError(f"Surya region coordinate conversion failed: {error}") from error
        regions_by_page[page_entry.page] = converted

    substantive_by_page: dict[int, list[Mapping[str, Any]]] = {}
    for entry in ledger.values():
        if entry["substantive_status"] == "substantive":
            substantive_by_page.setdefault(entry["page"], []).append(entry)
    source_position_by_occurrence: dict[str, int] = {}
    for values in substantive_by_page.values():
        values.sort(key=lambda item: item["source_item_index"])
        source_position_by_occurrence.update({item["occurrence_id"]: index for index, item in enumerate(values)})

    comparison_count = sum(
        len(regions_by_page.get(page, ())) * len(entries)
        for page, entries in substantive_by_page.items()
    )
    if comparison_count > MAX_REGION_NATIVE_COMPARISONS:
        raise PdfFragmentGroupsError("Surya region/native comparison count exceeds safety cap")
    # Bound geometry work once. Candidates later use set intersection rather
    # than multiplying the same region/native scan by every ODL proposal.
    region_members: dict[str, frozenset[str]] = {}
    regions_per_occurrence: dict[str, set[str]] = {}
    exact_regions_by_members: dict[frozenset[str], list[str]] = {}
    ambiguous_regions: set[str] = set()
    aggregate_memberships = 0
    for page, regions in regions_by_page.items():
        for region_id, region_bbox in regions:
            contained = frozenset(
                entry["occurrence_id"] for entry in substantive_by_page.get(page, [])
                if entry["bbox_pdf_user_space"] is not None and _contains(region_bbox, entry["bbox_pdf_user_space"])
            )
            aggregate_memberships += len(contained)
            if aggregate_memberships > MAX_AGGREGATE_MEMBERSHIPS:
                raise PdfFragmentGroupsError("region/native aggregate membership count exceeds safety cap")
            region_members[region_id] = contained
            exact_regions_by_members.setdefault(contained, []).append(region_id)
            # Exact membership is full-bbox containment.  A different native
            # atom that materially overlaps the region, or whose center lies
            # inside it, is nevertheless ambiguous and vetoes promotion.
            for entry in substantive_by_page.get(page, []):
                if entry["occurrence_id"] not in contained and entry["bbox_pdf_user_space"] is not None and (
                    _center_inside(region_bbox, entry["bbox_pdf_user_space"])
                    or _materially_overlaps(region_bbox, entry["bbox_pdf_user_space"])
                ):
                    ambiguous_regions.add(region_id)
                    break
            for occurrence_id in contained:
                regions_per_occurrence.setdefault(occurrence_id, set()).add(region_id)

    eligible = [unit for unit in plan["units"] if unit["origin"] == "opendataloader" and unit["kind"] == "paragraph" and unit["use_policy"] == "diagnostic_only" and "multi_occurrence_leaf_unverified" in unit["reason_codes"]]
    if len(eligible) > MAX_REJECTIONS:
        raise PdfFragmentGroupsError("eligible ODL paragraph count exceeds safety cap")
    rejected: dict[str, set[str]] = {}
    candidates: list[tuple[Mapping[str, Any], tuple[str, ...], str]] = []
    valid_proposals: list[tuple[Mapping[str, Any], tuple[str, ...]]] = []
    proposal_region_work = 0
    for unit in eligible:
        unit_id = unit["unit_id"]
        occurrence_ids = tuple(unit["reference_occurrence_ids"])
        entries = [ledger.get(occurrence_id) for occurrence_id in occurrence_ids]
        if len(occurrence_ids) < 2 or len(occurrence_ids) > MAX_OCCURRENCES_PER_GROUP or any(entry is None for entry in entries):
            _add_rejection(rejected, unit_id, "wrong_native_owner")
            continue
        pages = {entry["page"] for entry in entries if entry is not None}
        if len(pages) != 1 or unit["page"] not in pages:
            _add_rejection(rejected, unit_id, "cross_page")
            continue
        if any(entry["primary_owner_unit_id"] != _native_owner(source_sha, entry["occurrence_id"]) for entry in entries if entry is not None):
            _add_rejection(rejected, unit_id, "wrong_native_owner")
            continue
        page = unit["page"]
        expected_set = set(occurrence_ids)
        positions = [source_position_by_occurrence[item] for item in occurrence_ids if item in source_position_by_occurrence]
        if len(positions) != len(occurrence_ids) or positions != list(range(min(positions), max(positions) + 1)):
            _add_rejection(rejected, unit_id, "non_contiguous_substantive_source_order")
            continue
        valid_proposals.append((unit, occurrence_ids))
        region_sets = [regions_per_occurrence.get(item, set()) for item in occurrence_ids]
        # Start from the smallest candidate-region set.  Charge every
        # membership probe before it executes, using deterministic set sizes.
        ordered_sets = sorted(region_sets, key=len)
        proposal_region_work += len(ordered_sets[0])
        if proposal_region_work > MAX_PROPOSAL_REGION_WORK:
            raise PdfFragmentGroupsError("proposal/region work count exceeds safety cap")
        containing = set(ordered_sets[0])
        for region_set in ordered_sets[1:]:
            proposal_region_work += len(containing)
            if proposal_region_work > MAX_PROPOSAL_REGION_WORK:
                raise PdfFragmentGroupsError("proposal/region work count exceeds safety cap")
            containing = {region_id for region_id in containing if region_id in region_set}
        if proposal_region_work > MAX_PROPOSAL_REGION_WORK:
            raise PdfFragmentGroupsError("proposal/region work count exceeds safety cap")
        expected_members = frozenset(expected_set)
        matching = [region_id for region_id in exact_regions_by_members.get(expected_members, []) if region_id not in ambiguous_regions]
        # A broader region that also contains the proposal is deliberately an
        # ambiguity: the narrow exact region must not silently win selection.
        extra = any(region_members[region_id] != expected_set for region_id in containing)
        material = any(region_id in ambiguous_regions for region_id in containing)
        if extra:
            _add_rejection(rejected, unit_id, "surya_region_extra_native_occurrence")
        if material:
            _add_rejection(rejected, unit_id, "surya_region_material_overlap")
        if len(matching) != 1:
            _add_rejection(rejected, unit_id, "no_unique_surya_text_region")
            continue
        if not extra:
            candidates.append((unit, occurrence_ids, matching[0]))

    # A native atom must never select between two ODL paragraph interpretations,
    # even when one of them independently fails a layout condition.
    first_owner: dict[str, str] = {}
    competing: set[str] = set()
    for unit, occurrences in valid_proposals:
        for occurrence in occurrences:
            prior = first_owner.setdefault(occurrence, unit["unit_id"])
            if prior != unit["unit_id"]:
                competing.update((prior, unit["unit_id"]))
    for unit_id in competing:
        _add_rejection(rejected, unit_id, "competing_odl_paragraph")
    groups = []
    for unit, occurrences, region_id in candidates:
        if unit["unit_id"] in rejected:
            continue
        groups.append({
            "fragment_group_id": _group_id(source_sha, unit["page"], occurrences, unit["unit_id"], region_id),
            "page": unit["page"], "occurrence_ids": list(occurrences),
            "owner_unit_ids": [_native_owner(source_sha, item) for item in occurrences],
            "odl_unit_id": unit["unit_id"], "surya_region_id": region_id,
        })
    groups.sort(key=lambda item: (item["page"], item["occurrence_ids"], item["odl_unit_id"]))
    rejected_items = [{"odl_unit_id": unit_id, "reason_codes": sorted(reasons)} for unit_id, reasons in sorted(rejected.items())]
    if len(groups) + len(rejected_items) > MAX_REJECTIONS:
        raise PdfFragmentGroupsError("accepted and rejected proposal count exceeds safety cap")
    if sum(len(group["occurrence_ids"]) for group in groups) > MAX_OUTPUT_MEMBERSHIPS:
        raise PdfFragmentGroupsError("fragment group output membership count exceeds native ledger cap")
    result = {
        "schema_version": SCHEMA_VERSION, "evaluation_only": True, "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only", "notice_id": plan["notice_id"], "source_pdf_sha256": source_sha,
        "page_scope": list(page_scope),
        "input_artifacts": {"reconstruction_plan_schema_version": plan["schema_version"], "reconstruction_plan_sha256": sha256(canonical_reconstruction_plan_json(plan)).hexdigest(), "surya_layout_artifact_schema_version": surya.schema_version, "surya_layout_artifact_sha256": surya.artifact_sha256()},
        "fragment_groups": groups, "rejected_proposals": rejected_items,
        "metrics": {"eligible_odl_paragraph_count": len(eligible), "accepted_fragment_group_count": len(groups), "rejected_proposal_count": len(rejected_items)},
    }
    return dict(validate_pdf_fragment_groups(result))


def validate_pdf_fragment_groups(value: Mapping[str, Any]) -> Mapping[str, Any]:
    root = _keys(value, _ROOT_KEYS, "fragment groups")
    if root["schema_version"] != SCHEMA_VERSION or root["evaluation_only"] is not True or root["non_promotable"] is not True or root["standalone_validation_scope"] != "internal_consistency_only":
        raise PdfFragmentGroupsError("fragment groups must remain evaluation-only and non-promotable")
    if not isinstance(root["notice_id"], str) or not root["notice_id"] or root["notice_id"] != root["notice_id"].strip() or len(root["notice_id"]) > 256 or any(0xD800 <= ord(item) <= 0xDFFF for item in root["notice_id"]):
        raise PdfFragmentGroupsError("notice_id must be a bounded non-empty trimmed string")
    source_sha = _sha("source_pdf_sha256", root["source_pdf_sha256"])
    if not isinstance(root["page_scope"], list) or not root["page_scope"]:
        raise PdfFragmentGroupsError("page_scope must be a non-empty array")
    pages = tuple(_positive("page_scope", page) for page in root["page_scope"])
    if pages != tuple(sorted(set(pages))):
        raise PdfFragmentGroupsError("page_scope must be sorted and unique")
    inputs = _keys(root["input_artifacts"], _INPUT_KEYS, "input_artifacts")
    if inputs["reconstruction_plan_schema_version"] != "pdf_reconstruction_plan/v1" or inputs["surya_layout_artifact_schema_version"] != SURYA_SCHEMA_VERSION:
        raise PdfFragmentGroupsError("input artifact schema versions are invalid")
    _sha("reconstruction_plan_sha256", inputs["reconstruction_plan_sha256"]); _sha("surya_layout_artifact_sha256", inputs["surya_layout_artifact_sha256"])
    if not isinstance(root["fragment_groups"], list) or len(root["fragment_groups"]) > MAX_FRAGMENT_GROUPS:
        raise PdfFragmentGroupsError("fragment_groups exceeds safety cap")
    seen_groups: set[str] = set(); seen_units: set[str] = set(); seen_occurrences: set[str] = set(); seen_regions: set[str] = set(); previous: tuple[Any, ...] | None = None; aggregate_memberships = 0
    for index, raw in enumerate(root["fragment_groups"]):
        group = _keys(raw, _GROUP_KEYS, f"fragment_groups[{index}]")
        if not isinstance(group["fragment_group_id"], str) or not _GROUP.fullmatch(group["fragment_group_id"]) or group["fragment_group_id"] in seen_groups:
            raise PdfFragmentGroupsError("fragment group identity is invalid or duplicated")
        page = _positive("fragment group page", group["page"])
        if page not in pages or not isinstance(group["occurrence_ids"], list) or not isinstance(group["owner_unit_ids"], list) or not 2 <= len(group["occurrence_ids"]) <= MAX_OCCURRENCES_PER_GROUP or len(group["occurrence_ids"]) != len(group["owner_unit_ids"]):
            raise PdfFragmentGroupsError("fragment group occurrence bindings are invalid")
        occurrences = tuple(group["occurrence_ids"])
        if any(not isinstance(item, str) for item in occurrences):
            raise PdfFragmentGroupsError("fragment group occurrence identities are invalid")
        occurrence_parts = [_occurrence_parts(item) for item in occurrences]
        if len(set(occurrences)) != len(occurrences) or any(parts is None or parts[0] != page for parts in occurrence_parts):
            raise PdfFragmentGroupsError("fragment group occurrence identities are invalid")
        source_indices = [parts[1] for parts in occurrence_parts if parts is not None]
        if source_indices != sorted(source_indices) or len(set(source_indices)) != len(source_indices):
            raise PdfFragmentGroupsError("fragment group occurrence source order is invalid")
        aggregate_memberships += len(occurrences)
        if aggregate_memberships > MAX_OUTPUT_MEMBERSHIPS:
            raise PdfFragmentGroupsError("fragment group output membership count exceeds native ledger cap")
        owners = tuple(group["owner_unit_ids"])
        if any(not isinstance(item, str) or not _UNIT.fullmatch(item) or item != _native_owner(source_sha, occurrence) for item, occurrence in zip(owners, occurrences)):
            raise PdfFragmentGroupsError("fragment group native owners are invalid")
        if not isinstance(group["odl_unit_id"], str) or not group["odl_unit_id"].startswith("unit-odl-") or not _UNIT.fullmatch(group["odl_unit_id"]) or group["odl_unit_id"] in seen_units:
            raise PdfFragmentGroupsError("fragment group ODL unit identity is invalid")
        if not isinstance(group["surya_region_id"], str) or not _REGION.fullmatch(group["surya_region_id"]) or int(group["surya_region_id"][1:5]) != page or group["surya_region_id"] in seen_regions:
            raise PdfFragmentGroupsError("fragment group Surya region identity is invalid")
        if group["fragment_group_id"] != _group_id(source_sha, page, occurrences, group["odl_unit_id"], group["surya_region_id"]):
            raise PdfFragmentGroupsError("fragment group identity does not bind its members")
        sort_key = (page, occurrences, group["odl_unit_id"])
        if previous is not None and sort_key <= previous:
            raise PdfFragmentGroupsError("fragment_groups must be canonically ordered")
        if seen_occurrences.intersection(occurrences):
            raise PdfFragmentGroupsError("native occurrence cannot belong to multiple fragment groups")
        previous = sort_key; seen_groups.add(group["fragment_group_id"]); seen_units.add(group["odl_unit_id"]); seen_regions.add(group["surya_region_id"]); seen_occurrences.update(occurrences)
    if not isinstance(root["rejected_proposals"], list) or len(root["rejected_proposals"]) > MAX_REJECTIONS:
        raise PdfFragmentGroupsError("rejected_proposals exceeds safety cap")
    rejected_ids: set[str] = set(); previous_rejected: str | None = None
    for index, raw in enumerate(root["rejected_proposals"]):
        item = _keys(raw, _REJECTED_KEYS, f"rejected_proposals[{index}]")
        unit = item["odl_unit_id"]
        if not isinstance(unit, str) or not unit.startswith("unit-odl-") or not _UNIT.fullmatch(unit) or unit in rejected_ids or unit in seen_units or (previous_rejected is not None and unit <= previous_rejected):
            raise PdfFragmentGroupsError("rejected ODL unit identity is invalid")
        if not isinstance(item["reason_codes"], list) or not item["reason_codes"] or any(not isinstance(reason, str) for reason in item["reason_codes"]) or item["reason_codes"] != sorted(set(item["reason_codes"])) or any(reason not in _REASONS for reason in item["reason_codes"]):
            raise PdfFragmentGroupsError("rejected proposal reasons are invalid")
        rejected_ids.add(unit); previous_rejected = unit
    metrics = _keys(root["metrics"], _METRIC_KEYS, "metrics")
    if len(seen_groups) + len(rejected_ids) > MAX_REJECTIONS:
        raise PdfFragmentGroupsError("accepted and rejected proposal count exceeds safety cap")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in metrics.values()) or metrics["accepted_fragment_group_count"] != len(seen_groups) or metrics["rejected_proposal_count"] != len(rejected_ids) or metrics["eligible_odl_paragraph_count"] != len(seen_groups) + len(rejected_ids):
        raise PdfFragmentGroupsError("fragment group metrics are inconsistent")
    return root


def validate_pdf_fragment_groups_against_inputs(value: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
    validated = validate_pdf_fragment_groups(value)
    expected = build_pdf_fragment_groups(**kwargs)
    if canonical_pdf_fragment_groups_json(validated) != canonical_pdf_fragment_groups_json(expected):
        raise PdfFragmentGroupsError("fragment groups do not match deterministic replay of input artifacts")
    return validated


def canonical_pdf_fragment_groups_json(value: Mapping[str, Any]) -> bytes:
    return _canonical(validate_pdf_fragment_groups(value))


__all__ = ["SCHEMA_VERSION", "PdfFragmentGroupsError", "build_pdf_fragment_groups", "validate_pdf_fragment_groups", "validate_pdf_fragment_groups_against_inputs", "canonical_pdf_fragment_groups_json"]
