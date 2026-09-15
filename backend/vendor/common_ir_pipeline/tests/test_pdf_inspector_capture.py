from __future__ import annotations

import copy
from importlib.metadata import PackageNotFoundError
import json
import math
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from common_ir_pipeline.adapters.pdf_native import main as pdf_native_main
from common_ir_pipeline.pdf_fusion.native_capture import (
    DEFAULT_LIMITS,
    NativeCaptureError,
    NativeCaptureLimits,
    canonical_json_bytes,
    capture_pdf_to_native,
    load_native_capture_file,
    validate_native_capture,
)
from common_ir_pipeline.workers.pdf_inspector_capture import write_capture_exclusive


class FakePdfInspector:
    def __init__(self, *, page_count: int = 2, negative_height: bool = False) -> None:
        self.page_count = page_count
        self.negative_height = negative_height

    def process_pdf_bytes(self, data: bytes) -> dict:
        del data
        return {
            "pdf_type": "text_based",
            "markdown": "첫 페이지\n\n둘째 페이지",
            "page_count": self.page_count,
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "title": "테스트 공고",
            "confidence": 1.0,
            "is_complex_layout": False,
            "pages_with_tables": [],
            "pages_with_columns": [],
            "has_encoding_issues": False,
        }

    def extract_pages_markdown_bytes(self, data: bytes) -> dict:
        del data
        return {
            "pages": [
                {"page": index, "markdown": f"page {index + 1}", "needs_ocr": False, "ocr_reason": None}
                for index in range(self.page_count)
            ],
            "pages_with_tables": [],
            "pages_with_columns": [],
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "is_complex": False,
        }

    def extract_text_with_positions_bytes(self, data: bytes) -> list[dict]:
        del data
        return [{
            "text": "원문",
            "x": 10.0,
            "y": 20.0,
            "width": 30.0,
            "height": -5.0 if self.negative_height else 5.0,
            "font": "TestFont",
            "font_tag": "F1",
            "font_size": 10.0,
            "page": 1,
            "is_bold": False,
            "is_italic": False,
            "is_underline": False,
            "is_strikeout": False,
            "item_type": "text",
            "mcid": 0,
        }]

    def extract_structure_elements_bytes(self, data: bytes) -> list[dict]:
        del data
        return [{"page": 1, "mcid": 0, "role": "P"}]


class PdfInspectorCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nfixture bytes\n")
        self.relative_path = "notices/PBLN_1/source.pdf"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def capture(self, *, inspector: object | None = None, **kwargs) -> dict:
        return capture_pdf_to_native(
            self.pdf,
            notice_id="PBLN_1",
            source_relative_path=self.relative_path,
            inspector_module=inspector or FakePdfInspector(),
            inspector_version="1.17.0",
            **kwargs,
        )

    def test_default_limits_are_bounded_for_offline_notice_pdfs(self) -> None:
        self.assertEqual(DEFAULT_LIMITS.max_source_bytes, 64 * 1024 * 1024)
        self.assertEqual(DEFAULT_LIMITS.max_pages, 256)
        self.assertEqual(DEFAULT_LIMITS.max_text_items, 500_000)
        self.assertEqual(DEFAULT_LIMITS.max_structure_elements, 500_000)
        self.assertEqual(DEFAULT_LIMITS.max_markdown_characters, 16 * 1024 * 1024)
        self.assertEqual(DEFAULT_LIMITS.max_text_characters, 16 * 1024 * 1024)
        self.assertEqual(DEFAULT_LIMITS.max_capture_bytes, 64 * 1024 * 1024)
        self.assertEqual(DEFAULT_LIMITS.max_absolute_coordinate, 1_000_000)

    def test_exact_version_is_required_and_missing_distribution_version_fails_closed(self) -> None:
        with self.assertRaisesRegex(NativeCaptureError, "exactly 1.17.0"):
            capture_pdf_to_native(
                self.pdf,
                notice_id="PBLN_1",
                source_relative_path=self.relative_path,
                inspector_module=FakePdfInspector(),
                inspector_version="1.16.0",
            )

        with patch(
            "common_ir_pipeline.pdf_fusion.native_capture.package_version",
            side_effect=PackageNotFoundError("pdf-inspector"),
        ):
            with self.assertRaisesRegex(NativeCaptureError, "cannot verify"):
                capture_pdf_to_native(
                    self.pdf,
                    notice_id="PBLN_1",
                    source_relative_path=self.relative_path,
                    inspector_module=FakePdfInspector(),
                )

        with self.assertRaisesRegex(NativeCaptureError, "missing process_pdf_bytes"):
            capture_pdf_to_native(
                self.pdf,
                notice_id="PBLN_1",
                source_relative_path=self.relative_path,
                inspector_module=object(),
                inspector_version="1.17.0",
            )

    def test_capture_is_source_bound_and_tamper_is_rejected(self) -> None:
        capture = self.capture()
        self.assertEqual(capture, validate_native_capture(
            capture,
            source_pdf=self.pdf,
            expected_notice_id="PBLN_1",
            expected_source_relative_path=self.relative_path,
        ))

        wrong_hash = copy.deepcopy(capture)
        wrong_hash["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(NativeCaptureError, "hash/size"):
            validate_native_capture(wrong_hash, source_pdf=self.pdf)

        self.pdf.write_bytes(b"%PDF-1.7\ntampered bytes\n")
        with self.assertRaisesRegex(NativeCaptureError, "hash/size"):
            validate_native_capture(capture, source_pdf=self.pdf)

    def test_pdf_adapter_rejects_bound_capture_with_a_different_pdf(self) -> None:
        capture = self.capture()
        native_path = self.root / "native.json"
        native_path.write_bytes(canonical_json_bytes(capture))
        other_pdf = self.root / "other.pdf"
        other_pdf.write_bytes(b"%PDF-1.7\ndifferent source bytes\n")
        output = self.root / "common-ir.json"

        with patch.object(sys, "argv", [
            "common-ir-pdf-native",
            "--notice-id", "PBLN_1",
            "--native", str(native_path),
            "--source-path", str(other_pdf),
            "--output", str(output),
        ]):
            with self.assertRaises(SystemExit) as raised:
                pdf_native_main()

        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(output.exists())

    def test_pdf_adapter_uses_bound_hash_and_rejects_conflicting_argument(self) -> None:
        capture = self.capture()
        native_path = self.root / "native.json"
        native_path.write_bytes(canonical_json_bytes(capture))
        output = self.root / "common-ir.json"

        with patch.object(sys, "argv", [
            "common-ir-pdf-native",
            "--notice-id", "PBLN_1",
            "--native", str(native_path),
            "--source-path", str(self.pdf),
            "--output", str(output),
        ]):
            pdf_native_main()

        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            document["document"]["provenance"]["source_sha256"],
            capture["source_sha256"],
        )

        output.unlink()
        with patch.object(sys, "argv", [
            "common-ir-pdf-native",
            "--notice-id", "PBLN_1",
            "--native", str(native_path),
            "--source-path", str(self.pdf),
            "--source-sha256", "0" * 64,
            "--output", str(output),
        ]):
            with self.assertRaises(SystemExit) as raised:
                pdf_native_main()

        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(output.exists())

    def test_pdf_adapter_rejects_unbound_legacy_capture(self) -> None:
        capture = self.capture()
        del capture["capture_schema_version"]
        native_path = self.root / "legacy-native.json"
        native_path.write_bytes(canonical_json_bytes(capture))
        output = self.root / "common-ir.json"

        with patch.object(sys, "argv", [
            "common-ir-pdf-native",
            "--notice-id", "PBLN_1",
            "--native", str(native_path),
            "--source-path", str(self.pdf),
            "--output", str(output),
        ]):
            with self.assertRaises(SystemExit) as raised:
                pdf_native_main()

        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(output.exists())

    def test_full_document_scope_and_complete_page_index_are_mandatory(self) -> None:
        capture = self.capture()

        partial = copy.deepcopy(capture)
        partial["extraction_scope"] = "pages:1"
        with self.assertRaisesRegex(NativeCaptureError, "Partial-page|partial-page"):
            validate_native_capture(partial)

        missing = copy.deepcopy(capture)
        missing["pages_markdown_result"]["pages"].pop()
        with self.assertRaisesRegex(NativeCaptureError, "every source page"):
            validate_native_capture(missing)

        wrong_index = copy.deepcopy(capture)
        wrong_index["pages_markdown_result"]["pages"][0]["page"] = 1
        with self.assertRaisesRegex(NativeCaptureError, "0-indexed"):
            validate_native_capture(wrong_index)

        out_of_range = copy.deepcopy(capture)
        out_of_range["text_items"][0]["page"] = 3
        with self.assertRaisesRegex(NativeCaptureError, "safety cap"):
            validate_native_capture(out_of_range)

        for unsafe_path in ("/tmp/source.pdf", "../source.pdf", r"C:\\source.pdf", "C:/source.pdf"):
            unsafe = copy.deepcopy(capture)
            unsafe["source_path"] = unsafe_path
            with self.subTest(unsafe_path=unsafe_path), self.assertRaisesRegex(NativeCaptureError, "relative path"):
                validate_native_capture(unsafe)

    def test_page_cap_stops_before_larger_native_extractions(self) -> None:
        inspector = FakePdfInspector(page_count=DEFAULT_LIMITS.max_pages + 1)
        inspector.extract_pages_markdown_bytes = Mock(side_effect=AssertionError("must not run"))
        inspector.extract_text_with_positions_bytes = Mock(side_effect=AssertionError("must not run"))
        inspector.extract_structure_elements_bytes = Mock(side_effect=AssertionError("must not run"))

        with self.assertRaisesRegex(NativeCaptureError, "page_count.*safety cap"):
            self.capture(inspector=inspector)

        inspector.extract_pages_markdown_bytes.assert_not_called()
        inspector.extract_text_with_positions_bytes.assert_not_called()
        inspector.extract_structure_elements_bytes.assert_not_called()

    def test_rejects_nonfinite_values_and_resource_cap_overruns(self) -> None:
        capture = self.capture()
        for value in (math.nan, math.inf, -math.inf):
            poisoned = copy.deepcopy(capture)
            poisoned["text_items"][0]["x"] = value
            with self.subTest(value=value), self.assertRaisesRegex(NativeCaptureError, "finite"):
                validate_native_capture(poisoned)

        over_cap = copy.deepcopy(capture)
        over_cap["text_items"].append(copy.deepcopy(over_cap["text_items"][0]))
        with self.assertRaisesRegex(NativeCaptureError, "text_items"):
            validate_native_capture(over_cap, limits=NativeCaptureLimits(max_text_items=1))

        with self.assertRaisesRegex(NativeCaptureError, "source_pdf size"):
            self.capture(limits=NativeCaptureLimits(max_source_bytes=5))

    def test_signed_link_geometry_is_preserved_and_canonical_bytes_are_deterministic(self) -> None:
        first = self.capture(inspector=FakePdfInspector(negative_height=True))
        second = self.capture(inspector=FakePdfInspector(negative_height=True))
        self.assertEqual(first["text_items"][0]["height"], -5.0)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertNotIn(b"processing_time_ms", canonical_json_bytes(first))
        self.assertNotIn(str(self.root).encode("utf-8"), canonical_json_bytes(first))

    def test_output_is_created_with_private_mode_and_never_overwritten(self) -> None:
        output = self.root / "capture.json"
        write_capture_exclusive(output, b"first\n")
        self.assertEqual(output.read_bytes(), b"first\n")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        with self.assertRaisesRegex(NativeCaptureError, "refusing to overwrite"):
            write_capture_exclusive(output, b"second\n")
        self.assertEqual(output.read_bytes(), b"first\n")

    def test_native_capture_file_loader_is_bounded_and_duplicate_safe(self) -> None:
        capture_path = self.root / "capture.json"
        capture_path.write_bytes(canonical_json_bytes(self.capture()))
        self.assertEqual(load_native_capture_file(capture_path), self.capture())

        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"notice_id":"one","notice_id":"two"}', encoding="utf-8")
        with self.assertRaisesRegex(NativeCaptureError, "duplicate JSON key"):
            load_native_capture_file(duplicate)

        linked = self.root / "linked.json"
        linked.symlink_to(capture_path)
        with self.assertRaisesRegex(NativeCaptureError, "failed to read"):
            load_native_capture_file(linked)

        with self.assertRaisesRegex(NativeCaptureError, "artifact safety cap"):
            load_native_capture_file(
                capture_path,
                limits=NativeCaptureLimits(max_capture_bytes=1),
            )


if __name__ == "__main__":
    unittest.main()
