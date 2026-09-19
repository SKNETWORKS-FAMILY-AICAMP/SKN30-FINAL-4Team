from __future__ import annotations

import copy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.coordinate_manifest import AffineTransform, PdfCoordinateManifest
from common_ir_pipeline.pdf_fusion.legacy_opendataloader_evaluation import (
    LegacyOdlPageToSourcePage,
    build_legacy_opendataloader_evaluation_bytes,
)
from common_ir_pipeline.pdf_fusion.native_capture import capture_pdf_to_native
from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    REVIEWED_PROOF_CANONICAL_SHA256,
    calibration_proof_sha256,
)
from common_ir_pipeline.pdf_fusion.reconstruction_plan import (
    MAX_CONTEXT_MEMBERSHIPS_PER_OCCURRENCE,
    MAX_CONTEXT_REFERENCES_PER_UNIT,
    PdfReconstructionPlanError,
    build_pdf_reconstruction_plan,
    canonical_reconstruction_plan_json,
    validate_reconstruction_plan,
    validate_reconstruction_plan_against_inputs,
)
from common_ir_pipeline.pdf_fusion.render_manifest import PdfRenderManifest, RenderedPage
from common_ir_pipeline.pdf_fusion.structure_candidates import project_structure_candidates


PROOF_PATH = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/opendataloader_coordinate_calibration_v1/calibration_proof.json"
)
ALIGNMENT_FINGERPRINT_PATH = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/native_odl_alignment_121019_p5.fingerprint.v1.json"
)
ALIGNMENT_FINGERPRINT_CANONICAL_SHA256 = "38b27975fdf1861423161cc027dd8a24cda33cc82e6052d32047f0f54fd039dc"
HELD_OUT_ALIGNMENT_FINGERPRINT_PATH = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/native_odl_alignment_114788_full.fingerprint.v1.json"
)
HELD_OUT_ALIGNMENT_FINGERPRINT_CANONICAL_SHA256 = (
    "481eaa09ed9c2b29f47ab1154da23d10020819b723c4870457833f50f948f4a9"
)


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def reconstruction_unit_id(origin: str, identity: str, source_sha256: str) -> str:
    return f"unit-{origin}-{digest(f'{source_sha256}:{origin}:{identity}')}"


class FakeInspector:
    def __init__(self, *, invalid_height: bool = False) -> None:
        self.invalid_height = invalid_height

    def process_pdf_bytes(self, data: bytes) -> dict:
        del data
        return {
            "pdf_type": "text_based",
            "markdown": "fixture",
            "page_count": 1,
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "title": "fixture",
            "confidence": 1.0,
            "is_complex_layout": True,
            "pages_with_tables": [],
            "pages_with_columns": [],
            "has_encoding_issues": False,
        }

    def extract_pages_markdown_bytes(self, data: bytes) -> dict:
        del data
        return {
            "pages": [{"page": 0, "markdown": "fixture", "needs_ocr": False, "ocr_reason": None}],
            "pages_with_tables": [],
            "pages_with_columns": [],
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "is_complex": True,
        }

    def extract_text_with_positions_bytes(self, data: bytes) -> list[dict]:
        del data
        values = (
            ("제목", 10.0, 72.0, 20.0, 8.0),
            ("왼쪽", 10.0, 52.0, 10.0, 8.0),
            ("오른쪽", 60.0, 52.0, 10.0, 8.0),
            ("□", 10.0, 32.0, 2.0, 8.0),
            ("본문", 16.0, 32.0, 20.0, 8.0),
            ("   ", 10.0, 15.0, 5.0, 5.0),
        )
        output = []
        for index, (text, x, y, width, height) in enumerate(values):
            output.append(
                {
                    "text": text,
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": -height if self.invalid_height and index == 0 else height,
                    "font": "Fixture",
                    "font_tag": "F1",
                    "font_size": 10.0,
                    "page": 1,
                    "is_bold": False,
                    "is_italic": False,
                    "is_underline": False,
                    "is_strikeout": False,
                    "item_type": "text",
                    "mcid": index,
                }
            )
        return output

    def extract_structure_elements_bytes(self, data: bytes) -> list[dict]:
        del data
        return []


class EmptyInspector(FakeInspector):
    def extract_text_with_positions_bytes(self, data: bytes) -> list[dict]:
        del data
        return []


class AdjacentFragmentInspector(FakeInspector):
    def extract_text_with_positions_bytes(self, data: bytes) -> list[dict]:
        items = super().extract_text_with_positions_bytes(data)
        items[3]["text"] = "앞"
        return items


