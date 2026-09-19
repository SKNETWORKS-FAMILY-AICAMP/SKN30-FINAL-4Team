from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch, sentinel

from jsonschema import Draft202012Validator, ValidationError

try:
    import test_primary_table_grid as table_grid_fixtures
except ModuleNotFoundError as error:
    if error.name != "test_primary_table_grid":
        raise
    from backend.vendor.common_ir_pipeline.tests import (
        test_primary_table_grid as table_grid_fixtures,
    )

from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    REVIEWED_PROOF_CANONICAL_SHA256,
)
from common_ir_pipeline.pdf_fusion.primary_table_grid import (
    build_pdf_primary_table_grid,
)
import common_ir_pipeline.pdf_fusion.primary_table_grid_evaluation as evaluation
from common_ir_pipeline.pdf_fusion.primary_table_grid_evaluation import (
    PdfPrimaryTableGridEvaluationError,
    _compare_replayed_table_grid,
    canonical_primary_table_grid_evaluation_json,
    evaluate_pdf_primary_table_grid,
    validate_primary_table_grid_evaluation,
)
from common_ir_pipeline.pdf_fusion.primary_table_grid_gold import (
    load_primary_table_grid_gold_file,
)
SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/common_ir_pipeline/pdf_fusion/schemas/"
    "pdf_primary_table_grid_evaluation_v1.schema.json"
)
NOTICE_ID = "PBLN_000000000114788"
ACTUAL_114788_GOLD = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/primary_table_grid_gold_114788_p3_p4.v1.json"
)
SOURCE_SHA = "1" * 64
NATIVE_SHA = "2" * 64


def _occ(index: int, *, page: int = 3) -> str:
    return f"occ:inspector:p{page}:t{index}"


class _Fixture:
    def __init__(self, payload: dict, *, trusted: bool, quality: str) -> None:
        self.payload = payload
        self.has_trusted_confirmation = trusted
        self.quality_gate_status = quality


