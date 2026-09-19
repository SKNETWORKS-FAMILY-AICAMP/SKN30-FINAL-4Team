from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch, sentinel

from jsonschema import Draft202012Validator

import common_ir_pipeline.pdf_fusion.primary_heading_relation_evaluation as evaluation
import common_ir_pipeline.pdf_fusion.primary_heading_relations as candidate_contract
import common_ir_pipeline.pdf_fusion.primary_view_evaluation as primary_evaluation
from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    REVIEWED_PROOF_CANONICAL_SHA256,
)
from common_ir_pipeline.pdf_fusion.primary_document_view import (
    build_pdf_primary_document_view,
)
from common_ir_pipeline.pdf_fusion.primary_heading_relation_evaluation import (
    PdfPrimaryHeadingRelationEvaluationError,
    _compare_replayed_heading_relations,
    canonical_primary_heading_relation_evaluation_json,
    evaluate_pdf_primary_heading_relations,
    validate_primary_heading_relation_evaluation,
)
from common_ir_pipeline.pdf_fusion.primary_heading_relation_gold import (
    load_primary_heading_relation_gold_file,
)
from common_ir_pipeline.pdf_fusion.primary_heading_relations import (
    build_pdf_primary_heading_relations,
)
from common_ir_pipeline.pdf_fusion.primary_structure_gold import (
    load_primary_structure_gold_file,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaProducerIdentity,
)


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/common_ir_pipeline/pdf_fusion/schemas/"
    "pdf_primary_heading_relation_evaluation_v1.schema.json"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
BASE_GOLD = (
    REPOSITORY_ROOT
    / "backend/baselines/pdf_reconstruction/primary_structure_gold_114788_p3_purpose.v1.json"
)
HEADING_GOLD = (
    REPOSITORY_ROOT
    / "backend/baselines/pdf_reconstruction/primary_heading_relation_gold_114788_p3_purpose.v1.json"
)
NOTICE_ID = "PBLN_000000000114788"
SOURCE_SHA = "1" * 64
NATIVE_SHA = "2" * 64
SURYA_SHA = "3" * 64
HEADING_LEAF = "leaf-" + "1" * 64
BODY_LEAF = "leaf-" + "2" * 64
EXTRA_HEADING_LEAF = "leaf-" + "3" * 64
EXTRA_BODY_LEAF = "leaf-" + "4" * 64
T17 = "occ:inspector:p3:t17"
T18 = "occ:inspector:p3:t18"
T19 = "occ:inspector:p3:t19"
T20 = "occ:inspector:p3:t20"
T30 = "occ:inspector:p3:t30"


class _FakeReplay:
    def __init__(
        self,
        payload: dict,
        *,
        trusted: bool = True,
        quality_status: str = "evaluable",
        reported_digest: str | None = None,
    ) -> None:
        self.payload = payload
        self.fixture = self
        self.has_trusted_confirmation = trusted
        self.quality_gate_status = quality_status
        self.reported_digest = reported_digest

    def canonical_json(self) -> bytes:
        return json.dumps(
            self.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def canonical_sha256(self) -> str:
        return self.reported_digest or sha256(self.canonical_json()).hexdigest()


def _leaf(leaf_id: str, kind: str, occurrences: list[str]) -> dict:
    boundaries = []
    if occurrences == [T19, T20]:
        boundaries.append(
            {
                "left_occurrence_id": T19,
                "right_occurrence_id": T20,
                "join_class": "intra_word_wrap",
            }
        )
    return {
        "leaf_id": leaf_id,
        "kind": kind,
        "page": 3,
        "occurrence_ids": occurrences,
        "boundaries": boundaries,
    }


def _view_payload() -> dict:
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
        "page_scope": [3],
        "leaves": [
            _leaf(HEADING_LEAF, "unclassified_text", [T17]),
            _leaf(EXTRA_HEADING_LEAF, "unclassified_text", [T18]),
            _leaf(BODY_LEAF, "paragraph", [T19, T20]),
            _leaf(EXTRA_BODY_LEAF, "paragraph", [T30]),
        ],
    }


def _base_gold_payload() -> dict:
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
                "occurrence_ids": [T17, T18, T19, T20],
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


