"""Evaluate one CPL report against a human-authored golden set."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def evaluate_golden(golden: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    actual_items = _actual_items(report)
    field_status_correct = 0
    golden_occurrences: list[dict[str, Any]] = []
    actual_occurrences: list[dict[str, Any]] = []

    for field_code, expected in golden["fields"].items():
        actual = actual_items.get(field_code, {"status": None, "occurrences": []})
        field_status_correct += actual.get("status") == expected.get("status")
        golden_occurrences.extend(
            {**occurrence, "field_code": field_code}
            for occurrence in expected.get("occurrences", [])
        )
        actual_occurrences.extend(
            {**occurrence, "field_code": field_code}
            for occurrence in actual.get("occurrences", [])
        )

    pairs = _maximum_matching(golden_occurrences, actual_occurrences)
    normalized_pairs = [
        (golden_occurrences[gold_index], actual_occurrences[actual_index])
        for gold_index, actual_index in pairs.items()
        if golden_occurrences[gold_index].get("normalized_value") is not None
    ]
    normalized_correct = sum(
        expected["normalized_value"] == actual.get("normalized_value")
        for expected, actual in normalized_pairs
    )
    return {
        "field_status": {
            "correct": field_status_correct,
            "total": len(golden["fields"]),
        },
        "occurrences": {
            "matched": len(pairs),
            "golden_total": len(golden_occurrences),
            "actual_total": len(actual_occurrences),
            "recall": _ratio(len(pairs), len(golden_occurrences)),
            "precision": _ratio(len(pairs), len(actual_occurrences)),
        },
        "normalized_values": {
            "correct": normalized_correct,
            "total": len(normalized_pairs),
            "accuracy": _ratio(normalized_correct, len(normalized_pairs)),
        },
    }


def _actual_items(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    container = report.get("self_check", report)
    return {item["field_code"]: item for item in container.get("items", [])}


def _maximum_matching(
    golden: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> dict[int, int]:
    actual_to_golden: dict[int, int] = {}

    def assign(gold_index: int, visited: set[int]) -> bool:
        for actual_index, candidate in enumerate(actual):
            if actual_index in visited or not _compatible(golden[gold_index], candidate):
                continue
            visited.add(actual_index)
            previous = actual_to_golden.get(actual_index)
            if previous is None or assign(previous, visited):
                actual_to_golden[actual_index] = gold_index
                return True
        return False

    for gold_index in range(len(golden)):
        assign(gold_index, set())
    return {gold_index: actual_index for actual_index, gold_index in actual_to_golden.items()}


def _compatible(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    if expected["field_code"] != actual.get("field_code"):
        return False
    if expected.get("axis_code") != actual.get("axis_code"):
        return False
    if expected.get("source_role") != actual.get("source_role"):
        return False
    expected_text = expected.get("raw_text", "")
    actual_text = actual.get("excerpt", actual.get("raw_text", ""))
    if not expected_text or not actual_text:
        return False
    if expected_text not in actual_text and actual_text not in expected_text:
        return False
    return _same_location(expected, actual)


def _same_location(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    reference = expected.get("evidence_ref", "")
    match = re.fullmatch(r"(?P<block>.+):cell:(?P<row>\d+):(?P<col>\d+)", reference)
    if match is None:
        return not reference or reference == actual.get("block_id")
    if match.group("block") != actual.get("block_id"):
        return False
    cell = actual.get("source_locator", {}).get("table_cell", {})
    return cell.get("row") == int(match.group("row")) and cell.get("col") == int(
        match.group("col")
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("golden", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    result = evaluate_golden(
        json.loads(args.golden.read_text(encoding="utf-8")),
        json.loads(args.report.read_text(encoding="utf-8")),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
