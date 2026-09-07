#!/usr/bin/env python3
"""Promote diagram edges into a PDF Base IR (common_ir_pdf_base_v2), from two
independent, evidence-only routes. No notice/page/node index is hardcoded;
every input is a path argument, so this is a generic, reusable CLI.

Route A — direct arrow (explicit):
    Surya high-accuracy Diagram HTML (`--surya-diagram-evidence`) is parsed by
    diagram_direct_arrow.py for literal arrow-linked node pairs. Each pair's
    two labels are resolved to native pdf-inspector occurrences inside the
    diagram's own bbox (diagram_native_node_match.py). A node counts as
    resolved only when it matches *exactly one* native occurrence
    (mapping_status=="unique"); a duplicate label (2+ identical-text
    occurrences -> "ambiguous") or no match at all ("unresolved") is never
    guessed at. If every edge in one diagram block resolves both ends
    uniquely, its relations are added with kind=diagram_edge, inferred=false,
    structure_status=explicit; otherwise that whole diagram's direct
    promotion is abstained (nothing added for it, logged in `notes`).

Route B — LLM candidate, native-occurrence corroborated (partial):
    A pre-existing diagram_relation_candidate_sidecar JSON
    (`--llm-candidate`) already carries structured candidate nodes/edges from
    a *prior* LLM call over the Diagram's alt-text description — this script
    never calls an LLM, and the candidate *is* the relation proposal; native
    occurrences only corroborate it, they never select among alternatives.
    Every edge from a well-formed sidecar is preserved as
    kind=diagram_edge, inferred=true, structure_status=partial,
    review_status=needs_review — this route never drops an edge for
    ambiguous or missing node evidence. Instead each endpoint carries a
    from_node/to_node object recording its label, mapping_status
    (unique/ambiguous/unresolved) and the *complete* list of eligible native
    occurrence_ids (empty when unresolved) — nothing is fabricated and
    nothing is silently chosen. relation.ambiguous_evidence is true whenever
    either endpoint is not uniquely resolved, as a fast filter for review.

Route A abstains a whole diagram (adds nothing) the moment any one edge in
it has a non-unique endpoint. Route B never abstains at the edge or diagram
level — see above.

common_ir_pdf_base_v2 has no published JSON Schema in this repo (confirmed:
no config/*pdf_base*schema* file, and build_pdf_base_ir_from_surya_blocks.py
writes it without a validator call) — extra fields like structure_status
already appear in hand-built pdf_base v2 relations (see
scripts/build_pdf_base_ir_from_surya_blocks.py's --direct-diagram-config
path), so this script's relation shape is consistent with existing practice
at this stage. See the module docstring end / README notes for why an
inferred=true relation cannot yet be promoted past this stage into the
published common_ir_v0_1+ schema unchanged.

Copy-on-write: --pdf-base is only ever read, never modified; --output is a
new file. Re-running with the same inputs is idempotent (byte-identical
output) because this script always rebuilds its own relations/evidence
blocks from scratch by relation_id/block_id, rather than accumulating them
onto a previous --output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_direct_arrow import extract_direct_arrow_edges
from diagram_native_node_match import collect_bbox_native_candidates, diagram_pdf_bbox, resolve_label

OWNED_RELATION_PREFIXES = ("rel:diagram:p",)  # regenerated fresh every run; see main()


def iou(a, b) -> float:
    x0 = max(a[0], b[0]); y0 = max(a[1], b[1]); x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    area = lambda z: max(0, z[2] - z[0]) * max(0, z[3] - z[1])
    return inter / (area(a) + area(b) - inter) if area(a) + area(b) - inter else 0


def iter_diagram_blocks_from_surya_evidence(path: Path):
    """Yields (page, block_index_on_page, render_bbox, html) for every
    label=='Diagram' block found in a Surya evidence file. Accepts either
    shape produced by this project's own scripts:
      - single-page: {"input": {"page": N}, "result": {"blocks": [...]}}
        (scripts/run_surya_targeted_structure_pages.py's per-page sibling /
        the *_diagram_high.json convention)
      - multi-page: {"pages": [{"page": N, "high_accuracy_result":
        {"blocks": [...]}}, ...]} (run_surya_targeted_structure_pages.py's
        own output, i.e. the production run_pdf_common_ir.py step 08
        artifact)
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if "pages" in data:
        page_entries = [(entry["page"], entry["high_accuracy_result"]["blocks"]) for entry in data["pages"]]
    elif "result" in data and "input" in data:
        page_entries = [(data["input"]["page"], data["result"]["blocks"])]
    else:
        print(f"warning: unrecognized Surya evidence shape, skipping: {path}", file=sys.stderr)
        return
    for page, blocks in page_entries:
        counter = 0
        for block in blocks:
            if block.get("label") != "Diagram":
                continue
            yield page, counter, block.get("bbox"), block.get("html", ""), str(path)
            counter += 1