def _heading_gold_payload() -> dict:
    base = _FakeReplay(_base_gold_payload())
    return {
        "schema_version": "pdf_primary_heading_relation_gold/v1",
        "notice_id": NOTICE_ID,
        "source": {
            "source_pdf_sha256": SOURCE_SHA,
            "native_capture": {
                "schema_version": "pdf_inspector_native_capture/v1",
                "canonical_sha256": NATIVE_SHA,
                "extractor_version": "1.17.0",
            },
            "primary_structure_gold": {
                "schema_version": "pdf_primary_structure_gold/v1",
                "canonical_sha256": base.canonical_sha256,
            },
        },
        "page_scope": [3],
        "reviewed_scopes": [
            {
                "scope_id": "p3.purpose.heading-relations",
                "physical_page": 3,
                "base_structure_scope_id": "p3.purpose",
                "expected_relations": [
                    {
                        "heading_occurrence_id": T17,
                        "body_group_id": "p3.purpose.paragraph",
                    }
                ],
            }
        ],
    }


def _primary_prerequisite(view: dict, base: dict) -> dict:
    with (
        patch.object(
            primary_evaluation, "ReplayedPrimaryDocumentView", _FakeReplay
        ),
        patch.object(
            primary_evaluation, "ReplayedPrimaryStructureGold", _FakeReplay
        ),
    ):
        return primary_evaluation._compare_replayed_primary_document_view(
            _FakeReplay(view), _FakeReplay(base)
        )


def _relation(
    view: dict,
    *,
    heading_leaf_id: str,
    body_leaf_id: str,
    region_number: int,
) -> dict:
    primary_sha = _FakeReplay(view).canonical_sha256
    region_id = f"p0003-section_header-{region_number:04d}"
    relation_id = candidate_contract._relation_id(
        source_pdf_sha256=SOURCE_SHA,
        policy_version=candidate_contract.POLICY_VERSION,
        primary_document_view_sha256=primary_sha,
        surya_layout_artifact_sha256=SURYA_SHA,
        kind="heading_to_body",
        page=3,
        heading_leaf_id=heading_leaf_id,
        body_leaf_id=body_leaf_id,
        surya_region_id=region_id,
    )
    return {
        "relation_id": relation_id,
        "kind": "heading_to_body",
        "page": 3,
        "heading_leaf_id": heading_leaf_id,
        "body_leaf_id": body_leaf_id,
        "surya_region_id": region_id,
    }


def _candidate_payload(view: dict, relations: list[dict]) -> dict:
    return {
        "schema_version": "pdf_primary_heading_relations/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "policy": {"policy_version": "primary_numbered_heading_relation/v1"},
        "notice_id": NOTICE_ID,
        "source_pdf_sha256": SOURCE_SHA,
        "page_scope": [3],
        "input_artifacts": {
            "primary_document_view_schema_version": "pdf_primary_document_view/v1",
            "primary_document_view_sha256": _FakeReplay(view).canonical_sha256,
            "surya_layout_artifact_schema_version": "surya_layout_artifact/v1",
            "surya_layout_artifact_sha256": SURYA_SHA,
        },
        "relations": sorted(
            relations,
            key=lambda item: (
                item["page"],
                item["surya_region_id"],
                item["heading_leaf_id"],
                item["body_leaf_id"],
            ),
        ),
    }


class PrimaryHeadingRelationEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.schema_validator = Draft202012Validator(cls.schema)

    def evaluate(
        self,
        relations: list[dict],
        *,
        base_replay: _FakeReplay | None = None,
        heading_replay: _FakeReplay | None = None,
        prerequisite: dict | None = None,
        view_replay: _FakeReplay | None = None,
    ) -> dict:
        view = _view_payload()
        base = _base_gold_payload()
        candidate = _candidate_payload(view, relations)
        prerequisite = prerequisite or _primary_prerequisite(view, base)
        view_replay = view_replay or _FakeReplay(view)
        base_replay = base_replay or _FakeReplay(base)
        heading_replay = heading_replay or _FakeReplay(_heading_gold_payload())
        with (
            patch.object(evaluation, "ReplayedPrimaryDocumentView", _FakeReplay),
            patch.object(evaluation, "ReplayedPrimaryStructureGold", _FakeReplay),
            patch.object(
                evaluation, "ReplayedPrimaryHeadingRelationGold", _FakeReplay
            ),
        ):
            return _compare_replayed_heading_relations(
                candidate,
                view_replay,
                base_replay,
                heading_replay,
                prerequisite,
            )

    def expected_relation(self) -> dict:
        view = _view_payload()
        return _relation(
            view,
            heading_leaf_id=HEADING_LEAF,
            body_leaf_id=BODY_LEAF,
            region_number=1,
        )

    def test_exact_relation_passes_and_is_canonical_and_schema_valid(self) -> None:
        result = self.evaluate([self.expected_relation()])
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["metrics"]["matched_relation_count"], 1)
        self.assertFalse(list(self.schema_validator.iter_errors(result)))
        canonical = canonical_primary_heading_relation_evaluation_json(result)
        self.assertEqual(json.loads(canonical), result)
        self.assertEqual(
            canonical,
            canonical_primary_heading_relation_evaluation_json(deepcopy(result)),
        )

    def test_missing_relation_fails(self) -> None:
        result = self.evaluate([])
        self.assertEqual(result["verdict"], "failed")
        relation = result["scope_results"][0]["relation_results"][0]
        self.assertEqual(relation["status"], "missing")
        self.assertEqual(result["metrics"]["missing_relation_count"], 1)

    def test_wrong_target_fails(self) -> None:
        view = _view_payload()
        wrong = _relation(
            view,
            heading_leaf_id=HEADING_LEAF,
            body_leaf_id=EXTRA_BODY_LEAF,
            region_number=1,
        )
        result = self.evaluate([wrong])
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(
            result["scope_results"][0]["relation_results"][0]["status"],
            "wrong_target",
        )

    def test_in_scope_false_positive_fails(self) -> None:
        view = _view_payload()
        extra = _relation(
            view,
            heading_leaf_id=EXTRA_HEADING_LEAF,
            body_leaf_id=EXTRA_BODY_LEAF,
            region_number=2,
        )
        result = self.evaluate([self.expected_relation(), extra])
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(result["metrics"]["false_positive_relation_count"], 1)
        self.assertEqual(
            result["scope_results"][0]["false_positive_relations"][0][
                "heading_occurrence_id"
            ],
            T18,
        )

    def test_outside_scope_relation_is_unscored(self) -> None:
        view = _view_payload()
        outside = _relation(
            view,
            heading_leaf_id=EXTRA_BODY_LEAF,
            body_leaf_id=EXTRA_HEADING_LEAF,
            region_number=2,
        )
        result = self.evaluate([self.expected_relation(), outside])
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["metrics"]["unscored_relation_count"], 1)
        self.assertEqual(result["unscored_relations"][0]["heading_occurrence_id"], T30)

    def test_untrusted_mixed_with_evaluable_is_not_evaluable(self) -> None:
        heading = _FakeReplay(
            _heading_gold_payload(),
            trusted=False,
            quality_status="not_evaluable_untrusted_confirmation",
        )
        result = self.evaluate([self.expected_relation()], heading_replay=heading)
        self.assertEqual(result["verdict"], "not_evaluable_gold_untrusted")
        self.assertEqual(result["scope_results"], [])

        forged = deepcopy(self.evaluate([self.expected_relation()]))
        forged["heading_gold"][
            "quality_status"
        ] = "not_evaluable_untrusted_confirmation"
        with self.assertRaises(PdfPrimaryHeadingRelationEvaluationError):
            validate_primary_heading_relation_evaluation(forged)
        self.assertTrue(list(self.schema_validator.iter_errors(forged)))

    def test_pending_gold_takes_precedence_over_untrusted(self) -> None:
        base = _FakeReplay(
            _base_gold_payload(),
            trusted=False,
            quality_status="not_evaluable_untrusted_confirmation",
        )
        heading = _FakeReplay(
            _heading_gold_payload(),
            trusted=False,
            quality_status="not_evaluable_gold_pending",
        )
        result = self.evaluate(
            [self.expected_relation()], base_replay=base, heading_replay=heading
        )
        self.assertEqual(result["verdict"], "not_evaluable_gold_pending")

    def test_primary_view_prerequisite_is_separate_and_digest_bound(self) -> None:
        view = _view_payload()
        base = _base_gold_payload()
        prerequisite = _primary_prerequisite(view, base)
        result = self.evaluate(
            [self.expected_relation()], prerequisite=prerequisite
        )
        self.assertEqual(result["primary_view_prerequisite"]["verdict"], "passed")
        self.assertEqual(
            result["primary_view_prerequisite"]["canonical_sha256"],
            sha256(
                primary_evaluation.canonical_primary_view_evaluation_json(
                    prerequisite
                )
            ).hexdigest(),
        )

        forged = deepcopy(prerequisite)
        forged["candidate"]["canonical_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationEvaluationError, "does not bind"
        ):
            self.evaluate([self.expected_relation()], prerequisite=forged)

    def test_nonpassing_primary_view_prerequisite_blocks_heading_verdict(self) -> None:
        view = _view_payload()
        base = _base_gold_payload()
        broken_view = deepcopy(view)
        for leaf in broken_view["leaves"]:
            if leaf["leaf_id"] == BODY_LEAF:
                leaf["kind"] = "unclassified_text"
        prerequisite = _primary_prerequisite(broken_view, base)
        self.assertEqual(prerequisite["verdict"], "failed")
        prerequisite["candidate"]["canonical_sha256"] = _FakeReplay(
            view
        ).canonical_sha256
        result = self.evaluate(
            [self.expected_relation()], prerequisite=prerequisite
        )
        self.assertEqual(result["verdict"], "blocked_primary_view_prerequisite")

    def test_replay_digest_disagreement_is_rejected(self) -> None:
        forged_view = _FakeReplay(_view_payload(), reported_digest="f" * 64)
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationEvaluationError, "digest disagrees"
        ):
            self.evaluate([self.expected_relation()], view_replay=forged_view)

    def test_validator_rejects_unknown_prerequisite_verdict_and_bad_metrics(self) -> None:
        result = self.evaluate([self.expected_relation()])
        forged_verdict = deepcopy(result)
        forged_verdict["primary_view_prerequisite"]["verdict"] = "garbage"
        with self.assertRaises(PdfPrimaryHeadingRelationEvaluationError):
            validate_primary_heading_relation_evaluation(forged_verdict)
        self.assertTrue(list(self.schema_validator.iter_errors(forged_verdict)))

        forged_metrics = deepcopy(result)
        forged_metrics["metrics"]["matched_relation_count"] = 2
        with self.assertRaises(PdfPrimaryHeadingRelationEvaluationError):
            validate_primary_heading_relation_evaluation(forged_metrics)

    def test_validator_rejects_every_reversed_canonical_result_order(self) -> None:
        passed = self.evaluate([self.expected_relation()])

        blocked = deepcopy(passed)
        blocked["verdict"] = "blocked_scope_mismatch"
        blocked["blocking_reason_codes"] = [
            "notice_id_mismatch",
            "source_pdf_mismatch",
        ]
        blocked["scope_results"] = []
        blocked["unscored_relations"] = []
        blocked["metrics"] = evaluation._empty_metrics(reviewed_scope_count=1)
        validate_primary_heading_relation_evaluation(blocked)
        blocked["blocking_reason_codes"].reverse()
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationEvaluationError, "canonical rank order"
        ):
            validate_primary_heading_relation_evaluation(blocked)

        scopes = deepcopy(passed)
        second_scope = deepcopy(scopes["scope_results"][0])
        second_scope["scope_id"] = "p3.zpurpose.heading-relations"
        scopes["scope_results"].append(second_scope)
        for name in (
            "reviewed_scope_count",
            "evaluated_scope_count",
            "passed_scope_count",
            "expected_relation_count",
            "matched_relation_count",
        ):
            scopes["metrics"][name] = 2
        validate_primary_heading_relation_evaluation(scopes)
        scopes["scope_results"].reverse()
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationEvaluationError, "page/scope order"
        ):
            validate_primary_heading_relation_evaluation(scopes)

        relation_results = deepcopy(passed)
        second_relation = deepcopy(
            relation_results["scope_results"][0]["relation_results"][0]
        )
        second_relation["heading_occurrence_id"] = T18
        second_relation["body_group_id"] = "p3.purpose.second"
        second_relation["expected_heading_leaf_id"] = EXTRA_HEADING_LEAF
        second_relation["observed_relation_id"] = "heading-relation-" + "f" * 64
        relation_results["scope_results"][0]["relation_results"].append(
            second_relation
        )
        relation_results["metrics"]["expected_relation_count"] = 2
        relation_results["metrics"]["matched_relation_count"] = 2
        validate_primary_heading_relation_evaluation(relation_results)
        relation_results["scope_results"][0]["relation_results"].reverse()
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationEvaluationError,
            "canonical occurrence/group order",
        ):
            validate_primary_heading_relation_evaluation(relation_results)

        view = _view_payload()
        extra = _relation(
            view,
            heading_leaf_id=EXTRA_HEADING_LEAF,
            body_leaf_id=EXTRA_BODY_LEAF,
            region_number=2,
        )
        false_positives = self.evaluate([self.expected_relation(), extra])
        second_false_positive = deepcopy(
            false_positives["scope_results"][0]["false_positive_relations"][0]
        )
        second_false_positive["relation_id"] = "heading-relation-" + "f" * 64
        second_false_positive["heading_occurrence_id"] = T19
        second_false_positive["heading_leaf_id"] = BODY_LEAF
        false_positives["scope_results"][0]["false_positive_relations"].append(
            second_false_positive
        )
        false_positives["metrics"]["false_positive_relation_count"] = 2
        validate_primary_heading_relation_evaluation(false_positives)
        false_positives["scope_results"][0]["false_positive_relations"].reverse()
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationEvaluationError, "canonical order"
        ):
            validate_primary_heading_relation_evaluation(false_positives)

        both_scope_failures = self.evaluate([extra])
        self.assertEqual(
            both_scope_failures["scope_results"][0]["failure_codes"],
            ["expected_relation_failed", "false_positive_relation"],
        )
        both_scope_failures["scope_results"][0]["failure_codes"].reverse()
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationEvaluationError, "canonical rank order"
        ):
            validate_primary_heading_relation_evaluation(both_scope_failures)

        outside = _relation(
            view,
            heading_leaf_id=EXTRA_BODY_LEAF,
            body_leaf_id=EXTRA_HEADING_LEAF,
            region_number=2,
        )
        unscored = self.evaluate([self.expected_relation(), outside])
        second_unscored = deepcopy(unscored["unscored_relations"][0])
        second_unscored["relation_id"] = "heading-relation-" + "f" * 64
        second_unscored["heading_occurrence_id"] = "occ:inspector:p3:t31"
        unscored["unscored_relations"].append(second_unscored)
        unscored["metrics"]["unscored_relation_count"] = 2
        validate_primary_heading_relation_evaluation(unscored)
        unscored["unscored_relations"].reverse()
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationEvaluationError, "canonical order"
        ):
            validate_primary_heading_relation_evaluation(unscored)

    def test_duplicate_candidate_heading_is_rejected_before_comparison(self) -> None:
        view = _view_payload()
        first = self.expected_relation()
        duplicate = _relation(
            view,
            heading_leaf_id=HEADING_LEAF,
            body_leaf_id=EXTRA_BODY_LEAF,
            region_number=2,
        )
        candidate = _candidate_payload(view, [first, duplicate])
        with self.assertRaises(candidate_contract.PdfPrimaryHeadingRelationsError):
            candidate_contract.validate_pdf_primary_heading_relations(candidate)

    def test_actual_114788_full_evaluator_when_artifacts_are_configured(
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
                "actual 114788 heading evaluator replay requires: "
                + ", ".join(sorted(missing))
            )

        def mapping(name: str) -> dict:
            return json.loads(Path(configured[name]).read_text(encoding="utf-8"))

        surya = mapping("surya_layout_artifact")
        arguments = {
            "source_pdf": Path(configured["source_pdf"]),
            "native_capture": mapping("native_capture"),
            "structure_candidates": mapping("structure_candidates"),
            "render_manifest": mapping("render_manifest"),
            "render_artifact_root": Path(configured["render_artifact_root"]),
            "calibration_proof": mapping("calibration_proof"),
            "expected_calibration_proof_sha256": REVIEWED_PROOF_CANONICAL_SHA256,
            "reconstruction_plan": mapping("reconstruction_plan"),
            "surya_layout_artifact": surya,
            "expected_surya_producer": SuryaProducerIdentity.from_dict(
                surya["producer"]
            ),
            "expected_surya_logical_compute_key": surya["logical_compute_key"],
            "expected_surya_pages": surya["requested_pages"],
        }
        primary_arguments = {
            key: arguments[key]
            for key in (
                "source_pdf",
                "native_capture",
                "structure_candidates",
                "render_manifest",
                "render_artifact_root",
                "calibration_proof",
                "expected_calibration_proof_sha256",
                "reconstruction_plan",
            )
        }
        primary = build_pdf_primary_document_view(**primary_arguments)
        candidate = build_pdf_primary_heading_relations(**arguments)
        base_gold = load_primary_structure_gold_file(BASE_GOLD)
        heading_gold = load_primary_heading_relation_gold_file(
            HEADING_GOLD,
            primary_structure_gold=base_gold,
        )
        result = evaluate_pdf_primary_heading_relations(
            candidate,
            primary,
            base_gold,
            heading_gold,
            **arguments,
        )
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["primary_view_prerequisite"]["verdict"], "passed")
        self.assertEqual(result["metrics"]["matched_relation_count"], 1)
        self.assertEqual(result["metrics"]["failed_scope_count"], 0)
        self.assertEqual(result["metrics"]["unscored_relation_count"], 0)
        relation_result = result["scope_results"][0]["relation_results"][0]
        self.assertEqual(relation_result["heading_occurrence_id"], T17)
        self.assertEqual(
            relation_result["body_group_id"], "p3.purpose.paragraph"
        )
        self.assertEqual(relation_result["status"], "matched")
        self.assertEqual(
            relation_result["observed_relation_id"],
            candidate["relations"][0]["relation_id"],
        )
        self.assertEqual(
            relation_result["expected_heading_leaf_id"],
            candidate["relations"][0]["heading_leaf_id"],
        )
        self.assertEqual(
            relation_result["expected_body_leaf_id"],
            candidate["relations"][0]["body_leaf_id"],
        )
        canonical = canonical_primary_heading_relation_evaluation_json(result)
        self.assertEqual(json.loads(canonical), result)

    @patch.object(evaluation, "_compare_replayed_heading_relations")
    @patch.object(evaluation, "evaluate_pdf_primary_document_view")
    @patch.object(
        evaluation, "validate_primary_heading_relation_gold_against_inputs"
    )
    @patch.object(evaluation, "validate_primary_structure_gold_against_inputs")
    @patch.object(evaluation, "validate_pdf_primary_document_view_against_inputs")
    @patch.object(evaluation, "validate_pdf_primary_heading_relations_against_inputs")
    def test_public_evaluator_replays_all_raw_inputs(
        self,
        replay_candidate,
        replay_view,
        replay_base,
        replay_heading,
        evaluate_primary,
        compare,
    ) -> None:
        replay_candidate.return_value = sentinel.replayed_candidate
        replay_view.return_value = sentinel.replayed_view
        replay_base.return_value = sentinel.replayed_base
        replay_heading.return_value = sentinel.replayed_heading
        evaluate_primary.return_value = sentinel.primary_result
        compare.return_value = sentinel.result
        raw = {
            "source_pdf": sentinel.source_pdf,
            "native_capture": sentinel.native_capture,
            "structure_candidates": sentinel.structure,
            "render_manifest": sentinel.render,
            "render_artifact_root": sentinel.render_root,
            "calibration_proof": sentinel.calibration,
            "expected_calibration_proof_sha256": "a" * 64,
            "reconstruction_plan": sentinel.plan,
            "surya_layout_artifact": sentinel.surya,
            "expected_surya_producer": sentinel.producer,
            "expected_surya_logical_compute_key": "b" * 64,
            "expected_surya_pages": [3],
        }
        result = evaluate_pdf_primary_heading_relations(
            sentinel.candidate,
            sentinel.primary_view,
            sentinel.base_gold,
            sentinel.heading_gold,
            **raw,
        )
        self.assertIs(result, sentinel.result)
        replay_candidate.assert_called_once()
        replay_view.assert_called_once()
        replay_base.assert_called_once()
        replay_heading.assert_called_once()
        evaluate_primary.assert_called_once()
        self.assertIs(
            replay_heading.call_args.kwargs["primary_structure_gold"],
            sentinel.base_gold,
        )
        compare.assert_called_once_with(
            sentinel.replayed_candidate,
            sentinel.replayed_view,
            sentinel.replayed_base,
            sentinel.replayed_heading,
            sentinel.primary_result,
        )


if __name__ == "__main__":
    unittest.main()
