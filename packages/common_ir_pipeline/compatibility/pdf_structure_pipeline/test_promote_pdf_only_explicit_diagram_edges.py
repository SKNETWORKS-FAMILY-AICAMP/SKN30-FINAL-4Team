#!/usr/bin/env python3
"""Tests for promote_pdf_only_explicit_diagram_edges.py.

Reuses only already-captured raw evidence (125090 direct-arrow positive,
125098 p3 <img/> negative + its existing LLM candidate sidecar). No GPU, no
Surya/vLLM/Docker, no new LLM call, no .env/local.env access.

Run from the repo root (common_ir_v0_1_schema.py resolves its schema path
relative to cwd, matching the rest of this codebase's convention):
    python3 exploratory_study/results/runpod_hybrid_ir_validation/scripts/test_promote_pdf_only_explicit_diagram_edges.py
"""
from __future__ import annotations

import hashlib
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

PROMOTE_SCRIPT = SCRIPTS / "promote_pdf_only_explicit_diagram_edges.py"

PDF_BASE_125090 = ROOT / "common_ir/pdf/PBLN_000000000125090_full_document_pdf_base_ir_v2.json"
PDF_BASE_125098 = ROOT / "common_ir/pdf/PBLN_000000000125098_full_document_pdf_base_ir_v2.json"
NATIVE_125090 = ROOT / "raw/pdf_inspector/PBLN_000000000125090.json"
NATIVE_125098 = ROOT / "raw/pdf_inspector/PBLN_000000000125098.json"
RENDER_125090 = ROOT / "runs/batch_b_native_20260826T063000Z/rendered/125090/render_manifest.json"
RENDER_125098 = ROOT / "runs/batch_b_native_20260826T063000Z/rendered/125098/render_manifest.json"
SURYA_DIAGRAM_125090 = ROOT / "runs/batch_b_native_20260826T063000Z/surya_targeted/PBLN_000000000125090_p3_diagram_high.json"
SURYA_DIAGRAM_125098 = ROOT / "runs/batch_b_native_20260826T063000Z/surya_targeted/PBLN_000000000125098_p3_diagram_high.json"
LLM_CANDIDATE_125098_V2 = ROOT / "runs/batch_b_native_20260826T063000Z/diagram_candidates/PBLN_000000000125098_p3_llm_candidate_v2.json"

REQUIRED_FIXTURES = [PDF_BASE_125090, PDF_BASE_125098, NATIVE_125090, NATIVE_125098, RENDER_125090, RENDER_125098, SURYA_DIAGRAM_125090, SURYA_DIAGRAM_125098, LLM_CANDIDATE_125098_V2]


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _run(pdf_base: Path, native: Path, render: Path, output: Path, surya_evidence=(), llm_candidates=()) -> dict:
    cmd = [sys.executable, str(PROMOTE_SCRIPT), "--pdf-base", str(pdf_base), "--native", str(native), "--render-manifest", str(render), "--output", str(output)]
    if surya_evidence:
        cmd += ["--surya-diagram-evidence", *[str(p) for p in surya_evidence]]
    if llm_candidates:
        cmd += ["--llm-candidate", *[str(p) for p in llm_candidates]]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"script failed (rc={result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return json.loads(result.stdout)