def find_or_append_evidence_block(pdf_base: dict, evidence_id: str, page: int, pdf_bbox, html: str, source_location: str, structure_status: str) -> str:
    existing = next((b for b in pdf_base["blocks"] if b["id"] == evidence_id), None)
    if existing is not None:
        # Reuse; but a later, more successful run may need to upgrade
        # partial -> explicit (or vice versa on a fresh empty rebuild).
        existing["structure_status"] = structure_status
        return evidence_id
    pdf_base["blocks"].append({
        "id": evidence_id, "kind": "diagram_candidate", "layout_label": "Diagram",
        "text": html, "structure_status": structure_status,
        "provenance": {
            "page": page, "source_location": source_location, "bbox": pdf_bbox,
            "coordinate_space": "pdf_user_space", "render_bbox": None,
            "extraction_method": "surya_targeted_high_accuracy",
        },
        "preservation_note": "Direct Diagram evidence. Arrow symbols are retained as observed OCR output.",
    })
    return evidence_id


def run_direct_arrow_route(pdf_base: dict, native_items: list[dict], render, evidence_paths: list[Path]) -> tuple[list[dict], list[str]]:
    """Explicit route: promotes only when *every* edge in a diagram resolves
    both endpoints to exactly one native occurrence each. A duplicate label
    (mapping_status=="ambiguous") or a label with no native match at all
    ("unresolved") aborts that whole diagram's direct promotion -- never a
    guess, never a first-occurrence pick among duplicates."""
    relations, notes = [], []
    for evidence_path in evidence_paths:
        for page, block_index, render_bbox, html, source_location in iter_diagram_blocks_from_surya_evidence(evidence_path):
            candidate_edges = extract_direct_arrow_edges(html)
            if not candidate_edges:
                continue  # nothing parseable (e.g. a bare <img/>) -> no evidence block, no relations
            height = next(p["height"] for p in render["pages"] if p["page"] == page)
            pdf_bbox = diagram_pdf_bbox(render_bbox, render["render_scale"], height)
            native_candidates = collect_bbox_native_candidates(native_items, page, pdf_bbox)

            resolved = []
            abstain_reason = None
            for edge in candidate_edges:
                from_evidence = resolve_label(edge.from_label, native_candidates)
                to_evidence = resolve_label(edge.to_label, native_candidates)
                if from_evidence.mapping_status != "unique" or to_evidence.mapping_status != "unique":
                    abstain_reason = (
                        f"non-unique node label(s): from={edge.from_label!r} status={from_evidence.mapping_status} "
                        f"({len(from_evidence.matches)} match(es)); to={edge.to_label!r} status={to_evidence.mapping_status} "
                        f"({len(to_evidence.matches)} match(es))"
                    )
                    break
                resolved.append((edge, from_evidence, to_evidence))

            evidence_id = f"occ:surya:p{page}:diagram_high:{block_index}"
            if abstain_reason:
                notes.append(f"[direct] abstain page={page} block={block_index} source={evidence_path.name}: {abstain_reason}")
                find_or_append_evidence_block(pdf_base, evidence_id, page, pdf_bbox, html, f"{source_location}:page{page}:diagram_block{block_index}", "partial")
                continue

            find_or_append_evidence_block(pdf_base, evidence_id, page, pdf_bbox, html, f"{source_location}:page{page}:diagram_block{block_index}", "explicit")
            for n, (edge, from_evidence, to_evidence) in enumerate(resolved, 1):
                relations.append({
                    "relation_id": f"rel:diagram:p{page}:direct:{n}",
                    "kind": "diagram_edge",
                    "from_id": from_evidence.occurrence_ids(page)[0],
                    "to_id": to_evidence.occurrence_ids(page)[0],
                    "evidence_ids": [evidence_id],
                    "inferred": False,
                    "structure_status": "explicit",
                    "observed_arrow": edge.arrow,
                    "provenance": {
                        "method": "surya_diagram_html_direct_arrow_native_occurrence_match",
                        "page": page, "bbox": None, "coordinate_space": None,
                        "source_location": f"{source_location}:page{page}:diagram_block{block_index}",
                    },
                })
            notes.append(f"[direct] promoted page={page} block={block_index} source={evidence_path.name}: {len(resolved)} edge(s)")
    return relations, notes


