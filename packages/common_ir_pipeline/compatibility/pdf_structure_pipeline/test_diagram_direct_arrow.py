#!/usr/bin/env python3
"""Fixture/unit tests for diagram_direct_arrow.py.

No GPU/vLLM/Docker; no network. The 125090/125098 fixtures reuse already-
captured raw Surya evidence (real surya_targeted *_diagram_high.json files)
so this doubles as a reassembly check against real evidence, not just a
hand-written string.

Run with:  python3 test_diagram_direct_arrow.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_direct_arrow import extract_direct_arrow_edges

ROOT = Path(__file__).resolve().parents[1]  # exploratory_study/results/runpod_hybrid_ir_validation
SURYA_TARGETED = ROOT / "runs/batch_b_native_20260826T063000Z/surya_targeted"

# Verbatim copy of the explicit Diagram HTML from
# runs/batch_b_native_20260826T063000Z/surya_targeted/PBLN_000000000125090_p3_diagram_high.json
# (result.blocks[label=Diagram].html), used as a hermetic fixture independent
# of the file on disk.
PBLN_125090_DIAGRAM_HTML = (
    '<table border="1">\n<tr>\n'
    '<td><b>사업공고</b><br/>8. 4.(화)<br/>(접수마감: 8. 25.)</td>\n'
    '<td>→</td>\n'
    '<td><b>1차 서류심사</b><br/>~ 9. 9.(수)<br/>* 결과안내: 9. 11.(금)</td>\n'
    '<td>→</td>\n'
    '<td><b>창업교육</b><br/>9. 17.(목) ~ 10. 1.(목)<br/>장소: 온라인, 오프라인(대전)<br/>↓</td>\n'
    '</tr>\n<tr>\n'
    '<td><b>데모데이</b><br/>10. 27.(화)</td>\n'
    '<td>←</td>\n'
    '<td><b>IR 피칭덱 디자인 제작 지원</b><br/>10. 13.(수) ~ 10. 23.(금)</td>\n'
    '<td>←</td>\n'
    '<td><b>사업계획서/IR 피칭 멘토링</b><br/>9. 17.(목) ~ 10. 12.(화)</td>\n'
    '</tr>\n</table>'
)

# The 5 edges manually curated in config/direct_diagram_relations_pbln_125090_v0_2.json,
# expressed as (from_label, to_label, arrow) using the node's own HTML label
# text rather than the config's internal node ids.
EXPECTED_125090_EDGES = [
    ("사업공고", "1차 서류심사", "→"),
    ("1차 서류심사", "창업교육", "→"),
    ("창업교육", "사업계획서/IR 피칭 멘토링", "↓"),
    ("사업계획서/IR 피칭 멘토링", "IR 피칭덱 디자인 제작 지원", "←"),
    ("IR 피칭덱 디자인 제작 지원", "데모데이", "←"),
]


def _load_diagram_html(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    blocks = data["result"]["blocks"]
    diagrams = [b for b in blocks if b["label"] == "Diagram"]
    assert len(diagrams) == 1, f"expected exactly one Diagram block in {path}, found {len(diagrams)}"
    return diagrams[0]["html"]


class DirectArrowEdgeTests(unittest.TestCase):
    def _assert_matches_125090(self, edges):
        # Edge order reflects HTML scan order (all same-row arrow cells
        # first, then vertical trailing tokens), not a path traversal, so
        # compare as an order-independent set of the 5 expected pairs.
        self.assertEqual(len(edges), 5, f"expected 5 edges, got {len(edges)}: {edges}")
        actual = [(e.from_label, e.to_label, e.arrow) for e in edges]
        self.assertCountEqual(actual, EXPECTED_125090_EDGES)

    def test_125090_inline_fixture_yields_five_edges_in_order(self):
        edges = extract_direct_arrow_edges(PBLN_125090_DIAGRAM_HTML)
        self._assert_matches_125090(edges)

    def test_125090_real_evidence_file_yields_five_edges_in_order(self):
        path = SURYA_TARGETED / "PBLN_000000000125090_p3_diagram_high.json"
        if not path.is_file():
            self.skipTest(f"raw evidence not present in this checkout: {path}")
        html = _load_diagram_html(path)
        self.assertEqual(html, PBLN_125090_DIAGRAM_HTML, "on-disk evidence drifted from the hermetic fixture copy")
        edges = extract_direct_arrow_edges(html)
        self._assert_matches_125090(edges)

    def test_125098_p3_real_evidence_natural_language_image_abstains(self):
        path = SURYA_TARGETED / "PBLN_000000000125098_p3_diagram_high.json"
        if not path.is_file():
            self.skipTest(f"raw evidence not present in this checkout: {path}")
        html = _load_diagram_html(path)
        self.assertNotIn("<table", html.lower(), "fixture assumption changed: 125098 p3 now has a table")
        edges = extract_direct_arrow_edges(html)
        self.assertEqual(edges, [])

    def test_bare_img_with_alt_text_abstains(self):
        # Covers the natural-language/image-only Diagram shape generically
        # (alt text describing the picture, no <table> at all).
        html = '<img alt="사업 절차를 설명하는 순서도 이미지"/>'
        self.assertEqual(extract_direct_arrow_edges(html), [])

    def test_empty_or_missing_html_abstains(self):
        self.assertEqual(extract_direct_arrow_edges(""), [])
        self.assertEqual(extract_direct_arrow_edges(None), [])

    def test_prose_paragraph_without_table_abstains(self):
        html = "<p>공고 이후 서류심사를 거쳐 교육을 진행합니다. 이어서 멘토링과 디자인 지원을 받고 데모데이로 마무리합니다.</p>"
        self.assertEqual(extract_direct_arrow_edges(html), [])

    def test_arrow_only_cell_missing_one_neighbor_abstains(self):
        html = '<table><tr><td><b>시작</b></td><td>→</td></tr></table>'
        self.assertEqual(extract_direct_arrow_edges(html), [])

    def test_two_adjacent_arrow_cells_abstain(self):
        html = '<table><tr><td><b>A</b></td><td>→</td><td>→</td><td><b>B</b></td></tr></table>'
        self.assertEqual(extract_direct_arrow_edges(html), [])

    def test_rowspan_table_abstains_entirely(self):
        html = (
            '<table><tr><td rowspan="2"><b>A</b></td><td>→</td><td><b>B</b></td></tr>'
            '<tr><td>→</td><td><b>C</b></td></tr></table>'
        )
        self.assertEqual(extract_direct_arrow_edges(html), [])

    def test_bidirectional_arrow_glyph_abstains(self):
        html = '<table><tr><td><b>A</b></td><td>↔</td><td><b>B</b></td></tr></table>'
        self.assertEqual(extract_direct_arrow_edges(html), [])

    def test_arrow_embedded_in_prose_line_is_not_an_arrow_cell(self):
        # The glyph is real but not alone in its own cell/line -> abstain,
        # not a guess.
        html = '<table><tr><td><b>A</b></td><td>다음 단계로 → 진행</td><td><b>B</b></td></tr></table>'
        self.assertEqual(extract_direct_arrow_edges(html), [])

    def test_trailing_arrow_with_no_row_below_abstains(self):
        html = '<table><tr><td><b>A</b><br/>↓</td></tr></table>'
        self.assertEqual(extract_direct_arrow_edges(html), [])

    def test_simple_vertical_chain(self):
        html = (
            '<table>'
            '<tr><td><b>A</b><br/>↓</td></tr>'
            '<tr><td><b>B</b><br/>↓</td></tr>'
            '<tr><td><b>C</b></td></tr>'
            '</table>'
        )
        edges = extract_direct_arrow_edges(html)
        actual = [(e.from_label, e.to_label, e.arrow) for e in edges]
        self.assertEqual(actual, [("A", "B", "↓"), ("B", "C", "↓")])


if __name__ == "__main__":
    unittest.main()
