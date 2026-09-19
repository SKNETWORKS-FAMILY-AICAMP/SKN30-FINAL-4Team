"""Deterministic, textless context hypotheses for PDF reconstruction v1.

This sidecar projects two already-reviewed shadow signals without selecting or
merging either interpretation:

* every ``partial/context_only`` OpenDataLoader unit from the reconstruction
  plan; and
* every accepted strict fragment-consensus group.

The output deliberately contains no text, geometry, inferred headers,
joiners, claims, values, or coverage.  It is evaluation-only and cannot be
promoted into Common IR evidence.

Standalone validation proves internal consistency only.  Even
``validate_pdf_context_groups_against_inputs`` binds only the two immediate
sidecars.  A caller crossing a trust boundary must first authenticate the
reconstruction plan and fragment groups by their full source-artifact replay
boundaries.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from .fragment_groups import (
    SCHEMA_VERSION as FRAGMENT_GROUPS_SCHEMA_VERSION,
    PdfFragmentGroupsError,
    canonical_pdf_fragment_groups_json,
    validate_pdf_fragment_groups,
)
from .reconstruction_plan import (
    SCHEMA_VERSION as RECONSTRUCTION_PLAN_SCHEMA_VERSION,
    PdfReconstructionPlanError,
    canonical_reconstruction_plan_json,
    validate_reconstruction_plan,
)


SCHEMA_VERSION = "pdf_context_groups/v1"
CONTEXT_POLICY_VERSION = "pdf-context-groups/v1"
MAX_CONTEXT_GROUPS = 25_000
MAX_REFERENCES_PER_GROUP = 256
MAX_AGGREGATE_MEMBERSHIPS = 500_000

_SHA = frozenset("0123456789abcdef")
_OCCURRENCE = re.compile(
    r"^occ:inspector:p(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6]):"
    r"t(?:0|[1-9][0-9]{0,4}|[1-4][0-9]{5})$"
)
_ODL_UNIT = re.compile(r"^unit-odl-[0-9a-f]{64}$")
_FRAGMENT_GROUP = re.compile(r"^fragment-[0-9a-f]{64}$")
_CONTEXT_GROUP = re.compile(r"^context-[0-9a-f]{64}$")
_KINDS = frozenset(
    {
        "heading",
        "paragraph",
        "list",
        "list_item",
        "table",
        "table_row",
        "table_cell",
        "text_block",
        "image",
        "caption",
    }
)
_LEAF_KINDS = frozenset(
    {"heading", "paragraph", "list_item", "table_cell", "text_block", "caption"}
)
_BASES = frozenset({"plan_context_unit", "fragment_consensus"})
_BASIS_RANK = {"plan_context_unit": 0, "fragment_consensus": 1}
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "context_policy_version",
        "notice_id",
        "source_pdf_sha256",
        "page_scope",
        "input_artifacts",
        "context_groups",
        "metrics",
    }
)
_INPUT_KEYS = frozenset(
    {
        "reconstruction_plan_schema_version",
        "reconstruction_plan_sha256",
        "fragment_groups_schema_version",
        "fragment_groups_sha256",
    }
)
_GROUP_KEYS = frozenset(
    {
        "context_group_id",
        "basis",
        "page",
        "hypothesis_kind",
        "source_unit_id",
        "source_fragment_group_id",
        "reference_occurrence_ids",
        "structural_parent_unit_id",
        "context_role",
    }
)
_METRIC_KEYS = frozenset(
    {
        "plan_context_unit_count",
        "fragment_consensus_count",
        "context_group_count",
        "aggregate_reference_membership_count",
        "unique_reference_count",
        "max_reference_count",
        "structural_parent_link_count",
        "suppressed_parent_link_count",
    }
)


class PdfContextGroupsError(ValueError):
    """Raised when a context-group projection is ambiguous or tampered."""


def _keys(value: object, expected: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value.keys()) != expected:
        raise PdfContextGroupsError(f"{name} keys are invalid")
    return value


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(item not in _SHA for item in value):
        raise PdfContextGroupsError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _positive(name: str, value: object, *, maximum: int = 256) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise PdfContextGroupsError(f"{name} must be a bounded positive integer")
    return value


def _nonnegative(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PdfContextGroupsError(f"{name} must be a non-negative integer")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PdfContextGroupsError("context groups are not canonical JSON") from error


def _occurrence_parts(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str) or _OCCURRENCE.fullmatch(value) is None:
        return None
    page_and_index = value.removeprefix("occ:inspector:p").split(":t", 1)
    return int(page_and_index[0]), int(page_and_index[1])


def _context_group_id(
    source_pdf_sha256: str,
    basis: str,
    source_unit_id: str,
    source_fragment_group_id: str | None,
    page: int,
    hypothesis_kind: str,
    reference_occurrence_ids: Sequence[str],
    structural_parent_unit_id: str | None,
) -> str:
    payload = {
        "source_pdf_sha256": source_pdf_sha256,
        "basis": basis,
        "source_unit_id": source_unit_id,
        "source_fragment_group_id": source_fragment_group_id,
        "page": page,
        "hypothesis_kind": hypothesis_kind,
        "reference_occurrence_ids": list(reference_occurrence_ids),
        "structural_parent_unit_id": structural_parent_unit_id,
    }
    return "context-" + sha256(_canonical(payload)).hexdigest()


def _checked_references(
    *,
    references: object,
    page: int,
    ledger: Mapping[str, Mapping[str, Any]],
    source_name: str,
) -> tuple[tuple[str, ...], int]:
    if not isinstance(references, list) or not references or len(references) > MAX_REFERENCES_PER_GROUP:
        raise PdfContextGroupsError(f"{source_name} reference count exceeds the safety cap")
    if any(not isinstance(item, str) for item in references) or len(references) != len(set(references)):
        raise PdfContextGroupsError(f"{source_name} references must be unique occurrence identities")
    entries: list[Mapping[str, Any]] = []
    for occurrence_id in references:
        parts = _occurrence_parts(occurrence_id)
        entry = ledger.get(occurrence_id)
        if (
            parts is None
            or parts[0] != page
            or entry is None
            or entry["page"] != page
            or entry["source_item_index"] != parts[1]
            or entry["substantive_status"] != "substantive"
            or entry["disposition"] != "owned_atomic"
            or entry["primary_owner_unit_id"] is None
        ):
            raise PdfContextGroupsError(f"{source_name} references non-owned or non-substantive native evidence")
        entries.append(entry)
    source_indices = [entry["source_item_index"] for entry in entries]
    if source_indices != sorted(source_indices) or len(source_indices) != len(set(source_indices)):
        raise PdfContextGroupsError(f"{source_name} references must follow native source order")
    return tuple(references), source_indices[0]


def build_pdf_context_groups(
    reconstruction_plan: Mapping[str, Any],
    fragment_groups: Mapping[str, Any],
) -> dict[str, Any]:
    """Project immediate context hypotheses from two validated sidecars.

    This function performs standalone validation and canonical hash binding of
    its immediate inputs.  It does *not* replay their source PDF, native
    capture, ODL, render, calibration, or Surya artifacts.  Callers must first
    run those full source-artifact replay validators when trust crosses a
    persistence or network boundary.
    """

    try:
        plan = validate_reconstruction_plan(reconstruction_plan)
    except PdfReconstructionPlanError as error:
        raise PdfContextGroupsError(f"reconstruction plan is invalid: {error}") from error
    try:
        fragments = validate_pdf_fragment_groups(fragment_groups)
    except PdfFragmentGroupsError as error:
        raise PdfContextGroupsError(f"fragment groups are invalid: {error}") from error

    if (
        plan["native_text_status"] != "native_text_available"
        or plan["metrics"]["native_ownership_gate_status"] != "passed"
    ):
        raise PdfContextGroupsError(
            "context projection requires available native text and a passed ownership gate"
        )

    plan_sha256 = sha256(canonical_reconstruction_plan_json(plan)).hexdigest()
    fragment_sha256 = sha256(canonical_pdf_fragment_groups_json(fragments)).hexdigest()
    if (
        fragments["notice_id"] != plan["notice_id"]
        or fragments["source_pdf_sha256"] != plan["source_pdf_sha256"]
        or fragments["page_scope"] != plan["page_scope"]
        or fragments["input_artifacts"]["reconstruction_plan_schema_version"]
        != plan["schema_version"]
        or fragments["input_artifacts"]["reconstruction_plan_sha256"] != plan_sha256
    ):
        raise PdfContextGroupsError("fragment groups are not bound to the reconstruction plan")

    units = {unit["unit_id"]: unit for unit in plan["units"]}
    ledger = {entry["occurrence_id"]: entry for entry in plan["native_occurrence_ledger"]}
    projected: list[tuple[dict[str, Any], int]] = []
    aggregate_memberships = 0
    unique_references: set[str] = set()
    max_reference_count = 0
    structural_parent_link_count = 0
    suppressed_parent_link_count = 0

    def append_group(
        *,
        basis: str,
        unit: Mapping[str, Any],
        source_fragment_group_id: str | None,
        references: object,
        structural_parent_unit_id: str | None,
    ) -> None:
        nonlocal aggregate_memberships, max_reference_count
        if len(projected) >= MAX_CONTEXT_GROUPS:
            raise PdfContextGroupsError("context group count exceeds the safety cap")
        checked, first_source_index = _checked_references(
            references=references,
            page=unit["page"],
            ledger=ledger,
            source_name=unit["unit_id"],
        )
        aggregate_memberships += len(checked)
        if aggregate_memberships > MAX_AGGREGATE_MEMBERSHIPS:
            raise PdfContextGroupsError("aggregate context membership count exceeds the safety cap")
        unique_references.update(checked)
        max_reference_count = max(max_reference_count, len(checked))
        group = {
            "context_group_id": _context_group_id(
                plan["source_pdf_sha256"],
                basis,
                unit["unit_id"],
                source_fragment_group_id,
                unit["page"],
                unit["kind"],
                checked,
                structural_parent_unit_id,
            ),
            "basis": basis,
            "page": unit["page"],
            "hypothesis_kind": unit["kind"],
            "source_unit_id": unit["unit_id"],
            "source_fragment_group_id": source_fragment_group_id,
            "reference_occurrence_ids": list(checked),
            "structural_parent_unit_id": structural_parent_unit_id,
            "context_role": "context_only",
        }
        projected.append((group, first_source_index))

    plan_context_count = 0
    for unit in plan["units"]:
        if (
            unit["origin"] != "opendataloader"
            or unit["alignment_status"] != "partial"
            or unit["use_policy"] != "context_only"
        ):
            continue
        parent_id = unit["parent_unit_id"]
        parent = units.get(parent_id) if parent_id is not None else None
        structural_parent = (
            parent_id
            if parent is not None
            and parent["origin"] == "opendataloader"
            and parent["page"] == unit["page"]
            and parent["alignment_status"] == "partial"
            and parent["use_policy"] == "context_only"
            else None
        )
        if structural_parent is not None:
            structural_parent_link_count += 1
        elif parent_id is not None:
            suppressed_parent_link_count += 1
        append_group(
            basis="plan_context_unit",
            unit=unit,
            source_fragment_group_id=None,
            references=unit["reference_occurrence_ids"],
            structural_parent_unit_id=structural_parent,
        )
        plan_context_count += 1

    fragment_count = 0
    for fragment in fragments["fragment_groups"]:
        unit = units.get(fragment["odl_unit_id"])
        if (
            unit is None
            or unit["origin"] != "opendataloader"
            or unit["kind"] != "paragraph"
            or unit["alignment_status"] != "rejected"
            or unit["use_policy"] != "diagnostic_only"
            or "multi_occurrence_leaf_unverified" not in unit["reason_codes"]
            or unit["page"] != fragment["page"]
            or unit["reference_occurrence_ids"] != fragment["occurrence_ids"]
        ):
            raise PdfContextGroupsError(
                "fragment consensus must exactly bind one diagnostic ODL paragraph"
            )
        if unit["parent_unit_id"] is not None:
            suppressed_parent_link_count += 1
        append_group(
            basis="fragment_consensus",
            unit=unit,
            source_fragment_group_id=fragment["fragment_group_id"],
            references=fragment["occurrence_ids"],
            structural_parent_unit_id=None,
        )
        fragment_count += 1

    projected.sort(
        key=lambda item: (
            item[0]["page"],
            item[1],
            _BASIS_RANK[item[0]["basis"]],
            item[0]["source_unit_id"],
            item[0]["source_fragment_group_id"] or "",
        )
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "context_policy_version": CONTEXT_POLICY_VERSION,
        "notice_id": plan["notice_id"],
        "source_pdf_sha256": plan["source_pdf_sha256"],
        "page_scope": list(plan["page_scope"]),
        "input_artifacts": {
            "reconstruction_plan_schema_version": plan["schema_version"],
            "reconstruction_plan_sha256": plan_sha256,
            "fragment_groups_schema_version": fragments["schema_version"],
            "fragment_groups_sha256": fragment_sha256,
        },
        "context_groups": [item[0] for item in projected],
        "metrics": {
            "plan_context_unit_count": plan_context_count,
            "fragment_consensus_count": fragment_count,
            "context_group_count": len(projected),
            "aggregate_reference_membership_count": aggregate_memberships,
            "unique_reference_count": len(unique_references),
            "max_reference_count": max_reference_count,
            "structural_parent_link_count": structural_parent_link_count,
            "suppressed_parent_link_count": suppressed_parent_link_count,
        },
    }
    return dict(validate_pdf_context_groups(result))


def validate_pdf_context_groups(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate internal structure only; input hashes are not authenticated."""

    root = _keys(value, _ROOT_KEYS, "context groups")
    if (
        root["schema_version"] != SCHEMA_VERSION
        or root["evaluation_only"] is not True
        or root["non_promotable"] is not True
        or root["standalone_validation_scope"] != "internal_consistency_only"
        or root["context_policy_version"] != CONTEXT_POLICY_VERSION
    ):
        raise PdfContextGroupsError("context groups must remain evaluation-only and non-promotable")
    notice_id = root["notice_id"]
    if (
        not isinstance(notice_id, str)
        or not notice_id
        or notice_id != notice_id.strip()
        or len(notice_id) > 256
        or any(0xD800 <= ord(item) <= 0xDFFF for item in notice_id)
    ):
        raise PdfContextGroupsError("notice_id must be a bounded non-empty trimmed string")
    source_sha = _sha("source_pdf_sha256", root["source_pdf_sha256"])
    if not isinstance(root["page_scope"], list) or not root["page_scope"]:
        raise PdfContextGroupsError("page_scope must be a non-empty array")
    pages = tuple(_positive("page_scope", page) for page in root["page_scope"])
    if pages != tuple(sorted(set(pages))):
        raise PdfContextGroupsError("page_scope must be sorted and unique")

    inputs = _keys(root["input_artifacts"], _INPUT_KEYS, "input_artifacts")
    if (
        inputs["reconstruction_plan_schema_version"] != RECONSTRUCTION_PLAN_SCHEMA_VERSION
        or inputs["fragment_groups_schema_version"] != FRAGMENT_GROUPS_SCHEMA_VERSION
    ):
        raise PdfContextGroupsError("input artifact schema versions are invalid")
    _sha("reconstruction_plan_sha256", inputs["reconstruction_plan_sha256"])
    _sha("fragment_groups_sha256", inputs["fragment_groups_sha256"])

    raw_groups = root["context_groups"]
    if not isinstance(raw_groups, list) or len(raw_groups) > MAX_CONTEXT_GROUPS:
        raise PdfContextGroupsError("context_groups exceeds the safety cap")
    seen_group_ids: set[str] = set()
    seen_source_units: set[str] = set()
    seen_fragment_ids: set[str] = set()
    fragment_occurrences: set[str] = set()
    plan_leaf_occurrences: set[str] = set()
    previous_key: tuple[Any, ...] | None = None
    aggregate_memberships = 0
    unique_references: set[str] = set()
    max_reference_count = 0
    structural_parent_link_count = 0
    basis_counts = {"plan_context_unit": 0, "fragment_consensus": 0}
    for index, raw_group in enumerate(raw_groups):
        group = _keys(raw_group, _GROUP_KEYS, f"context_groups[{index}]")
        group_id = group["context_group_id"]
        if (
            not isinstance(group_id, str)
            or _CONTEXT_GROUP.fullmatch(group_id) is None
            or group_id in seen_group_ids
        ):
            raise PdfContextGroupsError("context group identity is invalid or duplicated")
        basis = group["basis"]
        if basis not in _BASES:
            raise PdfContextGroupsError("context group basis is invalid")
        page = _positive("context group page", group["page"])
        if page not in pages:
            raise PdfContextGroupsError("context group page is outside page_scope")
        kind = group["hypothesis_kind"]
        if kind not in _KINDS:
            raise PdfContextGroupsError("context hypothesis kind is invalid")
        source_unit = group["source_unit_id"]
        if (
            not isinstance(source_unit, str)
            or _ODL_UNIT.fullmatch(source_unit) is None
            or source_unit in seen_source_units
        ):
            raise PdfContextGroupsError("context source unit is invalid or duplicated")
        fragment_id = group["source_fragment_group_id"]
        parent_id = group["structural_parent_unit_id"]
        if basis == "plan_context_unit":
            if fragment_id is not None:
                raise PdfContextGroupsError("plan context must not claim a fragment source")
            if parent_id is not None and (
                not isinstance(parent_id, str)
                or _ODL_UNIT.fullmatch(parent_id) is None
                or parent_id == source_unit
            ):
                raise PdfContextGroupsError("structural parent identity is invalid")
        else:
            if (
                not isinstance(fragment_id, str)
                or _FRAGMENT_GROUP.fullmatch(fragment_id) is None
                or fragment_id in seen_fragment_ids
                or parent_id is not None
                or kind != "paragraph"
            ):
                raise PdfContextGroupsError("fragment consensus source binding is invalid")
        references = group["reference_occurrence_ids"]
        if (
            not isinstance(references, list)
            or not references
            or len(references) > MAX_REFERENCES_PER_GROUP
            or any(not isinstance(item, str) for item in references)
            or len(references) != len(set(references))
        ):
            raise PdfContextGroupsError("context references are invalid or exceed the safety cap")
        parts = [_occurrence_parts(item) for item in references]
        if any(item is None or item[0] != page for item in parts):
            raise PdfContextGroupsError("context references must be same-page occurrence identities")
        source_indices = [item[1] for item in parts if item is not None]
        if source_indices != sorted(source_indices) or len(source_indices) != len(set(source_indices)):
            raise PdfContextGroupsError("context references must follow native source order")
        if basis == "fragment_consensus" and len(references) < 2:
            raise PdfContextGroupsError("fragment consensus must reference at least two native atoms")
        if basis == "fragment_consensus":
            if fragment_occurrences.intersection(references):
                raise PdfContextGroupsError(
                    "native occurrence cannot belong to multiple fragment consensus groups"
                )
            fragment_occurrences.update(references)
        if basis == "plan_context_unit" and kind in _LEAF_KINDS:
            if plan_leaf_occurrences.intersection(references):
                raise PdfContextGroupsError(
                    "native occurrence cannot belong to multiple plan leaf hypotheses"
                )
            plan_leaf_occurrences.update(references)
        aggregate_memberships += len(references)
        if aggregate_memberships > MAX_AGGREGATE_MEMBERSHIPS:
            raise PdfContextGroupsError("aggregate context membership count exceeds the safety cap")
        unique_references.update(references)
        max_reference_count = max(max_reference_count, len(references))
        if parent_id is not None:
            structural_parent_link_count += 1
        if group["context_role"] != "context_only":
            raise PdfContextGroupsError("context group role is invalid")
        expected_id = _context_group_id(
            source_sha,
            basis,
            source_unit,
            fragment_id,
            page,
            kind,
            references,
            parent_id,
        )
        if group_id != expected_id:
            raise PdfContextGroupsError("context group identity does not bind its source hypothesis")
        sort_key = (
            page,
            source_indices[0],
            _BASIS_RANK[basis],
            source_unit,
            fragment_id or "",
        )
        if previous_key is not None and sort_key <= previous_key:
            raise PdfContextGroupsError("context_groups must be canonically ordered")
        previous_key = sort_key
        seen_group_ids.add(group_id)
        seen_source_units.add(source_unit)
        if fragment_id is not None:
            seen_fragment_ids.add(fragment_id)
        basis_counts[basis] += 1

    groups_by_source_unit = {
        group["source_unit_id"]: group
        for group in raw_groups
    }
    parent_by_source_unit: dict[str, str | None] = {}
    for group in raw_groups:
        parent_id = group["structural_parent_unit_id"]
        parent_by_source_unit[group["source_unit_id"]] = parent_id
        if parent_id is None:
            continue
        parent = groups_by_source_unit.get(parent_id)
        if parent is None:
            raise PdfContextGroupsError("structural parent must be an emitted context group")
        if (
            parent["basis"] != "plan_context_unit"
            or parent["page"] != group["page"]
        ):
            raise PdfContextGroupsError(
                "structural parent must be a same-page plan context"
            )

    # Every node has at most one parent. Three-state iterative traversal proves
    # acyclicity in O(groups + links), rather than rescanning a deep parent
    # chain from every child.
    state: dict[str, int] = {}
    for source_unit_id in parent_by_source_unit:
        if state.get(source_unit_id) == 2:
            continue
        path: list[str] = []
        current_id: str | None = source_unit_id
        while current_id is not None:
            current_state = state.get(current_id, 0)
            if current_state == 2:
                break
            if current_state == 1:
                raise PdfContextGroupsError("structural parent graph must be acyclic")
            state[current_id] = 1
            path.append(current_id)
            current_id = parent_by_source_unit[current_id]
        for visited_id in path:
            state[visited_id] = 2

    metrics = _keys(root["metrics"], _METRIC_KEYS, "metrics")
    for name, item in metrics.items():
        _nonnegative(f"metrics.{name}", item)
    if (
        metrics["plan_context_unit_count"] != basis_counts["plan_context_unit"]
        or metrics["fragment_consensus_count"] != basis_counts["fragment_consensus"]
        or metrics["context_group_count"] != len(raw_groups)
        or metrics["aggregate_reference_membership_count"] != aggregate_memberships
        or metrics["unique_reference_count"] != len(unique_references)
        or metrics["max_reference_count"] != max_reference_count
        or metrics["structural_parent_link_count"] != structural_parent_link_count
        or metrics["suppressed_parent_link_count"] > metrics["context_group_count"]
        or metrics["context_group_count"]
        != metrics["plan_context_unit_count"] + metrics["fragment_consensus_count"]
    ):
        raise PdfContextGroupsError("context group metrics are inconsistent")
    return root


def validate_pdf_context_groups_against_inputs(
    value: Mapping[str, Any],
    reconstruction_plan: Mapping[str, Any],
    fragment_groups: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Require canonical identity with a deterministic immediate-input replay.

    Full source-artifact replay of both inputs remains a caller prerequisite.
    """

    validated = validate_pdf_context_groups(value)
    expected = build_pdf_context_groups(reconstruction_plan, fragment_groups)
    if canonical_pdf_context_groups_json(validated) != canonical_pdf_context_groups_json(expected):
        raise PdfContextGroupsError(
            "context groups do not match deterministic replay of input artifacts"
        )
    return validated


def canonical_pdf_context_groups_json(value: Mapping[str, Any]) -> bytes:
    """Structurally validate and serialize canonical JSON."""

    return _canonical(validate_pdf_context_groups(value))


__all__ = [
    "SCHEMA_VERSION",
    "CONTEXT_POLICY_VERSION",
    "PdfContextGroupsError",
    "build_pdf_context_groups",
    "validate_pdf_context_groups",
    "validate_pdf_context_groups_against_inputs",
    "canonical_pdf_context_groups_json",
]