def structure_document() -> dict[str, object]:
    return {
        "number of pages": 1,
        "kids": [
            {"type": "heading", "id": 1, "page number": 1, "bounding box": [9, 70, 31, 82]},
            # One parser leaf swallowing two distant same-line columns must be diagnostic only.
            {"type": "paragraph", "id": 2, "page number": 1, "bounding box": [9, 50, 71, 62]},
            {
                "type": "list",
                "id": 3,
                "page number": 1,
                "bounding box": [9, 30, 37, 42],
                "list items": [
                    {"type": "list item", "id": 4, "page number": 1, "bounding box": [9, 30, 37, 42], "kids": []}
                ],
            },
            {"type": "paragraph", "id": 5, "page number": 1},
        ],
    }


class PdfReconstructionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source.pdf"
        self.source.write_bytes(b"%PDF-1.7\nreconstruction fixture\n")
        self.source_sha256 = digest(self.source.read_bytes())
        self.proof = json.loads(PROOF_PATH.read_text(encoding="utf-8"))
        self.proof_sha256 = calibration_proof_sha256(self.proof)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def native(self, *, invalid_height: bool = False) -> dict:
        return capture_pdf_to_native(
            self.source,
            notice_id="PBLN_fixture",
            source_relative_path="attachments/source.pdf",
            inspector_module=FakeInspector(invalid_height=invalid_height),
            inspector_version="1.17.0",
        )

    def native_without_text(self) -> dict:
        return capture_pdf_to_native(
            self.source,
            notice_id="PBLN_fixture",
            source_relative_path="attachments/source.pdf",
            inspector_module=EmptyInspector(),
            inspector_version="1.17.0",
        )

    def native_with_adjacent_fragments(self) -> dict:
        return capture_pdf_to_native(
            self.source,
            notice_id="PBLN_fixture",
            source_relative_path="attachments/source.pdf",
            inspector_module=AdjacentFragmentInspector(),
            inspector_version="1.17.0",
        )

    def render(self) -> PdfRenderManifest:
        image_sha256 = digest("fixture image")
        forward = AffineTransform.from_sequence([2, 0, 0, -2, 0, 200])
        coordinate = PdfCoordinateManifest(
            source_sha256=self.source_sha256,
            page=1,
            page_count=1,
            media_box=(0, 0, 100, 100),
            crop_box=(0, 0, 100, 100),
            rotation=0,
            user_unit=1.0,
            canonical_width_pt=100.0,
            canonical_height_pt=100.0,
            render_scale_px_per_point=2.0,
            rendered_width_px=200,
            rendered_height_px=200,
            pdf_origin="bottom_left",
            pdf_x_axis="right",
            pdf_y_axis="up",
            pixel_origin="top_left",
            pixel_x_axis="right",
            pixel_y_axis="down",
            user_to_pixel=forward,
            pixel_to_user=forward.inverse(),
            renderer="fixture-renderer",
            renderer_version="1.0",
            renderer_config_sha256=digest("renderer config"),
            page_image_sha256=image_sha256,
        )
        page = RenderedPage(
            page=1,
            image_relative_path="rendered/page-0001.png",
            image_sha256=image_sha256,
            image_size_bytes=1,
            coordinate_manifest=coordinate,
            coordinate_manifest_sha256=coordinate.manifest_sha256(),
        )
        return PdfRenderManifest(
            source_pdf_relative_path="source.pdf",
            source_pdf_sha256=self.source_sha256,
            source_pdf_size_bytes=self.source.stat().st_size,
            page_count=1,
            renderer="fixture-renderer",
            renderer_version="1.0",
            renderer_config_sha256=digest("renderer config"),
            pages=(page,),
        )

    def candidates(self, raw: dict[str, object] | None = None) -> dict:
        raw = raw or structure_document()
        raw_bytes = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        binding = build_legacy_opendataloader_evaluation_bytes(
            raw_bytes,
            notice_id="PBLN_fixture",
            source_pdf_sha256=self.source_sha256,
            source_page_count=1,
            page_scope=(1,),
            odl_page_to_source_page=(LegacyOdlPageToSourcePage(1, 1),),
        )
        return project_structure_candidates(binding, raw, raw_bytes=raw_bytes, actual_odl_page_count=1)

    def plan(self, *, invalid_height: bool = False) -> dict:
        return build_pdf_reconstruction_plan(
            source_pdf=self.source,
            native_capture=self.native(invalid_height=invalid_height),
            structure_candidates=self.candidates(),
            render_manifest=self.render(),
            calibration_proof=self.proof,
            expected_calibration_proof_sha256=self.proof_sha256,
        )

    def schema_validator(self) -> Draft202012Validator:
        schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_reconstruction_plan_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def test_native_atoms_own_text_and_legacy_structure_is_context_only(self) -> None:
        plan = self.plan()
        self.assertEqual(plan["metrics"]["native_ownership_gate_status"], "passed")
        self.assertEqual(plan["metrics"]["scoped_native_occurrence_count"], 6)
        self.assertEqual(plan["metrics"]["substantive_native_occurrence_count"], 5)
        self.assertEqual(plan["metrics"]["atomic_evidence_unit_count"], 5)
        self.assertEqual(plan["metrics"]["structure_candidate_count"], 5)
        self.assertEqual(plan["coordinate_projection"]["parser_binding_status"], "legacy_claim_unbound")

        native_units = [unit for unit in plan["units"] if unit["origin"] == "native"]
        odl_units = [unit for unit in plan["units"] if unit["origin"] == "opendataloader"]
        self.assertTrue(all(unit["use_policy"] == "evidence_atomic" for unit in native_units))
        self.assertTrue(all(not unit["primary_occurrence_ids"] for unit in odl_units))
        self.assertTrue(all(unit["alignment_status"] != "accepted" for unit in odl_units))

        rejected = [unit for unit in odl_units if unit["use_policy"] == "diagnostic_only"]
        self.assertEqual(len(rejected), 2)
        self.assertIn("cross_column_or_row_merge", rejected[0]["reason_codes"])
        self.assertIn("no_candidate_bbox", rejected[1]["reason_codes"])
        bullet = next(unit for unit in odl_units if "split_bullet_marker" in unit["reason_codes"])
        self.assertEqual(len(bullet["reference_occurrence_ids"]), 2)
        self.assertEqual(bullet["use_policy"], "context_only")

        encoded = canonical_reconstruction_plan_json(plan)
        for forbidden in ("제목", "왼쪽", "오른쪽", "본문"):
            self.assertNotIn(forbidden, encoded.decode("utf-8"))
        self.assertEqual(plan, validate_reconstruction_plan(plan))

    def test_schema_matches_runtime_and_plan_is_deterministic(self) -> None:
        first, second = self.plan(), self.plan()
        self.assertEqual(canonical_reconstruction_plan_json(first), canonical_reconstruction_plan_json(second))
        self.schema_validator().validate(first)

    def test_artifact_bound_replay_rejects_hash_and_geometry_tampering(self) -> None:
        plan = self.plan()
        replay_arguments = {
            "source_pdf": self.source,
            "native_capture": self.native(),
            "structure_candidates": self.candidates(),
            "render_manifest": self.render(),
            "calibration_proof": self.proof,
            "expected_calibration_proof_sha256": self.proof_sha256,
        }
        self.assertEqual(
            plan,
            validate_reconstruction_plan_against_inputs(plan, **replay_arguments),
        )

        hash_tampered = copy.deepcopy(plan)
        hash_tampered["input_artifacts"]["native_capture_sha256"] = "0" * 64
        # Standalone validation has an explicit internal-consistency-only
        # scope.  The artifact-bound boundary must catch this claim.
        validate_reconstruction_plan(hash_tampered)
        with self.assertRaisesRegex(PdfReconstructionPlanError, "deterministic replay"):
            validate_reconstruction_plan_against_inputs(hash_tampered, **replay_arguments)

        geometry_tampered = copy.deepcopy(plan)
        for collection in (geometry_tampered["units"], geometry_tampered["native_occurrence_ledger"]):
            for item in collection:
                bbox = item["bbox_pdf_user_space"]
                if bbox is not None:
                    item["bbox_pdf_user_space"] = [coordinate + 1_000_000_000 for coordinate in bbox]
        validate_reconstruction_plan(geometry_tampered)
        with self.assertRaisesRegex(PdfReconstructionPlanError, "deterministic replay"):
            validate_reconstruction_plan_against_inputs(geometry_tampered, **replay_arguments)

    def test_no_native_text_requires_ocr_semantic_v2_and_cannot_pass_ownership_gate(self) -> None:
        plan = build_pdf_reconstruction_plan(
            source_pdf=self.source,
            native_capture=self.native_without_text(),
            structure_candidates=self.candidates(),
            render_manifest=self.render(),
            calibration_proof=self.proof,
            expected_calibration_proof_sha256=self.proof_sha256,
        )
        self.assertEqual(plan["native_text_status"], "requires_ocr_semantic_v2")
        self.assertEqual(plan["metrics"]["native_ownership_gate_status"], "failed")
        self.assertEqual(plan["metrics"]["substantive_native_occurrence_count"], 0)
        self.schema_validator().validate(plan)

    def test_adjacent_fragments_are_not_mislabeled_as_a_cross_column_merge(self) -> None:
        plan = build_pdf_reconstruction_plan(
            source_pdf=self.source,
            native_capture=self.native_with_adjacent_fragments(),
            structure_candidates=self.candidates(),
            render_manifest=self.render(),
            calibration_proof=self.proof,
            expected_calibration_proof_sha256=self.proof_sha256,
        )
        unit = next(
            item
            for item in plan["units"]
            if item["origin"] == "opendataloader"
            and len(item["reference_occurrence_ids"]) == 2
            and item["kind"] == "list_item"
        )
        self.assertEqual(unit["alignment_status"], "rejected")
        self.assertIn("multi_occurrence_leaf_unverified", unit["reason_codes"])
        self.assertNotIn("cross_column_or_row_merge", unit["reason_codes"])

    def test_calibration_file_and_canonical_proof_hashes_are_not_interchanged(self) -> None:
        file_sha256 = digest(PROOF_PATH.read_bytes())
        self.assertEqual(
            file_sha256,
            "b7b7a7e2556d7f907d655ac37d5b50d70e9fbfdd519d8516b6e9e2df86de1eca",
        )
        self.assertEqual(self.proof_sha256, REVIEWED_PROOF_CANONICAL_SHA256)
        self.assertEqual(
            self.proof_sha256,
            "f8c041e14cad1637150165050dc12ff87ef9a752d8192e46e73bda12edf003be",
        )
        self.assertNotEqual(file_sha256, self.proof_sha256)

    def test_tampered_proof_source_and_reciprocal_ledger_fail_closed(self) -> None:
        tampered_proof = copy.deepcopy(self.proof)
        tampered_proof["status"] = "failed"
        with self.assertRaisesRegex(PdfReconstructionPlanError, "reviewed canonical digest"):
            build_pdf_reconstruction_plan(
                source_pdf=self.source,
                native_capture=self.native(),
                structure_candidates=self.candidates(),
                render_manifest=self.render(),
                calibration_proof=tampered_proof,
                expected_calibration_proof_sha256=self.proof_sha256,
            )

        capture = self.native()
        capture["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(PdfReconstructionPlanError, "native capture is invalid"):
            build_pdf_reconstruction_plan(
                source_pdf=self.source,
                native_capture=capture,
                structure_candidates=self.candidates(),
                render_manifest=self.render(),
                calibration_proof=self.proof,
                expected_calibration_proof_sha256=self.proof_sha256,
            )

        plan = self.plan()
        owned = next(entry for entry in plan["native_occurrence_ledger"] if entry["primary_owner_unit_id"])
        owned["primary_owner_unit_id"] = None
        with self.assertRaisesRegex(PdfReconstructionPlanError, "missing from ledger"):
            validate_reconstruction_plan(plan)

    def test_invalid_native_geometry_is_visible_and_fails_the_gate(self) -> None:
        plan = self.plan(invalid_height=True)
        self.assertEqual(plan["metrics"]["native_ownership_gate_status"], "failed")
        self.assertEqual(plan["metrics"]["unowned_substantive_occurrence_count"], 1)
        invalid = plan["native_occurrence_ledger"][0]
        self.assertEqual(invalid["substantive_status"], "invalid_geometry")
        self.assertIsNone(invalid["primary_owner_unit_id"])
        self.assertIn("invalid_native_geometry", invalid["reason_codes"])

    def test_competing_leaf_contexts_fail_closed_without_changing_native_ownership(self) -> None:
        raw = structure_document()
        raw["kids"].append(  # type: ignore[union-attr]
            {"type": "paragraph", "id": 6, "page number": 1, "bounding box": [9, 70, 31, 82]}
        )
        plan = build_pdf_reconstruction_plan(
            source_pdf=self.source,
            native_capture=self.native(),
            structure_candidates=self.candidates(raw),
            render_manifest=self.render(),
            calibration_proof=self.proof,
            expected_calibration_proof_sha256=self.proof_sha256,
        )
        competing = [
            unit for unit in plan["units"]
            if unit["origin"] == "opendataloader" and "competing_leaf_context" in unit["reason_codes"]
        ]
        self.assertEqual(len(competing), 2)
        self.assertTrue(all(unit["use_policy"] == "diagnostic_only" for unit in competing))
        title = plan["native_occurrence_ledger"][0]
        self.assertIsNotNone(title["primary_owner_unit_id"])
        self.assertEqual(title["context_unit_ids"], [])
        self.assertEqual(plan["metrics"]["unowned_substantive_occurrence_count"], 0)

    def test_rejects_legacy_projection_relabelled_as_strict(self) -> None:
        candidates = self.candidates()
        candidates["input_binding_schema_version"] = "opendataloader_artifact/v1"
        candidates["input_binding_sha256"] = "0" * 64
        with self.assertRaises(PdfReconstructionPlanError):
            build_pdf_reconstruction_plan(
                source_pdf=self.source,
                native_capture=self.native(),
                structure_candidates=candidates,
                render_manifest=self.render(),
                calibration_proof=self.proof,
                expected_calibration_proof_sha256=self.proof_sha256,
            )

    def test_validator_rejects_odl_promotion_kind_and_identity_tampering(self) -> None:
        validator = self.schema_validator()

        with self.subTest("legacy accepted"):
            plan = self.plan()
            unit = next(
                item
                for item in plan["units"]
                if item["origin"] == "opendataloader" and item["use_policy"] == "context_only"
            )
            unit["alignment_status"] = "accepted"
            plan["metrics"]["candidate_alignment_counts"]["partial"] -= 1
            plan["metrics"]["candidate_alignment_counts"]["accepted"] += 1
            self.assertFalse(validator.is_valid(plan))
            with self.assertRaises(PdfReconstructionPlanError):
                validate_reconstruction_plan(plan)

        with self.subTest("semantic text in kind"):
            plan = self.plan()
            unit = next(item for item in plan["units"] if item["origin"] == "opendataloader")
            unit["kind"] = "LEAKED SEMANTIC TEXT"
            self.assertFalse(validator.is_valid(plan))
            with self.assertRaises(PdfReconstructionPlanError):
                validate_reconstruction_plan(plan)

        with self.subTest("non-deterministic ODL unit id"):
            plan = self.plan()
            unit = next(
                item
                for item in plan["units"]
                if item["origin"] == "opendataloader" and item["use_policy"] == "context_only"
            )
            old_id = unit["unit_id"]
            new_id = "unit-odl-" + "0" * 64
            self.assertNotEqual(old_id, new_id)
            unit["unit_id"] = new_id
            for entry in plan["native_occurrence_ledger"]:
                entry["context_unit_ids"] = [
                    new_id if item == old_id else item for item in entry["context_unit_ids"]
                ]
            for child in plan["units"]:
                if child["parent_unit_id"] == old_id:
                    child["parent_unit_id"] = new_id
            with self.assertRaises(PdfReconstructionPlanError):
                validate_reconstruction_plan(plan)

    def test_validator_rejects_native_owner_geometry_and_state_tampering(self) -> None:
        with self.subTest("bbox"):
            plan = self.plan()
            unit = next(item for item in plan["units"] if item["origin"] == "native")
            unit["bbox_pdf_user_space"] = [80.0, 80.0, 90.0, 90.0]
            with self.assertRaises(PdfReconstructionPlanError):
                validate_reconstruction_plan(plan)

        with self.subTest("page"):
            plan = self.plan()
            plan["page_scope"].append(2)
            unit = next(item for item in plan["units"] if item["origin"] == "native")
            unit["page"] = 2
            with self.assertRaises(PdfReconstructionPlanError):
                validate_reconstruction_plan(plan)

        with self.subTest("status-disposition tuple"):
            plan = self.plan()
            entry = next(item for item in plan["native_occurrence_ledger"] if item["primary_owner_unit_id"])
            entry["substantive_status"] = "non_substantive"
            entry["disposition"] = "non_substantive"
            entry["reason_codes"] = ["non_substantive_native"]
            plan["metrics"]["substantive_native_occurrence_count"] -= 1
            with self.assertRaises(PdfReconstructionPlanError):
                validate_reconstruction_plan(plan)

        with self.subTest("parser parent"):
            plan = self.plan()
            native = next(item for item in plan["units"] if item["origin"] == "native")
            parser = next(item for item in plan["units"] if item["origin"] == "opendataloader")
            native["parent_unit_id"] = parser["unit_id"]
            self.assertFalse(self.schema_validator().is_valid(plan))
            with self.assertRaises(PdfReconstructionPlanError):
                validate_reconstruction_plan(plan)

    def test_validator_rejects_parser_bbox_reference_tampering(self) -> None:
        plan = self.plan()
        unit = next(
            item
            for item in plan["units"]
            if item["origin"] == "opendataloader"
            and item["use_policy"] == "context_only"
            and len(item["reference_occurrence_ids"]) == 1
        )
        unit["bbox_pdf_user_space"] = [80.0, 80.0, 90.0, 90.0]
        with self.assertRaisesRegex(PdfReconstructionPlanError, "geometrically bind"):
            validate_reconstruction_plan(plan)

    def test_validator_rejects_cross_page_context_reference(self) -> None:
        plan = self.plan()
        plan["page_scope"].append(2)
        unit = next(
            item
            for item in plan["units"]
            if item["origin"] == "opendataloader"
            and item["use_policy"] == "context_only"
            and item["parent_unit_id"] is None
        )
        unit["page"] = 2
        with self.assertRaises(PdfReconstructionPlanError):
            validate_reconstruction_plan(plan)

    def test_validator_rejects_suppressed_competing_leaf_conflict(self) -> None:
        raw = structure_document()
        raw["kids"].append(  # type: ignore[union-attr]
            {"type": "paragraph", "id": 6, "page number": 1, "bounding box": [9, 70, 31, 82]}
        )
        plan = build_pdf_reconstruction_plan(
            source_pdf=self.source,
            native_capture=self.native(),
            structure_candidates=self.candidates(raw),
            render_manifest=self.render(),
            calibration_proof=self.proof,
            expected_calibration_proof_sha256=self.proof_sha256,
        )
        competing = [
            unit for unit in plan["units"] if "competing_leaf_context" in unit["reason_codes"]
        ]
        competing_ids = {unit["unit_id"] for unit in competing}
        plan["rejected_candidates"] = [
            row for row in plan["rejected_candidates"] if row["unit_id"] not in competing_ids
        ]
        plan["metrics"]["rejected_candidate_count"] -= len(competing)
        plan["metrics"]["candidate_alignment_counts"]["rejected"] -= len(competing)
        plan["metrics"]["candidate_alignment_counts"]["partial"] += len(competing)
        plan["metrics"]["candidate_use_policy_counts"]["diagnostic_only"] -= len(competing)
        plan["metrics"]["candidate_use_policy_counts"]["context_only"] += len(competing)
        ledger = {entry["occurrence_id"]: entry for entry in plan["native_occurrence_ledger"]}
        for unit in competing:
            unit["alignment_status"] = "partial"
            unit["use_policy"] = "context_only"
            unit["reason_codes"].remove("competing_leaf_context")
            for occurrence_id in unit["reference_occurrence_ids"]:
                ledger[occurrence_id]["context_unit_ids"].append(unit["unit_id"])
        plan["metrics"]["max_context_reference_count"] = max(
            len(entry["context_unit_ids"]) for entry in plan["native_occurrence_ledger"]
        )
        self.schema_validator().validate(plan)
        with self.assertRaises(PdfReconstructionPlanError):
            validate_reconstruction_plan(plan)

    def test_validator_rejects_boolean_candidate_metrics(self) -> None:
        plan = self.plan()
        plan["metrics"]["candidate_alignment_counts"]["accepted"] = False
        self.assertFalse(self.schema_validator().is_valid(plan))
        with self.assertRaises(PdfReconstructionPlanError):
            validate_reconstruction_plan(plan)

    def test_validator_and_schema_enforce_context_membership_cap(self) -> None:
        plan = self.plan()
        entry = next(item for item in plan["native_occurrence_ledger"] if item["primary_owner_unit_id"])
        existing = len(entry["context_unit_ids"])
        additions = MAX_CONTEXT_MEMBERSHIPS_PER_OCCURRENCE + 1 - existing
        for index in range(additions):
            candidate_id = "odl-" + digest(f"membership-cap-{index}")
            unit_id = reconstruction_unit_id("odl", candidate_id, plan["source_pdf_sha256"])
            plan["units"].append(
                {
                    "unit_id": unit_id,
                    "origin": "opendataloader",
                    "source_candidate_id": candidate_id,
                    "kind": "list",
                    "page": entry["page"],
                    "bbox_pdf_user_space": list(entry["bbox_pdf_user_space"]),
                    "alignment_status": "partial",
                    "use_policy": "context_only",
                    "primary_occurrence_ids": [],
                    "reference_occurrence_ids": [entry["occurrence_id"]],
                    "parent_unit_id": None,
                    "header_unit_ids": [],
                    "joiners": [],
                    "reason_codes": [
                        "calibration_kind_unverified",
                        "container_reference_only",
                        "legacy_odl_non_promotable",
                    ],
                }
            )
            entry["context_unit_ids"].append(unit_id)
        plan["metrics"]["structure_candidate_count"] += additions
        plan["metrics"]["candidate_alignment_counts"]["partial"] += additions
        plan["metrics"]["candidate_use_policy_counts"]["context_only"] += additions
        plan["metrics"]["max_context_reference_count"] = len(entry["context_unit_ids"])
        self.assertFalse(self.schema_validator().is_valid(plan))
        with self.assertRaises(PdfReconstructionPlanError):
            validate_reconstruction_plan(plan)

    def test_validator_and_schema_enforce_per_unit_reference_cap(self) -> None:
        plan = self.plan()
        new_native_units = []
        new_entries = []
        occurrence_ids = []
        for index in range(MAX_CONTEXT_REFERENCES_PER_UNIT + 1):
            source_item_index = 100_000 + index
            occurrence_id = f"occ:inspector:p1:t{source_item_index}"
            owner_id = reconstruction_unit_id("native", occurrence_id, plan["source_pdf_sha256"])
            occurrence_ids.append(occurrence_id)
            new_native_units.append(
                {
                    "unit_id": owner_id,
                    "origin": "native",
                    "source_candidate_id": None,
                    "kind": "native_text",
                    "page": 1,
                    "bbox_pdf_user_space": [1.0, 1.0, 2.0, 2.0],
                    "alignment_status": "accepted",
                    "use_policy": "evidence_atomic",
                    "primary_occurrence_ids": [occurrence_id],
                    "reference_occurrence_ids": [],
                    "parent_unit_id": None,
                    "header_unit_ids": [],
                    "joiners": [],
                    "reason_codes": [],
                }
            )
            new_entries.append(
                {
                    "occurrence_id": occurrence_id,
                    "page": 1,
                    "source_item_index": source_item_index,
                    "bbox_pdf_user_space": [1.0, 1.0, 2.0, 2.0],
                    "substantive_status": "substantive",
                    "primary_owner_unit_id": owner_id,
                    "context_unit_ids": [],
                    "disposition": "owned_atomic",
                    "reason_codes": [],
                }
            )
        first_odl = next(
            index for index, unit in enumerate(plan["units"]) if unit["origin"] == "opendataloader"
        )
        plan["units"][first_odl:first_odl] = new_native_units
        plan["native_occurrence_ledger"].extend(new_entries)
        candidate_id = "odl-" + digest("reference-cap")
        context_id = reconstruction_unit_id("odl", candidate_id, plan["source_pdf_sha256"])
        plan["units"].append(
            {
                "unit_id": context_id,
                "origin": "opendataloader",
                "source_candidate_id": candidate_id,
                "kind": "list",
                "page": 1,
                "bbox_pdf_user_space": [0.5, 0.5, 2.5, 2.5],
                "alignment_status": "partial",
                "use_policy": "context_only",
                "primary_occurrence_ids": [],
                "reference_occurrence_ids": occurrence_ids,
                "parent_unit_id": None,
                "header_unit_ids": [],
                "joiners": [],
                "reason_codes": [
                    "calibration_kind_unverified",
                    "container_reference_only",
                    "legacy_odl_non_promotable",
                ],
            }
        )
        for entry in new_entries:
            entry["context_unit_ids"].append(context_id)
        added = len(new_entries)
        plan["metrics"]["scoped_native_occurrence_count"] += added
        plan["metrics"]["substantive_native_occurrence_count"] += added
        plan["metrics"]["atomic_evidence_unit_count"] += added
        plan["metrics"]["structure_candidate_count"] += 1
        plan["metrics"]["candidate_alignment_counts"]["partial"] += 1
        plan["metrics"]["candidate_use_policy_counts"]["context_only"] += 1
        plan["metrics"]["max_context_reference_count"] = max(
            len(entry["context_unit_ids"]) for entry in plan["native_occurrence_ledger"]
        )
        self.assertFalse(self.schema_validator().is_valid(plan))
        with self.assertRaises(PdfReconstructionPlanError):
            validate_reconstruction_plan(plan)

    def test_121019_external_shadow_attestation_is_frozen_without_source_text(self) -> None:
        fingerprint = json.loads(ALIGNMENT_FINGERPRINT_PATH.read_text(encoding="utf-8"))
        encoded = json.dumps(
            fingerprint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(digest(encoded), ALIGNMENT_FINGERPRINT_CANONICAL_SHA256)
        self.assertEqual(fingerprint["schema_version"], "pdf_reconstruction_plan_fingerprint/v1")
        self.assertEqual(fingerprint["fixture_role"], "non_reproducible_review_attestation")
        self.assertEqual(fingerprint["attestation"]["replayed_on"], "2026-09-19")
        self.assertFalse(fingerprint["attestation"]["hermetic_replay_available_in_repository"])
        self.assertTrue(fingerprint["evaluation_only"])
        self.assertTrue(fingerprint["non_promotable"])
        self.assertFalse(fingerprint["source"]["source_pdf_in_repository"])
        projection = fingerprint["projection"]
        self.assertEqual(projection["standalone_validation_scope"], "internal_consistency_only")
        self.assertEqual(projection["native_text_status"], "native_text_available")
        self.assertEqual(projection["native_ownership_gate_status"], "passed")
        self.assertEqual(projection["scoped_native_occurrence_count"], 60)
        self.assertEqual(projection["atomic_evidence_unit_count"], 60)
        self.assertEqual(projection["unowned_substantive_occurrence_count"], 0)
        self.assertEqual(projection["duplicate_primary_owner_count"], 0)
        self.assertEqual(projection["structure_candidate_count"], 63)
        self.assertEqual(projection["candidate_alignment_counts"], {
            "accepted": 0, "partial": 59, "rejected": 4,
        })
        self.assertEqual(projection["rejected_candidate_traversal_orders"], [2, 20, 54, 55])
        self.assertEqual(projection["split_bullet_candidate_traversal_orders"], [44])
        self.assertEqual(projection["evidence_composite_unit_count"], 0)
        self.assertFalse(projection["semantic_text_copied_into_plan"])
        self.assertFalse(projection["table_or_list_promoted_to_evidence"])

    def test_114788_held_out_attestation_freezes_only_alignment_safety(self) -> None:
        fingerprint = json.loads(HELD_OUT_ALIGNMENT_FINGERPRINT_PATH.read_text(encoding="utf-8"))
        encoded = json.dumps(
            fingerprint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(digest(encoded), HELD_OUT_ALIGNMENT_FINGERPRINT_CANONICAL_SHA256)
        self.assertEqual(fingerprint["schema_version"], "pdf_reconstruction_plan_fingerprint/v1")
        self.assertEqual(fingerprint["fixture_role"], "non_reproducible_review_attestation")
        self.assertTrue(fingerprint["evaluation_only"])
        self.assertTrue(fingerprint["non_promotable"])
        self.assertFalse(fingerprint["source"]["source_pdf_in_repository"])
        self.assertFalse(fingerprint["attestation"]["hermetic_replay_available_in_repository"])
        self.assertEqual(fingerprint["attestation"]["artifact_bound_replay_validation"], "passed")

        quality_scope = fingerprint["quality_scope"]
        self.assertEqual(quality_scope["claim"], "alignment_safety_and_native_ownership_only")
        self.assertFalse(quality_scope["semantic_gold_bound_to_attestation"])
        self.assertFalse(quality_scope["structural_gold_available"])
        self.assertFalse(quality_scope["semantic_quality_approved"])
        self.assertFalse(quality_scope["structural_quality_approved"])

        legacy_observation = fingerprint["legacy_observation"]
        self.assertEqual(legacy_observation["old_top_level_object_count"], 71)
        self.assertEqual(legacy_observation["old_exact_binding_count"], 44)
        self.assertEqual(legacy_observation["recursive_table_cell_candidate_count"], 79)
        self.assertEqual(legacy_observation["recursive_typed_candidate_count"], 268)
        self.assertFalse(legacy_observation["old_embedded_odl_sha_is_authentication_input"])

        projection = fingerprint["projection"]
        self.assertEqual(projection["standalone_validation_scope"], "internal_consistency_only")
        self.assertEqual(projection["native_text_status"], "native_text_available")
        self.assertEqual(projection["native_ownership_gate_status"], "passed")
        self.assertEqual(projection["scoped_native_occurrence_count"], 230)
        self.assertEqual(projection["substantive_native_occurrence_count"], 218)
        self.assertEqual(projection["atomic_evidence_unit_count"], 218)
        self.assertEqual(projection["unowned_substantive_occurrence_count"], 0)
        self.assertEqual(projection["duplicate_primary_owner_count"], 0)
        self.assertEqual(projection["structure_candidate_count"], 268)
        self.assertEqual(
            projection["candidate_alignment_counts"],
            {"accepted": 0, "partial": 101, "rejected": 167},
        )
        self.assertEqual(
            projection["candidate_use_policy_counts"],
            {"context_only": 101, "diagnostic_only": 167},
        )
        self.assertEqual(
            projection["candidate_alignment_counts_by_page"],
            {
                "1": {"partial": 1, "rejected": 3},
                "2": {"partial": 2, "rejected": 3},
                "3": {"partial": 9, "rejected": 26},
                "4": {"partial": 14, "rejected": 26},
                "5": {"partial": 38, "rejected": 52},
                "6": {"partial": 35, "rejected": 54},
                "7": {"partial": 2, "rejected": 3},
            },
        )
        self.assertEqual(
            projection["rejection_reason_membership_counts"],
            {
                "no_native_occurrence": 94,
                "calibration_kind_unverified": 47,
                "multi_occurrence_leaf_unverified": 33,
                "no_candidate_bbox": 28,
                "unsupported_candidate_kind": 10,
                "cross_column_or_row_merge": 2,
            },
        )
        self.assertEqual(projection["cross_column_or_row_merge_traversal_orders"], [63, 88])
        self.assertEqual(projection["evidence_composite_unit_count"], 0)
        self.assertFalse(projection["semantic_text_copied_into_plan"])
        self.assertFalse(projection["table_or_list_promoted_to_evidence"])


if __name__ == "__main__":
    unittest.main()