@unittest.skipUnless(all(p.is_file() for p in REQUIRED_FIXTURES), "raw evidence not present in this checkout")
class DiagramPromotionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    # ---- Route A: direct arrow -----------------------------------------

    def test_125090_direct_route_abstains_because_one_node_label_is_split_across_two_native_runs(self):
        # Honest result, not a forced one: "사업계획서/IR 피칭 멘토링" is the
        # bold title of the mentoring node cell, but the real PDF holds it as
        # two separate native text runs ("사업계획서" and "/IR 피칭 멘토링");
        # neither equals the full label, so it has zero (not one) exact
        # native match. Per the no-picking, no-concatenation-guessing rule,
        # that one non-unique endpoint aborts the *whole* diagram's direct
        # promotion -- 0 edges, not 5. This regression-locks that the fix in
        # diagram_native_node_match.py actually removed the old
        # first-occurrence/concatenation shortcuts rather than only
        # reformatting them.
        out = self.tmp_path / "125090_direct.json"
        summary = _run(PDF_BASE_125090, NATIVE_125090, RENDER_125090, out, surya_evidence=[SURYA_DIAGRAM_125090])
        self.assertEqual(summary["direct_edges"], 0)
        self.assertEqual(summary["llm_candidate_edges"], 0)
        self.assertTrue(any("abstain" in note and "unresolved" in note for note in summary["notes"]))
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([r for r in doc["relations"] if r["kind"] == "diagram_edge"], [])

    def test_direct_route_promotes_when_every_node_is_uniquely_matched(self):
        # Positive control for the explicit path: a minimal synthetic
        # 2-node diagram where both labels are genuinely unique on the
        # page. Proves the strict single-match rule still succeeds when it
        # honestly can, not just that it now abstains more often.
        tmp = self.tmp_path
        pdf_base = tmp / "pdf_base.json"
        pdf_base.write_text(json.dumps({"schema_version": "common_ir_pdf_base_v2", "document": {"source_format": "pdf", "source_path": "x.pdf", "source_notice_key": "PBLN_TEST", "scope": "whole_document"}, "blocks": [], "relations": []}), encoding="utf-8")
        native = tmp / "native.json"
        native.write_text(json.dumps({"text_items": [
            {"page": 1, "text": "A", "x": 0, "y": 90, "width": 10, "height": 10},
            {"page": 1, "text": "B", "x": 50, "y": 90, "width": 10, "height": 10},
        ]}), encoding="utf-8")
        render = tmp / "render_manifest.json"
        render.write_text(json.dumps({"render_scale": 1.0, "pages": [{"page": 1, "height": 100}]}), encoding="utf-8")
        surya = tmp / "diagram_high.json"
        surya.write_text(json.dumps({"input": {"page": 1}, "result": {"blocks": [{"label": "Diagram", "bbox": [0, 0, 60, 20], "html": "<table><tr><td><b>A</b></td><td>→</td><td><b>B</b></td></tr></table>"}]}}), encoding="utf-8")

        out = tmp / "out.json"
        summary = _run(pdf_base, native, render, out, surya_evidence=[surya])
        self.assertEqual(summary["direct_edges"], 1)
        doc = json.loads(out.read_text(encoding="utf-8"))
        relations = [r for r in doc["relations"] if r["kind"] == "diagram_edge"]
        self.assertEqual(len(relations), 1)
        r = relations[0]
        self.assertEqual(r["from_id"], "occ:inspector:p1:t0")
        self.assertEqual(r["to_id"], "occ:inspector:p1:t1")
        self.assertFalse(r["inferred"])
        self.assertEqual(r["structure_status"], "explicit")

    def test_125098_direct_route_abstains_on_bare_img(self):
        out = self.tmp_path / "125098_direct.json"
        before_block_count = len(json.loads(PDF_BASE_125098.read_text(encoding="utf-8"))["blocks"])
        summary = _run(PDF_BASE_125098, NATIVE_125098, RENDER_125098, out, surya_evidence=[SURYA_DIAGRAM_125098])
        self.assertEqual(summary["direct_edges"], 0)
        self.assertEqual(summary["llm_candidate_edges"], 0)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([r for r in doc["relations"] if r["kind"] == "diagram_edge"], [])
        # No new evidence block either -- nothing parseable to record.
        self.assertEqual(len(doc["blocks"]), before_block_count)

    # ---- Route B: LLM candidate, native-occurrence verified -------------

    def test_125098_llm_candidate_route_preserves_all_five_edges_with_full_node_evidence(self):
        # This route must never pick a winner and never drop an edge: every
        # one of the sidecar's 5 candidate edges survives, each endpoint
        # carrying its own mapping_status and the *complete* set of eligible
        # native occurrence_ids rather than a single chosen one.
        out = self.tmp_path / "125098_llm.json"
        summary = _run(PDF_BASE_125098, NATIVE_125098, RENDER_125098, out, llm_candidates=[LLM_CANDIDATE_125098_V2])
        self.assertEqual(summary["direct_edges"], 0)
        self.assertEqual(summary["llm_candidate_edges"], 5)

        doc = json.loads(out.read_text(encoding="utf-8"))
        diagram_relations = sorted((r for r in doc["relations"] if r["kind"] == "diagram_edge"), key=lambda r: r["relation_id"])
        self.assertEqual(len(diagram_relations), 5)
        for r in diagram_relations:
            self.assertTrue(r["inferred"])
            self.assertEqual(r["structure_status"], "partial")
            self.assertEqual(r["review_status"], "needs_review")
            self.assertIn("from_node", r)
            self.assertIn("to_node", r)
            self.assertNotIn("from_id", r)
            self.assertNotIn("to_id", r)
            self.assertIn(r["from_node"]["mapping_status"], {"unique", "ambiguous", "unresolved"})
            self.assertIn(r["to_node"]["mapping_status"], {"unique", "ambiguous", "unresolved"})
            self.assertIn("llm_provenance", r)
            self.assertEqual(r["llm_provenance"]["candidate_status"], "candidate")
            self.assertTrue(all(e.startswith("occ:surya:p3:b") for e in r["evidence_ids"]))

        labels = [(r["from_node"]["label"], r["to_node"]["label"]) for r in diagram_relations]
        self.assertEqual(labels, [
            ("모집공고", "연장공고(9일)"),
            ("연장공고(9일)", "접수"),
            ("접수", "선정평가"),
            ("선정평가", "결과통보"),
            ("결과통보", "협약진행"),
        ])

        # "접수" genuinely occurs twice on this page -> preserved as
        # ambiguous with BOTH occurrence ids, never reduced to one.
        entrance_from = diagram_relations[2]["from_node"]  # 접수 -> 선정평가
        self.assertEqual(entrance_from["label"], "접수")
        self.assertEqual(entrance_from["mapping_status"], "ambiguous")
        self.assertEqual(set(entrance_from["occurrence_ids"]), {"occ:inspector:p3:t64", "occ:inspector:p3:t70"})
        self.assertTrue(diagram_relations[2]["ambiguous_evidence"])

        # "연장공고(9일)" never matches as a single native run (the PDF
        # splits it into "연장공고" + "(9일)") -> unresolved, not fabricated
        # by stitching the two runs together, and not dropped either.
        extension_to = diagram_relations[0]["to_node"]  # 모집공고 -> 연장공고(9일)
        self.assertEqual(extension_to["label"], "연장공고(9일)")
        self.assertEqual(extension_to["mapping_status"], "unresolved")
        self.assertEqual(extension_to["occurrence_ids"], [])
        self.assertTrue(diagram_relations[0]["ambiguous_evidence"])

        # The two edges whose both endpoints are cleanly unique (선정평가->
        # 결과통보, 결과통보->협약진행) are still inferred=true/partial (LLM
        # provenance always needs review) but ambiguous_evidence is False.
        self.assertFalse(diagram_relations[3]["ambiguous_evidence"])
        self.assertFalse(diagram_relations[4]["ambiguous_evidence"])

    def test_combined_run_both_routes_on_125098_p3(self):
        out = self.tmp_path / "125098_combined.json"
        summary = _run(PDF_BASE_125098, NATIVE_125098, RENDER_125098, out, surya_evidence=[SURYA_DIAGRAM_125098], llm_candidates=[LLM_CANDIDATE_125098_V2])
        self.assertEqual(summary["direct_edges"], 0)
        self.assertEqual(summary["llm_candidate_edges"], 5)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len([r for r in doc["relations"] if r["kind"] == "diagram_edge"]), 5)

    # ---- Copy-on-write / idempotency -------------------------------------

    def test_input_pdf_base_never_mutated(self):
        before = _md5(PDF_BASE_125090)
        out = self.tmp_path / "125090_cow.json"
        _run(PDF_BASE_125090, NATIVE_125090, RENDER_125090, out, surya_evidence=[SURYA_DIAGRAM_125090])
        self.assertEqual(_md5(PDF_BASE_125090), before)

    def test_rerun_is_idempotent(self):
        out = self.tmp_path / "125090_repeat.json"
        _run(PDF_BASE_125090, NATIVE_125090, RENDER_125090, out, surya_evidence=[SURYA_DIAGRAM_125090])
        first = out.read_text(encoding="utf-8")
        _run(PDF_BASE_125090, NATIVE_125090, RENDER_125090, out, surya_evidence=[SURYA_DIAGRAM_125090])
        second = out.read_text(encoding="utf-8")
        self.assertEqual(first, second)


