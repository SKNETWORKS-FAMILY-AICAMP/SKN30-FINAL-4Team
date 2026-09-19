from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch, sentinel

from jsonschema import Draft202012Validator

try:
    import test_primary_document_view as view_fixtures
except ModuleNotFoundError as error:
    if error.name != "test_primary_document_view":
        raise
    from backend.vendor.common_ir_pipeline.tests import (
        test_primary_document_view as view_fixtures,
    )

import common_ir_pipeline.pdf_fusion.primary_view_evaluation as evaluation
import common_ir_pipeline.pdf_fusion.primary_document_view as document_view
import common_ir_pipeline.pdf_fusion.primary_structure_gold as structure_gold
from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    REVIEWED_PROOF_CANONICAL_SHA256,
)
from common_ir_pipeline.pdf_fusion.primary_document_view import (
    PrimaryDocumentViewFixture,
    build_pdf_primary_document_view,
)
from common_ir_pipeline.pdf_fusion.primary_structure_gold import (
    PrimaryStructureGoldFixture,
    load_primary_structure_gold_file,
)
from common_ir_pipeline.pdf_fusion.primary_view_evaluation import (
    PdfPrimaryViewEvaluationError,
    _compare_replayed_primary_document_view,
    canonical_primary_view_evaluation_json,
    evaluate_pdf_primary_document_view,
    validate_primary_view_evaluation,
)


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/common_ir_pipeline/pdf_fusion/schemas/pdf_primary_view_evaluation_v1.schema.json"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CONFIRMED_GOLD = (
    REPOSITORY_ROOT
    / "backend/baselines/pdf_reconstruction/primary_structure_gold_114788_p3_purpose.v1.json"
)
SOURCE_SHA = "1" * 64
NATIVE_SHA = "2" * 64
NOTICE_ID = "PBLN_000000000114788"
T17 = "occ:inspector:p3:t17"
T19 = "occ:inspector:p3:t19"
T20 = "occ:inspector:p3:t20"
T21 = "occ:inspector:p3:t21"


class _FakeReplayedCandidate:
    def __init__(self, payload: dict):
        self.payload = payload

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


class _FakeReplayedGold:
    def __init__(
        self,
        payload: dict,
        *,
        is_evaluable: bool = True,
        quality_gate_status: str = "evaluable",
    ):
        self.payload = payload
        self.has_trusted_confirmation = is_evaluable
        self.quality_gate_status = quality_gate_status
        self.fixture = self

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


@contextmanager
def _fake_replay_types():
    with (
        patch.object(
            evaluation,
            "ReplayedPrimaryDocumentView",
            _FakeReplayedCandidate,
        ),
        patch.object(
            evaluation,
            "ReplayedPrimaryStructureGold",
            _FakeReplayedGold,
        ),
    ):
        yield


def _leaf(
    leaf_id: str,
    kind: str,
    occurrence_ids: list[str],
    boundaries: list[tuple[str, str, str]] = (),
) -> dict:
    return {
        "leaf_id": leaf_id,
        "kind": kind,
        "page": 3,
        "occurrence_ids": occurrence_ids,
        "boundaries": [
            {
                "left_occurrence_id": left,
                "right_occurrence_id": right,
                "join_class": join_class,
            }
            for left, right, join_class in boundaries
        ],
    }


def _candidate_payload(*, leaves: list[dict], page_scope: list[int] | None = None) -> dict:
    return {
        "schema_version": "pdf_primary_document_view/v1",
        "notice_id": NOTICE_ID,
        "source": {
            "source_pdf_sha256": SOURCE_SHA,
            "native_capture": {
                "schema_version": "pdf_inspector_native_capture/v1",
                "canonical_sha256": NATIVE_SHA,
                "extractor_version": "1.17.0",
            },
        },
        "page_scope": [3] if page_scope is None else page_scope,
        "leaves": leaves,
    }


