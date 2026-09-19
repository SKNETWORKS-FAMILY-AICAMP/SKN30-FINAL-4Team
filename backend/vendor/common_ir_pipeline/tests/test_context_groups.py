from __future__ import annotations

import copy
import hashlib
from importlib.resources import files
import json
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

import test_fragment_groups as fragment_fixtures
import test_reconstruction_plan as reconstruction_fixtures
import common_ir_pipeline.pdf_fusion.context_groups as contract
from common_ir_pipeline.pdf_fusion.context_groups import (
    PdfContextGroupsError,
    build_pdf_context_groups,
    canonical_pdf_context_groups_json,
    validate_pdf_context_groups,
    validate_pdf_context_groups_against_inputs,
)
from common_ir_pipeline.pdf_fusion.fragment_groups import (
    build_pdf_fragment_groups,
    canonical_pdf_fragment_groups_json,
)
from common_ir_pipeline.pdf_fusion.reconstruction_plan import (
    build_pdf_reconstruction_plan,
    canonical_reconstruction_plan_json,
)


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class ContextGroupsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = reconstruction_fixtures.PdfReconstructionPlanTests()
        self.fixture.setUp()
        self.plan = self.fixture.plan()
        self.fragments = self.empty_fragments(self.plan)

    def tearDown(self) -> None:
        self.fixture.tearDown()

    @staticmethod
    def empty_fragments(plan: dict) -> dict:
        return {
            "schema_version": "pdf_fragment_groups/v1",
            "evaluation_only": True,
            "non_promotable": True,
            "standalone_validation_scope": "internal_consistency_only",
            "notice_id": plan["notice_id"],
            "source_pdf_sha256": plan["source_pdf_sha256"],
            "page_scope": list(plan["page_scope"]),
            "input_artifacts": {
                "reconstruction_plan_schema_version": plan["schema_version"],
                "reconstruction_plan_sha256": digest(canonical_reconstruction_plan_json(plan)),
                "surya_layout_artifact_schema_version": "surya_layout_artifact/v1",
                "surya_layout_artifact_sha256": digest("empty Surya fixture"),
            },
            "fragment_groups": [],
            "rejected_proposals": [],
            "metrics": {
                "eligible_odl_paragraph_count": 0,
                "accepted_fragment_group_count": 0,
                "rejected_proposal_count": 0,
            },
        }

    def build_plan(self, raw: dict[str, object], *, invalid_height: bool = False) -> dict:
        return build_pdf_reconstruction_plan(
            source_pdf=self.fixture.source,
            native_capture=self.fixture.native(invalid_height=invalid_height),
            structure_candidates=self.fixture.candidates(raw),
            render_manifest=self.fixture.render(),
            calibration_proof=self.fixture.proof,
            expected_calibration_proof_sha256=self.fixture.proof_sha256,
        )

    @staticmethod
    def schema_validator() -> Draft202012Validator:
        schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_context_groups_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def test_projects_every_plan_context_once_without_semantic_payload(self) -> None:
        result = build_pdf_context_groups(self.plan, self.fragments)
        eligible = {
            unit["unit_id"]
            for unit in self.plan["units"]
            if unit["origin"] == "opendataloader"
            and unit["alignment_status"] == "partial"
            and unit["use_policy"] == "context_only"
        }
        projected = {
            group["source_unit_id"]
            for group in result["context_groups"]
            if group["basis"] == "plan_context_unit"
        }
        self.assertEqual(projected, eligible)
        self.assertEqual(result["metrics"]["plan_context_unit_count"], len(eligible))
        self.assertEqual(result["metrics"]["fragment_consensus_count"], 0)
        self.assertEqual(result, validate_pdf_context_groups(result))
        self.assertEqual(
            result,
            validate_pdf_context_groups_against_inputs(result, self.plan, self.fragments),
        )

        encoded = canonical_pdf_context_groups_json(result).decode("utf-8")
        for sentinel in ("제목", "왼쪽", "오른쪽", "본문", "bbox", "joiner", "coverage", "value"):
            self.assertNotIn(sentinel, encoded)
        self.schema_validator().validate(result)

    def test_parent_link_never_expands_child_seed_references(self) -> None:
        raw = {
            "number of pages": 1,
            "kids": [
                {
                    "type": "list",
                    "id": 1,
                    "page number": 1,
                    "bounding box": [9, 30, 37, 42],
                    "list items": [
                        {
                            "type": "list item",
                            "id": 2,
                            "page number": 1,
                            "bounding box": [15, 30, 37, 42],
                            "kids": [],
                        }
                    ],
                }
            ],
        }
        plan = self.build_plan(raw)
        result = build_pdf_context_groups(plan, self.empty_fragments(plan))
        units = {unit["unit_id"]: unit for unit in plan["units"]}
        child = next(
            group
            for group in result["context_groups"]
            if group["hypothesis_kind"] == "list_item"
        )
        parent = units[child["structural_parent_unit_id"]]
        source = units[child["source_unit_id"]]
        self.assertEqual(child["reference_occurrence_ids"], source["reference_occurrence_ids"])
        self.assertNotEqual(child["reference_occurrence_ids"], parent["reference_occurrence_ids"])
        self.assertEqual(result["metrics"]["structural_parent_link_count"], 1)
        self.assertEqual(result["metrics"]["suppressed_parent_link_count"], 0)

    def test_diagnostic_parent_is_suppressed_instead_of_inferred(self) -> None:
        raw = {
            "number of pages": 1,
            "kids": [
                {
                    "type": "paragraph",
                    "id": 1,
                    "page number": 1,
                    "bounding box": [9, 50, 71, 62],
                    "kids": [
                        {
                            "type": "heading",
                            "id": 2,
                            "page number": 1,
                            "bounding box": [9, 70, 31, 82],
                            "kids": [],
                        }
                    ],
                }
            ],
        }
        plan = self.build_plan(raw)
        result = build_pdf_context_groups(plan, self.empty_fragments(plan))
        self.assertEqual(len(result["context_groups"]), 1)
        self.assertIsNone(result["context_groups"][0]["structural_parent_unit_id"])
        self.assertEqual(result["metrics"]["structural_parent_link_count"], 0)
        self.assertEqual(result["metrics"]["suppressed_parent_link_count"], 1)

    def test_standalone_parent_must_resolve_and_remain_acyclic(self) -> None:
        raw = {
            "number of pages": 1,
            "kids": [
                {
                    "type": "list",
                    "id": 1,
                    "page number": 1,
                    "bounding box": [9, 30, 37, 42],
                    "list items": [
                        {
                            "type": "list item",
                            "id": 2,
                            "page number": 1,
                            "bounding box": [15, 30, 37, 42],
                            "kids": [],
                        }
                    ],
                }
            ],
        }
        plan = self.build_plan(raw)
        result = build_pdf_context_groups(plan, self.empty_fragments(plan))
        child = next(
            group for group in result["context_groups"]
            if group["structural_parent_unit_id"] is not None
        )

        missing = copy.deepcopy(result)
        missing_child = next(
            group for group in missing["context_groups"]
            if group["source_unit_id"] == child["source_unit_id"]
        )
        missing_child["structural_parent_unit_id"] = "unit-odl-" + "f" * 64
        missing_child["context_group_id"] = contract._context_group_id(
            missing["source_pdf_sha256"],
            missing_child["basis"],
            missing_child["source_unit_id"],
            missing_child["source_fragment_group_id"],
            missing_child["page"],
            missing_child["hypothesis_kind"],
            missing_child["reference_occurrence_ids"],
            missing_child["structural_parent_unit_id"],
        )
        with self.assertRaisesRegex(PdfContextGroupsError, "emitted context group"):
            validate_pdf_context_groups(missing)

        cyclic = copy.deepcopy(result)
        cyclic_child = next(
            group for group in cyclic["context_groups"]
            if group["source_unit_id"] == child["source_unit_id"]
        )
        cyclic_parent = next(
            group for group in cyclic["context_groups"]
            if group["source_unit_id"] == child["structural_parent_unit_id"]
        )
        cyclic_parent["structural_parent_unit_id"] = cyclic_child["source_unit_id"]
        cyclic_parent["context_group_id"] = contract._context_group_id(
            cyclic["source_pdf_sha256"],
            cyclic_parent["basis"],
            cyclic_parent["source_unit_id"],
            cyclic_parent["source_fragment_group_id"],
            cyclic_parent["page"],
            cyclic_parent["hypothesis_kind"],
            cyclic_parent["reference_occurrence_ids"],
            cyclic_parent["structural_parent_unit_id"],
        )
        cyclic["metrics"]["structural_parent_link_count"] += 1
        with self.assertRaisesRegex(PdfContextGroupsError, "acyclic"):
            validate_pdf_context_groups(cyclic)

    def test_accepted_fragment_becomes_independent_paragraph_hypothesis(self) -> None:
        fixture = fragment_fixtures.FragmentGroupsTests()
        fixture.setUp()
        try:
            artifact = fixture.artifact(fixture.region(0, (18, 118, 74, 138)))
            fragments = build_pdf_fragment_groups(**fixture.args(artifact))
            result = build_pdf_context_groups(fixture.plan, fragments)
            self.assertEqual(result["metrics"]["plan_context_unit_count"], 0)
            self.assertEqual(result["metrics"]["fragment_consensus_count"], 1)
            group = result["context_groups"][0]
            fragment = fragments["fragment_groups"][0]
            self.assertEqual(group["basis"], "fragment_consensus")
            self.assertEqual(group["hypothesis_kind"], "paragraph")
            self.assertEqual(group["source_unit_id"], fragment["odl_unit_id"])
            self.assertEqual(group["source_fragment_group_id"], fragment["fragment_group_id"])
            self.assertEqual(group["reference_occurrence_ids"], fragment["occurrence_ids"])
            self.assertIsNone(group["structural_parent_unit_id"])
            self.schema_validator().validate(result)
        finally:
            fixture.tearDown()

    def test_fragment_source_requires_multi_occurrence_diagnostic_reason(self) -> None:
        fixture = fragment_fixtures.FragmentGroupsTests()
        fixture.setUp()
        try:
            artifact = fixture.artifact(fixture.region(0, (18, 118, 74, 138)))
            fragments = build_pdf_fragment_groups(**fixture.args(artifact))
            plan = copy.deepcopy(fixture.plan)
            source_id = fragments["fragment_groups"][0]["odl_unit_id"]
            source = next(unit for unit in plan["units"] if unit["unit_id"] == source_id)
            source["reason_codes"] = ["cross_column_or_row_merge"]
            rejected = next(item for item in plan["rejected_candidates"] if item["unit_id"] == source_id)
            rejected["reason_codes"] = list(source["reason_codes"])
            fragments = copy.deepcopy(fragments)
            fragments["input_artifacts"]["reconstruction_plan_sha256"] = digest(
                canonical_reconstruction_plan_json(plan)
            )
            with self.assertRaisesRegex(PdfContextGroupsError, "diagnostic ODL paragraph"):
                build_pdf_context_groups(plan, fragments)
        finally:
            fixture.tearDown()

    def test_standalone_fragment_kind_and_membership_exclusivity(self) -> None:
        fixture = fragment_fixtures.FragmentGroupsTests()
        fixture.setUp()
        try:
            artifact = fixture.artifact(fixture.region(0, (18, 118, 74, 138)))
            fragments = build_pdf_fragment_groups(**fixture.args(artifact))
            result = build_pdf_context_groups(fixture.plan, fragments)

            wrong_kind = copy.deepcopy(result)
            group = wrong_kind["context_groups"][0]
            group["hypothesis_kind"] = "table"
            group["context_group_id"] = contract._context_group_id(
                wrong_kind["source_pdf_sha256"],
                group["basis"],
                group["source_unit_id"],
                group["source_fragment_group_id"],
                group["page"],
                group["hypothesis_kind"],
                group["reference_occurrence_ids"],
                group["structural_parent_unit_id"],
            )
            self.assertFalse(self.schema_validator().is_valid(wrong_kind))
            with self.assertRaisesRegex(PdfContextGroupsError, "fragment consensus"):
                validate_pdf_context_groups(wrong_kind)

            overlapping = copy.deepcopy(result)
            duplicate = copy.deepcopy(overlapping["context_groups"][0])
            duplicate["source_unit_id"] = "unit-odl-" + "f" * 64
            duplicate["source_fragment_group_id"] = "fragment-" + "f" * 64
            duplicate["context_group_id"] = contract._context_group_id(
                overlapping["source_pdf_sha256"],
                duplicate["basis"],
                duplicate["source_unit_id"],
                duplicate["source_fragment_group_id"],
                duplicate["page"],
                duplicate["hypothesis_kind"],
                duplicate["reference_occurrence_ids"],
                duplicate["structural_parent_unit_id"],
            )
            overlapping["context_groups"].append(duplicate)
            overlapping["metrics"]["fragment_consensus_count"] = 2
            overlapping["metrics"]["context_group_count"] = 2
            overlapping["metrics"]["aggregate_reference_membership_count"] *= 2
            with self.assertRaisesRegex(PdfContextGroupsError, "multiple fragment"):
                validate_pdf_context_groups(overlapping)
        finally:
            fixture.tearDown()

    def test_standalone_plan_leaf_membership_is_exclusive(self) -> None:
        result = build_pdf_context_groups(self.plan, self.fragments)
        original = next(
            group for group in result["context_groups"]
            if group["basis"] == "plan_context_unit"
            and group["hypothesis_kind"] in {
                "heading", "paragraph", "list_item", "table_cell", "text_block", "caption"
            }
        )
        tampered = copy.deepcopy(result)
        duplicate = copy.deepcopy(original)
        duplicate["source_unit_id"] = "unit-odl-" + "f" * 64
        duplicate["structural_parent_unit_id"] = None
        duplicate["context_group_id"] = contract._context_group_id(
            tampered["source_pdf_sha256"],
            duplicate["basis"],
            duplicate["source_unit_id"],
            duplicate["source_fragment_group_id"],
            duplicate["page"],
            duplicate["hypothesis_kind"],
            duplicate["reference_occurrence_ids"],
            duplicate["structural_parent_unit_id"],
        )
        tampered["context_groups"].append(duplicate)
        tampered["metrics"]["plan_context_unit_count"] += 1
        tampered["metrics"]["context_group_count"] += 1
        tampered["metrics"]["aggregate_reference_membership_count"] += len(
            duplicate["reference_occurrence_ids"]
        )
        with self.assertRaisesRegex(PdfContextGroupsError, "multiple plan leaf"):
            validate_pdf_context_groups(tampered)

    def test_native_text_and_ownership_gate_are_required(self) -> None:
        plan = self.fixture.plan(invalid_height=True)
        with self.assertRaisesRegex(PdfContextGroupsError, "ownership gate"):
            build_pdf_context_groups(plan, self.empty_fragments(plan))

    def test_standalone_mutation_caps_and_replay_tamper_guards(self) -> None:
        result = build_pdf_context_groups(self.plan, self.fragments)

        reordered = copy.deepcopy(result)
        reordered["context_groups"].reverse()
        with self.assertRaisesRegex(PdfContextGroupsError, "canonically ordered"):
            validate_pdf_context_groups(reordered)

        extra = copy.deepcopy(result)
        extra["context_groups"][0]["semantic_text"] = "절대 포함 금지"
        with self.assertRaisesRegex(PdfContextGroupsError, "keys"):
            validate_pdf_context_groups(extra)

        bad_id = copy.deepcopy(result)
        bad_id["context_groups"][0]["reference_occurrence_ids"][0] = "occ:inspector:p1:t2"
        with self.assertRaisesRegex(PdfContextGroupsError, "identity does not bind"):
            validate_pdf_context_groups(bad_id)

        hash_tampered = copy.deepcopy(result)
        hash_tampered["input_artifacts"]["fragment_groups_sha256"] = "0" * 64
        validate_pdf_context_groups(hash_tampered)
        with self.assertRaisesRegex(PdfContextGroupsError, "deterministic replay"):
            validate_pdf_context_groups_against_inputs(
                hash_tampered, self.plan, self.fragments
            )

        with patch.object(contract, "MAX_CONTEXT_GROUPS", 0):
            with self.assertRaisesRegex(PdfContextGroupsError, "group count"):
                build_pdf_context_groups(self.plan, self.fragments)
        with patch.object(contract, "MAX_REFERENCES_PER_GROUP", 1):
            with self.assertRaisesRegex(PdfContextGroupsError, "reference count"):
                build_pdf_context_groups(self.plan, self.fragments)
        with patch.object(contract, "MAX_AGGREGATE_MEMBERSHIPS", 0):
            with self.assertRaisesRegex(PdfContextGroupsError, "aggregate"):
                build_pdf_context_groups(self.plan, self.fragments)

    def test_schema_runtime_parity_for_nullable_basis_specific_fields(self) -> None:
        result = build_pdf_context_groups(self.plan, self.fragments)
        validator = self.schema_validator()
        validator.validate(result)

        wrong_fragment = copy.deepcopy(result)
        wrong_fragment["context_groups"][0]["source_fragment_group_id"] = (
            "fragment-" + "1" * 64
        )
        self.assertFalse(validator.is_valid(wrong_fragment))
        with self.assertRaisesRegex(PdfContextGroupsError, "must not claim"):
            validate_pdf_context_groups(wrong_fragment)

        surrogate = copy.deepcopy(result)
        surrogate["notice_id"] = "\ud800"
        self.assertFalse(validator.is_valid(surrogate))
        with self.assertRaisesRegex(PdfContextGroupsError, "notice_id"):
            validate_pdf_context_groups(surrogate)


if __name__ == "__main__":
    unittest.main()
