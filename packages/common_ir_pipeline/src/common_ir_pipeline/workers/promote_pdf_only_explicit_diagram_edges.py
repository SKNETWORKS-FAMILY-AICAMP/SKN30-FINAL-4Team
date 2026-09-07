"""Promote *only* literally observed Surya Diagram arrows into a PDF sidecar.

This is the production-safe successor of the exploratory script with the same
name.  It accepts Surya Diagram HTML as a diagnostic input, but creates a
relation only when every endpoint has exactly one matching native
pdf-inspector occurrence inside the diagram bbox.  It never calls an LLM,
never picks a duplicate match, and never copies Surya text/HTML into Common
IR semantic text.

The result is consumed by ``common-ir-pdf-native --diagram-relations-pdf-base``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


SIDECAR_SCHEMA = "common_ir_v1_pdf_explicit_diagram_relations_v1"
_WS = re.compile(r"\s+")
RIGHT, LEFT, DOWN, UP = {"→", "⇒"}, {"←", "⇐"}, {"↓", "⇓"}, {"↑", "⇑"}
ALL_ARROWS = RIGHT | LEFT | DOWN | UP


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean(value: str | None) -> str:
    return _WS.sub(" ", value or "").strip()


@dataclass
class Cell:
    lines: list[str] = field(default_factory=lambda: [""])
    bold_lines: list[str] = field(default_factory=lambda: [""])
    rowspan: int = 1
    colspan: int = 1

    def label(self) -> str:
        return next((clean(line) for line in self.bold_lines if clean(line)), next((clean(line) for line in self.lines if clean(line)), ""))

    def arrow_only(self) -> str | None:
        token = clean("".join(self.lines))
        return token if token in ALL_ARROWS else None

    def trailing_vertical_arrow(self) -> str | None:
        token = next((clean(line) for line in reversed(self.lines) if clean(line)), "")
        return token if token in DOWN | UP else None


class DiagramTable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[Cell]] = []
        self.row: list[Cell] | None = None
        self.cell: Cell | None = None
        self.bold_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            values = dict(attrs)
            def span(key: str) -> int:
                try:
                    return max(1, int(values.get(key, 1)))
                except (TypeError, ValueError):
                    return 1
            self.cell = Cell(rowspan=span("rowspan"), colspan=span("colspan"))
        elif tag in {"b", "strong"}:
            self.bold_depth += 1
            if self.cell is not None:
                self.cell.bold_lines.append("")
        elif tag in {"br", "p", "div"} and self.cell is not None:
            self.cell.lines.append("")
            if self.bold_depth:
                self.cell.bold_lines.append("")

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            (self.row if self.row is not None else []).append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag in {"b", "strong"}:
            self.bold_depth = max(0, self.bold_depth - 1)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.lines[-1] += data
            if self.bold_depth:
                self.cell.bold_lines[-1] += data


def direct_edges(html: str) -> list[tuple[str, str, str]]:
    """Return literal arrow-linked labels; return no edges on span ambiguity."""
    if "<table" not in (html or "").lower():
        return []
    parser = DiagramTable()
    parser.feed(html)
    rows = parser.rows
    if not rows or any(cell.rowspan != 1 or cell.colspan != 1 for row in rows for cell in row):
        return []
    edges: list[tuple[str, str, str]] = []
    for row in rows:
        for index, cell in enumerate(row):
            arrow = cell.arrow_only()
            if arrow not in RIGHT | LEFT or not (0 < index < len(row) - 1):
                continue
            before, after = row[index - 1], row[index + 1]
            if before.arrow_only() or after.arrow_only() or not before.label() or not after.label():
                continue
            edges.append((before.label(), after.label(), arrow) if arrow in RIGHT else (after.label(), before.label(), arrow))
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(row):
            arrow = cell.trailing_vertical_arrow()
            next_row = row_index + 1 if arrow in DOWN else row_index - 1
            if not arrow or not 0 <= next_row < len(rows) or col_index >= len(rows[next_row]):
                continue
            other = rows[next_row][col_index]
            if not cell.label() or not other.label():
                continue
            edges.append((cell.label(), other.label(), arrow) if arrow in DOWN else (other.label(), cell.label(), arrow))
    return edges


def pdf_bbox(render_bbox: list[float], scale: float, page_height: float) -> list[float]:
    x0, y0, x1, y1 = render_bbox
    return [x0 / scale, (page_height - y1) / scale, x1 / scale, (page_height - y0) / scale]


def contained_native_items(items: list[dict], page: int, bbox: list[float]) -> list[tuple[int, dict]]:
    x0, y0, x1, y1 = bbox
    result = []
    for index, item in enumerate(items):
        if item.get("page") != page:
            continue
        ix0, iy0 = item["x"], item["y"]
        ix1, iy1 = ix0 + item["width"], iy0 + item["height"]
        if ix0 >= x0 and iy0 >= y0 and ix1 <= x1 and iy1 <= y1:
            result.append((index, item))
    return result


def unique_occurrence_id(label: str, candidates: list[tuple[int, dict]], page: int) -> str | None:
    matches = [index for index, item in candidates if clean(item.get("text")) == clean(label)]
    return f"occ:inspector:p{page}:t{matches[0]}" if len(matches) == 1 else None


def diagrams(evidence_path: Path):
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    if "pages" in data:
        pages = ((entry.get("page"), entry.get("high_accuracy_result", {}).get("blocks", [])) for entry in data["pages"])
    elif "input" in data and "result" in data:
        pages = ((data["input"].get("page"), data["result"].get("blocks", [])),)
    else:
        raise ValueError(f"unrecognized Surya Diagram evidence: {evidence_path}")
    for page, blocks in pages:
        for index, block in enumerate(blocks):
            if block.get("label") == "Diagram" and isinstance(block.get("bbox"), list) and isinstance(block.get("html"), str):
                yield int(page), index, block["bbox"], block["html"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--render-manifest", type=Path, required=True)
    parser.add_argument("--surya-diagram-evidence", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.source_pdf.is_file():
        parser.error(f"source PDF does not exist: {args.source_pdf}")
    native = json.loads(args.native.read_text(encoding="utf-8"))
    render = json.loads(args.render_manifest.read_text(encoding="utf-8"))
    scale = render.get("render_scale")
    heights = {entry["page"]: entry["height"] for entry in render.get("pages", [])}
    if not isinstance(scale, (int, float)) or not heights:
        parser.error("render manifest must contain render_scale and page heights")
    relations, blocks, notes = [], [], []
    for evidence_path in args.surya_diagram_evidence:
        for page, block_index, render_bbox, html in diagrams(evidence_path):
            if page not in heights:
                raise ValueError(f"diagram page {page} is absent from render manifest")
            candidates = contained_native_items(native.get("text_items", []), page, pdf_bbox(render_bbox, scale, heights[page]))
            candidate_edges = direct_edges(html)
            if not candidate_edges:
                continue
            resolved: list[tuple[str, str, str, str, str]] = []
            for from_label, to_label, arrow in candidate_edges:
                from_id, to_id = unique_occurrence_id(from_label, candidates, page), unique_occurrence_id(to_label, candidates, page)
                if from_id is None or to_id is None:
                    resolved = []
                    notes.append(f"abstain page={page} block={block_index}: endpoint not uniquely native-matched")
                    break
                resolved.append((from_id, to_id, arrow, from_label, to_label))
            if not resolved:
                continue
            evidence_id = f"occ:surya:p{page}:diagram_high:{block_index}"
            blocks.append({"id": evidence_id, "kind": "diagram_candidate", "layout_label": "Diagram", "text": "", "structure_status": "explicit", "provenance": {"page": page, "source_location": f"{evidence_path}:page{page}:diagram_block{block_index}", "bbox": pdf_bbox(render_bbox, scale, heights[page]), "coordinate_space": "pdf_user_space", "extraction_method": "surya_diagram_html_direct_arrow_native_occurrence_match"}})
            for edge_index, (from_id, to_id, arrow, _from_label, _to_label) in enumerate(resolved, 1):
                relations.append({"relation_id": f"rel:diagram:p{page}:direct:{block_index}:{edge_index}", "kind": "diagram_edge", "from_id": from_id, "to_id": to_id, "evidence_ids": [evidence_id], "inferred": False, "structure_status": "explicit", "observed_arrow": arrow, "provenance": {"method": "surya_diagram_html_direct_arrow_native_occurrence_match", "page": page, "bbox": None, "coordinate_space": None, "source_location": f"{evidence_path}:page{page}:diagram_block{block_index}"}})
            notes.append(f"promoted page={page} block={block_index}: {len(resolved)} edge(s)")
    output = {"sidecar_schema_version": SIDECAR_SCHEMA, "source": {"source_location": str(args.source_pdf), "source_sha256": sha256_file(args.source_pdf), "native_artifact": str(args.native), "render_manifest": str(args.render_manifest)}, "policy": {"semantic_text_source": "native_pdf_only", "surya_html_allowed_in_common_ir_text": False, "promotion": "literal_arrow_and_unique_native_endpoint_only", "does_not_promote": ["llm_candidate_edges", "ambiguous_endpoints", "unresolved_endpoints", "ocr_text"]}, "blocks": blocks, "relations": relations, "notes": notes}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "explicit_edges": len(relations), "notes": notes}, ensure_ascii=False))


if __name__ == "__main__":
    main()
