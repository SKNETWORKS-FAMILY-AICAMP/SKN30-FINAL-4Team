"""Regression tests for the Markdown-fixture Common IR v1 adapter."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from semantic_structuring.run_request_markdown_to_common_ir_v1 import (
    ROOT,
    assert_fixture_integrity,
    build_document,
    main,
    parse_markdown,
)
from semantic_structuring.request_profile_v012 import build_request_candidate_pack


FIXTURE_INPUT = ROOT / "docs/PreReview_Request_Profile/test_request_corpus_v0.1/semantic_input"


class MarkdownFixtureCommonIRTests(unittest.TestCase):
    def test_fixture_corpus_validates_and_uses_only_markdown_fixture_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "out"
            exit_code = main_with_args(FIXTURE_INPUT, output_root)
            self.assertEqual(exit_code, 0)
            manifest = json.loads((output_root / "request_common_ir_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["entries"]), 5)
            for entry in manifest["entries"]:
                source = Path(entry["source_path"])
                document = json.loads(Path(entry["common_ir_path"]).read_text(encoding="utf-8"))
                self.assertEqual(document["document"]["source_kind"], "markdown_fixture")
                self.assertEqual(document["document"]["provenance"]["source_sha256"], entry["source_sha256"])
                self.assertEqual(assert_fixture_integrity(document, source), [])
                self.assertNotIn("reference_notice_id", json.dumps(document, ensure_ascii=False))

    def test_heading_list_and_table_keep_exact_source_occurrences(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "fixture.input.md"
            source.write_text("# 제목\n\n- 목록 항목\n\n| 항목 | 값 |\n| --- | --- |\n| 지원금 | 10 |\n", encoding="utf-8")
            document = build_document(source, "TEST")
            self.assertEqual(assert_fixture_integrity(document, source), [])
            self.assertEqual([block["kind"] for block in document["blocks"]], ["heading", "paragraph", "table"])
            table = document["blocks"][2]
            self.assertEqual([(cell["row_index"], cell["col_index"]) for cell in table["cells"]], [(0, 0), (0, 1), (1, 0), (1, 1)])
            texts = {occurrence["text"] for occurrence in table["occurrences"]}
            self.assertIn("지원금", texts)
            self.assertIn("10", texts)

    def test_skipped_heading_levels_keep_same_level_headings_as_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "skipped_levels.input.md"
            source.write_text(
                "# 최상위\n\n### 중간\n\n#### 첫째\n본문 1\n\n#### 둘째\n본문 2\n",
                encoding="utf-8",
            )
            blocks = parse_markdown(source, "SKIPPED")
            self.assertEqual(
                [block["section_path"] for block in blocks],
                [
                    "최상위",
                    "최상위/중간",
                    "최상위/중간/첫째",
                    "최상위/중간/첫째",
                    "최상위/중간/둘째",
                    "최상위/중간/둘째",
                ],
            )

    def test_request_01_h4_siblings_do_not_create_false_parent_paths(self):
        source = FIXTURE_INPUT / "request_01_scale_period.input.md"
        blocks = parse_markdown(source, "PREREVIEW-TEST-2027-01")
        reason = next(block for block in blocks if block["text"] == "**사전협의 요청사유**")
        business_name = next(block for block in blocks if block["text"] == "**사업명**")
        self.assertEqual(reason["section_path"], "중소기업지원사업 사전협의 요청서/< 가상 사회연대경제진흥원 >/**사전협의 요청사유**")
        self.assertEqual(business_name["section_path"], "중소기업지원사업 사전협의 요청서/< 가상 사회연대경제진흥원 >/**사업명**")
        self.assertNotIn("**사전협의 요청사유**/**사업명**", business_name["section_path"])

    def test_presentation_clean_run_uses_separate_exact_span_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "presentation-clean"
            self.assertEqual(main_with_args(FIXTURE_INPUT, output_root, presentation_clean=True), 0)
            run_manifest = json.loads((output_root / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(run_manifest["presentation_cleaning"]["enabled"])
            self.assertEqual(run_manifest["presentation_cleaning"]["version"], "markdown_inline_presentation_clean_v1")
            self.assertEqual(len(run_manifest["cleaned_source_lineage"]), 5)
            cleaned_source = output_root / "cleaned_semantic_input" / "request_02_target_eligibility.input.md"
            self.assertNotIn("**", cleaned_source.read_text(encoding="utf-8"))

            document = json.loads((output_root / "common_ir/PREREVIEW-TEST-2027-02.common_ir_v1.json").read_text(encoding="utf-8"))
            self.assertEqual(assert_fixture_integrity(document, cleaned_source), [])
            self.assertNotIn("**", json.dumps(document["blocks"], ensure_ascii=False))
            self.assertTrue(any(block["kind"] == "heading" for block in document["blocks"]))
            self.assertTrue(any(block["source_block_label"] == "markdown_list_item" for block in document["blocks"]))
            pack = build_request_candidate_pack(document)
            self.assertNotIn("**", "".join(block.text for block in pack.blocks))


def main_with_args(input_dir: Path, output_root: Path, *, presentation_clean: bool = False) -> int:
    import sys
    from unittest.mock import patch

    argv = ["request-markdown-common-ir", "--input-dir", str(input_dir), "--output-root", str(output_root)]
    if presentation_clean:
        argv.append("--presentation-clean")
    with patch.object(sys, "argv", argv):
        return main()


if __name__ == "__main__":
    unittest.main()