@unittest.skipUnless((REPO_ROOT / "exploratory_study/results/runpod_hybrid_ir_validation/config/common_ir_v0_1.schema.json").is_file(), "v0.1 schema config not present")
class SchemaGapDemonstrationTests(unittest.TestCase):
    """Concretely documents why route B's relations cannot yet be promoted
    unchanged past the pdf_base stage into the published common_ir_v0_1+
    schema, using the *real* schema validator already in this repo."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts"))
        import importlib
        cls.v0_1_schema = importlib.import_module("common_ir_v0_1_schema")

    def _stub_document(self, relation: dict) -> dict:
        # Minimal, otherwise-valid v0.1 document shell with exactly the two
        # native occurrences a diagram_edge relation needs to exist.
        occ_a = {"occurrence_id": "occ:inspector:p3:t0", "role": "native_text", "text": "A", "provenance": {"method": "pdf_hybrid", "page": 3, "bbox": [0, 0, 1, 1], "coordinate_space": "pdf_user_space", "source_location": "x"}}
        occ_b = {"occurrence_id": "occ:inspector:p3:t1", "role": "native_text", "text": "B", "provenance": occ_a["provenance"]}
        evidence_block = {"block_id": "occ:surya:p3:b0", "kind": "diagram_candidate", "structure_status": "explicit", "occurrences": [{"occurrence_id": "occ:surya:p3:b0", "role": "layout_region", "provenance": occ_a["provenance"]}], "provenance": occ_a["provenance"]}
        return {
            "schema_version": "common_ir_v0_1",
            "document": {"document_id": "pdf:PBLN_TEST", "source_kind": "pdf", "artifact_role": "production", "page_count": 3, "raw_artifact_ids": [], "provenance": occ_a["provenance"]},
            "blocks": [
                {"block_id": "occ:inspector:p3:t0", "kind": "paragraph", "structure_status": "explicit", "occurrences": [occ_a], "provenance": occ_a["provenance"]},
                {"block_id": "occ:inspector:p3:t1", "kind": "paragraph", "structure_status": "explicit", "occurrences": [occ_b], "provenance": occ_a["provenance"]},
                evidence_block,
            ],
            "conflicts": [],
            "relations": [relation],
        }

    def test_route_a_shaped_relation_passes_v0_1_schema(self):
        # This mirrors exactly what adapt_pdf_base_to_common_ir_v0_1.py's
        # relation conversion keeps (relation_id/kind/from_id/to_id/
        # evidence_ids/inferred/provenance/observed_arrow) -- it already
        # drops pdf_base-only extras like structure_status on the way in,
        # so a route-A relation is schema-clean once adapted.
        relation = {
            "relation_id": "rel:diagram:p3:direct:1", "kind": "diagram_edge",
            "from_id": "occ:inspector:p3:t0", "to_id": "occ:inspector:p3:t1",
            "evidence_ids": ["occ:surya:p3:b0"], "inferred": False,
            "provenance": {"method": "surya_diagram_html_direct_arrow_native_occurrence_match", "page": 3, "bbox": None, "coordinate_space": None, "source_location": "x"},
            "observed_arrow": "→",
        }
        errors = self.v0_1_schema.validation_errors(self._stub_document(relation))
        self.assertEqual(errors, [])

    def test_route_b_shaped_relation_fails_v0_1_schema_today(self):
        # The *raw* pdf_base-level shape this script emits for route B is
        # what a naive "just copy relations[] forward" promotion would
        # produce: inferred=True, from_node/to_node evidence objects instead
        # of from_id/to_id strings (so an ambiguous/unresolved endpoint is
        # never silently collapsed to one occurrence), plus
        # structure_status/review_status/ambiguous_evidence/llm_provenance.
        # It must fail today's schema on *both* counts (missing required
        # from_id/to_id, and inferred != false) -- this test exists to make
        # that limitation concrete and regression-checked, not to assert a
        # wish.
        relation = {
            "relation_id": "rel:diagram:p3:llm_candidate:1", "kind": "diagram_edge",
            "from_node": {"label": "A", "mapping_status": "unique", "occurrence_ids": ["occ:inspector:p3:t0"]},
            "to_node": {"label": "B", "mapping_status": "ambiguous", "occurrence_ids": ["occ:inspector:p3:t1", "occ:inspector:p3:t2"]},
            "evidence_ids": ["occ:surya:p3:b0"], "inferred": True,
            "structure_status": "partial", "review_status": "needs_review", "ambiguous_evidence": True,
            "llm_provenance": {"model_requested": "x"},
            "provenance": {"method": "llm_diagram_candidate_native_occurrence_corroboration", "page": 3, "bbox": None, "coordinate_space": None, "source_location": "x"},
        }
        errors = self.v0_1_schema.validation_errors(self._stub_document(relation))
        self.assertTrue(errors, "expected the published v0.1 schema to reject a from_node/to_node, inferred=true diagram_edge relation")

    def test_route_b_relation_is_schema_clean_once_moved_to_candidate_relations(self):
        # The schema's own designated home for exactly this content
        # (inferred, LLM-sourced, human-review-pending) is
        # block.candidate_relations[] with kind=diagram_relation_candidate,
        # status=candidate -- not top-level relations[]. Confirms that path
        # is already schema-valid today, as the concrete alternative to
        # extending the relation schema.
        base = self._stub_document({
            "relation_id": "rel:diagram:p3:direct:1", "kind": "diagram_edge",
            "from_id": "occ:inspector:p3:t0", "to_id": "occ:inspector:p3:t1",
            "evidence_ids": ["occ:surya:p3:b0"], "inferred": False,
            "provenance": {"method": "x", "page": 3, "bbox": None, "coordinate_space": None, "source_location": "x"},
        })
        base["relations"] = []
        base["blocks"][2]["candidate_relations"] = [{
            "candidate_id": "cand:diagram:p3:1", "kind": "diagram_relation_candidate", "status": "candidate",
            "method": "llm_diagram_candidate_native_occurrence_verified", "input_scope": "diagram html alt text",
            "nodes": [{"node_id": "n0", "label": "A", "source_phrase": "A"}, {"node_id": "n1", "label": "B", "source_phrase": "B"}],
            "edges": [{"from_node_id": "n0", "to_node_id": "n1", "evidence_phrase": "A then B"}],
            "uncertainties": ["order inference"],
            "provenance": {"method": "x", "page": 3, "bbox": None, "coordinate_space": None, "source_location": "x"},
        }]
        # candidate_relations is only defined in the v0.3-derived schema
        # (finalize_common_ir_v0_3.py), not the v0.1 one -- validate with
        # plain jsonschema against that schema builder directly.
        sys.path.insert(0, str(REPO_ROOT / "scripts/pipeline"))
        import importlib
        finalize = importlib.import_module("finalize_common_ir_v0_3")
        base["schema_version"] = "common_ir_v0_3"
        errors = finalize.validation_errors(base)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
