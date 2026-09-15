"""Deterministic comparison of a structured extraction with a frozen semantic gold file."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import SemanticExtraction
from .table_policy import resolve_table_policy


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()


def _facts_from_gold(gold: dict[str, Any]) -> list[dict[str, Any]]:
    return gold["a_facts"]


def _matches(gold: dict[str, Any], actual: Any) -> bool:
    if gold["kind"] != actual.kind or gold["field_name"] != actual.field_name.value:
        return False
    if gold["scope"] != actual.scope.value or gold["status"] != actual.status.value:
        return False
    if not set(gold["source_block_ids"]) & set(actual.source_block_ids):
        return False

    if gold["kind"] == "delivery_role":
        return (
            _compact(gold["organization_name"]) == _compact(actual.organization_name)
            and gold["canonical_role"] == actual.canonical_role
        )

    expected_normalized = gold.get("normalized_value")
    actual_normalized = getattr(actual, "normalized_value", None)
    if expected_normalized:
        if actual_normalized is None:
            return False
        actual_data = actual_normalized.model_dump(exclude_none=True, mode="json")
        for key, value in expected_normalized.items():
            if actual_data.get(key) != value:
                return False

    return _compact(gold["value_raw"]) in _compact(actual.value_raw) or _compact(actual.value_raw) in _compact(gold["value_raw"])


def evaluate_extraction(extraction: SemanticExtraction, gold_path: str | Path) -> dict[str, Any]:
    """Return field-level matching without asking another model to judge the output."""

    gold = json.loads(Path(gold_path).read_text())
    if gold["status"] != "frozen":
        raise ValueError("evaluation requires a frozen gold file")
    if extraction.notice_id != gold["notice_id"]:
        raise ValueError("extraction notice_id does not match the gold file")

    unmatched_actual = set(range(len(extraction.facts)))
    matched, missing = [], []
    for expected in _facts_from_gold(gold):
        matching_index = next(
            (index for index in unmatched_actual if _matches(expected, extraction.facts[index])),
            None,
        )
        if matching_index is None:
            missing.append(expected["gold_id"])
        else:
            unmatched_actual.remove(matching_index)
            matched.append({"gold_id": expected["gold_id"], "fact_index": matching_index})

    expected_tables = gold.get("b_table_catalog", [])
    matched_tables = [
        item["table_ref"]
        for item in expected_tables
        if any(
            table.table_ref == item["table_ref"]
            and table.table_class.value == item["table_class"]
            and resolve_table_policy(table).policy.value == item["policy"]
            for table in extraction.table_catalog
        )
    ]
    missing_tables = [item["table_ref"] for item in expected_tables if item["table_ref"] not in matched_tables]

    true_positive = len(matched)
    false_positive = len(unmatched_actual)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / len(gold["a_facts"]) if gold["a_facts"] else 1.0
    return {
        "notice_id": extraction.notice_id,
        "gold_path": str(gold_path),
        "a_facts": {
            "expected": len(gold["a_facts"]),
            "matched": matched,
            "missing_gold_ids": missing,
            "extra_fact_indexes": sorted(unmatched_actual),
            "precision": precision,
            "recall": recall,
        },
        "b_tables": {
            "expected": len(expected_tables),
            "matched_table_refs": matched_tables,
            "missing_table_refs": missing_tables,
        },
    }