class _FakeGoldReplay:
    def __init__(
        self,
        payload: dict,
        *,
        trusted: bool = True,
        quality: str = "not_evaluable_input_replay_required",
    ) -> None:
        self.payload = payload
        self.fixture = _Fixture(payload, trusted=trusted, quality=quality)
        self.has_trusted_confirmation = trusted

    def canonical_json(self) -> bytes:
        return json.dumps(
            self.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def canonical_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()


def _gold_payload() -> dict:
    return {
        "schema_version": "pdf_primary_table_grid_gold/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "notice_id": NOTICE_ID,
        "source": {
            "source_pdf_sha256": SOURCE_SHA,
            "canonical_page_renders": [
                {"physical_page": 3, "canonical_render_sha256": "3" * 64}
            ],
            "native_capture": {
                "schema_version": "pdf_inspector_native_capture/v1",
                "canonical_sha256": NATIVE_SHA,
                "extractor_version": "1.17.0",
            },
        },
        "page_scope": [3],
        "reviewed_scopes": [
            {
                "scope_id": "p3.support.table-grid",
                "physical_page": 3,
                "segment_id": "p3.support.segment",
                "review_status": "human_confirmed",
                "reviewer_ref": "reviewer:test",
                "confirmed_at": "2026-09-19T00:00:00Z",
                "occurrence_ids": [_occ(i) for i in range(36, 42)],
                "row_ids": ["p3.support.r1", "p3.support.r2"],
                "column_ids": ["p3.support.c1", "p3.support.c2"],
                "cells": [
                    {
                        "cell_id": "p3.support.r1.c1",
                        "row_ids": ["p3.support.r1"],
                        "column_ids": ["p3.support.c1"],
                        "content_status": "populated",
                        "occurrence_ids": [_occ(37)],
                    },
                    {
                        "cell_id": "p3.support.r1.c2",
                        "row_ids": ["p3.support.r1"],
                        "column_ids": ["p3.support.c2"],
                        "content_status": "populated",
                        "occurrence_ids": [_occ(38)],
                    },
                    {
                        "cell_id": "p3.support.r2.c1",
                        "row_ids": ["p3.support.r2"],
                        "column_ids": ["p3.support.c1"],
                        "content_status": "populated",
                        "occurrence_ids": [_occ(39)],
                    },
                    {
                        "cell_id": "p3.support.r2.c2",
                        "row_ids": ["p3.support.r2"],
                        "column_ids": ["p3.support.c2"],
                        "content_status": "populated",
                        "occurrence_ids": [_occ(40)],
                    },
                ],
                "hard_negatives": [
                    {
                        "kind": "forbidden_segment_membership",
                        "occurrence_id": _occ(36),
                        "anchor_position": "before_segment",
                    },
                    {
                        "kind": "forbidden_segment_membership",
                        "occurrence_id": _occ(41),
                        "anchor_position": "after_segment",
                    },
                ],
            }
        ],
    }


def _cell(row: int, column: int, occurrences: list[str]) -> dict:
    return {
        "cell_id": f"table-cell-r{row}-c{column}",
        "row_index": row,
        "column_index": column,
        "row_span": 1,
        "column_span": 1,
        "source_cell_candidate_id": f"odl-cell-r{row}-c{column}",
        "occurrence_ids": occurrences,
    }


def _table(
    table_id: str,
    *,
    page: int,
    matrix: list[list[list[str]]],
) -> dict:
    rows = []
    for row_index, row_cells in enumerate(matrix, start=1):
        rows.append(
            {
                "row_id": f"table-row-{table_id}-r{row_index}",
                "row_index": row_index,
                "source_row_candidate_id": f"odl-row-{table_id}-r{row_index}",
                "cells": [
                    _cell(row_index, column_index, occurrences)
                    for column_index, occurrences in enumerate(row_cells, start=1)
                ],
            }
        )
    return {
        "table_grid_id": table_id,
        "page": page,
        "row_count": len(matrix),
        "column_count": len(matrix[0]),
        "source_table_candidate_id": f"odl-{table_id}",
        "surya_region_id": f"p{page:04d}-table-0001",
        "rows": rows,
    }


def _candidate() -> dict:
    return {
        "schema_version": "pdf_primary_table_grid/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "policy": {"policy_version": "primary_page_local_table_grid/v1"},
        "notice_id": NOTICE_ID,
        "source_pdf_sha256": SOURCE_SHA,
        "page_scope": [3, 4],
        "input_artifacts": {"native_capture_sha256": NATIVE_SHA},
        "tables": [
            _table(
                "table-grid-reviewed",
                page=3,
                matrix=[
                    [[_occ(37)], [_occ(38)]],
                    [[_occ(39)], [_occ(40)]],
                ],
            ),
            _table(
                "table-grid-unreviewed",
                page=4,
                matrix=[
                    [[_occ(85, page=4)], [_occ(86, page=4)]],
                ],
            ),
        ],
    }


class PrimaryTableGridEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def compare(
        self,
        candidate: dict | None = None,
        *,
        gold_payload: dict | None = None,
        trusted: bool = True,
        quality: str = "not_evaluable_input_replay_required",
    ) -> dict:
        candidate = candidate or _candidate()
        replay = _FakeGoldReplay(
            gold_payload or _gold_payload(), trusted=trusted, quality=quality
        )
        with (
            patch.object(evaluation, "ReplayedPrimaryTableGridGold", _FakeGoldReplay),
            patch.object(
                evaluation,
                "canonical_pdf_primary_table_grid_json",
                side_effect=lambda value: json.dumps(
                    value, sort_keys=True, separators=(",", ":")
                ).encode(),
            ),
        ):
            result = _compare_replayed_table_grid(candidate, replay)
        self.validator.validate(result)
        return result

    def test_matching_page_local_grid_passes_and_unrelated_table_is_unscored(self) -> None:
        result = self.compare()
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["scope_results"][0]["topology_status"], "matched")
        self.assertEqual(result["metrics"]["matched_cell_count"], 4)
        self.assertEqual(result["metrics"]["unscored_table_count"], 1)
        self.assertEqual(
            result["unscored_tables"][0]["table_grid_id"],
            "table-grid-unreviewed",
        )

    def test_swapped_cell_membership_fails_wrong_partition(self) -> None:
        candidate = _candidate()
        cells = candidate["tables"][0]["rows"][1]["cells"]
        cells[0]["occurrence_ids"], cells[1]["occurrence_ids"] = (
            cells[1]["occurrence_ids"],
            cells[0]["occurrence_ids"],
        )
        result = self.compare(candidate)
        scope = result["scope_results"][0]
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(
            scope["wrong_partition_occurrence_ids"], [_occ(39), _occ(40)]
        )
        self.assertIn("wrong_partition", scope["failure_codes"])

    def test_merged_gold_cell_fails_two_candidate_cell_slot_partition(self) -> None:
        gold = _gold_payload()
        scope = gold["reviewed_scopes"][0]
        first, second = scope["cells"][:2]
        first["column_ids"] = ["p3.support.c1", "p3.support.c2"]
        first["occurrence_ids"] = [_occ(37), _occ(38)]
        scope["cells"].remove(second)
        result = self.compare(gold_payload=gold)
        evaluated = result["scope_results"][0]
        merged = next(
            item
            for item in evaluated["cell_results"]
            if item["cell_id"] == "p3.support.r1.c1"
        )
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(evaluated["topology_status"], "mismatched")
        self.assertIn("topology_mismatch", evaluated["failure_codes"])
        self.assertIn("wrong_partition", merged["failure_codes"])
        self.assertEqual(
            merged["observed_candidate_cell_ids"],
            ["table-cell-r1-c1", "table-cell-r1-c2"],
        )

    def test_missing_attachment_and_negative_anchor_attachment_fail(self) -> None:
        candidate = _candidate()
        candidate["tables"][0]["rows"][1]["cells"][0]["occurrence_ids"] = []
        candidate["tables"][0]["rows"][1]["cells"][1]["occurrence_ids"].append(
            _occ(41)
        )
        result = self.compare(candidate)
        scope = result["scope_results"][0]
        self.assertEqual(scope["missing_attachment_occurrence_ids"], [_occ(39)])
        self.assertEqual(
            scope["negative_anchor_attachment_occurrence_ids"], [_occ(41)]
        )
        self.assertIn("missing_attachment", scope["failure_codes"])
        self.assertIn("negative_anchor_attachment", scope["failure_codes"])

    def test_scope_outside_attachment_is_unscored_not_failure(self) -> None:
        candidate = _candidate()
        candidate["tables"][0]["rows"][1]["cells"][1]["occurrence_ids"].append(
            _occ(99)
        )
        result = self.compare(candidate)
        scope = result["scope_results"][0]
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(scope["unscored_attachment_occurrence_ids"], [_occ(99)])
        self.assertEqual(result["metrics"]["unscored_attachment_count"], 1)

    def test_one_candidate_table_shared_by_two_gold_scopes_fails_overmerge(self) -> None:
        gold = _gold_payload()
        first = gold["reviewed_scopes"][0]
        first["occurrence_ids"] = [_occ(36), _occ(37), _occ(38)]
        first["row_ids"] = ["p3.support.r1"]
        first["cells"] = first["cells"][:2]
        first["hard_negatives"] = first["hard_negatives"][:1]
        second = {
            "scope_id": "p3.second.table-grid",
            "physical_page": 3,
            "segment_id": "p3.second.segment",
            "review_status": "human_confirmed",
            "reviewer_ref": "reviewer:test",
            "confirmed_at": "2026-09-19T00:00:00Z",
            "occurrence_ids": [_occ(39), _occ(40), _occ(41)],
            "row_ids": ["p3.second.r1"],
            "column_ids": ["p3.second.c1", "p3.second.c2"],
            "cells": [
                {
                    "cell_id": "p3.second.r1.c1",
                    "row_ids": ["p3.second.r1"],
                    "column_ids": ["p3.second.c1"],
                    "content_status": "populated",
                    "occurrence_ids": [_occ(39)],
                },
                {
                    "cell_id": "p3.second.r1.c2",
                    "row_ids": ["p3.second.r1"],
                    "column_ids": ["p3.second.c2"],
                    "content_status": "populated",
                    "occurrence_ids": [_occ(40)],
                },
            ],
            "hard_negatives": [
                {
                    "kind": "forbidden_segment_membership",
                    "occurrence_id": _occ(41),
                    "anchor_position": "after_segment",
                }
            ],
        }
        gold["reviewed_scopes"].append(second)
        result = self.compare(gold_payload=gold)
        self.assertEqual(result["verdict"], "failed")
        for scope in result["scope_results"]:
            self.assertIn("shared_candidate_table", scope["failure_codes"])
            self.assertEqual(
                scope["shared_candidate_table_ids"], ["table-grid-reviewed"]
            )
        self.assertNotIn(
            _occ(39),
            result["scope_results"][0]["unscored_attachment_occurrence_ids"],
        )

    def test_duplicate_and_orphan_attachments_fail_closed(self) -> None:
        candidate = _candidate()
        candidate["tables"][0]["row_count"] = 3
        candidate["tables"][0]["rows"].append(
            {
                "row_id": "table-row-extra",
                "row_index": 3,
                "source_row_candidate_id": "odl-row-extra",
                "cells": [
                    _cell(3, 1, [_occ(37)]),
                    _cell(3, 2, [_occ(100)]),
                ],
            }
        )
        result = self.compare(candidate)
        scope = result["scope_results"][0]
        self.assertEqual(scope["duplicate_attachment_occurrence_ids"], [_occ(37)])
        self.assertEqual(scope["orphan_attachment_occurrence_ids"], [_occ(37)])
        self.assertIn("topology_mismatch", scope["failure_codes"])

    def test_pending_and_untrusted_gold_never_compare_quality(self) -> None:
        for trusted, quality, expected in (
            (
                False,
                "not_evaluable_gold_pending",
                "not_evaluable_gold_pending",
            ),
            (
                False,
                "not_evaluable_untrusted_confirmation",
                "not_evaluable_gold_untrusted",
            ),
        ):
            with self.subTest(expected=expected):
                result = self.compare(trusted=trusted, quality=quality)
                self.assertEqual(result["verdict"], expected)
                self.assertEqual(result["scope_results"], [])
                self.assertEqual(result["unscored_tables"], [])

    def test_native_capture_binding_mismatch_blocks_comparison(self) -> None:
        candidate = _candidate()
        candidate["input_artifacts"]["native_capture_sha256"] = "4" * 64
        result = self.compare(candidate)
        self.assertEqual(result["verdict"], "blocked_scope_mismatch")
        self.assertEqual(
            result["blocking_reason_codes"], ["native_capture_mismatch"]
        )
        self.assertEqual(result["scope_results"], [])

    def test_standalone_validator_rejects_metric_and_order_mutations(self) -> None:
        result = self.compare()
        changed = deepcopy(result)
        changed["metrics"]["matched_cell_count"] = 3
        with self.assertRaisesRegex(
            PdfPrimaryTableGridEvaluationError, "matched_cell_count"
        ):
            validate_primary_table_grid_evaluation(changed)
        changed = deepcopy(result)
        changed["unscored_tables"] = list(reversed(changed["unscored_tables"]))
        # Add a second item so reversal changes canonical order.
        changed["unscored_tables"].append(
            {
                "table_grid_id": "table-grid-a",
                "physical_page": 3,
                "occurrence_ids": [_occ(90)],
            }
        )
        changed["metrics"]["unscored_table_count"] = 2
        with self.assertRaisesRegex(
            PdfPrimaryTableGridEvaluationError, "canonical"
        ):
            validate_primary_table_grid_evaluation(changed)

    def test_standalone_validator_rejects_missing_topology_with_observed_table(
        self,
    ) -> None:
        changed = deepcopy(self.compare())
        scope = changed["scope_results"][0]
        expected_occurrences = sorted(
            {
                occurrence_id
                for cell in scope["cell_results"]
                for occurrence_id in cell["expected_occurrence_ids"]
            },
            key=evaluation._occurrence_sort_key,
        )
        scope["topology_status"] = "missing"
        scope["verdict"] = "failed"
        scope["failure_codes"] = ["segment_missing", "missing_attachment"]
        scope["missing_attachment_occurrence_ids"] = expected_occurrences
        for cell in scope["cell_results"]:
            cell["status"] = "failed"
            cell["failure_codes"] = ["missing_attachment"]
            cell["observed_reviewed_occurrence_ids"] = []
            cell["observed_candidate_cell_ids"] = []
        changed["verdict"] = "failed"
        changed["metrics"]["passed_scope_count"] = 0
        changed["metrics"]["failed_scope_count"] = 1
        changed["metrics"]["matched_cell_count"] = 0
        changed["metrics"]["failed_cell_count"] = len(scope["cell_results"])
        changed["metrics"]["missing_attachment_count"] = len(
            expected_occurrences
        )
        with self.assertRaisesRegex(
            PdfPrimaryTableGridEvaluationError,
            "cannot be missing",
        ):
            validate_primary_table_grid_evaluation(changed)
        with self.assertRaises(ValidationError):
            self.validator.validate(changed)

    def test_canonical_serializer_is_stable(self) -> None:
        result = self.compare()
        first = canonical_primary_table_grid_evaluation_json(result)
        second = canonical_primary_table_grid_evaluation_json(
            json.loads(first.decode("utf-8"))
        )
        self.assertEqual(first, second)

    def test_occurrence_array_runtime_and_schema_caps_are_both_4096(self) -> None:
        result = self.compare()
        boundary = deepcopy(result)
        boundary["unscored_tables"] = [
            {
                "table_grid_id": "table-grid-boundary",
                "physical_page": 4,
                "occurrence_ids": [_occ(index, page=4) for index in range(4_096)],
            }
        ]
        boundary["metrics"]["unscored_table_count"] = 1
        validate_primary_table_grid_evaluation(boundary)
        self.validator.validate(boundary)

        overflow = deepcopy(boundary)
        overflow["unscored_tables"][0]["occurrence_ids"].append(
            _occ(4_096, page=4)
        )
        with self.assertRaisesRegex(
            PdfPrimaryTableGridEvaluationError, "bounded array"
        ):
            validate_primary_table_grid_evaluation(overflow)
        with self.assertRaises(ValidationError):
            self.validator.validate(overflow)

        empty_axis = deepcopy(result)
        empty_axis["scope_results"][0]["cell_results"][0]["row_ids"] = []
        with self.assertRaisesRegex(
            PdfPrimaryTableGridEvaluationError, "bounded array"
        ):
            validate_primary_table_grid_evaluation(empty_axis)
        with self.assertRaises(ValidationError):
            self.validator.validate(empty_axis)

    def test_json_node_and_expanded_slot_work_caps_are_enforced(self) -> None:
        self.assertEqual(evaluation.MAX_JSON_NODES, 250_000)
        result = self.compare()
        with (
            patch.object(evaluation, "MAX_EXPANDED_GRID_SLOTS", 7),
            self.assertRaisesRegex(
                PdfPrimaryTableGridEvaluationError, "expanded-grid-slot"
            ),
        ):
            validate_primary_table_grid_evaluation(result)
        with (
            patch.object(evaluation, "MAX_EXPANDED_GRID_SLOTS", 7),
            self.assertRaisesRegex(
                PdfPrimaryTableGridEvaluationError, "expanded-grid-slot"
            ),
        ):
            self.compare()

    def test_scope_membership_index_has_an_explicit_linear_work_cap(self) -> None:
        # Four Gold positives plus six candidate occurrences are visited once.
        with (
            patch.object(evaluation, "MAX_SCOPE_MEMBERSHIP_WORK", 9),
            self.assertRaisesRegex(
                PdfPrimaryTableGridEvaluationError,
                "scope-membership work cap",
            ),
        ):
            self.compare()

    def test_public_gate_replays_candidate_and_gold_with_same_raw_inputs(self) -> None:
        candidate = _candidate()
        replay = _FakeGoldReplay(_gold_payload())
        candidate_calls: list[dict] = []
        gold_calls: list[dict] = []

        def replay_candidate(value: object, **kwargs: object) -> dict:
            self.assertIs(value, sentinel.candidate)
            candidate_calls.append(kwargs)
            return candidate

        def replay_gold(value: object, **kwargs: object) -> _FakeGoldReplay:
            self.assertIs(value, sentinel.gold)
            gold_calls.append(kwargs)
            return replay

        with (
            patch.object(
                evaluation,
                "validate_pdf_primary_table_grid_against_inputs",
                side_effect=replay_candidate,
            ),
            patch.object(
                evaluation,
                "validate_primary_table_grid_gold_against_inputs",
                side_effect=replay_gold,
            ),
            patch.object(evaluation, "ReplayedPrimaryTableGridGold", _FakeGoldReplay),
            patch.object(
                evaluation,
                "canonical_pdf_primary_table_grid_json",
                side_effect=lambda value: json.dumps(
                    value, sort_keys=True, separators=(",", ":")
                ).encode(),
            ),
        ):
            result = evaluate_pdf_primary_table_grid(
                sentinel.candidate,
                sentinel.gold,
                source_pdf=Path("source.pdf"),
                native_capture={"native": "same"},
                structure_candidates={"structure": "same"},
                render_manifest={"render": "same"},
                render_artifact_root=Path("rendered"),
                calibration_proof={"calibration": "same"},
                expected_calibration_proof_sha256="a" * 64,
                reconstruction_plan={"plan": "same"},
                surya_layout_artifact={"surya": "same"},
                expected_surya_producer=sentinel.producer,
                expected_surya_logical_compute_key="compute-key",
                expected_surya_pages=[3],
            )
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(len(candidate_calls), 1)
        self.assertEqual(len(gold_calls), 1)
        self.assertIs(
            candidate_calls[0]["native_capture"],
            gold_calls[0]["native_capture"],
        )
        self.assertIs(
            candidate_calls[0]["render_manifest"],
            gold_calls[0]["render_manifest"],
        )

    def test_actual_114788_confirmed_evaluator_replay_when_environment_is_configured(
        self,
    ) -> None:
        names = {
            "source_pdf": "PRIMARY_DOCUMENT_VIEW_114788_SOURCE_PDF",
            "native_capture": "PRIMARY_DOCUMENT_VIEW_114788_NATIVE_CAPTURE",
            "structure_candidates": "PRIMARY_DOCUMENT_VIEW_114788_STRUCTURE_CANDIDATES",
            "render_manifest": "PRIMARY_DOCUMENT_VIEW_114788_RENDER_MANIFEST",
            "render_artifact_root": "PRIMARY_DOCUMENT_VIEW_114788_RENDER_ROOT",
            "reconstruction_plan": "PRIMARY_DOCUMENT_VIEW_114788_RECONSTRUCTION_PLAN",
            "calibration_proof": "PRIMARY_DOCUMENT_VIEW_114788_CALIBRATION_PROOF",
            "surya_layout_artifact": "PRIMARY_HEADING_RELATIONS_114788_SURYA_LAYOUT_ARTIFACT",
        }
        configured = {key: os.environ.get(name) for key, name in names.items()}
        missing = [names[key] for key, value in configured.items() if not value]
        if missing:
            self.skipTest(
                "actual 114788 pending evaluator replay requires: "
                + ", ".join(sorted(missing))
            )

        def mapping(name: str) -> dict:
            return json.loads(Path(configured[name]).read_text(encoding="utf-8"))  # type: ignore[arg-type]

        source_pdf = Path(configured["source_pdf"])  # type: ignore[arg-type]
        native_capture = mapping("native_capture")
        render_manifest = mapping("render_manifest")
        render_root = Path(configured["render_artifact_root"])  # type: ignore[arg-type]
        build_arguments = {
            "source_pdf": source_pdf,
            "native_capture": native_capture,
            "structure_candidates": mapping("structure_candidates"),
            "render_manifest": render_manifest,
            "render_artifact_root": render_root,
            "calibration_proof": mapping("calibration_proof"),
            "expected_calibration_proof_sha256": REVIEWED_PROOF_CANONICAL_SHA256,
            "reconstruction_plan": mapping("reconstruction_plan"),
            "surya_layout_artifact": mapping("surya_layout_artifact"),
            "expected_surya_producer": (
                table_grid_fixtures.ACTUAL_114788_EXPECTED_SURYA_PRODUCER
            ),
            "expected_surya_logical_compute_key": (
                table_grid_fixtures.ACTUAL_114788_EXPECTED_SURYA_LOGICAL_COMPUTE_KEY
            ),
            "expected_surya_pages": (
                table_grid_fixtures.ACTUAL_114788_EXPECTED_SURYA_REQUESTED_PAGES
            ),
        }
        candidate = build_pdf_primary_table_grid(**build_arguments)
        result = evaluate_pdf_primary_table_grid(
            candidate,
            load_primary_table_grid_gold_file(ACTUAL_114788_GOLD),
            **build_arguments,
        )
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["gold"]["quality_status"], "evaluable")
        self.assertEqual(result["metrics"]["reviewed_scope_count"], 2)
        self.assertEqual(result["metrics"]["evaluated_scope_count"], 2)
        self.assertEqual(result["metrics"]["matched_cell_count"], 12)
        self.assertEqual(result["metrics"]["unscored_table_count"], 1)


if __name__ == "__main__":
    unittest.main()
