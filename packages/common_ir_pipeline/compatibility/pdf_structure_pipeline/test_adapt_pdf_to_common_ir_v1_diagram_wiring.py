#!/usr/bin/env python3
"""Tests for wiring promote_pdf_only_explicit_diagram_edges.py's output into
adapt_pdf_to_common_ir_v1.py's --diagram-relations-pdf-base.

Covers the one gap fixed here: Route A's own diagram_high evidence block
(minted only inside the diagram-relations-pdf-base file, never in
--enriched-common-ir) must be carried into v1 so an explicit relation's
evidence_ids actually resolve -- without duplicating any existing
block/occurrence id and without overwriting native/OCR content.

No GPU/Surya/vLLM/Docker; no .env access. The 125098/125090 cases reuse
already-generated real evidence from this project; they skip cleanly if that
evidence isn't present in the checkout.

Run from the repo root:
    python3 exploratory_study/results/runpod_hybrid_ir_validation/scripts/test_adapt_pdf_to_common_ir_v1_diagram_wiring.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parents[0]  # exploratory_study/results/runpod_hybrid_ir_validation
REPO_ROOT = SCRIPTS.parents[3]
sys.path.insert(0, str(SCRIPTS))
from adapt_pdf_to_common_ir_v1 import _pdf_nested_table_relations  # noqa: E402

PROMOTE_SCRIPT = SCRIPTS / "promote_pdf_only_explicit_diagram_edges.py"
ADAPT_SCRIPT = SCRIPTS / "adapt_pdf_to_common_ir_v1.py"

NATIVE_125098 = ROOT / "raw/pdf_inspector/PBLN_000000000125098.json"
NATIVE_125090 = ROOT / "raw/pdf_inspector/PBLN_000000000125090.json"
RENDER_125098 = ROOT / "runs/batch_b_native_20260826T063000Z/rendered/125098/render_manifest.json"
RENDER_125090 = ROOT / "runs/batch_b_native_20260826T063000Z/rendered/125090/render_manifest.json"
SURYA_ALL_PAGES_125098 = ROOT / "runs/batch_b_native_20260826T063000Z/surya/PBLN_000000000125098_all_pages.json"
SURYA_ALL_PAGES_125090 = ROOT / "runs/batch_b_native_20260826T063000Z/surya/PBLN_000000000125090_all_pages.json"
ENRICHED_125098 = ROOT / "runs/delivery_pdf_20260826/PBLN_000000000125098/common_ir_v0_3_pdf_enriched/PBLN_000000000125098.json"
ENRICHED_125090 = ROOT / "runs/delivery_pdf_20260826/PBLN_000000000125090/common_ir_v0_3_pdf_enriched/PBLN_000000000125090.json"
DIAGRAM_EDGES_125098 = ROOT / "runs/diagram_edge_promotion_smoke_20260827/PBLN_000000000125098_full_document_pdf_base_ir_v2.diagram_edges.json"
DIAGRAM_EDGES_125090 = ROOT / "runs/diagram_edge_promotion_smoke_20260827/PBLN_000000000125090_full_document_pdf_base_ir_v2.diagram_edges.json"


def _run_adapt(tmp_path: Path, notice_id: str, native: Path, render: Path, enriched: Path, diagram_relations_pdf_base: Path | None, output: Path) -> dict:
    source = tmp_path / "original.pdf"
    if not source.exists():
        source.write_bytes(b"%PDF-1.4\nsynthetic source\n")
    cmd = [sys.executable, str(ADAPT_SCRIPT), "--notice-id", notice_id, "--native", str(native), "--source-path", str(source), "--render-manifest", str(render), "--enriched-common-ir", str(enriched), "--output", str(output)]
    if diagram_relations_pdf_base:
        cmd += ["--diagram-relations-pdf-base", str(diagram_relations_pdf_base)]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"adapt_pdf_to_common_ir_v1.py failed (rc={result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return json.loads(result.stdout)


class SyntheticRouteAWiringTests(unittest.TestCase):
    """Builds a minimal, hermetic 2-node Route A diagram (same fixture shape
    as promote_pdf_only_explicit_diagram_edges.py's own positive-control
    test) and threads it all the way through adapt_pdf_to_common_ir_v1.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_inherited_ocr_occurrence_becomes_textless_layout_evidence(self):
        native_path = self.tmp / "native.json"
        native_path.write_text(json.dumps({"text_items": []}), encoding="utf-8")
        render_path = self.tmp / "render_manifest.json"
        render_path.write_text(json.dumps({"render_scale": 1.0, "pages": [{"page": 1, "height": 100}]}), encoding="utf-8")
        evidence_id = "occ:surya:p1:b9:ocr"
        enriched_path = self.tmp / "enriched.json"
        enriched_path.write_text(json.dumps({
            "blocks": [{
                "block_id": "occ:surya:p1:b9", "kind": "table", "structure_status": "explicit", "text": "OCR table", "source_block_label": "Table",
                "occurrences": [{"occurrence_id": evidence_id, "role": "ocr_text", "text": "OCR table", "provenance": {"method": "surya", "page": 1, "bbox": [0, 0, 10, 10], "coordinate_space": "pdf_user_space", "source_location": "x"}}],
                "cells": [{"cell_id": "cell:1", "evidence_ids": [evidence_id], "row_index": 0, "col_index": 0, "provenance": {"method": "surya", "page": 1, "bbox": [0, 0, 10, 10], "coordinate_space": "pdf_user_space", "source_location": "x"}}],
                "provenance": {"method": "surya", "page": 1, "bbox": [0, 0, 10, 10], "coordinate_space": "pdf_user_space", "source_location": "x"}
            }]
        }), encoding="utf-8")
        output_path = self.tmp / "v1.json"
        _run_adapt(self.tmp, "PBLN_TEST", native_path, render_path, enriched_path, None, output_path)
        doc = json.loads(output_path.read_text(encoding="utf-8"))
        occurrence = next(o for b in doc["blocks"] for o in b["occurrences"] if o["occurrence_id"] == evidence_id)
        self.assertEqual(occurrence["role"], "layout_region")
        self.assertNotIn("text", occurrence)
        self.assertFalse(any(o["role"] == "ocr_text" for b in doc["blocks"] for o in b["occurrences"]))
        self.assertIn(evidence_id, doc["blocks"][0]["cells"][0]["evidence_ids"])

    def test_placeholder_is_not_semantic_text_and_eligibility_uses_real_text(self):
        native_path = self.tmp / "native.json"
        native_path.write_text(json.dumps({"text_items": [
            {"page": 1, "text": "[Image: Im1]", "x": 0, "y": 90, "width": 10, "height": 10, "font_size": 10},
            {"page": 1, "text": "지원규모: 2개사", "x": 0, "y": 70, "width": 50, "height": 10, "font_size": 10},
        ]}), encoding="utf-8")
        render_path = self.tmp / "render_manifest.json"
        render_path.write_text(json.dumps({"render_scale": 1.0, "pages": [{"page": 1, "height": 100}]}), encoding="utf-8")
        enriched_path = self.tmp / "enriched.json"
        enriched_path.write_text(json.dumps({"blocks": []}), encoding="utf-8")
        output_path = self.tmp / "v1.json"
        _run_adapt(self.tmp, "PBLN_TEST", native_path, render_path, enriched_path, None, output_path)
        doc = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["document"]["pdf_semantic_eligibility"], "eligible_native_text")
        self.assertEqual(doc["document"]["native_text_page_count"], 1)
        all_text = [b["text"] for b in doc["blocks"]] + [o.get("text", "") for b in doc["blocks"] for o in b["occurrences"]]
        self.assertFalse(any("[Image:" in text for text in all_text))

    def test_parent_table_bbox_cannot_be_used_as_a_cell_containment_bbox(self):
        outer_bbox = [0, 0, 100, 100]
        table = {
            "block_id": "table:outer", "kind": "table", "structure_status": "explicit", "page": 1,
            "provenance": {"bbox": outer_bbox},
            "cells": [
                {"cell_id": "cell:outer:0", "provenance": {"bbox": outer_bbox}},
                {"cell_id": "cell:outer:1", "provenance": {"bbox": outer_bbox}},
            ],
        }
        inner = {
            "block_id": "table:inner", "kind": "table_candidate", "structure_status": "partial", "page": 1,
            "provenance": {"bbox": [10, 10, 20, 20]},
        }
        self.assertEqual(_pdf_nested_table_relations([table, inner]), [])

    def test_placeholder_only_pdf_is_excluded(self):
        native_path = self.tmp / "native.json"
        native_path.write_text(json.dumps({"text_items": [{"page": 1, "text": "[Image: Image53]", "x": 0, "y": 90, "width": 10, "height": 10, "font_size": 10}]}), encoding="utf-8")
        render_path = self.tmp / "render_manifest.json"
        render_path.write_text(json.dumps({"render_scale": 1.0, "pages": [{"page": 1, "height": 100}]}), encoding="utf-8")
        enriched_path = self.tmp / "enriched.json"
        enriched_path.write_text(json.dumps({"blocks": []}), encoding="utf-8")
        output_path = self.tmp / "v1.json"
        _run_adapt(self.tmp, "PBLN_TEST", native_path, render_path, enriched_path, None, output_path)
        doc = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["document"]["pdf_semantic_eligibility"], "excluded_image_only")
        self.assertEqual(doc["document"]["pdf_semantic_reason"], "no_substantive_native_text")
        self.assertEqual(doc["document"]["native_text_page_count"], 0)

    def test_image_only_pdf_needs_no_render_or_enriched_artifacts(self):
        native_path = self.tmp / "native.json"
        native_path.write_text(json.dumps({"process_result": {"page_count": 2}, "text_items": [
            {"page": 1, "text": "[Image: Im1]", "x": 0, "y": 90, "width": 10, "height": 10, "font_size": 10},
            {"page": 2, "text": "", "x": 0, "y": 90, "width": 10, "height": 10, "font_size": 10},
        ]}), encoding="utf-8")
        source_path = self.tmp / "original.pdf"
        source_path.write_bytes(b"%PDF-1.4\nsynthetic source\n")
        output_path = self.tmp / "v1.json"
        result = subprocess.run([sys.executable, str(ADAPT_SCRIPT), "--notice-id", "PBLN_TEST", "--native", str(native_path), "--source-path", str(source_path), "--output", str(output_path)], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["document"]["page_count"], 2)
        self.assertEqual(doc["document"]["pdf_semantic_eligibility"], "excluded_image_only")
        self.assertEqual(doc["blocks"], [])

    def test_route_a_explicit_relation_evidence_resolves_in_final_v1(self):
        native_items = [
            {"page": 1, "text": "A", "x": 0, "y": 90, "width": 10, "height": 10, "font_size": 10},
            {"page": 1, "text": "B", "x": 50, "y": 90, "width": 10, "height": 10, "font_size": 10},
        ]
        native_path = self.tmp / "native.json"
        native_path.write_text(json.dumps({"text_items": native_items}), encoding="utf-8")
        render_path = self.tmp / "render_manifest.json"
        render_path.write_text(json.dumps({"render_scale": 1.0, "pages": [{"page": 1, "height": 100}]}), encoding="utf-8")
        surya_diagram_path = self.tmp / "diagram_high.json"
        surya_diagram_path.write_text(json.dumps({"input": {"page": 1}, "result": {"blocks": [{"label": "Diagram", "bbox": [0, 0, 60, 20], "html": "<table><tr><td><b>A</b></td><td>→</td><td><b>B</b></td></tr></table>"}]}}), encoding="utf-8")
        pdf_base_path = self.tmp / "pdf_base.json"
        pdf_base_path.write_text(json.dumps({"schema_version": "common_ir_pdf_base_v2", "document": {"source_format": "pdf", "source_path": "x.pdf", "source_notice_key": "PBLN_TEST", "scope": "whole_document"}, "blocks": [], "relations": []}), encoding="utf-8")

        # 1) Generate a real Route A output via promote_pdf_only_explicit_diagram_edges.py
        # (not hand-written) -- this is what --diagram-relations-pdf-base actually is.
        diagram_edges_path = self.tmp / "diagram_edges.json"
        result = subprocess.run([sys.executable, str(PROMOTE_SCRIPT), "--pdf-base", str(pdf_base_path), "--native", str(native_path), "--render-manifest", str(render_path), "--surya-diagram-evidence", str(surya_diagram_path), "--output", str(diagram_edges_path)], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        diagram_edges = json.loads(diagram_edges_path.read_text(encoding="utf-8"))
        self.assertEqual(len(diagram_edges["relations"]), 1)
        relation = diagram_edges["relations"][0]
        self.assertFalse(relation["inferred"])
        self.assertEqual(relation["structure_status"], "explicit")
        evidence_id = relation["evidence_ids"][0]
        self.assertTrue(evidence_id.startswith("occ:surya:p1:diagram_high:"))
        # Confirms the gap this fix addresses: the evidence block only
        # exists in the diagram-relations file's own "blocks", not anywhere
        # --enriched-common-ir could ever have carried it from.
        self.assertTrue(any(b.get("id") == evidence_id for b in diagram_edges["blocks"]))

        # 2) An --enriched-common-ir snapshot that (correctly) predates
        # diagram promotion -- no table/diagram blocks at all.
        enriched_path = self.tmp / "enriched.json"
        enriched_path.write_text(json.dumps({"schema_version": "common_ir_v0_1", "document": {"document_id": "pdf:PBLN_TEST", "source_kind": "pdf", "artifact_role": "production"}, "blocks": [], "conflicts": [], "relations": []}), encoding="utf-8")
        # 3) Run the v1 adapter with this diagram-relations file.
        output_path = self.tmp / "v1.json"
        summary = _run_adapt(self.tmp, "PBLN_TEST", native_path, render_path, enriched_path, diagram_edges_path, output_path)
        self.assertEqual(summary["relations"], 1)
        self.assertEqual(summary["extra_diagram_evidence_blocks"], 1)
        self.assertEqual(summary["validation_errors"], 0)

        doc = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(len(doc["relations"]), 1)
        v1_relation = doc["relations"][0]
        self.assertEqual(v1_relation["kind"], "diagram_edge")
        self.assertFalse(v1_relation["inferred"])
        self.assertEqual(v1_relation["structure_status"], "explicit")

        occurrence_ids = {o["occurrence_id"] for b in doc["blocks"] for o in b["occurrences"]}
        self.assertIn(v1_relation["from_id"], occurrence_ids)
        self.assertIn(v1_relation["to_id"], occurrence_ids)
        self.assertIn(v1_relation["evidence_ids"][0], occurrence_ids)

        # No duplicate block_id/occurrence_id anywhere (independent re-check
        # of what the adapter's own schema validation already enforced).
        block_ids = [b["block_id"] for b in doc["blocks"]]
        self.assertEqual(len(block_ids), len(set(block_ids)))
        all_occurrence_ids = [o["occurrence_id"] for b in doc["blocks"] for o in b["occurrences"]]
        self.assertEqual(len(all_occurrence_ids), len(set(all_occurrence_ids)))
        self.assertFalse(any(o["role"] == "ocr_text" for b in doc["blocks"] for o in b["occurrences"]))
        self.assertEqual(next(o["role"] for b in doc["blocks"] for o in b["occurrences"] if o["occurrence_id"] == evidence_id), "layout_region")

        # Native text for A/B is untouched (not overwritten by anything OCR).
        native_texts = {o["occurrence_id"]: o["text"] for b in doc["blocks"] for o in b["occurrences"] if o["role"] == "native_text"}
        self.assertEqual(native_texts.get(v1_relation["from_id"]), "A")
        self.assertEqual(native_texts.get(v1_relation["to_id"]), "B")

    def test_rerun_is_idempotent_and_does_not_duplicate_evidence_block(self):
        # Running the adapter twice against the same diagram-relations file
        # must not accumulate a second copy of the diagram_high block.
        native_items = [
            {"page": 1, "text": "A", "x": 0, "y": 90, "width": 10, "height": 10, "font_size": 10},
            {"page": 1, "text": "B", "x": 50, "y": 90, "width": 10, "height": 10, "font_size": 10},
        ]
        native_path = self.tmp / "native.json"
        native_path.write_text(json.dumps({"text_items": native_items}), encoding="utf-8")
        render_path = self.tmp / "render_manifest.json"
        render_path.write_text(json.dumps({"render_scale": 1.0, "pages": [{"page": 1, "height": 100}]}), encoding="utf-8")
        surya_diagram_path = self.tmp / "diagram_high.json"
        surya_diagram_path.write_text(json.dumps({"input": {"page": 1}, "result": {"blocks": [{"label": "Diagram", "bbox": [0, 0, 60, 20], "html": "<table><tr><td><b>A</b></td><td>→</td><td><b>B</b></td></tr></table>"}]}}), encoding="utf-8")
        pdf_base_path = self.tmp / "pdf_base.json"
        pdf_base_path.write_text(json.dumps({"schema_version": "common_ir_pdf_base_v2", "document": {"source_format": "pdf", "source_path": "x.pdf", "source_notice_key": "PBLN_TEST", "scope": "whole_document"}, "blocks": [], "relations": []}), encoding="utf-8")
        diagram_edges_path = self.tmp / "diagram_edges.json"
        subprocess.run([sys.executable, str(PROMOTE_SCRIPT), "--pdf-base", str(pdf_base_path), "--native", str(native_path), "--render-manifest", str(render_path), "--surya-diagram-evidence", str(surya_diagram_path), "--output", str(diagram_edges_path)], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        enriched_path = self.tmp / "enriched.json"
        enriched_path.write_text(json.dumps({"schema_version": "common_ir_v0_1", "document": {"document_id": "pdf:PBLN_TEST", "source_kind": "pdf", "artifact_role": "production"}, "blocks": [], "conflicts": [], "relations": []}), encoding="utf-8")
        output_path = self.tmp / "v1.json"
        _run_adapt(self.tmp, "PBLN_TEST", native_path, render_path, enriched_path, diagram_edges_path, output_path)
        first = output_path.read_text(encoding="utf-8")
        _run_adapt(self.tmp, "PBLN_TEST", native_path, render_path, enriched_path, diagram_edges_path, output_path)
        second = output_path.read_text(encoding="utf-8")
        self.assertEqual(first, second)


@unittest.skipUnless(all(p.is_file() for p in (NATIVE_125098, RENDER_125098, SURYA_ALL_PAGES_125098, ENRICHED_125098, DIAGRAM_EDGES_125098)), "125098 real evidence not present in this checkout")
class RealRouteBSmokeTests(unittest.TestCase):
    def test_125098_route_b_relations_stay_partial_inferred_never_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "125098_v1.json"
            summary = _run_adapt(Path(tmp), "PBLN_000000000125098", NATIVE_125098, RENDER_125098, ENRICHED_125098, DIAGRAM_EDGES_125098, output_path)
            self.assertEqual(summary["relations"], 5)
            self.assertEqual(summary["validation_errors"], 0)

            doc = json.loads(output_path.read_text(encoding="utf-8"))
            diagram_relations = [r for r in doc["relations"] if r["kind"] == "diagram_edge"]
            self.assertEqual(len(diagram_relations), 5)
            for r in diagram_relations:
                self.assertTrue(r["inferred"])
                self.assertEqual(r["structure_status"], "partial")
                self.assertEqual(r["review_status"], "needs_review")
                self.assertIn("from_node", r)
                self.assertIn("to_node", r)
                self.assertNotIn("from_id", r)  # never silently promoted to the explicit shape

            occurrence_ids = {o["occurrence_id"] for b in doc["blocks"] for o in b["occurrences"]}
            for r in diagram_relations:
                for eid in r["evidence_ids"]:
                    self.assertIn(eid, occurrence_ids)
                for node in (r["from_node"], r["to_node"]):
                    for oid in node["occurrence_ids"]:
                        self.assertIn(oid, occurrence_ids)


@unittest.skipUnless(all(p.is_file() for p in (NATIVE_125090, RENDER_125090, SURYA_ALL_PAGES_125090, ENRICHED_125090, DIAGRAM_EDGES_125090)), "125090 real evidence not present in this checkout")
class RealRouteANegativeSmokeTests(unittest.TestCase):
    def test_125090_direct_route_records_zero_edges_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "125090_v1.json"
            summary = _run_adapt(Path(tmp), "PBLN_000000000125090", NATIVE_125090, RENDER_125090, ENRICHED_125090, DIAGRAM_EDGES_125090, output_path)
            self.assertEqual(summary["relations"], 0)
            self.assertEqual(summary["validation_errors"], 0)
            doc = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual([r for r in doc["relations"] if r["kind"] == "diagram_edge"], [])


if __name__ == "__main__":
    unittest.main()
