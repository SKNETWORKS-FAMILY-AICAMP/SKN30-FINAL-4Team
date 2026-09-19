from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from common_ir_pipeline.adapters.markdown_fixture import (
    assert_fixture_integrity,
    build_document,
    validate_input_manifest,
)
from common_ir_pipeline.adapters.pdf_native import main as pdf_native_main
from common_ir_pipeline.pdf_fusion.native_capture import (
    CAPTURE_SCHEMA_VERSION,
    canonical_json_bytes,
)
from common_ir_pipeline.pdf_fusion.coordinate_manifest import AffineTransform, PdfCoordinateManifest, build_sidecar_binding
from common_ir_pipeline.pdf_fusion.render_manifest import PdfRenderManifest, RenderedPage
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact,
    SuryaLayoutPage,
    SuryaLayoutRegion,
    SuryaProducerIdentity,
)
from common_ir_pipeline.schema import validation_errors
from common_ir_pipeline.shared import new_document_shell
from common_ir_pipeline.workers.pdf_ocr_layout import build_sidecar, geometry_regions


def _bound_native_capture(source_pdf: Path, *, notice_id: str, text: str) -> dict:
    return {
        "capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "notice_id": notice_id,
        "source_kind": "pdf",
        "artifact_role": "production",
        "method": "pdf_inspector",
        "version": "1.17.0",
        "extraction_scope": "full_document",
        "source_path": source_pdf.name,
        "source_sha256": hashlib.sha256(source_pdf.read_bytes()).hexdigest(),
        "source_size_bytes": source_pdf.stat().st_size,
        "process_result": {
            "pdf_type": "text_based",
            "markdown": text,
            "page_count": 1,
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "title": None,
            "confidence": 1.0,
            "is_complex_layout": False,
            "pages_with_tables": [],
            "pages_with_columns": [],
            "has_encoding_issues": False,
        },
        "pages_markdown_result": {
            "pages": [{"page": 0, "markdown": text, "needs_ocr": False, "ocr_reason": None}],
            "pages_with_tables": [],
            "pages_with_columns": [],
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "is_complex": False,
        },
        "text_items": [{
            "page": 1,
            "text": text,
            "x": 0.0,
            "y": 10.0,
            "width": 50.0,
            "height": 8.0,
            "font": "TestFont",
            "font_tag": "F1",
            "font_size": 10.0,
            "is_bold": False,
            "is_italic": False,
            "is_underline": False,
            "is_strikeout": False,
            "item_type": "text",
            "mcid": None,
        }],
        "structure_elements": [],
    }