def _gold_payload() -> dict:
    return {
        "schema_version": "pdf_primary_structure_gold/v1",
        "notice_id": NOTICE_ID,
        "source": {
            "source_pdf_sha256": SOURCE_SHA,
            "native_capture": {
                "schema_version": "pdf_inspector_native_capture/v1",
                "canonical_sha256": NATIVE_SHA,
                "extractor_version": "1.17.0",
            },
        },
        "page_scope": [3],
        "reviewed_scopes": [
            {
                "scope_id": "p3.purpose",
                "physical_page": 3,
                "occurrence_ids": [T17, T19, T20],
                "ordered_groups": [
                    {
                        "group_id": "p3.purpose.paragraph",
                        "kind": "paragraph",
                        "occurrence_ids": [T19, T20],
                        "boundaries": [
                            {
                                "left_occurrence_id": T19,
                                "right_occurrence_id": T20,
                                "join_class": "intra_word_wrap",
                            }
                        ],
                    }
                ],
                "hard_negatives": [
                    {
                        "kind": "forbidden_same_leaf",
                        "occurrence_id": T17,
                        "target_group_id": "p3.purpose.paragraph",
                    }
                ],
            }
        ],
    }


def _passing_leaves() -> list[dict]:
    return [
        _leaf("leaf-title", "unclassified_text", [T17]),
        _leaf(
            "leaf-purpose",
            "paragraph",
            [T19, T20],
            [(T19, T20, "intra_word_wrap")],
        ),
    ]


class PrimaryViewEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.schema_validator = Draft202012Validator(cls.schema)

    def evaluate(
        self,
        leaves: list[dict],
        *,
        gold: _FakeReplayedGold | None = None,
        page_scope: list[int] | None = None,
    ) -> dict:
        candidate = _FakeReplayedCandidate(
            _candidate_payload(leaves=leaves, page_scope=page_scope)
        )
        gold = gold or _FakeReplayedGold(_gold_payload())
        with _fake_replay_types():
            return _compare_replayed_primary_document_view(candidate, gold)

    def test_matching_partial_gold_passes_and_is_canonical(self) -> None:
        result = self.evaluate(_passing_leaves())
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["metrics"]["matched_group_count"], 1)
        self.assertEqual(result["metrics"]["hard_negative_violation_count"], 0)
        self.assertFalse(list(self.schema_validator.iter_errors(result)))
        first = canonical_primary_view_evaluation_json(result)
        second = canonical_primary_view_evaluation_json(deepcopy(result))
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), result)

    def test_atomic_occurrences_are_reported_as_under_merged(self) -> None:
        result = self.evaluate(
            [
                _leaf("leaf-title", "unclassified_text", [T17]),
                _leaf("leaf-first", "unclassified_text", [T19]),
                _leaf("leaf-second", "unclassified_text", [T20]),
            ]
        )
        group = result["scope_results"][0]["group_results"][0]
        self.assertEqual(result["verdict"], "failed")
        self.assertIn("under_merged", group["failure_codes"])
        self.assertIn("boundary_mismatch", group["failure_codes"])

    def test_wrong_join_class_fails_boundary(self) -> None:
        leaves = [
            _leaf("leaf-title", "unclassified_text", [T17]),
            _leaf(
                "leaf-purpose",
                "paragraph",
                [T19, T20],
                [(T19, T20, "inter_token_space")],
            ),
        ]
        result = self.evaluate(leaves)
        group = result["scope_results"][0]["group_results"][0]
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(group["boundary_results"][0]["status"], "mismatched")

    def test_reversed_occurrence_order_fails(self) -> None:
        leaves = [
            _leaf("leaf-title", "unclassified_text", [T17]),
            _leaf(
                "leaf-purpose",
                "paragraph",
                [T20, T19],
                [(T20, T19, "intra_word_wrap")],
            ),
        ]
        result = self.evaluate(leaves)
        failures = result["scope_results"][0]["group_results"][0]["failure_codes"]
        self.assertIn("occurrence_order_mismatch", failures)
        self.assertIn("boundary_mismatch", failures)

    def test_forbidden_same_leaf_fails_without_scoring_heading_kind(self) -> None:
        leaves = [
            _leaf(
                "leaf-overmerged",
                "paragraph",
                [T17, T19, T20],
                [
                    (T17, T19, "inter_token_space"),
                    (T19, T20, "intra_word_wrap"),
                ],
            )
        ]
        result = self.evaluate(leaves)
        scope = result["scope_results"][0]
        group = scope["group_results"][0]
        hard = scope["hard_negative_results"][0]
        self.assertIn("reviewed_scope_over_merge", group["failure_codes"])
        self.assertEqual(hard["status"], "violated")
        self.assertEqual(hard["violating_leaf_id"], "leaf-overmerged")
        self.assertNotIn("heading", json.dumps(result))

    def test_scope_outside_attachment_is_diagnostic_not_failure(self) -> None:
        leaves = [
            _leaf("leaf-title", "unclassified_text", [T17]),
            _leaf(
                "leaf-purpose",
                "paragraph",
                [T19, T21, T20],
                [
                    (T19, T21, "intra_word_wrap"),
                    (T21, T20, "intra_word_wrap"),
                ],
            ),
        ]
        result = self.evaluate(leaves)
        group = result["scope_results"][0]["group_results"][0]
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(group["unscored_attached_occurrence_ids"], [T21])
        self.assertEqual(
            group["boundary_results"][0]["status"],
            "unscored_scope_outside",
        )
        self.assertEqual(result["metrics"]["unscored_attachment_count"], 1)

    def test_gold_page_scope_must_be_candidate_subset(self) -> None:
        result = self.evaluate(_passing_leaves(), page_scope=[2])
        self.assertEqual(result["verdict"], "blocked_scope_mismatch")
        self.assertIn("gold_page_scope_not_covered", result["blocking_reason_codes"])
        self.assertEqual(result["scope_results"], [])

    def test_pending_and_untrusted_gold_never_produce_quality_verdict(self) -> None:
        for quality_status, expected in (
            ("not_evaluable_gold_pending", "not_evaluable_gold_pending"),
            ("not_evaluable_untrusted_confirmation", "not_evaluable_gold_untrusted"),
        ):
            with self.subTest(quality_status=quality_status):
                gold = _FakeReplayedGold(
                    _gold_payload(),
                    is_evaluable=False,
                    quality_gate_status=quality_status,
                )
                result = self.evaluate(_passing_leaves(), gold=gold)
                self.assertEqual(result["verdict"], expected)
                self.assertEqual(result["scope_results"], [])
                self.assertEqual(result["metrics"]["evaluated_scope_count"], 0)

    def test_raw_mappings_are_rejected_at_replay_boundary(self) -> None:
        with self.assertRaisesRegex(
            PdfPrimaryViewEvaluationError,
            "candidate must be produced by strict",
        ):
            _compare_replayed_primary_document_view({}, {})  # type: ignore[arg-type]

    def test_public_quality_gate_replays_both_inputs_from_same_artifacts(self) -> None:
        arguments = {
            "source_pdf": Path("source.pdf"),
            "native_capture": {"native": "same"},
            "structure_candidates": {"structure": "candidate"},
            "render_manifest": {"render": "same"},
            "render_artifact_root": Path("render-root"),
            "calibration_proof": {"calibration": "candidate"},
            "expected_calibration_proof_sha256": "a" * 64,
            "reconstruction_plan": {"plan": "candidate"},
        }
        with (
            patch.object(
                evaluation,
                "validate_pdf_primary_document_view_against_inputs",
                return_value=sentinel.candidate_receipt,
            ) as candidate_replay,
            patch.object(
                evaluation,
                "validate_primary_structure_gold_against_inputs",
                return_value=sentinel.gold_receipt,
            ) as gold_replay,
            patch.object(
                evaluation,
                "_compare_replayed_primary_document_view",
                return_value=sentinel.result,
            ) as compare,
        ):
            result = evaluate_pdf_primary_document_view(
                {"candidate": "raw"},
                {"gold": "raw"},
                **arguments,
            )
        self.assertIs(result, sentinel.result)
        candidate_replay.assert_called_once_with(
            {"candidate": "raw"},
            **arguments,
        )
        gold_replay.assert_called_once_with(
            {"gold": "raw"},
            source_pdf=arguments["source_pdf"],
            native_capture=arguments["native_capture"],
            render_manifest=arguments["render_manifest"],
            render_artifact_root=arguments["render_artifact_root"],
        )
        compare.assert_called_once_with(
            sentinel.candidate_receipt,
            sentinel.gold_receipt,
        )

    def test_public_quality_gate_performs_real_synthetic_replays(self) -> None:
        fixture_case = view_fixtures.PrimaryDocumentViewTests()
        fixture_case.setUp()
        try:
            valid_notice_id = "PBLN_000000000000001"
            reconstruction_fixtures = view_fixtures.reconstruction_fixtures
            native_capture = reconstruction_fixtures.capture_pdf_to_native(
                fixture_case.fixture.source,
                notice_id=valid_notice_id,
                source_relative_path="attachments/source.pdf",
                inspector_module=reconstruction_fixtures.FakeInspector(),
                inspector_version="1.17.0",
            )
            raw_structure = reconstruction_fixtures.structure_document()
            raw_structure_bytes = json.dumps(
                raw_structure,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            input_binding = (
                reconstruction_fixtures.build_legacy_opendataloader_evaluation_bytes(
                    raw_structure_bytes,
                    notice_id=valid_notice_id,
                    source_pdf_sha256=fixture_case.fixture.source_sha256,
                    source_page_count=1,
                    page_scope=(1,),
                    odl_page_to_source_page=(
                        reconstruction_fixtures.LegacyOdlPageToSourcePage(1, 1),
                    ),
                )
            )
            structure_candidates = reconstruction_fixtures.project_structure_candidates(
                input_binding,
                raw_structure,
                raw_bytes=raw_structure_bytes,
                actual_odl_page_count=1,
            )
            reconstruction_plan = (
                reconstruction_fixtures.build_pdf_reconstruction_plan(
                    source_pdf=fixture_case.fixture.source,
                    native_capture=native_capture,
                    structure_candidates=structure_candidates,
                    render_manifest=fixture_case.render,
                    calibration_proof=fixture_case.fixture.proof,
                    expected_calibration_proof_sha256=(
                        fixture_case.fixture.proof_sha256
                    ),
                )
            )
            arguments = {
                **fixture_case.arguments,
                "native_capture": native_capture,
                "structure_candidates": structure_candidates,
                "reconstruction_plan": reconstruction_plan,
            }
            authoritative = [
                entry
                for entry in reconstruction_plan["native_occurrence_ledger"]
                if entry["substantive_status"] == "substantive"
                and entry["disposition"] == "owned_atomic"
            ]
            selected = {
                authoritative[0]["source_item_index"],
                authoritative[1]["source_item_index"],
            }

            def classifier(**kwargs):
                indices = {
                    kwargs["left_ledger"]["source_item_index"],
                    kwargs["right_ledger"]["source_item_index"],
                }
                return (
                    view_fixtures.qualified_decision()
                    if indices == selected
                    else view_fixtures.rejected_decision()
                )

            with patch.object(
                document_view,
                "classify_native_continuity_boundary",
                side_effect=classifier,
            ):
                candidate = build_pdf_primary_document_view(
                    **arguments
                )
            paragraph = next(
                leaf for leaf in candidate["leaves"] if leaf["kind"] == "paragraph"
            )
            left, right = paragraph["occurrence_ids"]
            gold_payload = {
                "schema_version": "pdf_primary_structure_gold/v1",
                "evaluation_only": True,
                "non_promotable": True,
                "standalone_validation_scope": "internal_consistency_only",
                "notice_id": candidate["notice_id"],
                "source": {
                    "source_pdf_sha256": candidate["source"]["source_pdf_sha256"],
                    "canonical_page_renders": [
                        {
                            "physical_page": 1,
                            "canonical_render_sha256": fixture_case.render.pages[
                                0
                            ].image_sha256,
                        }
                    ],
                    "native_capture": dict(candidate["source"]["native_capture"]),
                },
                "page_scope": [1],
                "reviewed_scopes": [
                    {
                        "scope_id": "p1.synthetic",
                        "physical_page": 1,
                        "printed_page_label_status": "absent",
                        "printed_page_label": None,
                        "review_status": "human_confirmed",
                        "reviewer_ref": "reviewer:synthetic-test",
                        "confirmed_at": "2026-09-19T12:00:00Z",
                        "occurrence_ids": [left, right],
                        "ordered_groups": [
                            {
                                "group_id": "p1.synthetic.paragraph",
                                "kind": "paragraph",
                                "occurrence_ids": [left, right],
                                "boundaries": [
                                    {
                                        "left_occurrence_id": left,
                                        "right_occurrence_id": right,
                                        "join_class": "intra_word_wrap",
                                    }
                                ],
                            }
                        ],
                        "hard_negatives": [],
                    }
                ],
            }
            gold = PrimaryStructureGoldFixture.from_dict(gold_payload)
            with (
                patch.object(
                    structure_gold,
                    "TRUSTED_CONFIRMED_GOLD_SHA256S",
                    frozenset({gold.canonical_sha256}),
                ),
                patch.object(
                    document_view,
                    "classify_native_continuity_boundary",
                    side_effect=classifier,
                ),
            ):
                result = evaluate_pdf_primary_document_view(
                    candidate,
                    gold,
                    **arguments,
                )
                self.assertEqual(result["verdict"], "passed")

                candidate_fixture = PrimaryDocumentViewFixture.from_dict(candidate)
                forged_candidate = document_view.ReplayedPrimaryDocumentView(
                    candidate_fixture,
                    _construction_token=document_view._REPLAY_CONSTRUCTION_TOKEN,
                    replayed_canonical_sha256=candidate_fixture.canonical_sha256,
                )
                with self.assertRaisesRegex(
                    PdfPrimaryViewEvaluationError,
                    "candidate did not pass deterministic source replay",
                ):
                    evaluate_pdf_primary_document_view(
                        forged_candidate,  # type: ignore[arg-type]
                        gold,
                        **arguments,
                    )

                forged_gold = structure_gold.ReplayedPrimaryStructureGold(
                    gold,
                    _construction_token=structure_gold._REPLAY_CONSTRUCTION_TOKEN,
                    replayed_canonical_sha256=gold.canonical_sha256,
                )
                with self.assertRaisesRegex(
                    PdfPrimaryViewEvaluationError,
                    "Gold did not pass deterministic source replay",
                ):
                    evaluate_pdf_primary_document_view(
                        candidate,
                        forged_gold,  # type: ignore[arg-type]
                        **arguments,
                    )
        finally:
            fixture_case.tearDown()

    def test_validator_rejects_tampered_metrics(self) -> None:
        result = self.evaluate(_passing_leaves())
        result["metrics"]["matched_group_count"] = 0
        with self.assertRaisesRegex(
            PdfPrimaryViewEvaluationError,
            "matched_group_count disagrees",
        ):
            validate_primary_view_evaluation(result)

    def test_validator_rejects_unaccounted_reviewed_scope(self) -> None:
        result = self.evaluate(_passing_leaves())
        result["metrics"]["reviewed_scope_count"] = 2
        with self.assertRaisesRegex(
            PdfPrimaryViewEvaluationError,
            "evaluate every reviewed scope",
        ):
            validate_primary_view_evaluation(result)

    def test_reviewed_scope_metric_is_bounded_before_cross_field_checks(self) -> None:
        result = self.evaluate(_passing_leaves())
        result["metrics"]["reviewed_scope_count"] = 257
        with self.assertRaisesRegex(
            PdfPrimaryViewEvaluationError,
            "reviewed_scope_count exceeds",
        ):
            validate_primary_view_evaluation(result)
        self.assertTrue(list(self.schema_validator.iter_errors(result)))

    def test_schema_rejects_status_payload_pairs_rejected_by_runtime(self) -> None:
        missing_join = self.evaluate(_passing_leaves())
        boundary = missing_join["scope_results"][0]["group_results"][0][
            "boundary_results"
        ][0]
        boundary["observed_join_class"] = None
        with self.assertRaisesRegex(
            PdfPrimaryViewEvaluationError,
            "matched status disagrees",
        ):
            validate_primary_view_evaluation(missing_join)
        self.assertTrue(list(self.schema_validator.iter_errors(missing_join)))

        spurious_leaf = self.evaluate(_passing_leaves())
        hard_negative = spurious_leaf["scope_results"][0][
            "hard_negative_results"
        ][0]
        hard_negative["violating_leaf_id"] = "leaf-spurious"
        with self.assertRaisesRegex(
            PdfPrimaryViewEvaluationError,
            "must be null",
        ):
            validate_primary_view_evaluation(spurious_leaf)
        self.assertTrue(list(self.schema_validator.iter_errors(spurious_leaf)))

    def test_actual_114788_evaluator_replay_when_artifact_environment_is_configured(
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
        }
        configured = {key: os.environ.get(name) for key, name in names.items()}
        missing = [names[key] for key, value in configured.items() if not value]
        if missing:
            self.skipTest(
                "actual 114788 evaluator replay requires: "
                + ", ".join(sorted(missing))
            )

        def mapping(name: str) -> dict:
            return json.loads(
                Path(configured[name]).read_text(encoding="utf-8")  # type: ignore[arg-type]
            )

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
        }
        candidate_payload = build_pdf_primary_document_view(**build_arguments)
        candidate_metrics = candidate_payload["metrics"]
        self.assertEqual(candidate_metrics["missing_occurrence_count"], 0)
        self.assertEqual(candidate_metrics["duplicate_occurrence_count"], 0)
        self.assertEqual(
            candidate_metrics["placed_occurrence_count"],
            candidate_metrics["authoritative_occurrence_count"],
        )
        gold_fixture = load_primary_structure_gold_file(CONFIRMED_GOLD)
        result = evaluate_pdf_primary_document_view(
            candidate_payload,
            gold_fixture,
            **build_arguments,
        )
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["metrics"]["ordered_group_count"], 1)
        self.assertEqual(result["metrics"]["matched_group_count"], 1)
        self.assertEqual(result["metrics"]["failed_group_count"], 0)
        self.assertEqual(result["metrics"]["hard_negative_count"], 1)
        self.assertEqual(result["metrics"]["hard_negative_violation_count"], 0)
        self.assertEqual(
            result["scope_results"][0]["hard_negative_results"][0]["status"],
            "satisfied",
        )

        candidate_fixture = PrimaryDocumentViewFixture.from_dict(candidate_payload)
        forged_candidate = document_view.ReplayedPrimaryDocumentView(
            candidate_fixture,
            _construction_token=document_view._REPLAY_CONSTRUCTION_TOKEN,
            replayed_canonical_sha256=candidate_fixture.canonical_sha256,
        )
        with self.assertRaisesRegex(
            PdfPrimaryViewEvaluationError,
            "candidate did not pass deterministic source replay",
        ):
            evaluate_pdf_primary_document_view(
                forged_candidate,  # type: ignore[arg-type]
                gold_fixture,
                **build_arguments,
            )

        forged_gold = structure_gold.ReplayedPrimaryStructureGold(
            gold_fixture,
            _construction_token=structure_gold._REPLAY_CONSTRUCTION_TOKEN,
            replayed_canonical_sha256=gold_fixture.canonical_sha256,
        )
        with self.assertRaisesRegex(
            PdfPrimaryViewEvaluationError,
            "Gold did not pass deterministic source replay",
        ):
            evaluate_pdf_primary_document_view(
                candidate_payload,
                forged_gold,  # type: ignore[arg-type]
                **build_arguments,
            )


if __name__ == "__main__":
    unittest.main()
