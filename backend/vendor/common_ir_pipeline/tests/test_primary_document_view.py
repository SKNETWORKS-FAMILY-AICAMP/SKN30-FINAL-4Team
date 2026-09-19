from __future__ import annotations

import ast
from collections import Counter
import copy
from dataclasses import replace
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import zlib

from jsonschema import Draft202012Validator, ValidationError

try:
    import test_reconstruction_plan as reconstruction_fixtures
except ModuleNotFoundError as error:
    if error.name != "test_reconstruction_plan":
        raise
    from backend.vendor.common_ir_pipeline.tests import (
        test_reconstruction_plan as reconstruction_fixtures,
    )
import common_ir_pipeline.pdf_fusion.primary_document_view as contract
from common_ir_pipeline.pdf_fusion.primary_document_view import (
    PdfPrimaryDocumentViewError,
    PrimaryDocumentViewFixture,
    build_pdf_primary_document_view,
    canonical_pdf_primary_document_view_json,
    load_pdf_primary_document_view_file,
    parse_pdf_primary_document_view_bytes,
    validate_pdf_primary_document_view,
    validate_pdf_primary_document_view_against_inputs,
)
from common_ir_pipeline.pdf_fusion.primary_paragraph_policy import BoundaryDecision
from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    REVIEWED_PROOF_CANONICAL_SHA256,
)
from common_ir_pipeline.pdf_fusion.reconstruction_plan import (
    build_pdf_reconstruction_plan,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    RenderedPage,
)


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def rgb_png(width: int, height: int) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        )
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def rejected_decision() -> BoundaryDecision:
    return BoundaryDecision(
        qualified=False,
        join_class=None,
        reason_codes=(),
        rejection_reason_codes=("fixture_rejection",),
    )


def qualified_decision() -> BoundaryDecision:
    return BoundaryDecision(
        qualified=True,
        join_class="intra_word_wrap",
        reason_codes=("native_source_adjacent", "owned_substantive_text"),
        rejection_reason_codes=(),
    )


class PrimaryDocumentViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = reconstruction_fixtures.PdfReconstructionPlanTests()
        self.fixture.setUp()
        self.native = self.fixture.native()
        self.candidates = self.fixture.candidates()
        self.render = self.render_with_real_files()
        self.plan = self.build_plan(self.candidates)
        self.arguments = {
            "source_pdf": self.fixture.source,
            "native_capture": self.native,
            "structure_candidates": self.candidates,
            "render_manifest": self.render,
            "render_artifact_root": self.fixture.root,
            "calibration_proof": self.fixture.proof,
            "expected_calibration_proof_sha256": self.fixture.proof_sha256,
            "reconstruction_plan": self.plan,
        }

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def render_with_real_files(self) -> PdfRenderManifest:
        base = self.fixture.render()
        encoded = rgb_png(200, 200)
        image_path = self.fixture.root / "rendered/page-0001.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(encoded)
        image_sha256 = digest(encoded)
        coordinate = replace(
            base.pages[0].coordinate_manifest,
            page_image_sha256=image_sha256,
        )
        page = RenderedPage(
            page=1,
            image_relative_path="rendered/page-0001.png",
            image_sha256=image_sha256,
            image_size_bytes=len(encoded),
            coordinate_manifest=coordinate,
            coordinate_manifest_sha256=coordinate.manifest_sha256(),
        )
        return PdfRenderManifest(
            source_pdf_relative_path="source.pdf",
            source_pdf_sha256=base.source_pdf_sha256,
            source_pdf_size_bytes=base.source_pdf_size_bytes,
            page_count=1,
            renderer=base.renderer,
            renderer_version=base.renderer_version,
            renderer_config_sha256=base.renderer_config_sha256,
            pages=(page,),
        )

    def build_plan(self, candidates: dict) -> dict:
        return build_pdf_reconstruction_plan(
            source_pdf=self.fixture.source,
            native_capture=self.native,
            structure_candidates=candidates,
            render_manifest=self.render,
            calibration_proof=self.fixture.proof,
            expected_calibration_proof_sha256=self.fixture.proof_sha256,
        )

    @staticmethod
    def schema_validator() -> Draft202012Validator:
        schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_primary_document_view_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def build_with_classifier(self, classifier) -> dict:
        with patch.object(
            contract, "classify_native_continuity_boundary", side_effect=classifier
        ):
            return build_pdf_primary_document_view(**self.arguments)

    def test_real_policy_build_is_deterministic_textless_and_schema_valid(self) -> None:
        first = build_pdf_primary_document_view(**self.arguments)
        second = build_pdf_primary_document_view(**self.arguments)
        self.assertEqual(
            canonical_pdf_primary_document_view_json(first),
            canonical_pdf_primary_document_view_json(second),
        )
        self.assertEqual(first, validate_pdf_primary_document_view(first))
        self.schema_validator().validate(first)

        authoritative = {
            entry["occurrence_id"]
            for entry in self.plan["native_occurrence_ledger"]
            if entry["substantive_status"] == "substantive"
            and entry["disposition"] == "owned_atomic"
        }
        placed = [item for leaf in first["leaves"] for item in leaf["occurrence_ids"]]
        self.assertEqual(Counter(placed), Counter(authoritative))
        self.assertEqual(first["metrics"]["missing_occurrence_count"], 0)
        self.assertEqual(first["metrics"]["duplicate_occurrence_count"], 0)
        self.assertEqual(first["relations"], [])
        encoded = canonical_pdf_primary_document_view_json(first).decode("utf-8")
        for semantic_text in ("제목", "왼쪽", "오른쪽", "본문"):
            self.assertNotIn(semantic_text, encoded)

    def test_one_qualified_edge_replaces_both_atomic_fallbacks(self) -> None:
        authoritative = [
            entry
            for entry in self.plan["native_occurrence_ledger"]
            if entry["substantive_status"] == "substantive"
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
            return qualified_decision() if indices == selected else rejected_decision()

        result = self.build_with_classifier(classifier)
        paragraphs = [leaf for leaf in result["leaves"] if leaf["kind"] == "paragraph"]
        self.assertEqual(len(paragraphs), 1)
        paragraph_members = set(paragraphs[0]["occurrence_ids"])
        atomic_members = {
            leaf["occurrence_ids"][0]
            for leaf in result["leaves"]
            if leaf["kind"] == "unclassified_text"
        }
        self.assertTrue(paragraph_members.isdisjoint(atomic_members))
        self.assertEqual(
            result["metrics"]["placed_occurrence_count"],
            result["metrics"]["authoritative_occurrence_count"],
        )

    def test_degree_conflict_suppresses_every_touching_edge(self) -> None:
        authoritative = [
            entry
            for entry in self.plan["native_occurrence_ledger"]
            if entry["substantive_status"] == "substantive"
        ]
        selected_pairs = {
            (
                authoritative[0]["source_item_index"],
                authoritative[1]["source_item_index"],
            ),
            (
                authoritative[1]["source_item_index"],
                authoritative[2]["source_item_index"],
            ),
        }

        def classifier(**kwargs):
            pair = (
                kwargs["left_ledger"]["source_item_index"],
                kwargs["right_ledger"]["source_item_index"],
            )
            return qualified_decision() if pair in selected_pairs else rejected_decision()

        result = self.build_with_classifier(classifier)
        self.assertEqual(result["metrics"]["paragraph_leaf_count"], 0)
        self.assertEqual(
            result["metrics"]["atomic_fallback_leaf_count"],
            result["metrics"]["authoritative_occurrence_count"],
        )

    def test_table_plan_context_is_passed_only_as_a_veto(self) -> None:
        raw = {
            "number of pages": 1,
            "kids": [
                {
                    "type": "table",
                    "id": 1,
                    "page number": 1,
                    "bounding box": [9, 70, 31, 82],
                    "kids": [],
                }
            ],
        }
        candidates = self.fixture.candidates(raw)
        plan = self.build_plan(candidates)
        arguments = {**self.arguments, "structure_candidates": candidates, "reconstruction_plan": plan}
        observed_vetoes: list[bool] = []

        def classifier(**kwargs):
            observed_vetoes.append(kwargs["table_context_veto"])
            return rejected_decision()

        with patch.object(
            contract, "classify_native_continuity_boundary", side_effect=classifier
        ):
            result = build_pdf_primary_document_view(**arguments)
        self.assertTrue(any(observed_vetoes))
        self.assertEqual(result["metrics"]["paragraph_leaf_count"], 0)

    def test_strict_mutations_reject_duplicate_extra_field_boundary_and_metrics(self) -> None:
        result = self.build_with_classifier(lambda **_: rejected_decision())

        duplicate = copy.deepcopy(result)
        duplicate["leaves"][1]["occurrence_ids"] = list(
            duplicate["leaves"][0]["occurrence_ids"]
        )
        with self.assertRaises(PdfPrimaryDocumentViewError):
            validate_pdf_primary_document_view(duplicate)

        extra = copy.deepcopy(result)
        extra["leaves"][0]["text"] = "forbidden"
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "unexpected keys"):
            validate_pdf_primary_document_view(extra)
        with self.assertRaises(ValidationError):
            self.schema_validator().validate(extra)

        metric = copy.deepcopy(result)
        metric["metrics"]["placed_occurrence_count"] -= 1
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "metrics"):
            validate_pdf_primary_document_view(metric)

        invalid_unicode = copy.deepcopy(result)
        invalid_unicode["notice_id"] = "\ud800"
        with self.assertRaises(PdfPrimaryDocumentViewError):
            validate_pdf_primary_document_view(invalid_unicode)

        merged = self.build_with_classifier(
            lambda **kwargs: qualified_decision()
            if kwargs["left_ledger"]["source_item_index"] == 0
            else rejected_decision()
        )
        paragraph = next(leaf for leaf in merged["leaves"] if leaf["kind"] == "paragraph")
        paragraph["boundaries"][0]["right_occurrence_id"] = paragraph["occurrence_ids"][0]
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "boundary"):
            validate_pdf_primary_document_view(merged)

    def test_exact_replay_returns_receipt_and_rejects_accidental_tampering(self) -> None:
        result = build_pdf_primary_document_view(**self.arguments)
        replayed = validate_pdf_primary_document_view_against_inputs(
            result, **self.arguments
        )
        self.assertTrue(replayed.is_replay_receipt)
        self.assertEqual(replayed.to_dict(), result)
        self.assertEqual(replayed.payload["notice_id"], result["notice_id"])

        fixture = PrimaryDocumentViewFixture.from_dict(result)
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "replay receipt"):
            contract.ReplayedPrimaryDocumentView(
                fixture,
                _construction_token=object(),
                replayed_canonical_sha256=fixture.canonical_sha256,
            )
        with self.assertRaises((TypeError, ValueError)):
            replace(
                replayed,
                _replayed_canonical_sha256="0" * 64,
            )

        tampered = copy.deepcopy(result)
        tampered["source"]["reconstruction_plan"]["canonical_sha256"] = "0" * 64
        validate_pdf_primary_document_view(tampered)
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "deterministic replay"):
            validate_pdf_primary_document_view_against_inputs(
                tampered, **self.arguments
            )

    def test_canonical_parse_and_safe_file_load(self) -> None:
        result = build_pdf_primary_document_view(**self.arguments)
        encoded = canonical_pdf_primary_document_view_json(result)
        parsed = parse_pdf_primary_document_view_bytes(encoded)
        self.assertEqual(parsed.to_dict(), result)

        path = self.fixture.root / "primary-view.json"
        path.write_bytes(encoded)
        self.assertEqual(load_pdf_primary_document_view_file(path).to_dict(), result)
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "not canonical"):
            parse_pdf_primary_document_view_bytes(encoded + b"\n")

        link = self.fixture.root / "primary-view-link.json"
        link.symlink_to(path)
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "non-symlink"):
            load_pdf_primary_document_view_file(link)

    def test_public_parsers_normalize_pathological_json_failures(self) -> None:
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "valid UTF-8 JSON"):
            parse_pdf_primary_document_view_bytes(
                b'{"oversized_integer":' + b"1" * 5_000 + b"}"
            )
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "keys must be strings"):
            validate_pdf_primary_document_view({1: None})  # type: ignore[dict-item]

    def test_direct_mapping_validation_checks_work_limits_first(self) -> None:
        result = build_pdf_primary_document_view(**self.arguments)
        with patch.object(contract, "MAX_JSON_NODES", 1):
            with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "node cap"):
                validate_pdf_primary_document_view(result)

    def test_render_file_replay_precedes_candidate_build(self) -> None:
        image_path = self.fixture.root / "rendered/page-0001.png"
        image_path.write_bytes(image_path.read_bytes() + b"tamper")
        with self.assertRaisesRegex(PdfPrimaryDocumentViewError, "render manifest"):
            build_pdf_primary_document_view(**self.arguments)

    def test_builder_import_graph_has_no_gold_or_environment_channel(self) -> None:
        module_path = Path(contract.__file__)
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertFalse(
            any("primary_structure_gold" in name for name in imported_modules)
        )
        self.assertNotIn("primary_structure_gold", source)
        self.assertFalse(
            any(
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in {"environ", "getenv"}
                for node in ast.walk(tree)
            )
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"glob", "rglob"}
                for node in ast.walk(tree)
            )
        )

    def test_actual_114788_replay_when_artifact_environment_is_configured(self) -> None:
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
        if not all(configured.values()):
            self.skipTest("actual 114788 artifact environment is not configured")

        def mapping(name: str) -> dict:
            path = Path(configured[name])
            return json.loads(path.read_text(encoding="utf-8"))

        actual_arguments = {
            "source_pdf": Path(configured["source_pdf"]),
            "native_capture": mapping("native_capture"),
            "structure_candidates": mapping("structure_candidates"),
            "render_manifest": mapping("render_manifest"),
            "render_artifact_root": Path(configured["render_artifact_root"]),
            "calibration_proof": mapping("calibration_proof"),
            "expected_calibration_proof_sha256": REVIEWED_PROOF_CANONICAL_SHA256,
            "reconstruction_plan": mapping("reconstruction_plan"),
        }
        first = build_pdf_primary_document_view(**actual_arguments)
        second = build_pdf_primary_document_view(**actual_arguments)
        self.assertEqual(
            canonical_pdf_primary_document_view_json(first),
            canonical_pdf_primary_document_view_json(second),
        )
        metrics = first["metrics"]
        self.assertGreater(metrics["authoritative_occurrence_count"], 0)
        self.assertEqual(
            metrics["placed_occurrence_count"],
            metrics["authoritative_occurrence_count"],
        )
        self.assertEqual(metrics["missing_occurrence_count"], 0)
        self.assertEqual(metrics["duplicate_occurrence_count"], 0)
        self.assertGreater(metrics["paragraph_leaf_count"], 0)
        title_occurrence = "occ:inspector:p3:t17"
        purpose_occurrences = [
            "occ:inspector:p3:t19",
            "occ:inspector:p3:t20",
        ]
        title_leaf = next(
            leaf
            for leaf in first["leaves"]
            if title_occurrence in leaf["occurrence_ids"]
        )
        purpose_leaf = next(
            leaf
            for leaf in first["leaves"]
            if purpose_occurrences[0] in leaf["occurrence_ids"]
        )
        self.assertEqual(title_leaf["kind"], "unclassified_text")
        self.assertEqual(title_leaf["occurrence_ids"], [title_occurrence])
        self.assertEqual(purpose_leaf["kind"], "paragraph")
        self.assertEqual(purpose_leaf["occurrence_ids"], purpose_occurrences)
        self.assertEqual(
            purpose_leaf["boundaries"],
            [
                {
                    "left_occurrence_id": purpose_occurrences[0],
                    "right_occurrence_id": purpose_occurrences[1],
                    "join_class": "intra_word_wrap",
                }
            ],
        )
        self.assertNotEqual(title_leaf["leaf_id"], purpose_leaf["leaf_id"])
        self.assertEqual(first["relations"], [])
        replayed = validate_pdf_primary_document_view_against_inputs(
            first, **actual_arguments
        )
        self.assertTrue(replayed.is_replay_receipt)
        self.assertEqual(
            replayed.canonical_json(),
            canonical_pdf_primary_document_view_json(first),
        )


if __name__ == "__main__":
    unittest.main()