class PortablePackageTests(unittest.TestCase):
    def _surya_artifacts_for_source(self, source: Path) -> tuple[PdfRenderManifest, SuryaLayoutArtifact]:
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        image_hash = hashlib.sha256(b"rendered page").hexdigest()
        forward = AffineTransform.from_sequence([2, 0, 0, -2, 0, 400])
        coordinate = PdfCoordinateManifest(
            source_sha256=source_hash, page=1, page_count=1,
            media_box=(0, 0, 100, 200), crop_box=(0, 0, 100, 200), rotation=0, user_unit=1.0,
            canonical_width_pt=100.0, canonical_height_pt=200.0, render_scale_px_per_point=2.0,
            rendered_width_px=200, rendered_height_px=400,
            pdf_origin="bottom_left", pdf_x_axis="right", pdf_y_axis="up",
            pixel_origin="top_left", pixel_x_axis="right", pixel_y_axis="down",
            user_to_pixel=forward, pixel_to_user=forward.inverse(), renderer="pdfium",
            renderer_version="153.0", renderer_config_sha256=hashlib.sha256(b"render-config").hexdigest(),
            page_image_sha256=image_hash,
        )
        manifest = PdfRenderManifest(
            source_pdf_relative_path="source.pdf", source_pdf_sha256=source_hash,
            source_pdf_size_bytes=source.stat().st_size, page_count=1, renderer="pdfium",
            renderer_version="153.0", renderer_config_sha256=coordinate.renderer_config_sha256,
            pages=(RenderedPage(
                page=1, image_relative_path="rendered/page-0001.png", image_sha256=image_hash,
                image_size_bytes=123, coordinate_manifest=coordinate,
                coordinate_manifest_sha256=coordinate.manifest_sha256(),
            ),),
        )
        producer = SuryaProducerIdentity(
            engine_id="surya", engine_version="0.15.0", model_id="surya-layout", model_revision="r1",
            model_weights_sha256=hashlib.sha256(b"model").hexdigest(), pipeline_revision="surya-layout-v1",
            config_sha256=hashlib.sha256(b"config").hexdigest(),
            worker_image_digest="sha256:" + hashlib.sha256(b"image").hexdigest(),
        )
        region = SuryaLayoutRegion(
            region_id="p0001-table-0001", label="table", bbox_px=(20, 20, 100, 80),
            polygon_px=((20, 20), (100, 20), (100, 80), (20, 80)), confidence=0.9, reading_order=0,
        )
        artifact = SuryaLayoutArtifact(
            logical_compute_key=hashlib.sha256(b"logical").hexdigest(), source_sha256=source_hash,
            page_count=1, render_manifest_schema_version=manifest.schema_version,
            render_manifest_sha256=manifest.manifest_sha256(), producer=producer, requested_pages=(1,),
            pages=(SuryaLayoutPage(
                page=1, sidecar_binding=build_sidecar_binding(coordinate), pixel_width=200,
                pixel_height=400, rendered_page_px=(0, 0, 200, 400), regions=(region,),
            ),),
        )
        return manifest, artifact

    def test_wheel_contains_pdf_fusion_json_schemas(self) -> None:
        """Package-data config is exercised from a real wheel, not source tree."""
        if os.environ.get("COMMON_IR_VERIFY_WHEEL") != "1":
            self.skipTest("set COMMON_IR_VERIFY_WHEEL=1 in a build-capable CI job")
        package_root = Path(__file__).resolve().parents[1]
        uv = shutil.which("uv")
        if uv is None:
            self.skipTest("uv is required for the reproducible wheel-content check")
        with tempfile.TemporaryDirectory() as directory:
            wheel_dir = Path(directory) / "wheel"
            completed = subprocess.run(
                [uv, "build", "--wheel", "--out-dir", str(wheel_dir)], cwd=package_root,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            wheels = list(wheel_dir.glob("common_ir_pipeline-*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = set(wheel.namelist())
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_coordinate_manifest_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_render_manifest_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/surya_layout_artifact_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_fragment_groups_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_context_groups_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_structure_gold_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_document_view_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_heading_relations_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_heading_relation_gold_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_heading_relation_evaluation_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_view_evaluation_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_table_grid_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_table_grid_gold_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_table_grid_evaluation_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_table_continuation_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_table_continuation_gold_v1.schema.json", names)
            self.assertIn("common_ir_pipeline/pdf_fusion/schemas/pdf_primary_table_continuation_evaluation_v1.schema.json", names)

    def test_document_shell_has_required_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            source.write_text("source", encoding="utf-8")
            document = new_document_shell(
                "fixture:one", "markdown_fixture", source, None, [], "test",
                parser="test_parser", parser_version="1",
            )
            self.assertEqual(validation_errors(document), [])

    def test_markdown_fixture_preserves_heading_list_and_table_spans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.input.md"
            source.write_text("# 제목\n\n- 항목\n\n| 구분 | 값 |\n| --- | --- |\n| 지원금 | 10 |\n", encoding="utf-8")
            document = build_document(source, "FIXTURE-01")
            self.assertEqual(validation_errors(document), [])
            self.assertEqual(assert_fixture_integrity(document, source), [])
            self.assertEqual([block["kind"] for block in document["blocks"]], ["heading", "paragraph", "table"])
            table = document["blocks"][2]
            self.assertEqual([(cell["row_index"], cell["col_index"]) for cell in table["cells"]], [(0, 0), (0, 1), (1, 0), (1, 1)])
            self.assertNotIn("reference_notice_id", json.dumps(document, ensure_ascii=False))

    def test_markdown_fixture_cli_runs_shipped_manifest(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        fixture_root = package_root / "fixtures"
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory) / "out"
            environment = {**os.environ, "PYTHONPATH": str(package_root / "src")}
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "common_ir_pipeline.adapters.markdown_fixture",
                    "--input-dir", str(fixture_root),
                    "--output-root", str(output_root),
                ],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            document_path = output_root / "common_ir" / "PREREVIEW-EXAMPLE-01.common_ir_v1.json"
            self.assertTrue(document_path.is_file())
            self.assertEqual(validation_errors(json.loads(document_path.read_text(encoding="utf-8"))), [])

    def test_markdown_manifest_rejects_duplicates_and_non_input_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_dir = Path(directory)
            (input_dir / "ok.input.md").write_text("# ok\n", encoding="utf-8")
            valid = {"documents": [{"doc_id": "one", "input_file": "ok.input.md"}]}
            self.assertEqual(validate_input_manifest(input_dir, valid), valid["documents"])
            invalid_manifests = [
                {"documents": [{"doc_id": "one", "input_file": "ok.input.md"}, {"doc_id": "two", "input_file": "ok.input.md"}]},
                {"documents": [{"doc_id": "one", "input_file": "../gold.md"}]},
                {"documents": [{"doc_id": "one", "input_file": "nested/ok.input.md"}]},
                {"documents": [{"doc_id": "one", "input_file": "gold_change_notes.md"}]},
            ]
            for manifest in invalid_manifests:
                with self.assertRaises(ValueError):
                    validate_input_manifest(input_dir, manifest)

    def test_pdf_native_cli_is_valid_without_layout_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "notice.pdf"
            source_pdf.write_bytes(b"%PDF-1.7\nminimal pdf fixture")
            native_path = root / "native.json"
            native_path.write_bytes(canonical_json_bytes(_bound_native_capture(
                source_pdf, notice_id="TEST-PDF", text="지원금 100만원",
            )))
            output = root / "notice.common_ir_v1.json"
            with patch.object(sys, "argv", [
                "common-ir-pdf-native", "--notice-id", "TEST-PDF", "--native", str(native_path),
                "--source-path", str(source_pdf),
                "--source-sha256", hashlib.sha256(source_pdf.read_bytes()).hexdigest(),
                "--output", str(output),
            ]):
                pdf_native_main()
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(validation_errors(document), [])
            self.assertEqual(document["document"]["pdf_semantic_eligibility"], "eligible_native_text")
            self.assertEqual(document["blocks"][0]["text"], "지원금 100만원")

    def test_ocr_layout_sidecar_discards_recognized_text(self) -> None:
        """The optional worker may receive OCR strings but never persists them."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "notice.pdf"
            source.write_bytes(b"synthetic PDF input")
            secret = "OCR 문자열은 Common IR semantic text가 아니어야 한다"
            paddle = geometry_regions("paddle", [
                ([[0, 0], [10, 0], [10, 5], [0, 5]], (secret, 0.91)),
            ])
            easy = geometry_regions("easy", [
                ([[2, 2], [12, 2], [12, 8], [2, 8]], secret, 0.82),
            ])
            sidecar = build_sidecar(source, 1, 1.5, [{
                "page": 1,
                "image_size": {"width": 100, "height": 100},
                "render_seconds": 0.01,
                "total_seconds": 0.02,
                "engines": {"paddle": {"seconds": 0.01, "region_count": 1}, "easy": {"seconds": 0.01, "region_count": 1}},
                "regions": [{"engine": "paddle", **paddle[0]}, {"engine": "easy", **easy[0]}],
            }])
            rendered = json.dumps(sidecar, ensure_ascii=False)
            self.assertNotIn(secret, rendered)
            self.assertNotIn('"text"', rendered)
            self.assertFalse(sidecar["policy"]["ocr_text_persisted"])
            self.assertEqual(sidecar["pages"][0]["regions"][0]["bbox"], [0.0, 0.0, 10.0, 5.0])

    def test_pdf_native_carries_bound_textless_ocr_layout_geometry(self) -> None:
        """A sidecar contributes layout-only provenance, never OCR text/cells/relations."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "notice.pdf"
            source_pdf.write_bytes(b"%PDF-1.7\nminimal pdf fixture")
            secret = "절대 Common IR에 들어가면 안 되는 OCR 문자열"
            regions = geometry_regions("easy", [
                ([[1, 2], [11, 2], [11, 9], [1, 9]], secret, 0.8),
            ])
            sidecar = build_sidecar(source_pdf, 1, 1.5, [{
                "page": 1, "image_size": {"width": 100, "height": 100},
                "render_seconds": 0.01, "total_seconds": 0.02,
                "engines": {"easy": {"seconds": 0.01, "region_count": 1}},
                "regions": [{"engine": "easy", **regions[0]}],
            }])
            sidecar_path = root / "layout.json"
            sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
            native_path = root / "native.json"
            native_path.write_bytes(canonical_json_bytes(_bound_native_capture(
                source_pdf, notice_id="TEST-PDF", text="지원금 100만원",
            )))
            output = root / "notice.common_ir_v1.json"
            with patch.object(sys, "argv", [
                "common-ir-pdf-native", "--notice-id", "TEST-PDF", "--native", str(native_path),
                "--source-path", str(source_pdf), "--ocr-layout-diagnostic", str(sidecar_path), "--output", str(output),
            ]):
                pdf_native_main()
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(validation_errors(document), [])
            self.assertNotIn(secret, json.dumps(document, ensure_ascii=False))
            layout = next(block for block in document["blocks"] if block["kind"] == "layout_candidate")
            self.assertEqual(layout["text"], "")
            self.assertEqual(layout["occurrences"][0]["role"], "layout_region")
            self.assertNotIn("text", layout["occurrences"][0])
            self.assertNotIn("cells", layout)
            self.assertEqual(document["relations"], [])

    def test_pdf_native_carries_only_bound_textless_surya_layout_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "notice.pdf"
            source_pdf.write_bytes(b"%PDF-1.7\nminimal pdf fixture")
            manifest, artifact = self._surya_artifacts_for_source(source_pdf)
            manifest_path = root / "render_manifest.json"
            manifest_path.write_bytes(manifest.canonical_json())
            artifact_path = root / "surya_layout_artifact.json"
            artifact_path.write_bytes(artifact.canonical_json())
            native_path = root / "native.json"
            native_path.write_bytes(canonical_json_bytes(_bound_native_capture(
                source_pdf, notice_id="TEST-PDF", text="지원금 100만원",
            )))
            output = root / "notice.common_ir_v1.json"
            with patch.object(sys, "argv", [
                "common-ir-pdf-native", "--notice-id", "TEST-PDF", "--native", str(native_path),
                "--source-path", str(source_pdf), "--render-manifest", str(manifest_path),
                "--surya-layout-artifact", str(artifact_path), "--output", str(output),
            ]):
                pdf_native_main()
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(validation_errors(document), [])
            layout = next(block for block in document["blocks"] if block["block_id"] == "surya-layout:p0001-table-0001")
            self.assertEqual(layout["kind"], "layout_candidate")
            self.assertEqual(layout["text"], "")
            self.assertNotIn("cells", layout)
            self.assertNotIn("text", layout["occurrences"][0])
            self.assertEqual(document["relations"], [])
            self.assertEqual(layout["provenance"]["coordinate_space"], "pdf_user_space")

            tampered = artifact.to_dict()
            tampered["source_sha256"] = "0" * 64
            artifact_path.write_text(
                json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8",
            )
            with patch.object(sys, "argv", [
                "common-ir-pdf-native", "--notice-id", "TEST-PDF", "--native", str(native_path),
                "--source-path", str(source_pdf), "--render-manifest", str(manifest_path),
                "--surya-layout-artifact", str(artifact_path), "--output", str(output),
            ]), self.assertRaises(SystemExit):
                pdf_native_main()


if __name__ == "__main__":
    unittest.main()
