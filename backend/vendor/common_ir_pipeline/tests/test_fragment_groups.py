from __future__ import annotations

import copy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

import test_reconstruction_plan as reconstruction_fixtures
from common_ir_pipeline.pdf_fusion.coordinate_manifest import build_sidecar_binding
from common_ir_pipeline.pdf_fusion.native_capture import capture_pdf_to_native
from common_ir_pipeline.pdf_fusion.fragment_groups import (
    PdfFragmentGroupsError,
    build_pdf_fragment_groups,
    canonical_pdf_fragment_groups_json,
    validate_pdf_fragment_groups,
    validate_pdf_fragment_groups_against_inputs,
)
import common_ir_pipeline.pdf_fusion.fragment_groups as contract
from common_ir_pipeline.pdf_fusion.reconstruction_plan import build_pdf_reconstruction_plan
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact, SuryaLayoutPage, SuryaLayoutRegion, SuryaProducerIdentity,
)


FRAGMENT_FINGERPRINT_PATH = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/strict_fragment_consensus_114788_full.fingerprint.v1.json"
)
FRAGMENT_FINGERPRINT_CANONICAL_SHA256 = "506a8560f0c4ec664cf14b1ab088770bf9748813a7ea0b15342f97894b1b377f"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FragmentGroupsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = reconstruction_fixtures.PdfReconstructionPlanTests()
        self.fixture.setUp()
        self.raw = {"number of pages": 1, "kids": [
            {"type": "paragraph", "id": 9, "page number": 1, "bounding box": [9, 30, 37, 42]},
        ]}
        self.native = self.fixture.native_with_adjacent_fragments()
        self.candidates = self.fixture.candidates(self.raw)
        self.render = self.fixture.render()
        self.plan = build_pdf_reconstruction_plan(
            source_pdf=self.fixture.source, native_capture=self.native, structure_candidates=self.candidates,
            render_manifest=self.render, calibration_proof=self.fixture.proof,
            expected_calibration_proof_sha256=self.fixture.proof_sha256,
        )
        self.producer = SuryaProducerIdentity(
            engine_id="surya", engine_version="1", model_id="fixture", model_revision="r1",
            model_weights_sha256=digest("model"), pipeline_revision="fixture-layout",
            config_sha256=digest("config"), worker_image_digest="sha256:" + digest("image"),
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def artifact(self, *regions: SuryaLayoutRegion) -> SuryaLayoutArtifact:
        page = SuryaLayoutPage(
            page=1, sidecar_binding=build_sidecar_binding(self.render.pages[0].coordinate_manifest),
            pixel_width=200, pixel_height=200, rendered_page_px=(0, 0, 200, 200), regions=regions,
        )
        return SuryaLayoutArtifact(
            logical_compute_key=digest("logical"), source_sha256=self.render.source_pdf_sha256, page_count=1,
            render_manifest_schema_version=self.render.schema_version, render_manifest_sha256=self.render.manifest_sha256(),
            producer=self.producer, requested_pages=(1,), pages=(page,),
        )

    def region(self, order: int, bbox: tuple[float, float, float, float], polygon: tuple[tuple[float, float], ...] | None = None) -> SuryaLayoutRegion:
        x0, y0, x1, y1 = bbox
        return SuryaLayoutRegion(
            region_id=f"p0001-text-{order + 1:04d}", label="text", bbox_px=bbox,
            polygon_px=polygon or ((x0, y0), (x1, y0), (x1, y1), (x0, y1)), confidence=None, reading_order=order,
        )

    def args(self, artifact: SuryaLayoutArtifact) -> dict:
        return {
            "source_pdf": self.fixture.source, "native_capture": self.native, "structure_candidates": self.candidates,
            "render_manifest": self.render, "calibration_proof": self.fixture.proof,
            "expected_calibration_proof_sha256": self.fixture.proof_sha256, "reconstruction_plan": self.plan,
            "surya_layout_artifact": artifact, "expected_surya_producer": self.producer,
            "expected_surya_logical_compute_key": digest("logical"), "expected_surya_pages": (1,),
        }

    def test_exact_single_region_creates_textless_non_promotable_group(self) -> None:
        result = build_pdf_fragment_groups(**self.args(self.artifact(self.region(0, (18, 118, 74, 138)))))
        self.assertEqual(result["metrics"], {"eligible_odl_paragraph_count": 1, "accepted_fragment_group_count": 1, "rejected_proposal_count": 0})
        self.assertEqual(result["notice_id"], "PBLN_fixture")
        encoded = canonical_pdf_fragment_groups_json(result).decode("utf-8")
        self.assertNotIn("본문", encoded)
        self.assertNotIn("bbox", encoded)
        self.assertNotIn("ocr", encoded.lower())
        self.assertEqual(result, validate_pdf_fragment_groups_against_inputs(result, **self.args(self.artifact(self.region(0, (18, 118, 74, 138))))))

    def test_multiple_region_or_extra_native_occurrence_fails_closed_to_rejection(self) -> None:
        two = self.artifact(self.region(0, (18, 118, 74, 138)), self.region(1, (17, 117, 75, 139)))
        result = build_pdf_fragment_groups(**self.args(two))
        self.assertEqual(result["fragment_groups"], [])
        self.assertIn("no_unique_surya_text_region", result["rejected_proposals"][0]["reason_codes"])

        # Includes the title native atom as well as the two intended atoms.
        extra = build_pdf_fragment_groups(**self.args(self.artifact(self.region(0, (18, 38, 74, 138)))))
        self.assertEqual(extra["fragment_groups"], [])
        self.assertIn("surya_region_extra_native_occurrence", extra["rejected_proposals"][0]["reason_codes"])

        # Even one exact narrow region cannot win when a broader text region
        # contains the same proposal plus a different native atom.
        narrow_and_broad = self.artifact(
            self.region(0, (18, 118, 74, 138)), self.region(1, (18, 38, 74, 138)),
        )
        ambiguous = build_pdf_fragment_groups(**self.args(narrow_and_broad))
        self.assertEqual(ambiguous["fragment_groups"], [])
        self.assertIn("surya_region_extra_native_occurrence", ambiguous["rejected_proposals"][0]["reason_codes"])

    def test_material_overlap_and_non_rectangle_polygon_veto_an_exact_membership(self) -> None:
        # This reviewer case fully contains t3/t4 but clips t1, so t1 is not
        # a member. Its large overlap must nevertheless veto the region.
        overlap = build_pdf_fragment_groups(**self.args(self.artifact(self.region(0, (18, 80.5, 74, 138)))))
        self.assertEqual(overlap["fragment_groups"], [])
        self.assertIn("surya_region_material_overlap", overlap["rejected_proposals"][0]["reason_codes"])

        diamond = self.region(
            0, (18, 118, 74, 138),
            ((46, 118), (74, 128), (46, 138), (18, 128)),
        )
        with self.assertRaisesRegex(PdfFragmentGroupsError, "polygon"):
            build_pdf_fragment_groups(**self.args(self.artifact(diamond)))

    def test_competing_and_non_contiguous_odl_proposals_are_rejected(self) -> None:
        raw = {"number of pages": 1, "kids": [
            {"type": "paragraph", "id": 9, "page number": 1, "bounding box": [9, 30, 37, 42]},
            {"type": "paragraph", "id": 10, "page number": 1, "bounding box": [9, 30, 37, 42]},
        ]}
        candidates = self.fixture.candidates(raw)
        plan = build_pdf_reconstruction_plan(
            source_pdf=self.fixture.source, native_capture=self.native, structure_candidates=candidates,
            render_manifest=self.render, calibration_proof=self.fixture.proof,
            expected_calibration_proof_sha256=self.fixture.proof_sha256,
        )
        args = self.args(self.artifact(self.region(0, (18, 118, 74, 138))))
        args.update(structure_candidates=candidates, reconstruction_plan=plan)
        competing = build_pdf_fragment_groups(**args)
        self.assertEqual(competing["fragment_groups"], [])
        self.assertTrue(all("competing_odl_paragraph" in item["reason_codes"] for item in competing["rejected_proposals"]))

        class NonContiguousInspector(reconstruction_fixtures.AdjacentFragmentInspector):
            def extract_text_with_positions_bytes(self, data: bytes) -> list[dict]:
                values = super().extract_text_with_positions_bytes(data)
                middle = dict(values[4])
                middle.update(text="중간", x=70.0, y=10.0, mcid=99)
                values.insert(4, middle)
                return values

        native = capture_pdf_to_native(
            self.fixture.source, notice_id="PBLN_fixture", source_relative_path="attachments/source.pdf",
            inspector_module=NonContiguousInspector(), inspector_version="1.17.0",
        )
        plan = build_pdf_reconstruction_plan(
            source_pdf=self.fixture.source, native_capture=native, structure_candidates=self.candidates,
            render_manifest=self.render, calibration_proof=self.fixture.proof,
            expected_calibration_proof_sha256=self.fixture.proof_sha256,
        )
        args = self.args(self.artifact(self.region(0, (18, 118, 74, 138))))
        args.update(native_capture=native, reconstruction_plan=plan)
        non_contiguous = build_pdf_fragment_groups(**args)
        self.assertIn("non_contiguous_substantive_source_order", non_contiguous["rejected_proposals"][0]["reason_codes"])

    def test_schema_runtime_parity_and_replay_tamper_rejection(self) -> None:
        result = build_pdf_fragment_groups(**self.args(self.artifact(self.region(0, (18, 118, 74, 138)))))
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath("schemas/pdf_fragment_groups_v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(result)
        tampered = copy.deepcopy(result)
        tampered["fragment_groups"][0]["owner_unit_ids"][0] = "unit-native-" + "0" * 64
        with self.assertRaisesRegex(PdfFragmentGroupsError, "owners"):
            validate_pdf_fragment_groups(tampered)
        tampered = copy.deepcopy(result)
        tampered["input_artifacts"]["surya_layout_artifact_sha256"] = "0" * 64
        validate_pdf_fragment_groups(tampered)
        with self.assertRaisesRegex(PdfFragmentGroupsError, "deterministic replay"):
            validate_pdf_fragment_groups_against_inputs(tampered, **self.args(self.artifact(self.region(0, (18, 118, 74, 138)))))

    def test_page_scope_and_canonical_member_rejection_guards(self) -> None:
        artifact = self.artifact(self.region(0, (18, 118, 74, 138)))
        args = self.args(artifact)
        args["expected_surya_pages"] = ()
        with self.assertRaisesRegex(PdfFragmentGroupsError, "page_scope"):
            build_pdf_fragment_groups(**args)

        result = build_pdf_fragment_groups(**self.args(artifact))
        bad_order = copy.deepcopy(result)
        bad_order["fragment_groups"][0]["occurrence_ids"].reverse()
        bad_order["fragment_groups"][0]["owner_unit_ids"].reverse()
        with self.assertRaisesRegex(PdfFragmentGroupsError, "source order"):
            validate_pdf_fragment_groups(bad_order)
        bad_region = copy.deepcopy(result)
        bad_region["fragment_groups"][0]["surya_region_id"] = "p0001-table-0001"
        with self.assertRaisesRegex(PdfFragmentGroupsError, "Surya region"):
            validate_pdf_fragment_groups(bad_region)

        canonical_rejections = copy.deepcopy(result)
        canonical_rejections["fragment_groups"] = []
        first, second = "unit-odl-" + "1" * 64, "unit-odl-" + "2" * 64
        canonical_rejections["rejected_proposals"] = [
            {"odl_unit_id": first, "reason_codes": ["no_unique_surya_text_region"]},
            {"odl_unit_id": second, "reason_codes": ["wrong_native_owner"]},
        ]
        canonical_rejections["metrics"] = {"eligible_odl_paragraph_count": 2, "accepted_fragment_group_count": 0, "rejected_proposal_count": 2}
        validate_pdf_fragment_groups(canonical_rejections)
        canonical_rejections["rejected_proposals"].reverse()
        with self.assertRaisesRegex(PdfFragmentGroupsError, "rejected ODL"):
            validate_pdf_fragment_groups(canonical_rejections)

    def test_generator_pages_surrogate_notice_and_membership_caps(self) -> None:
        artifact = self.artifact(self.region(0, (18, 118, 74, 138)))
        args = self.args(artifact)
        args["expected_surya_pages"] = (page for page in (1,))
        result = build_pdf_fragment_groups(**args)
        surrogate = copy.deepcopy(result)
        surrogate["notice_id"] = "\ud800"
        with self.assertRaisesRegex(PdfFragmentGroupsError, "notice_id"):
            validate_pdf_fragment_groups(surrogate)
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath("schemas/pdf_fragment_groups_v1.schema.json").read_text(encoding="utf-8"))
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(surrogate)))
        with patch.object(contract, "MAX_REGION_NATIVE_COMPARISONS", 0):
            with self.assertRaisesRegex(PdfFragmentGroupsError, "comparison count"):
                build_pdf_fragment_groups(**self.args(artifact))
        with patch.object(contract, "MAX_AGGREGATE_MEMBERSHIPS", 1):
            with self.assertRaisesRegex(PdfFragmentGroupsError, "aggregate membership"):
                build_pdf_fragment_groups(**self.args(artifact))
        with patch.object(contract, "MAX_PROPOSAL_REGION_WORK", 0):
            with self.assertRaisesRegex(PdfFragmentGroupsError, "proposal/region work"):
                build_pdf_fragment_groups(**self.args(artifact))
        with patch.object(contract, "MAX_OUTPUT_MEMBERSHIPS", 1):
            with self.assertRaisesRegex(PdfFragmentGroupsError, "output membership"):
                validate_pdf_fragment_groups(result)

    def test_standalone_malformed_occurrence_inputs_never_leak_conversion_errors(self) -> None:
        result = build_pdf_fragment_groups(**self.args(self.artifact(self.region(0, (18, 118, 74, 138)))))
        for malformed in (None, 17, "occ:inspector:p1:t" + "9" * 5000):
            with self.subTest(malformed_type=type(malformed).__name__):
                bad = copy.deepcopy(result)
                bad["fragment_groups"][0]["occurrence_ids"][0] = malformed
                with self.assertRaises(PdfFragmentGroupsError):
                    validate_pdf_fragment_groups(bad)

    def test_114788_external_fragment_attestation_freezes_fail_closed_disposition(self) -> None:
        fingerprint = json.loads(FRAGMENT_FINGERPRINT_PATH.read_text(encoding="utf-8"))
        encoded = json.dumps(
            fingerprint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), FRAGMENT_FINGERPRINT_CANONICAL_SHA256)
        self.assertEqual(fingerprint["schema_version"], "pdf_fragment_groups_fingerprint/v1")
        self.assertTrue(fingerprint["evaluation_only"])
        self.assertTrue(fingerprint["non_promotable"])
        self.assertFalse(fingerprint["source"]["source_pdf_in_repository"])
        self.assertFalse(fingerprint["attestation"]["hermetic_replay_available_in_repository"])
        self.assertEqual(fingerprint["attestation"]["artifact_bound_replay_validation"], "passed")

        quality = fingerprint["quality_scope"]
        self.assertEqual(quality["claim"], "strict_fragment_consensus_disposition_only")
        self.assertFalse(quality["semantic_quality_approved"])
        self.assertFalse(quality["structural_quality_approved"])
        self.assertFalse(quality["common_ir_promotion_approved"])

        surya = fingerprint["surya_observation"]
        self.assertEqual(surya["region_count"], 64)
        self.assertEqual(sum(surya["region_label_counts"].values()), 64)
        self.assertEqual(surya["canonical_rectangle_region_count"], 64)
        self.assertFalse(surya["production_provenance_approved"])

        projection = fingerprint["projection"]
        self.assertEqual(projection["eligible_odl_paragraph_count"], 8)
        self.assertEqual(projection["accepted_fragment_group_count"], 0)
        self.assertEqual(projection["rejected_proposal_count"], 8)
        self.assertEqual(
            projection["rejection_reason_membership_counts"],
            {
                "no_unique_surya_text_region": 5,
                "non_contiguous_substantive_source_order": 3,
            },
        )
        self.assertFalse(projection["semantic_text_copied_into_fragment_groups"])
        self.assertFalse(projection["common_ir_or_selector_materialized"])

        context = fingerprint["context_projection"]
        self.assertEqual(context["schema_version"], "pdf_context_groups/v1")
        self.assertEqual(context["context_policy_version"], "pdf-context-groups/v1")
        self.assertEqual(context["context_group_count"], 101)
        self.assertEqual(context["plan_context_unit_count"], 101)
        self.assertEqual(context["fragment_consensus_count"], 0)
        self.assertEqual(context["aggregate_reference_membership_count"], 214)
        self.assertEqual(context["unique_reference_count"], 129)
        self.assertEqual(context["max_reference_count"], 37)
        self.assertEqual(context["structural_parent_link_count"], 1)
        self.assertEqual(context["suppressed_parent_link_count"], 77)
        self.assertFalse(context["semantic_text_copied_into_context_groups"])
        self.assertFalse(context["selector_or_llm_payload_materialized"])