def run_llm_candidate_route(pdf_base: dict, native_items: list[dict], render, candidate_paths: list[Path]) -> tuple[list[dict], list[str]]:
    """Candidate route: the sidecar IS the relation proposal (an LLM already
    read only the Surya alt-text description; this script calls no LLM).
    Native occurrences are corroboration only, never a selection mechanism:
    every edge from a well-formed sidecar is preserved as a partial,
    inferred=true relation, with each endpoint's *full* set of eligible
    native occurrences recorded (mapping_status unique/ambiguous/unresolved)
    instead of picking one. A relation is only ever dropped when the sidecar
    itself is missing the page/bbox/edges needed to even attempt this."""
    relations, notes = [], []
    for candidate_path in candidate_paths:
        sidecar = json.loads(candidate_path.read_text(encoding="utf-8"))
        source = sidecar.get("source", {})
        page = source.get("page")
        block_index = source.get("block_index")
        render_bbox = source.get("bbox_render")
        output = sidecar.get("response", {}).get("output", {})
        candidate_edges = output.get("edges", [])
        if page is None or render_bbox is None or not candidate_edges:
            notes.append(f"[llm] skip source={candidate_path.name}: no page/bbox/edges in sidecar (nothing to attach native evidence to)")
            continue

        height = next(p["height"] for p in render["pages"] if p["page"] == page)
        pdf_bbox = diagram_pdf_bbox(render_bbox, render["render_scale"], height)
        native_candidates = collect_bbox_native_candidates(native_items, page, pdf_bbox)

        evidence_id = f"occ:surya:p{page}:b{block_index}" if block_index is not None else None
        existing_evidence = evidence_id and any(b["id"] == evidence_id for b in pdf_base["blocks"])
        evidence_ids = [evidence_id] if existing_evidence else []
        if not evidence_ids:
            notes.append(f"[llm] warning page={page} source={candidate_path.name}: expected Surya evidence block {evidence_id!r} not found in --pdf-base; relation will cite the sidecar file only")

        counts = {"unique": 0, "ambiguous": 0, "unresolved": 0}
        for n, edge in enumerate(candidate_edges, 1):
            from_evidence = resolve_label(edge.get("from_label", ""), native_candidates)
            to_evidence = resolve_label(edge.get("to_label", ""), native_candidates)
            counts[from_evidence.mapping_status] += 1
            counts[to_evidence.mapping_status] += 1
            ambiguous_evidence = from_evidence.mapping_status != "unique" or to_evidence.mapping_status != "unique"

            def node_payload(evidence):
                return {
                    "label": evidence.label,
                    "mapping_status": evidence.mapping_status,
                    "occurrence_ids": evidence.occurrence_ids(page),
                }

            relations.append({
                "relation_id": f"rel:diagram:p{page}:llm_candidate:{n}",
                "kind": "diagram_edge",
                "from_node": node_payload(from_evidence),
                "to_node": node_payload(to_evidence),
                "evidence_ids": evidence_ids or [str(candidate_path)],
                "inferred": True,
                "structure_status": "partial",
                "review_status": "needs_review",
                "ambiguous_evidence": ambiguous_evidence,
                "llm_provenance": {
                    "model_requested": sidecar.get("request", {}).get("model_requested"),
                    "model_reported": sidecar.get("response", {}).get("model_reported"),
                    "candidate_source": str(candidate_path),
                    "candidate_status": output.get("status"),
                    "evidence_phrase": edge.get("evidence_phrase"),
                },
                "provenance": {
                    "method": "llm_diagram_candidate_native_occurrence_corroboration",
                    "page": page, "bbox": None, "coordinate_space": None,
                    "source_location": str(candidate_path),
                },
            })
        notes.append(f"[llm] preserved page={page} source={candidate_path.name}: {len(candidate_edges)} candidate edge(s); node mapping_status counts={counts}")
    return relations, notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf-base", type=Path, required=True, help="common_ir_pdf_base_v2 JSON to enrich (read-only)")
    parser.add_argument("--native", type=Path, required=True, help="raw pdf-inspector JSON (native text_items)")
    parser.add_argument("--render-manifest", type=Path, required=True)
    parser.add_argument("--surya-diagram-evidence", type=Path, nargs="*", default=[], help="0+ Surya high-accuracy Diagram evidence JSON files (route A)")
    parser.add_argument("--llm-candidate", type=Path, nargs="*", default=[], help="0+ existing diagram_relation_candidate_sidecar JSON files (route B; never calls an LLM)")
    parser.add_argument("--output", type=Path, required=True, help="new enriched pdf_base v2 JSON (copy-on-write; --pdf-base is never modified)")
    args = parser.parse_args()

    pdf_base = json.loads(args.pdf_base.read_text(encoding="utf-8"))
    native = json.loads(args.native.read_text(encoding="utf-8"))
    render = json.loads(args.render_manifest.read_text(encoding="utf-8"))
    native_items = native["text_items"]

    # Idempotency: drop any relations this script previously generated
    # (by id prefix) before regenerating, so re-running never duplicates.
    pdf_base["relations"] = [r for r in pdf_base.get("relations", []) if not r["relation_id"].startswith(OWNED_RELATION_PREFIXES)]

    direct_relations, direct_notes = run_direct_arrow_route(pdf_base, native_items, render, args.surya_diagram_evidence)
    llm_relations, llm_notes = run_llm_candidate_route(pdf_base, native_items, render, args.llm_candidate)

    existing_ids = {r["relation_id"] for r in pdf_base["relations"]}
    for relation in direct_relations + llm_relations:
        if relation["relation_id"] in existing_ids:
            raise SystemExit(f"relation_id collision, refusing to overwrite: {relation['relation_id']}")
        existing_ids.add(relation["relation_id"])
    pdf_base["relations"].extend(direct_relations)
    pdf_base["relations"].extend(llm_relations)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(pdf_base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "output": str(args.output),
        "direct_edges": len(direct_relations), "llm_candidate_edges": len(llm_relations),
        "notes": direct_notes + llm_notes,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
