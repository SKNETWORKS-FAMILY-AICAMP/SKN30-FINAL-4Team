"""Deterministic regression evaluation for ``existing_program_profile/v0.2``.

Gold files are human-review fixtures, never model input.  This evaluator keeps
raw-fact, relation, projection, and provenance failures separate so a schema
upgrade is not misreported as a single extraction score.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .profile_v02 import validate_profile_v02


def _compact(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "").lower()


def _notice_key(value: str | None) -> str:
    """Profiles retain a storage namespace; Gold uses the portal notice id."""

    return (value or "").removeprefix("bizinfo:")


def _flatten_profile_facts(profile: dict[str, Any]) -> list[dict[str, Any]]:
    facts = [fact for rows in profile.get("comparison_profile", {}).values() for fact in rows]
    facts.extend(fact for component in profile.get("support_components", []) for fact in component.get("facts", []))
    return facts


def _component_mapping(
    gold: dict[str, Any], profile: dict[str, Any]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Map Gold component ids to actual ids by kind and server-recovered name."""

    unmatched = list(profile.get("support_components", []))
    mapping: dict[str, str] = {}
    missing: list[dict[str, Any]] = []
    for expected in gold.get("expected_components", []):
        anchor = expected.get("name_anchor", {})
        expected_name = anchor.get("anchor_text")
        found_index = next(
            (
                index
                for index, actual in enumerate(unmatched)
                if actual.get("component_kind") == expected.get("component_kind")
                and _compact(actual.get("name_raw")) == _compact(expected_name)
            ),
            None,
        )
        if found_index is None:
            missing.append({
                "component_id": expected.get("component_id"),
                "component_kind": expected.get("component_kind"),
                "name_anchor": anchor,
            })
            continue
        actual = unmatched.pop(found_index)
        mapping[expected["component_id"]] = actual["support_component_id"]
    return mapping, missing


def _fact_matches(
    expected: dict[str, Any],
    actual: dict[str, Any],
    component_map: dict[str, str],
) -> bool:
    if expected.get("field_name") != actual.get("field_name"):
        return False
    if expected.get("scope") != actual.get("scope"):
        return False
    expected_component = expected.get("component_id")
    if expected_component is not None and actual.get("support_component_id") != component_map.get(expected_component):
        return False
    if expected_component is None and actual.get("support_component_id") is not None:
        return False
    anchor = expected.get("source_anchor", {})
    source = actual.get("value_source") or {}
    return (
        source.get("source_block_id") == anchor.get("source_block_id")
        and actual.get("value_raw") == anchor.get("anchor_text")
    )


