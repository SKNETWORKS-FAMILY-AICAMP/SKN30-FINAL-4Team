from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

try:
    import test_primary_table_continuation as continuation_fixtures
except ModuleNotFoundError as error:
    if error.name != "test_primary_table_continuation":
        raise
    from backend.vendor.common_ir_pipeline.tests import (
        test_primary_table_continuation as continuation_fixtures,
    )

import common_ir_pipeline.pdf_fusion.primary_table_continuation_evaluation as evaluation
from common_ir_pipeline.pdf_fusion.primary_table_continuation import (
    build_pdf_primary_table_continuation,
    canonical_pdf_primary_table_continuation_json,
)
from common_ir_pipeline.pdf_fusion.primary_table_continuation_gold import (
    load_primary_table_continuation_gold_file,
)
from common_ir_pipeline.pdf_fusion.primary_table_continuation_evaluation import (
    PdfPrimaryTableContinuationEvaluationError,
    _compare_replayed_table_continuations,
    canonical_primary_table_continuation_evaluation_json,
    validate_primary_table_continuation_evaluation,
)
from common_ir_pipeline.pdf_fusion.primary_table_grid import (
    build_pdf_primary_table_grid,
    canonical_pdf_primary_table_grid_json,
)
from common_ir_pipeline.pdf_fusion.primary_table_grid_gold import (
    load_primary_table_grid_gold_file,
)


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/common_ir_pipeline/pdf_fusion/schemas/"
    "pdf_primary_table_continuation_evaluation_v1.schema.json"
)
ACTUAL_BASE_GOLD = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/primary_table_grid_gold_114788_p3_p4.v1.json"
)
ACTUAL_CONTINUATION_GOLD = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/"
    "primary_table_continuation_gold_114788_p3_p4.v1.json"
)
NOTICE_ID = "PBLN_000000000114788"
SOURCE_SHA = "1" * 64
NATIVE_SHA = "2" * 64
GRID_SHA = "3" * 64
GRID_GOLD_SHA = "4" * 64
GRID_EVALUATION_SHA = "5" * 64
CANDIDATE_SHA = "6" * 64
GOLD_SHA = "7" * 64


def _hash_id(prefix: str, digit: str) -> str:
    return prefix + digit * 64


P3_TABLE = _hash_id("table-grid-", "a")
P4_TABLE = _hash_id("table-grid-", "b")
P4_LOWER_TABLE = _hash_id("table-grid-", "c")
P5_TABLE = _hash_id("table-grid-", "d")
P6_TABLE = _hash_id("table-grid-", "e")


def _table(table_id: str, page: int, occurrence_indexes: tuple[int, int]) -> dict:
    return {
        "table_grid_id": table_id,
        "page": page,
        "row_count": 1,
        "column_count": 2,
        "source_table_candidate_id": "odl-" + "0" * 64,
        "surya_region_id": f"p{page:04d}-table-0001",
        "rows": [
            {
                "row_id": _hash_id("table-row-", str(page)),
                "row_index": 1,
                "cells": [
                    {
                        "cell_id": _hash_id("table-cell-", str(page)),
                        "row_index": 1,
                        "column_index": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "source_cell_candidate_id": "odl-" + "1" * 64,
                        "occurrence_ids": [
                            f"occ:inspector:p{page}:t{occurrence_indexes[0]}"
                        ],
                    },
                    {
                        "cell_id": _hash_id("table-cell-", chr(102 + page)),
                        "row_index": 1,
                        "column_index": 2,
                        "row_span": 1,
                        "column_span": 1,
                        "source_cell_candidate_id": "odl-" + "2" * 64,
                        "occurrence_ids": [
                            f"occ:inspector:p{page}:t{occurrence_indexes[1]}"
                        ],
                    },
                ],
            }
        ],
    }


