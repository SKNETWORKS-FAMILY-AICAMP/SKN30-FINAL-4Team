import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.services.cpl.golden_evaluator import evaluate_golden


def test_golden_evaluator_is_one_to_one_and_checks_normalized_values() -> None:
    golden = {
        "fields": {
            "LEGAL_BASIS": {
                "status": "PRESENT",
                "occurrences": [
                    _gold("법률 A 제1조", {"law_name": "법률 A", "article": "제1조"}),
                    _gold("조례 B 제2조", {"law_name": "조례 B", "article": "제2조"}),
                ],
            },
            "BUSINESS_PERIOD": {
                "status": "PRESENT",
                "occurrences": [
                    _gold(
                        "2026. 1. 1. ~ 2028. 12. 31.",
                        {"start": "2026-01-01", "end": "2028-12-31"},
                    )
                ],
            },
        }
    }
    report = {
        "self_check": {
            "items": [
                {
                    "field_code": "LEGAL_BASIS",
                    "status": "PRESENT",
                    "occurrences": [
                        _actual(
                            "법률 A 제1조, 조례 B 제2조",
                            {"law_name": "법률 A", "article": None},
                        )
                    ],
                },
                {
                    "field_code": "BUSINESS_PERIOD",
                    "status": "PRESENT",
                    "occurrences": [
                        _actual("2026. 1. 1. ~ 2028. 12. 31.", None)
                    ],
                },
            ]
        }
    }

    score = evaluate_golden(golden, report)

    assert score["field_status"] == {"correct": 2, "total": 2}
    assert score["occurrences"]["matched"] == 2
    assert score["occurrences"]["golden_total"] == 3
    assert score["occurrences"]["actual_total"] == 2
    assert score["normalized_values"] == {
        "correct": 0,
        "total": 2,
        "accuracy": 0.0,
    }


@pytest.mark.parametrize("name", ["mockup_03", "mockup_08"])
def test_authored_golden_occurrences_exist_in_the_exact_hwpx_cells(name: str) -> None:
    root = Path(__file__).parents[2]
    golden = json.loads(
        (root / "samples" / "golden" / f"{name}.json").read_text(encoding="utf-8")
    )
    hwpx = root / golden["document"]
    with zipfile.ZipFile(hwpx) as archive:
        section = ET.fromstring(archive.read("Contents/section0.xml"))

    cells: dict[tuple[int, int], str] = {}
    for cell in section.iter():
        if not cell.tag.endswith("}tc"):
            continue
        address = next(child for child in cell if child.tag.endswith("}cellAddr"))
        text = "".join(
            node.text or "" for node in cell.iter() if node.tag.endswith("}t")
        )
        cells[(int(address.attrib["rowAddr"]), int(address.attrib["colAddr"]))] = text

    occurrences = [
        occurrence
        for field in golden["fields"].values()
        for occurrence in field["occurrences"]
    ]
    assert len(occurrences) == golden["occurrence_count"]
    for occurrence in occurrences:
        match = re.fullmatch(
            r"body:\d+:cell:(?P<row>\d+):(?P<col>\d+)",
            occurrence["evidence_ref"],
        )
        assert match is not None
        cell_text = cells[(int(match.group("row")), int(match.group("col")))]
        assert occurrence["raw_text"] in cell_text


def _gold(raw_text: str, normalized_value: object) -> dict:
    return {
        "evidence_ref": "body:4:cell:0:1",
        "raw_text": raw_text,
        "axis_code": None,
        "source_role": None,
        "normalized_value": normalized_value,
    }


def _actual(excerpt: str, normalized_value: object) -> dict:
    return {
        "field_code": None,
        "block_id": "body:4",
        "source_locator": {"table_cell": {"row": 0, "col": 1}},
        "excerpt": excerpt,
        "axis_code": None,
        "source_role": None,
        "normalized_value": normalized_value,
    }