def _evaluate_relations(
    gold_facts: list[dict[str, Any]],
    matched_ids: dict[str, str],
    actual_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    missing_recipients: list[dict[str, Any]] = []
    for expected in gold_facts:
        source_actual_id = matched_ids.get(expected["gold_id"])
        if not source_actual_id:
            continue
        expected_recipients = expected.get("expected_recipient_gold_ids", [])
        actual_recipients = set(actual_by_id[source_actual_id].get("recipient_fact_ids", []))
        for expected_recipient in expected_recipients:
            actual_recipient_id = matched_ids.get(expected_recipient)
            if actual_recipient_id is None or actual_recipient_id not in actual_recipients:
                missing_recipients.append({
                    "fact_gold_id": expected["gold_id"],
                    "recipient_gold_id": expected_recipient,
                })

    return {"missing_recipient_relations": missing_recipients}


def _evaluate_variants(
    gold: dict[str, Any],
    matched_ids: dict[str, str],
    actual_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    missing: list[dict[str, Any]] = []
    for variant in gold.get("expected_variants", []):
        conditions = [matched_ids.get(item) for item in variant.get("condition_fact_ids", [])]
        supports = {matched_ids.get(item) for item in variant.get("support_fact_ids", [])}
        if not all(conditions) or None in supports:
            missing.append({"variant_id": variant.get("variant_id"), "reason": "constituent_fact_missing"})
            continue
        modified = set().union(*(set(actual_by_id[item].get("modifies_fact_ids", [])) for item in conditions))
        if not supports <= modified:
            missing.append({
                "variant_id": variant.get("variant_id"),
                "reason": "condition_to_support_relation_missing",
                "missing_actual_fact_ids": sorted(supports - modified),
            })
    return {"missing_variant_relations": missing}


def _evaluate_projections(
    gold_facts: list[dict[str, Any]],
    matched_ids: dict[str, str],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate only declared Gold expectations; absent projection stages are explicit."""

    projections = profile.get("derived_projections", [])
    measures = [
        measure
        for projection in projections
        if projection.get("projection_type") == "support_scale_measures"
        for measure in projection.get("measures", [])
    ]
    facets = [projection for projection in projections if projection.get("projection_type") == "support_facets"]
    missing_measures, missing_facets, missing_periods = [], [], []
    for expected in gold_facts:
        actual_fact_id = matched_ids.get(expected["gold_id"])
        if actual_fact_id is None:
            continue
        expected_measure = expected.get("expected_measure")
        if expected_measure:
            if not any(
                measure.get("source_fact_id") == actual_fact_id
                and all(measure.get(key) == value for key, value in expected_measure.items())
                for measure in measures
            ):
                missing_measures.append(expected["gold_id"])
        expected_facets = expected.get("expected_facets")
        if expected_facets:
            if not any(
                actual_fact_id in projection.get("source_fact_ids", [])
                and all(set(values) <= set(projection.get(key, [])) for key, values in expected_facets.items())
                for projection in facets
            ):
                missing_facets.append(expected["gold_id"])
        if expected.get("expected_period"):
            # Period normalization is intentionally not yet a projection
            # contract. Report the pending expectation rather than guessing a
            # representation or treating the raw fact as a normalized value.
            missing_periods.append(expected["gold_id"])
    return {
        "missing_scale_measures": missing_measures,
        "missing_support_facets": missing_facets,
        "pending_period_normalization": missing_periods,
    }


def evaluate_profile_v02(
    profile: dict[str, Any],
    selection_artifact: dict[str, Any],
    gold_path: str | Path,
    *,
    allow_draft: bool = False,
) -> dict[str, Any]:
    """Evaluate one v0.2 profile against a human-authored v0.2 Gold fixture."""

    gold = json.loads(Path(gold_path).read_text())
    if gold.get("gold_schema_version") != "0.2-draft" and gold.get("gold_schema_version") != "0.2":
        raise ValueError("evaluation requires a v0.2 Gold fixture")
    if gold.get("status") != "frozen" and not allow_draft:
        raise ValueError("evaluation requires frozen Gold; pass allow_draft only for fixture review")
    if _notice_key(profile.get("notice_id")) != _notice_key(gold.get("notice_id")):
        raise ValueError("profile notice_id does not match Gold")
    block_texts = selection_artifact.get("source_block_texts")
    if not isinstance(block_texts, dict):
        raise ValueError("selection artifact requires source_block_texts")

    provenance_issues = validate_profile_v02(profile, block_texts)
    component_map, missing_components = _component_mapping(gold, profile)
    actual_facts = _flatten_profile_facts(profile)
    actual_by_id = {fact["fact_id"]: fact for fact in actual_facts if fact.get("fact_id")}
    unmatched_actual = set(actual_by_id)
    matched_ids: dict[str, str] = {}
    missing_facts: list[dict[str, Any]] = []
    for expected in gold.get("expected_facts", []):
        matching_id = next(
            (
                fact_id
                for fact_id in sorted(unmatched_actual)
                if _fact_matches(expected, actual_by_id[fact_id], component_map)
            ),
            None,
        )
        if matching_id is None:
            missing_facts.append({"gold_id": expected["gold_id"], "field_name": expected["field_name"]})
        else:
            unmatched_actual.remove(matching_id)
            matched_ids[expected["gold_id"]] = matching_id

    expected_count = len(gold.get("expected_facts", []))
    matched_count = len(matched_ids)
    return {
        "notice_id": profile["notice_id"],
        "schema_version": profile.get("schema_version"),
        "gold_path": str(gold_path),
        "gold_status": gold.get("status"),
        "provenance": {"issues": provenance_issues, "passed": not provenance_issues},
        "components": {
            "expected": len(gold.get("expected_components", [])),
            "mapped": component_map,
            "missing": missing_components,
        },
        "raw_facts": {
            "expected": expected_count,
            "matched": matched_count,
            "recall": matched_count / expected_count if expected_count else 1.0,
            "missing": missing_facts,
            "extra_actual_fact_ids": sorted(unmatched_actual),
        },
        "relations": {
            **_evaluate_relations(gold.get("expected_facts", []), matched_ids, actual_by_id),
            **_evaluate_variants(gold, matched_ids, actual_by_id),
        },
        "projections": _evaluate_projections(gold.get("expected_facts", []), matched_ids, profile),
    }