def _relation(
    digit: str,
    predecessor: str,
    predecessor_page: int,
    successor: str,
    successor_page: int,
    *,
    kind: str = "between_rows",
    mapping: tuple[tuple[int, int], ...] = ((1, 1), (2, 2)),
) -> dict:
    return {
        "continuation_id": _hash_id("table-continuation-", digit),
        "relation_kind": kind,
        "predecessor_table_grid_id": predecessor,
        "predecessor_page": predecessor_page,
        "successor_table_grid_id": successor,
        "successor_page": successor_page,
        "column_mapping": [
            {
                "predecessor_column_index": left,
                "successor_column_index": right,
            }
            for left, right in mapping
        ],
        "geometry_metrics": {
            "predecessor_bottom_gap_ppm": 10,
            "successor_top_gap_ppm": 10,
            "outer_x_interval_iou_ppm": 1_000_000,
            "max_column_edge_drift_ppm": 0,
            "min_column_interval_iou_ppm": 1_000_000,
        },
    }


def _page_local_candidate() -> dict:
    return {
        "schema_version": "pdf_primary_table_grid/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "policy": {"policy_version": "primary_page_local_table_grid/v1"},
        "notice_id": NOTICE_ID,
        "source_pdf_sha256": SOURCE_SHA,
        "page_scope": [3, 4, 5, 6],
        "input_artifacts": {},
        "tables": [
            _table(P3_TABLE, 3, (1, 2)),
            _table(P4_TABLE, 4, (3, 4)),
            _table(P4_LOWER_TABLE, 4, (10, 11)),
            _table(P5_TABLE, 5, (20, 21)),
            _table(P6_TABLE, 6, (30, 31)),
        ],
    }


def _page_local_gold() -> dict:
    def scope(page: int, prefix: str, indexes: tuple[int, int]) -> dict:
        return {
            "scope_id": f"p{page}.support.table-grid",
            "physical_page": page,
            "segment_id": f"p{page}.support.segment",
            "row_ids": [f"p{page}.support.r1"],
            "column_ids": [f"p{page}.support.c1", f"p{page}.support.c2"],
            "cells": [
                {
                    "cell_id": f"p{page}.support.cell.r1c1",
                    "row_ids": [f"p{page}.support.r1"],
                    "column_ids": [f"p{page}.support.c1"],
                    "content_status": "populated",
                    "occurrence_ids": [f"occ:inspector:p{page}:t{indexes[0]}"],
                },
                {
                    "cell_id": f"p{page}.support.cell.r1c2",
                    "row_ids": [f"p{page}.support.r1"],
                    "column_ids": [f"p{page}.support.c2"],
                    "content_status": "populated",
                    "occurrence_ids": [f"occ:inspector:p{page}:t{indexes[1]}"],
                },
            ],
        }

    return {
        "schema_version": "pdf_primary_table_grid_gold/v1",
        "notice_id": NOTICE_ID,
        "source": {
            "source_pdf_sha256": SOURCE_SHA,
            "native_capture": {"canonical_sha256": NATIVE_SHA},
        },
        "page_scope": [3, 4],
        "reviewed_scopes": [
            scope(3, "p3", (1, 2)),
            scope(4, "p4", (3, 4)),
        ],
    }


def _gold() -> dict:
    return {
        "schema_version": "pdf_primary_table_continuation_gold/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "notice_id": NOTICE_ID,
        "source": {
            "source_pdf_sha256": SOURCE_SHA,
            "native_capture": {"canonical_sha256": NATIVE_SHA},
            "base_table_grid_gold": {
                "schema_version": "pdf_primary_table_grid_gold/v1",
                "canonical_sha256": GRID_GOLD_SHA,
            },
        },
        "page_scope": [3, 4],
        "reviewed_continuations": [
            {
                "continuation_id": "p3-p4.support.between-rows",
                "predecessor_scope_id": "p3.support.table-grid",
                "successor_scope_id": "p4.support.table-grid",
                "relation_kind": "between_rows",
                "column_mapping": [
                    {
                        "predecessor_column_id": "p3.support.c1",
                        "successor_column_id": "p4.support.c1",
                    },
                    {
                        "predecessor_column_id": "p3.support.c2",
                        "successor_column_id": "p4.support.c2",
                    },
                ],
                "review_status": "human_confirmed",
                "reviewer_ref": "reviewer:test",
                "confirmed_at": "2026-09-20T00:00:00Z",
            }
        ],
    }


def _candidate(continuations: list[dict] | None = None) -> dict:
    return {
        "schema_version": "pdf_primary_table_continuation/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "policy": {"policy_version": "primary_adjacent_page_table_continuation/v1"},
        "notice_id": NOTICE_ID,
        "source_pdf_sha256": SOURCE_SHA,
        "page_scope": [3, 4, 5, 6],
        "input_artifacts": {
            "primary_table_grid_sha256": GRID_SHA,
            "native_capture_sha256": NATIVE_SHA,
        },
        "continuations": (
            continuations
            if continuations is not None
            else [_relation("8", P3_TABLE, 3, P4_TABLE, 4)]
        ),
    }


