from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common_ir_pipeline.adapters.markdown_fixture import (
    assert_fixture_integrity,
    build_document,
    validate_input_manifest,
)
from common_ir_pipeline.adapters.pdf_native import main as pdf_native_main
from common_ir_pipeline.schema import validation_errors
from common_ir_pipeline.shared import new_document_shell
from common_ir_pipeline.workers.pdf_ocr_layout import build_sidecar, geometry_regions


class PortablePackageTests(unittest.TestCase):
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
            source_pdf.write_bytes(b"minimal pdf fixture")
            native_path = root / "native.json"
            native_path.write_text(json.dumps({
                "method": "pdf_inspector",
                "version": "test",
                "process_result": {"page_count": 1},
                "text_items": [{"page": 1, "text": "지원금 100만원", "x": 0, "y": 10, "width": 50, "height": 8, "font_size": 10}],
            }), encoding="utf-8")
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
            source_pdf.write_bytes(b"minimal pdf fixture")
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
            native_path.write_text(json.dumps({
                "method": "pdf_inspector", "version": "test", "process_result": {"page_count": 1},
                "text_items": [{"page": 1, "text": "지원금 100만원", "x": 0, "y": 10, "width": 50, "height": 8, "font_size": 10}],
            }), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
