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