def _page_local_evaluation(verdict: str = "passed") -> dict:
    return {
        "schema_version": "pdf_primary_table_grid_evaluation/v1",
        "verdict": verdict,
    }


class PrimaryTableContinuationEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.schema_validator = Draft202012Validator(cls.schema)

    def compare(
        self,
        candidate: dict | None = None,
        *,
        quality_status: str = "evaluable",
        prerequisite_verdict: str = "passed",
    ) -> dict:
        result = _compare_replayed_table_continuations(
            candidate or _candidate(),
            _gold(),
            gold_quality_status=quality_status,
            candidate_canonical_sha256=CANDIDATE_SHA,
            gold_canonical_sha256=GOLD_SHA,
            page_local_candidate=_page_local_candidate(),
            page_local_candidate_canonical_sha256=GRID_SHA,
            page_local_gold=_page_local_gold(),
            page_local_gold_canonical_sha256=GRID_GOLD_SHA,
            page_local_evaluation=_page_local_evaluation(prerequisite_verdict),
            page_local_evaluation_canonical_sha256=GRID_EVALUATION_SHA,
        )
        self.schema_validator.validate(result)
        return result

    def test_exact_edge_kind_and_column_mapping_pass(self) -> None:
        result = self.compare()
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["metrics"]["matched_continuation_count"], 1)
        self.assertEqual(result["false_positive_relations"], [])

    def test_missing_relation_fails(self) -> None:
        result = self.compare(_candidate([]))
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(
            result["continuation_results"][0]["failure_codes"],
            ["missing_relation"],
        )

    def test_p3_to_p4_lower_table_is_false_positive_wrong_successor(self) -> None:
        wrong = _relation("9", P3_TABLE, 3, P4_LOWER_TABLE, 4)
        result = self.compare(_candidate([wrong]))
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(result["metrics"]["missing_continuation_count"], 1)
        false_positive = result["false_positive_relations"][0]
        self.assertEqual(false_positive["failure_codes"], ["unexpected_edge"])
        self.assertEqual(
            false_positive["relation"]["mapped_predecessor_scope_id"],
            "p3.support.table-grid",
        )
        self.assertIsNone(
            false_positive["relation"]["mapped_successor_scope_id"]
        )

    def test_wrong_kind_and_mapping_are_false_positives(self) -> None:
        for relation, expected_code in (
            (
                _relation("9", P3_TABLE, 3, P4_TABLE, 4, kind="same_row"),
                "wrong_relation_kind",
            ),
            (
                _relation(
                    "9",
                    P3_TABLE,
                    3,
                    P4_TABLE,
                    4,
                    mapping=((1, 2), (2, 1)),
                ),
                "wrong_column_mapping",
            ),
        ):
            with self.subTest(expected_code=expected_code):
                result = self.compare(_candidate([relation]))
                self.assertEqual(result["verdict"], "failed")
                self.assertIn(
                    expected_code,
                    result["false_positive_relations"][0]["failure_codes"],
                )

    def test_shared_predecessor_fails_even_when_expected_edge_exists(self) -> None:
        exact = _relation("8", P3_TABLE, 3, P4_TABLE, 4)
        extra = _relation("9", P3_TABLE, 3, P4_LOWER_TABLE, 4)
        result = self.compare(_candidate([exact, extra]))
        self.assertEqual(result["verdict"], "failed")
        self.assertIn(
            "shared_predecessor",
            result["continuation_results"][0]["failure_codes"],
        )
        self.assertIn(
            "shared_predecessor",
            result["false_positive_relations"][0]["failure_codes"],
        )

    def test_relations_wholly_outside_reviewed_scopes_are_unscored(self) -> None:
        exact = _relation("8", P3_TABLE, 3, P4_TABLE, 4)
        outside = _relation("9", P5_TABLE, 5, P6_TABLE, 6)
        result = self.compare(_candidate([exact, outside]))
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["metrics"]["unscored_relation_count"], 1)

    def test_pending_untrusted_and_failed_prerequisite_never_compare(self) -> None:
        for quality, prerequisite, expected in (
            ("not_evaluable_gold_pending", "passed", "not_evaluable_gold_pending"),
            (
                "not_evaluable_untrusted_confirmation",
                "passed",
                "not_evaluable_gold_untrusted",
            ),
            ("evaluable", "failed", "blocked_page_local_prerequisite"),
        ):
            with self.subTest(expected=expected):
                result = self.compare(
                    quality_status=quality,
                    prerequisite_verdict=prerequisite,
                )
                self.assertEqual(result["verdict"], expected)
                self.assertEqual(result["continuation_results"], [])
                self.assertEqual(result["false_positive_relations"], [])

    def test_standalone_validator_rejects_metrics_and_disposition_duplication(
        self,
    ) -> None:
        result = self.compare()
        changed = deepcopy(result)
        changed["metrics"]["matched_continuation_count"] = 0
        with self.assertRaisesRegex(
            PdfPrimaryTableContinuationEvaluationError,
            "matched_continuation_count",
        ):
            validate_primary_table_continuation_evaluation(changed)

        changed = deepcopy(result)
        changed["unscored_relations"] = deepcopy(
            changed["continuation_results"][0]["observed_relations"]
        )
        changed["metrics"]["unscored_relation_count"] = 1
        with self.assertRaisesRegex(
            PdfPrimaryTableContinuationEvaluationError,
            "outside every reviewed",
        ):
            validate_primary_table_continuation_evaluation(changed)

        false_positive = self.compare(
            _candidate([_relation("9", P3_TABLE, 3, P4_LOWER_TABLE, 4)])
        )
        changed = deepcopy(false_positive)
        changed["false_positive_relations"][0]["failure_codes"] = [
            "wrong_column_mapping"
        ]
        with self.assertRaisesRegex(
            PdfPrimaryTableContinuationEvaluationError,
            "false-positive failure codes",
        ):
            validate_primary_table_continuation_evaluation(changed)

        changed = deepcopy(self.compare())
        exact = changed["continuation_results"][0]["observed_relations"].pop()
        changed["continuation_results"][0]["status"] = "failed"
        changed["continuation_results"][0]["failure_codes"] = [
            "missing_relation"
        ]
        changed["false_positive_relations"] = [
            {"relation": exact, "failure_codes": ["unexpected_edge"]}
        ]
        changed["metrics"].update(
            {
                "matched_continuation_count": 0,
                "failed_continuation_count": 1,
                "missing_continuation_count": 1,
                "false_positive_relation_count": 1,
            }
        )
        changed["verdict"] = "failed"
        with self.assertRaisesRegex(
            PdfPrimaryTableContinuationEvaluationError,
            "exact reviewed relation",
        ):
            validate_primary_table_continuation_evaluation(changed)

    def test_public_gate_replays_candidate_gold_and_page_local_prerequisite(
        self,
    ) -> None:
        candidate = _candidate()
        grid = _page_local_candidate()
        gold = _gold()
        base_gold = _page_local_gold()
        grid_digest = evaluation.sha256(b"grid-replay").hexdigest()
        candidate["input_artifacts"]["primary_table_grid_sha256"] = grid_digest

        class FakeContinuationGoldReceipt:
            def __init__(self) -> None:
                self.canonical_sha256 = GOLD_SHA
                self.has_trusted_confirmation = True
                self.fixture = SimpleNamespace(
                    quality_gate_status="not_evaluable_input_replay_required"
                )

            def to_dict(self) -> dict:
                return deepcopy(gold)

        class FakeGridGoldReceipt:
            def __init__(self) -> None:
                self.canonical_sha256 = GRID_GOLD_SHA

            def to_dict(self) -> dict:
                return deepcopy(base_gold)

        continuation_receipt = FakeContinuationGoldReceipt()
        grid_receipt = FakeGridGoldReceipt()
        raw_inputs = {
            "source_pdf": Path("source.pdf"),
            "native_capture": {"sentinel": "native"},
            "structure_candidates": {"sentinel": "structure"},
            "render_manifest": {"sentinel": "render"},
            "render_artifact_root": Path("renders"),
            "calibration_proof": {"sentinel": "proof"},
            "expected_calibration_proof_sha256": "8" * 64,
            "reconstruction_plan": {"sentinel": "plan"},
            "surya_layout_artifact": {"sentinel": "surya"},
            "expected_surya_producer": SimpleNamespace(),
            "expected_surya_logical_compute_key": "logical-key",
            "expected_surya_pages": (3, 4),
        }
        with (
            patch.object(
                evaluation,
                "validate_pdf_primary_table_grid_against_inputs",
                return_value=grid,
            ) as replay_grid,
            patch.object(
                evaluation,
                "validate_pdf_primary_table_continuation_against_inputs",
                return_value=candidate,
            ) as replay_candidate,
            patch.object(
                evaluation,
                "validate_primary_table_grid_gold_against_inputs",
                return_value=grid_receipt,
            ) as replay_base_gold,
            patch.object(
                evaluation,
                "validate_primary_table_continuation_gold_against_inputs",
                return_value=continuation_receipt,
            ) as replay_gold,
            patch.object(
                evaluation,
                "evaluate_pdf_primary_table_grid",
                return_value=_page_local_evaluation(),
            ) as replay_prerequisite,
            patch.object(
                evaluation,
                "ReplayedPrimaryTableGridGold",
                FakeGridGoldReceipt,
            ),
            patch.object(
                evaluation,
                "ReplayedPrimaryTableContinuationGold",
                FakeContinuationGoldReceipt,
            ),
            patch.object(
                evaluation,
                "canonical_pdf_primary_table_grid_json",
                return_value=b"grid-replay",
            ),
            patch.object(
                evaluation,
                "canonical_pdf_primary_table_continuation_json",
                return_value=b"candidate-replay",
            ),
            patch.object(
                evaluation,
                "canonical_primary_table_grid_evaluation_json",
                return_value=b"prerequisite-replay",
            ),
        ):
            result = evaluation.evaluate_pdf_primary_table_continuation(
                candidate,
                gold,
                primary_table_grid=grid,
                base_table_grid_gold=base_gold,
                **raw_inputs,
            )

        self.assertEqual(result["verdict"], "passed")
        replay_grid.assert_called_once_with(grid, **raw_inputs)
        replay_candidate.assert_called_once_with(
            candidate, primary_table_grid=grid, **raw_inputs
        )
        replay_gold.assert_called_once_with(
            gold,
            source_pdf=raw_inputs["source_pdf"],
            native_capture=raw_inputs["native_capture"],
            render_manifest=raw_inputs["render_manifest"],
            render_artifact_root=raw_inputs["render_artifact_root"],
            base_table_grid_gold=base_gold,
        )
        replay_base_gold.assert_called_once()
        replay_prerequisite.assert_called_once_with(
            grid, base_gold, **raw_inputs
        )

    def test_actual_114788_lower_table_is_a_wrong_successor_false_positive(
        self,
    ) -> None:
        fixture = continuation_fixtures.PrimaryTableContinuationTests(
            methodName="runTest"
        )
        arguments = fixture.actual_arguments()
        grid = build_pdf_primary_table_grid(**arguments)
        candidate = build_pdf_primary_table_continuation(
            primary_table_grid=grid, **arguments
        )
        base_gold_fixture = load_primary_table_grid_gold_file(ACTUAL_BASE_GOLD)
        continuation_gold_fixture = load_primary_table_continuation_gold_file(
            ACTUAL_CONTINUATION_GOLD,
            base_table_grid_gold=base_gold_fixture,
        )
        public_result = evaluation.evaluate_pdf_primary_table_continuation(
            candidate,
            continuation_gold_fixture,
            primary_table_grid=grid,
            base_table_grid_gold=base_gold_fixture,
            **arguments,
        )
        self.assertEqual(public_result["verdict"], "passed")
        self.assertEqual(public_result["gold"]["quality_status"], "evaluable")
        self.assertEqual(
            public_result["metrics"]["matched_continuation_count"],
            1,
        )
        self.assertEqual(
            public_result["metrics"]["false_positive_relation_count"],
            0,
        )
        grid_sha = evaluation.sha256(
            canonical_pdf_primary_table_grid_json(grid)
        ).hexdigest()
        candidate_sha = evaluation.sha256(
            canonical_pdf_primary_table_continuation_json(candidate)
        ).hexdigest()
        prerequisite = _page_local_evaluation()
        prerequisite_sha = evaluation.sha256(
            json.dumps(prerequisite, sort_keys=True).encode("utf-8")
        ).hexdigest()

        passed = _compare_replayed_table_continuations(
            candidate,
            continuation_gold_fixture.to_dict(),
            gold_quality_status="evaluable",
            candidate_canonical_sha256=candidate_sha,
            gold_canonical_sha256=continuation_gold_fixture.canonical_sha256,
            page_local_candidate=grid,
            page_local_candidate_canonical_sha256=grid_sha,
            page_local_gold=base_gold_fixture.to_dict(),
            page_local_gold_canonical_sha256=base_gold_fixture.canonical_sha256,
            page_local_evaluation=prerequisite,
            page_local_evaluation_canonical_sha256=prerequisite_sha,
        )
        self.assertEqual(passed["verdict"], "passed")

        wrong = deepcopy(candidate)
        relation = wrong["continuations"][0]
        relation["continuation_id"] = _hash_id("table-continuation-", "f")
        relation["successor_table_grid_id"] = (
            continuation_fixtures.ACTUAL_114788_LOWER_HARD_NEGATIVE_TABLE_ID
        )
        failed = _compare_replayed_table_continuations(
            wrong,
            continuation_gold_fixture.to_dict(),
            gold_quality_status="evaluable",
            candidate_canonical_sha256="f" * 64,
            gold_canonical_sha256=continuation_gold_fixture.canonical_sha256,
            page_local_candidate=grid,
            page_local_candidate_canonical_sha256=grid_sha,
            page_local_gold=base_gold_fixture.to_dict(),
            page_local_gold_canonical_sha256=base_gold_fixture.canonical_sha256,
            page_local_evaluation=prerequisite,
            page_local_evaluation_canonical_sha256=prerequisite_sha,
        )
        self.assertEqual(failed["verdict"], "failed")
        self.assertEqual(failed["metrics"]["missing_continuation_count"], 1)
        false_positive = failed["false_positive_relations"][0]
        self.assertEqual(false_positive["failure_codes"], ["unexpected_edge"])
        self.assertEqual(
            false_positive["relation"]["mapped_predecessor_scope_id"],
            "p3.support.table-grid",
        )
        self.assertIsNone(
            false_positive["relation"]["mapped_successor_scope_id"]
        )

    def test_canonical_schema_and_work_cap(self) -> None:
        result = self.compare()
        encoded = canonical_primary_table_continuation_evaluation_json(result)
        self.assertEqual(
            encoded,
            canonical_primary_table_continuation_evaluation_json(
                json.loads(encoded.decode("utf-8"))
            ),
        )
        self.schema_validator.validate(result)
        with (
            patch.object(evaluation, "MAX_ENDPOINT_MEMBERSHIP_WORK", 3),
            self.assertRaisesRegex(
                PdfPrimaryTableContinuationEvaluationError,
                "work cap",
            ),
        ):
            self.compare()

        malformed = deepcopy(result)
        malformed["continuation_results"][0]["expected_column_mapping"] = []
        with self.assertRaises(PdfPrimaryTableContinuationEvaluationError):
            validate_primary_table_continuation_evaluation(malformed)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(malformed)

        # Gold column identifiers are opaque; their canonical order is the
        # reviewed scope order, not lexical identifier order.
        opaque_order = deepcopy(result)
        expected = opaque_order["continuation_results"][0]
        expected["expected_column_mapping"] = [
            {
                "predecessor_column_id": "p3.support.z",
                "successor_column_id": "p4.support.z",
            },
            {
                "predecessor_column_id": "p3.support.a",
                "successor_column_id": "p4.support.a",
            },
        ]
        expected["observed_relations"][0]["normalized_column_mapping"] = deepcopy(
            expected["expected_column_mapping"]
        )
        validate_primary_table_continuation_evaluation(opaque_order)

        missing_pair = deepcopy(result)
        missing_pair["continuation_results"][0]["observed_relations"][0][
            "normalized_column_mapping"
        ].pop()
        with self.assertRaisesRegex(
            PdfPrimaryTableContinuationEvaluationError,
            "preserve every candidate column pair",
        ):
            validate_primary_table_continuation_evaluation(missing_pair)


if __name__ == "__main__":
    unittest.main()
