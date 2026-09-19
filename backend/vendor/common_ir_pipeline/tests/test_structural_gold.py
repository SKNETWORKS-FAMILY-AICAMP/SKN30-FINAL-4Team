from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.structural_gold import (
    EXPECTED_CONFIDENCE,
    EXPECTED_GROUP_COUNTS,
    EXPECTED_RECORDED_RENDERED_IMAGE_SHA256,
    EXPECTED_RENDER_MANIFEST_SHA256,
    EXPECTED_RENDER_STAGE_FINGERPRINT,
    EXPECTED_RENDER_STAGE_SHA256,
    EXPECTED_RENDERED_IMAGE_DIGEST_STATUS,
    EXPECTED_SOURCE_SHA256,
    StructuralGoldError,
    StructuralGoldFixture,
    load_structural_gold_file,
    parse_structural_gold_bytes,
)


FIXTURE = Path(__file__).resolve().parents[3] / "baselines/pdf_reconstruction/structural_gold_121019_p5.v1.json"


class StructuralGoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = FIXTURE.read_bytes()
        self.payload = json.loads(self.raw)

    def parsed(self, payload: dict[str, object] | None = None) -> StructuralGoldFixture:
        raw = self.raw if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return parse_structural_gold_bytes(raw)

    def test_frozen_page_five_fixture_and_schema_are_valid(self) -> None:
        fixture = load_structural_gold_file(FIXTURE)
        self.assertEqual(fixture.to_dict()["source"]["source_pdf_sha256"], EXPECTED_SOURCE_SHA256)
        self.assertEqual(fixture.to_dict()["source"]["physical_page"], 5)
        self.assertEqual(fixture.to_dict()["source"]["rendered_image"], {
            "pixel_width": 1653,
            "pixel_height": 2337,
            "recorded_rendered_image_sha256": EXPECTED_RECORDED_RENDERED_IMAGE_SHA256,
            "rendered_image_digest_status": EXPECTED_RENDERED_IMAGE_DIGEST_STATUS,
            "render_manifest_sha256": EXPECTED_RENDER_MANIFEST_SHA256,
            "render_stage_sha256": EXPECTED_RENDER_STAGE_SHA256,
            "render_stage_fingerprint": EXPECTED_RENDER_STAGE_FINGERPRINT,
        })
        self.assertEqual(fixture.to_dict()["title"], {"text": "[붙임] 사업재편 인센티브", "relation": "above_table"})
        self.assertEqual(fixture.to_dict()["table"]["bbox_px"], [159, 245, 1496, 1985])
        self.assertEqual(fixture.to_dict()["table"]["physical_row_count"], 43)
        self.assertEqual([(item["category"], item["child_count"]) for item in fixture.to_dict()["table"]["groups"]], list(EXPECTED_GROUP_COUNTS))
        self.assertEqual(fixture.to_dict()["adjudication"], {"method": "image_first_assisted_review", "review_status": "human_confirmed", "human_confirmation_required": False, "confidence": EXPECTED_CONFIDENCE, "parser_outputs_are_truth": False})
        self.assertEqual(fixture.to_dict()["below_table_notes"], {"relation": "below_table", "semantic_role": "explanatory_content", "note_count": 3})
        self.assertEqual(fixture.to_dict()["footer"], {"text": "- 5 -", "semantic_role": "nonsemantic"})
        self.assertEqual(fixture.to_dict()["native_term_assertion"], {"accepted_native_term": "익금불산입", "rejected_ocr_term": "의금불산입"})
        self.assertEqual(fixture.to_dict()["hard_negatives"]["reject_surya_rowspans"], [10, 8])
        self.assertEqual(fixture.to_dict()["hard_negatives"]["table_list_conflict_production_evidence"], "reject_parser_only_mixed_type_promotion")
        self.assertNotIn(b'"children"', fixture.canonical_json())

        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath("schemas/pdf_structural_gold_v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture.to_dict())
        self.assertEqual(fixture.canonical_json(), self.parsed().canonical_json())

    def test_mutations_fail_closed_for_source_structure_and_parser_truth(self) -> None:
        cases: list[tuple[str, dict[str, object], str]] = []
        bad = deepcopy(self.payload)
        bad["source"]["source_pdf_sha256"] = "0" * 64  # type: ignore[index]
        cases.append(("source hash", bad, "frozen PDF"))
        bad = deepcopy(self.payload)
        bad["source"]["rendered_image"]["recorded_rendered_image_sha256"] = "0" * 64  # type: ignore[index]
        cases.append(("recorded raster digest", bad, "page-5 digest"))
        bad = deepcopy(self.payload)
        bad["source"]["rendered_image"]["rendered_image_digest_status"] = "locally_rehashed"  # type: ignore[index]
        cases.append(("raster digest provenance", bad, "not locally rehashed"))
        bad = deepcopy(self.payload)
        bad["source"]["rendered_image"]["render_manifest_sha256"] = "0" * 64  # type: ignore[index]
        cases.append(("render manifest digest", bad, "frozen manifest"))
        bad = deepcopy(self.payload)
        bad["source"]["rendered_image"]["render_stage_sha256"] = "0" * 64  # type: ignore[index]
        cases.append(("render stage digest", bad, "stage record"))
        bad = deepcopy(self.payload)
        bad["adjudication"]["confidence"] = 0.81  # type: ignore[index]
        cases.append(("fixture confidence", bad, "exactly 0.8"))
        bad = deepcopy(self.payload)
        bad["adjudication"]["review_status"] = "provisional"  # type: ignore[index]
        bad["adjudication"]["human_confirmation_required"] = True  # type: ignore[index]
        cases.append(("human confirmation rollback", bad, "completed human confirmation"))
        bad = deepcopy(self.payload)
        bad["table"]["groups"][3]["child_count"] = 11  # type: ignore[index]
        cases.append(("child count", bad, "child-row counts"))
        bad = deepcopy(self.payload)
        bad["table"]["physical_row_count"] = 42  # type: ignore[index]
        cases.append(("physical rows", bad, "physical_row_count"))
        bad = deepcopy(self.payload)
        bad["adjudication"]["parser_outputs_are_truth"] = True  # type: ignore[index]
        cases.append(("parser truth", bad, "must not be made truth"))
        bad = deepcopy(self.payload)
        bad["hard_negatives"]["reject_ocr_typo"] = "익금불산입"  # type: ignore[index]
        cases.append(("ocr typo", bad, "의금불산입"))
        bad = deepcopy(self.payload)
        bad["hard_negatives"]["table_list_conflict_production_evidence"] = "promote_table"  # type: ignore[index]
        cases.append(("table/list conflict", bad, "mixed-type"))
        bad = deepcopy(self.payload)
        bad["footer"]["text"] = "attached to table"  # type: ignore[index]
        cases.append(("footer attachment", bad, "footer"))
        for name, payload, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(StructuralGoldError, message):
                self.parsed(payload)

    def test_rejects_exact_key_violations_and_hard_negative_rewrites(self) -> None:
        bad = deepcopy(self.payload)
        bad["table"]["checked"] = True  # type: ignore[index]
        with self.assertRaisesRegex(StructuralGoldError, "unexpected keys"):
            self.parsed(bad)
        bad = deepcopy(self.payload)
        bad["hard_negatives"]["reject_surya_rowspans"] = [6, 6]  # type: ignore[index]
        with self.assertRaisesRegex(StructuralGoldError, "rowspans"):
            self.parsed(bad)
        bad = deepcopy(self.payload)
        bad["table"]["groups"] = [{"category": f"category-{number}", "child_count": 1} for number in range(42)]  # type: ignore[index]
        with self.assertRaisesRegex(StructuralGoldError, "10 category"):
            self.parsed(bad)
        bad = deepcopy(self.payload)
        bad["table"]["groups"][7]["child_count"] = 2  # type: ignore[index]
        with self.assertRaisesRegex(StructuralGoldError, "child-row counts"):
            self.parsed(bad)

    def test_duplicate_json_keys_and_nonsemantic_fields_cannot_be_smuggled_in(self) -> None:
        duplicate = self.raw.replace(b'"schema_version": "pdf_structural_gold/v1",', b'"schema_version": "pdf_structural_gold/v1", "schema_version": "pdf_structural_gold/v1",', 1)
        with self.assertRaisesRegex(StructuralGoldError, "duplicate key"):
            parse_structural_gold_bytes(duplicate)
        bad = deepcopy(self.payload)
        bad["below_table_notes"]["attached_to_table"] = True  # type: ignore[index]
        with self.assertRaisesRegex(StructuralGoldError, "unexpected keys"):
            self.parsed(bad)


if __name__ == "__main__":
    unittest.main()
