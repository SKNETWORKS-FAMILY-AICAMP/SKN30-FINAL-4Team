#!/usr/bin/env python3
"""Create standalone Common IR v1 from one PDF's existing evidence.

One source file -> one output; no paired HWP/HWPX is read or required.
Reuses already-computed evidence rather than re-deriving it:
  - --native (raw pdf-inspector): this adapter turns native PDF text
    fragments (often one per line/styled run) into semantic
    paragraph/heading blocks. Native text is the sole textual evidence
    emitted by Common IR v1.
  - --enriched-common-ir (an existing common_ir_v0_3_pdf_enriched/*.json):
    table/table_candidate/diagram_candidate blocks are carried over
    (already correctly structured by the table gate/coverage/promotion and
    diagram routing work upstream) rather than rebuilt here -- but their
    canonical `text` is (re)assembled by this adapter, not taken from
    --enriched-common-ir as-is: an explicit table's text is its cells'
    own native occurrences in (row_index, col_index) reading order, and a
    table_candidate's text is whichever native text_items sit inside its
    own bbox, in coordinate reading order -- never synthetic cells. OCR
    artifacts may have supplied upstream layout geometry, but their text is
    not copied into this production IR. See _assemble_explicit_table_text /
    _assemble_candidate_table_text.
  - --diagram-relations-pdf-base (optional; output of
    promote_pdf_only_explicit_diagram_edges.py): its relations[] are copied
    into v1's relations[] unchanged in shape (explicit from_id/to_id,
    inferred=true from_node/to_node with full ambiguous/unresolved
    evidence) -- nothing is coerced or dropped.

Paragraph/heading merging is a documented geometric heuristic (vertical-gap
line clustering; a block's own occurrences retain the original
occ:inspector:p{page}:t{index} ids so any relation minted elsewhere against
the same --native file still resolves), not a claim of a verified layout
parse -- see _merge_native_paragraphs. Native text_items claimed by a
table/table_candidate's own canonical text are excluded from this merge
entirely (see _table_and_candidate_claimed_native_indices), so the same
native content never appears twice as two different semantic blocks.

Nested tables: when one table/table_candidate's bbox sits fully inside a
specific cell of another *explicit* table on the same page, a
`table_contains` relation (from_id=parent cell_id, to_id=child block_id,
inferred=false, structure_status=explicit) is minted -- the child stays
its own block, and nothing in the parent cell's own text is duplicated
with it. See _pdf_nested_table_relations. A table_candidate can never be
a table_contains *source* (it has no cells to anchor from_id to); this
document's own Surya HTML has no such nesting either, so this branch is
exercised only by synthetic tests here.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

from common_ir_pipeline.schema import validation_errors
from common_ir_pipeline.shared import detect_boundary_markers, make_provenance, new_document_shell

HEADING_FONT_RATIO = 1.15
HEADING_MAX_ITEMS = 3
LINE_GAP_HEIGHT_RATIO = 0.55  # vertical gap larger than this * line height -> new block
_NATIVE_OCC_RE = re.compile(r"^occ:inspector:p(\d+):t(\d+)$")
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image:\s*[^\]]+\]")
_OCR_LAYOUT_SIDECAR_SCHEMA = "common_ir_v1_pdf_ocr_layout_diagnostic_v1"
_SURYA_LAYOUT_SIDECAR_SCHEMA = "common_ir_v1_pdf_surya_layout_diagnostic_v1"


def _semantic_text(text: str | None) -> str:
    """Strip parser image placeholders without changing source item indexes.
    The original PDF artifact remains the visual evidence; Common IR text is
    only the text that may safely become a semantic value."""
    without_placeholders = _IMAGE_PLACEHOLDER_RE.sub("", text or "")
    return "\n".join(line.strip() for line in without_placeholders.splitlines() if line.strip())


def _is_substantive_text(text: str | None) -> bool:
    return bool(_semantic_text(text))


def _overlap_area(a, b) -> float:
    x0 = max(a[0], b[0]); y0 = max(a[1], b[1]); x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def iou(a, b) -> float:
    inter = _overlap_area(a, b)
    area = lambda z: max(0, z[2] - z[0]) * max(0, z[3] - z[1])
    return inter / (area(a) + area(b) - inter) if area(a) + area(b) - inter else 0


def contains(outer, inner) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _merge_native_paragraphs(native_items: list[dict], excluded_indices: frozenset = frozenset()) -> list[dict]:
    """Groups consecutive native text_items (pdf-inspector's own reading
    order) into paragraph/heading blocks by vertical-gap line clustering.

    A new block starts when the page changes, or the vertical gap to the
    previous item exceeds LINE_GAP_HEIGHT_RATIO * that item's own height
    (a paragraph break -- wrapped lines of the same paragraph sit close
    together; a blank line or new paragraph leaves a bigger gap). Heading
    classification compares each candidate block's font_size against its
    page's median native font_size (computed once, from ALL text on that
    page) -- large, short blocks are headings.

    `excluded_indices` are native items already claimed by a table or
    table_candidate's own canonical text (see
    _table_and_candidate_claimed_native_indices): they are skipped here
    entirely (never form their own independent paragraph block) so the
    *only* place that declares that occurrence is the table/candidate's
    own block -- one declaration, no duplicate occurrence_id, and no
    cross-block ordering dependency for the schema's cell-reference check.

    Returns a list of {"page", "kind", "items": [(index, item), ...]}."""
    by_page: dict[int, list[float]] = {}
    for item in native_items:
        if _is_substantive_text(item.get("text")):
            by_page.setdefault(item["page"], []).append(item["font_size"])
    median_font = {page: statistics.median(sizes) for page, sizes in by_page.items()}

    groups: list[dict] = []
    current: list[tuple[int, dict]] | None = None
    for index, item in enumerate(native_items):
        if not _is_substantive_text(item.get("text")):
            continue
        if index in excluded_indices:
            if current:
                groups.append(_finish_group(current, median_font))
                current = None
            continue
        if current is None:
            current = [(index, item)]
            continue
        prev_index, prev = current[-1]
        same_page = prev["page"] == item["page"]
        gap = prev["y"] - (item["y"] + item["height"]) if same_page else None
        line_height = max(prev["height"], item["height"], 1.0)
        breaks = not same_page or gap is None or gap > LINE_GAP_HEIGHT_RATIO * line_height or gap < -line_height
        if breaks:
            groups.append(_finish_group(current, median_font))
            current = [(index, item)]
        else:
            current.append((index, item))
    if current:
        groups.append(_finish_group(current, median_font))
    return groups


def _table_and_candidate_claimed_native_indices(table_diagram_blocks: list[dict]) -> frozenset:
    """Native text_item indices already embedded as a table/table_candidate
    block's own native_text occurrence -- an explicit table's cell text
    (see _assemble_explicit_table_text) or a table_candidate's bbox
    reading-order text (see _assemble_candidate_table_text). `table_diagram_blocks`
    is the *already-assembled* carry_table_and_diagram_blocks() output (not
    --enriched-common-ir), because that assembly is what decides which
    native items actually end up on the table/candidate block; scanning it
    directly means this never drifts out of sync with the assembly logic.
    These indices must not also spawn an independent paragraph block (that
    would either duplicate the occurrence id or leave the table's own
    reference resolvable only if that other block happens to sort
    earlier)."""
    claimed = set()
    for block in table_diagram_blocks:
        for occurrence in block.get("occurrences", []):
            if occurrence.get("role") != "native_text":
                continue
            match = _NATIVE_OCC_RE.match(occurrence["occurrence_id"])
            if match:
                claimed.add(int(match.group(2)))
    return frozenset(claimed)


def _finish_group(items: list[tuple[int, dict]], median_font: dict[int, float]) -> dict:
    page = items[0][1]["page"]
    is_heading = (
        len(items) <= HEADING_MAX_ITEMS
        and all(it["font_size"] >= HEADING_FONT_RATIO * median_font[page] for _, it in items)
    )
    return {"page": page, "kind": "heading" if is_heading else "paragraph", "items": items}


def _group_bbox(group: dict) -> list[float]:
    items = group["items"]
    xs0 = min(it["x"] for _, it in items); ys0 = min(it["y"] for _, it in items)
    xs1 = max(it["x"] + it["width"] for _, it in items); ys1 = max(it["y"] + it["height"] for _, it in items)
    return [xs0, ys0, xs1, ys1]


def build_paragraph_blocks(notice_id, native_items, excluded_indices: frozenset = frozenset()):
    groups = _merge_native_paragraphs(native_items, excluded_indices)
    blocks = []
    for gi, group in enumerate(groups):
        page = group["page"]
        items = group["items"]
        native_text = "\n".join(_semantic_text(it["text"]) for _, it in items)
        xs0 = min(it["x"] for _, it in items); ys0 = min(it["y"] for _, it in items)
        xs1 = max(it["x"] + it["width"] for _, it in items); ys1 = max(it["y"] + it["height"] for _, it in items)
        bbox = [xs0, ys0, xs1, ys1]
        block_id = f"occ:inspector:p{page}:t{items[0][0]}"
        occurrences = []
        for idx, it in items:
            occurrence_id = f"occ:inspector:p{page}:t{idx}"
            occurrences.append({"occurrence_id": occurrence_id, "role": "native_text", "text": _semantic_text(it["text"]), "provenance": make_provenance("pdf_inspector", page, [it["x"], it["y"], it["x"] + it["width"], it["y"] + it["height"]], "pdf_user_space", f"text_items[{idx}]")})
        text_occurrence_ids = [o["occurrence_id"] for o in occurrences]

        blocks.append({
            "block_id": block_id, "kind": group["kind"], "structure_status": "explicit",
            "text": native_text, "text_occurrence_ids": text_occurrence_ids,
            "page": page, "section_path": f"page[{page}]",
            "occurrences": occurrences, "boundary_markers": detect_boundary_markers(native_text),
            "provenance": make_provenance("pdf_inspector_native_line_geometry_paragraph_merge", page, bbox, "pdf_user_space", f"text_items[{items[0][0]}:{items[-1][0]+1}]"),
            "_sort_key": (page, -ys1),
        })

    return blocks


def _ensure_cell_occurrences_exist(table_diagram_blocks: list[dict], native_items: list[dict], registry: set) -> int:
    """Explicit table cells reference native occurrence ids
    (occ:inspector:p{page}:t{index}) that must exist somewhere in the final
    document's occurrences -- whether or not that native item also ended up
    inside a merged paragraph block. Any reference missing from `registry`
    gets one supplementary native_text occurrence added directly on the
    table/diagram block that needs it (never a new duplicate if the id is
    already registered, and never a second copy for the same id even if two
    cells cite it)."""
    added = 0
    for block in table_diagram_blocks:
        existing_ids_on_block = {o["occurrence_id"] for o in block["occurrences"]}
        for cell in block.get("cells") or []:
            for reference in (*cell.get("evidence_ids", []), *cell.get("text_occurrence_ids", [])):
                if reference in registry:
                    continue
                match = _NATIVE_OCC_RE.match(reference)
                if not match:
                    continue  # not a native occurrence reference; nothing we can supply here
                index = int(match.group(2))
                if index >= len(native_items):
                    continue  # reference doesn't correspond to a real native item; leave as-is
                item = native_items[index]
                if not _is_substantive_text(item.get("text")):
                    block["occurrences"].append({
                        "occurrence_id": reference, "role": "layout_region",
                        "provenance": make_provenance("pdf_inspector_image_placeholder" if _IMAGE_PLACEHOLDER_RE.search(item.get("text") or "") else "pdf_inspector_nonsemantic_text", item["page"], [item["x"], item["y"], item["x"] + item["width"], item["y"] + item["height"]], "pdf_user_space", f"text_items[{index}]"),
                    })
                    registry.add(reference)
                    added += 1
                    continue
                page = item["page"]  # trust the actual item's own page over the id string
                block["occurrences"].append({
                    "occurrence_id": reference, "role": "native_text", "text": _semantic_text(item["text"]),
                    "provenance": make_provenance("pdf_inspector", page, [item["x"], item["y"], item["x"] + item["width"], item["y"] + item["height"]], "pdf_user_space", f"text_items[{index}]"),
                })
                registry.add(reference)
                existing_ids_on_block.add(reference)
                added += 1
    return added


def _resolve_native_text(reference: str, native_items: list[dict]) -> str | None:
    match = _NATIVE_OCC_RE.match(reference)
    if not match:
        return None
    index = int(match.group(2))
    if index >= len(native_items):
        return None
    return _semantic_text(native_items[index]["text"])


def _native_occurrence(reference: str, native_items: list[dict]) -> dict | None:
    match = _NATIVE_OCC_RE.match(reference)
    if not match:
        return None
    index = int(match.group(2))
    if index >= len(native_items):
        return None
    item = native_items[index]
    if not _is_substantive_text(item.get("text")):
        return None
    return {
        "occurrence_id": reference, "role": "native_text", "text": _semantic_text(item["text"]),
        "provenance": make_provenance("pdf_inspector", item["page"], [item["x"], item["y"], item["x"] + item["width"], item["y"] + item["height"]], "pdf_user_space", f"text_items[{index}]"),
    }


def _assemble_explicit_table_text(block: dict, native_items: list[dict], claimed_occurrence_ids: set[str]) -> tuple[str, list[str], list[dict]]:
    """Canonical text for an explicit table: each cell's own native
    occurrence text (never the whole-table Surya HTML, which stays
    attached as a separate structure occurrence), concatenated in
    (row_index, col_index) reading order -- one line per row, cells
    tab-separated.

    Returns (text, text_occurrence_ids, new_native_occurrences): the third
    element is exactly the native occurrences this text cites that aren't
    already present on `block["occurrences"]`, so the caller can embed them
    without ever adding the same occurrence_id twice."""
    existing_ids = claimed_occurrence_ids | {o["occurrence_id"] for o in block.get("occurrences", [])}
    rows: dict[int, list[tuple[int, str]]] = {}
    used_ids: list[str] = []
    new_occurrences: list[dict] = []
    for cell in sorted(block.get("cells") or [], key=lambda c: (c.get("row_index", 0), c.get("col_index", 0))):
        parts = []
        for reference in cell.get("text_occurrence_ids", []):
            text = _resolve_native_text(reference, native_items)
            if not text:
                continue
            parts.append(text)
            if reference not in used_ids:
                used_ids.append(reference)
            if reference not in existing_ids:
                occurrence = _native_occurrence(reference, native_items)
                if occurrence is not None:
                    new_occurrences.append(occurrence)
                    existing_ids.add(reference)
                    claimed_occurrence_ids.add(reference)
        cell_text = " ".join(part.strip() for part in parts if part and part.strip())
        rows.setdefault(cell.get("row_index", 0), []).append((cell.get("col_index", 0), cell_text))
    row_lines = ["\t".join(text for _, text in sorted(cols, key=lambda c: c[0])) for _, cols in sorted(rows.items())]
    return ("\n".join(row_lines) if used_ids else ""), (used_ids or [block["block_id"]]), new_occurrences


def _cluster_lines(items: list[tuple[int, dict]]) -> list[list[tuple[int, dict]]]:
    """Groups (index, item) pairs into top-to-bottom visual lines using the
    same vertical-gap heuristic as _merge_native_paragraphs, generalized to
    an arbitrary (possibly non-contiguous) item set -- a table region's
    native items are not a contiguous run of `native_items`. Each line is
    sorted left-to-right."""
    ordered = sorted(items, key=lambda pair: (-(pair[1]["y"] + pair[1]["height"]), pair[1]["x"]))
    lines: list[list[tuple[int, dict]]] = []
    for index, item in ordered:
        if lines:
            _, first_item = lines[-1][0]
            line_height = max(first_item["height"], item["height"], 1.0)
            if abs((item["y"] + item["height"]) - (first_item["y"] + first_item["height"])) <= LINE_GAP_HEIGHT_RATIO * line_height:
                lines[-1].append((index, item))
                continue
        lines.append([(index, item)])
    for line in lines:
        line.sort(key=lambda pair: pair[1]["x"])
    return lines


def _native_items_in_bbox(native_items: list[dict], page: int, bbox: list[float], min_overlap_ratio: float = 0.5) -> list[list[tuple[int, dict]]]:
    """Native text_items on `page` whose own bbox mostly (>= min_overlap_ratio
    of the item's own area) falls inside `bbox`, grouped into coordinate
    reading-order lines by _cluster_lines. A ratio (not full containment) so
    a line that just grazes the candidate's bbox edge by a point or two
    still counts -- pdf-inspector/Surya bboxes are independently measured
    and rarely agree to the pixel."""
    candidates = []
    for index, item in enumerate(native_items):
        if item["page"] != page or not _is_substantive_text(item.get("text")):
            continue
        item_bbox = [item["x"], item["y"], item["x"] + item["width"], item["y"] + item["height"]]
        item_area = max(0.0, item_bbox[2] - item_bbox[0]) * max(0.0, item_bbox[3] - item_bbox[1])
        if item_area <= 0:
            continue
        if _overlap_area(item_bbox, bbox) / item_area >= min_overlap_ratio:
            candidates.append((index, item))
    return _cluster_lines(candidates)


def _assemble_candidate_table_text(block: dict, native_items: list[dict], claimed_occurrence_ids: set[str]) -> tuple[str, list[str], list[dict]]:
    """Canonical text for a table_candidate: native text_items inside the
    block's own bbox, in coordinate reading order -- never synthetic cells
    (table_candidate is structure_status=partial; the schema forbids cells
    on anything but an explicit table). If native text is unavailable, it
    remains an empty partial candidate. Returns (text, text_occurrence_ids,
    new_native_occurrences)."""
    page = block.get("page")
    bbox = block["provenance"].get("bbox")
    lines = _native_items_in_bbox(native_items, page, bbox) if (page and bbox) else []
    if not lines:
        return "", [block["block_id"]], []

    existing_ids = claimed_occurrence_ids | {o["occurrence_id"] for o in block.get("occurrences", [])}
    new_occurrences, text_occurrence_ids = [], []
    for line in lines:
        for index, item in line:
            occurrence_id = f"occ:inspector:p{page}:t{index}"
            text_occurrence_ids.append(occurrence_id)
            if occurrence_id not in existing_ids:
                new_occurrences.append({
                "occurrence_id": occurrence_id, "role": "native_text", "text": _semantic_text(item["text"]),
                    "provenance": make_provenance("pdf_inspector", page, [item["x"], item["y"], item["x"] + item["width"], item["y"] + item["height"]], "pdf_user_space", f"text_items[{index}]"),
                })
                existing_ids.add(occurrence_id)
                claimed_occurrence_ids.add(occurrence_id)
    text = "\n".join(" ".join(_semantic_text(item["text"]) for _, item in line) for line in lines)

    return text, text_occurrence_ids, new_occurrences


def _without_ocr_text(occurrence: dict) -> dict:
    """Retain legacy OCR-derived layout ids/bboxes as structural evidence,
    but never emit their text in production Common IR."""
    if occurrence.get("role") != "ocr_text":
        return occurrence
    return {key: value for key, value in occurrence.items() if key != "text"} | {"role": "layout_region"}


def _semantic_cells(cells: list[dict] | None, native_items: list[dict]) -> list[dict] | None:
    """Keep cell geometry/evidence intact, but omit placeholder-only native
    references from the cell's semantic text references."""
    if not cells:
        return cells
    cleaned = []
    for cell in cells:
        semantic_cell = dict(cell)
        if "text_occurrence_ids" in semantic_cell:
            semantic_cell["text_occurrence_ids"] = [
                reference for reference in semantic_cell["text_occurrence_ids"]
                if not _NATIVE_OCC_RE.match(reference) or _resolve_native_text(reference, native_items) not in (None, "")
            ]
        cleaned.append(semantic_cell)
    return cleaned


def carry_table_and_diagram_blocks(enriched_doc: dict, native_items: list[dict]) -> list[dict]:
    out = []
    claimed_occurrence_ids: set[str] = set()
    for block in enriched_doc.get("blocks", []):
        if block.get("kind") not in ("table", "table_candidate", "diagram_candidate"):
            continue
        page = block["provenance"].get("page")
        occurrences = [_without_ocr_text(occurrence) for occurrence in block.get("occurrences", [])]
        cells = _semantic_cells(block.get("cells"), native_items)
        claimed_occurrence_ids.update(
            occurrence["occurrence_id"] for occurrence in occurrences
            if occurrence.get("role") == "native_text"
        )
        # This codebase's own convention: a block's primary occurrence id is
        # often the block_id itself (see e.g. pdf_inspector native blocks).
        # Diagram relation evidence_ids minted elsewhere cite the bare
        # occ:surya:p{page}:b{n} / occ:surya:p{page}:diagram_high:{n}
        # block id directly, so alias it as an occurrence here too, rather
        # than rewriting those already-generated relation files.
        if not any(o["occurrence_id"] == block["block_id"] for o in occurrences):
            occurrences.insert(0, {"occurrence_id": block["block_id"], "role": "layout_region", "provenance": block["provenance"]})

        if block.get("kind") == "table" and block.get("structure_status") == "explicit" and block.get("cells"):
            text, text_occurrence_ids, new_occurrences = _assemble_explicit_table_text(
                {"block_id": block["block_id"], "occurrences": occurrences, "cells": cells}, native_items, claimed_occurrence_ids)
            occurrences.extend(new_occurrences)
        elif block.get("kind") == "table_candidate":
            text, text_occurrence_ids, new_occurrences = _assemble_candidate_table_text(
                {"block_id": block["block_id"], "page": page, "provenance": block["provenance"], "occurrences": occurrences}, native_items, claimed_occurrence_ids)
            occurrences.extend(new_occurrences)
        else:
            text = ""
            text_occurrence_ids = [block["block_id"]]

        out.append({
            "block_id": block["block_id"], "kind": block["kind"], "structure_status": block["structure_status"],
            "text": text, "text_occurrence_ids": text_occurrence_ids,
            "page": page, "section_path": f"page[{page}]" if page else "",
            "occurrences": occurrences,
            **({"cells": cells} if block.get("kind") == "table" and block.get("structure_status") == "explicit" and cells else {}),
            "boundary_markers": detect_boundary_markers(text),
            "source_block_label": block.get("source_block_label"),
            "provenance": block["provenance"],
            "_sort_key": (page or 0, -(block["provenance"].get("bbox") or [0, 0, 0, 0])[3]),
        })
    return out


def _pdf_nested_table_relations(table_diagram_blocks: list[dict]) -> list[dict]:
    """`table_contains` relations (from_id=parent cell_id, to_id=child
    block_id) for PDF table/table_candidate blocks whose bbox containment
    is geometrically unambiguous: the parent must be an *explicit* table
    (real cells, each with its own bbox) and the child block's own bbox
    must sit fully inside one specific cell's bbox. A table_candidate
    parent has no cells at all, so it can never be a table_contains
    source (nothing to anchor from_id to) -- that pairing stays
    unrepresented (absent) rather than guessed at. Overlapping-but-not-
    contained pairs likewise get no relation."""
    relations = []
    by_page: dict[int, list[dict]] = {}
    for block in table_diagram_blocks:
        if block.get("kind") in ("table", "table_candidate"):
            by_page.setdefault(block.get("page"), []).append(block)
    for page, blocks in by_page.items():
        if page is None:
            continue
        containing_cells = []
        for outer in blocks:
            if outer.get("kind") != "table" or outer.get("structure_status") != "explicit":
                continue
            outer_bbox = outer.get("provenance", {}).get("bbox")
            cells = outer.get("cells") or []
            # A one-cell table legitimately has the same bounds as its only
            # cell.  The unsafe legacy shape is a *multi-cell* table where
            # every cell has been assigned the parent table's bounds.
            has_reused_parent_bbox = bool(outer_bbox) and len(cells) > 1 and all(
                cell.get("provenance", {}).get("bbox") == outer_bbox for cell in cells
            )
            for cell in cells:
                cell_bbox = cell.get("provenance", {}).get("bbox")
                if not cell_bbox:
                    continue
                # Older enriched producers sometimes copied the parent table
                # bbox onto every cell.  That is layout provenance, not an
                # explicit cell geometry, so it cannot support a cell-level
                # containment relation.
                if has_reused_parent_bbox and cell_bbox == outer_bbox:
                    continue
                area = max(0.0, cell_bbox[2] - cell_bbox[0]) * max(0.0, cell_bbox[3] - cell_bbox[1])
                containing_cells.append((area, cell["cell_id"], outer, cell, cell_bbox))
        for inner in blocks:
            inner_bbox = inner["provenance"].get("bbox")
            if not inner_bbox:
                continue
            candidates = [
                candidate
                for candidate in containing_cells
                if inner is not candidate[2] and contains(candidate[4], inner_bbox)
            ]
            if not candidates:
                continue
            # An outer PDF table cell can also geometrically contain a
            # grandchild. Keep only the innermost containing cell so every
            # child has one parent, matching rhwp's structural recursion.
            _, _, outer, cell, cell_bbox = min(candidates, key=lambda item: (item[0], item[1]))
            relations.append({
                "relation_id": f"relation:table_contains:{cell['cell_id']}:{inner['block_id']}",
                "kind": "table_contains", "from_id": cell["cell_id"], "to_id": inner["block_id"],
                "evidence_ids": [outer["block_id"], inner["block_id"]],
                "inferred": False, "structure_status": "explicit",
                "provenance": make_provenance("pdf_bbox_containment", page, cell_bbox, "pdf_user_space", f"cell={cell['cell_id']} contains block={inner['block_id']}"),
            })
    return relations


def carry_referenced_diagram_evidence_blocks(pdf_base_blocks: list[dict], relations: list[dict], existing_ids: set) -> list[dict]:
    """Carries over only the pdf_base-v2-shaped diagram evidence blocks
    (`{"id", "kind", "text", "structure_status", "provenance": {...
    "extraction_method", "bbox", "coordinate_space", "source_location"}}`,
    the shape promote_pdf_only_explicit_diagram_edges.py writes -- distinct
    from --enriched-common-ir's v0.1-adapted "block_id"/"occurrences[]"
    shape) that a relation's own evidence_ids actually reference.

    Route B's evidence (occ:surya:p{page}:b{n}, the original all-page
    block) is already carried via --enriched-common-ir's diagram_candidate
    block, so this is normally a no-op for it. Route A's evidence
    (occ:surya:p{page}:diagram_high:{n}) is minted by
    promote_pdf_only_explicit_diagram_edges.py itself and never appears in
    --enriched-common-ir (that snapshot predates diagram promotion) -- this
    is the one gap this function closes, and only when Route A actually
    produced an explicit edge citing it.

    `existing_ids` (already-registered occurrence ids) is checked first so
    an id already carried some other way is never duplicated; nothing here
    creates a second copy of, or overwrites, an existing native/OCR
    occurrence."""
    referenced = {evidence_id for relation in relations for evidence_id in relation.get("evidence_ids", [])}
    by_id = {block["id"]: block for block in pdf_base_blocks if "id" in block}
    out = []
    for evidence_id in sorted(referenced):
        if evidence_id in existing_ids:
            continue  # already present (e.g. via --enriched-common-ir) -- never duplicate
        block = by_id.get(evidence_id)
        if block is None:
            continue  # nothing to carry; the schema check below will report the dangling reference honestly
        raw_provenance = block.get("provenance", {})
        page = raw_provenance.get("page")
        provenance = make_provenance(
            raw_provenance.get("extraction_method", "surya_targeted_high_accuracy"), page,
            raw_provenance.get("bbox"), raw_provenance.get("coordinate_space"),
            raw_provenance.get("source_location", evidence_id),
        )
        occurrence = {"occurrence_id": evidence_id, "role": "layout_region", "provenance": provenance}
        out.append({
            "block_id": evidence_id, "kind": block.get("kind", "diagram_candidate"),
            "structure_status": block.get("structure_status", "partial"),
            "text": "", "text_occurrence_ids": [evidence_id],
            "page": page, "section_path": f"page[{page}]" if page else "",
            "occurrences": [occurrence], "boundary_markers": [],
            "source_block_label": block.get("layout_label"),
            "provenance": provenance,
            "_sort_key": (page or 0, -(raw_provenance.get("bbox") or [0, 0, 0, 0])[3]),
        })
        existing_ids.add(evidence_id)
    return out


def carry_ocr_layout_diagnostic_blocks(sidecar: dict, sidecar_path: Path, source_sha256: str) -> list[dict]:
    """Project a textless OCR or Surya layout sidecar into non-semantic Common IR blocks.

    The diagnostic worker may run recognition internally, but its sidecar is
    deliberately limited to geometry and timing.  This projection validates
    the sidecar's source hash before use, rejects any text-shaped region
    payload, and creates no cells or relations.  ``layout_candidate`` blocks
    must be excluded by CandidatePack consumers because their canonical text
    is always empty.
    """
    sidecar_schema = sidecar.get("sidecar_schema_version")
    if sidecar_schema not in {_OCR_LAYOUT_SIDECAR_SCHEMA, _SURYA_LAYOUT_SIDECAR_SCHEMA}:
        raise ValueError(f"unsupported textless layout sidecar schema: {sidecar_schema!r}")
    if sidecar.get("source", {}).get("source_sha256") != source_sha256:
        raise ValueError("OCR layout sidecar source_sha256 does not match the source PDF")
    policy = sidecar.get("policy", {})
    if sidecar_schema == _OCR_LAYOUT_SIDECAR_SCHEMA:
        text_persisted_key, text_allowed_key, worker_prefix, provenance_method = (
            "ocr_text_persisted", "ocr_text_allowed_in_common_ir", "ocr-layout", "cpu_ocr_text_region_geometry"
        )
    else:
        text_persisted_key, text_allowed_key, worker_prefix, provenance_method = (
            "surya_text_persisted", "surya_text_allowed_in_common_ir", "surya-layout", "surya_layout_geometry"
        )
    if policy.get(text_persisted_key) is not False or policy.get(text_allowed_key) is not False:
        raise ValueError("layout sidecar policy permits recognized text")
    blocks = []
    seen_ids: set[str] = set()
    for page_index, page in enumerate(sidecar.get("pages", [])):
        page_number = page.get("page")
        if not isinstance(page_number, int) or page_number < 1:
            raise ValueError(f"OCR layout sidecar page[{page_index}] has invalid page")
        for region_index, region in enumerate(page.get("regions", [])):
            forbidden = {"text", "raw_text", "ocr_text", "html", "content"} & set(region)
            if forbidden:
                raise ValueError(f"OCR layout sidecar region contains forbidden semantic text keys: {sorted(forbidden)}")
            region_id = region.get("region_id")
            bbox = region.get("bbox")
            if not isinstance(region_id, str) or not region_id or not re.fullmatch(r"[A-Za-z0-9:_-]+", region_id):
                raise ValueError(f"OCR layout sidecar region[{page_index}:{region_index}] has invalid region_id")
            if not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(value, (int, float)) for value in bbox):
                raise ValueError(f"OCR layout sidecar region[{page_index}:{region_index}] has invalid bbox")
            block_id = f"{worker_prefix}:p{page_number}:{region_id}"
            if block_id in seen_ids:
                raise ValueError(f"OCR layout sidecar has duplicate region block_id: {block_id}")
            seen_ids.add(block_id)
            occurrence_id = f"{block_id}:geometry"
            location = f"{sidecar_path}#/pages/{page_index}/regions/{region_index}"
            provenance = make_provenance(
                provenance_method, page_number, [float(value) for value in bbox],
                sidecar.get("worker", {}).get("coordinate_space", "rendered_page_px"), location,
            )
            blocks.append({
                "block_id": block_id,
                "kind": "layout_candidate",
                "structure_status": "partial",
                "text": "",
                "text_occurrence_ids": [occurrence_id],
                "page": page_number,
                "section_path": f"page[{page_number}]",
                "occurrences": [{"occurrence_id": occurrence_id, "role": "layout_region", "provenance": provenance}],
                "boundary_markers": [],
                "source_block_label": region.get("label") or provenance_method,
                "provenance": provenance,
                "_sort_key": (page_number, -float(bbox[3])),
            })
    return blocks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--source-path", type=Path, help="original PDF file; defaults to native source_path only when it resolves locally")
    parser.add_argument("--source-sha256", help="SHA-256 of --source-path; validated against the readable original when supplied")
    parser.add_argument("--render-manifest", type=Path, help="optional archived render/layout manifest")
    parser.add_argument("--enriched-common-ir", type=Path, help="optional archived table/diagram layout sidecar")
    parser.add_argument("--ocr-layout-diagnostic", type=Path, help="optional textless sidecar from common-ir-pdf-ocr-layout or common-ir-pdf-surya-layout; source hash is verified")
    parser.add_argument("--diagram-relations-pdf-base", type=Path, help="optional output of promote_pdf_only_explicit_diagram_edges.py, for relations[]")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    native = json.loads(args.native.read_text(encoding="utf-8"))
    source_path = args.source_path
    if source_path is None and native.get("source_path"):
        candidate = Path(native["source_path"])
        if candidate.is_file():
            source_path = candidate
    if source_path is None:
        parser.error("--source-path is required when native source_path is absent or does not resolve locally")
    if not source_path.is_file():
        parser.error(f"source PDF does not exist: {source_path}")
    document_id = f"pdf:{args.notice_id}"
    native_text_pages = {item["page"] for item in native["text_items"] if _is_substantive_text(item.get("text"))}
    eligibility = "eligible_native_text" if native_text_pages else "excluded_image_only"
    render = json.loads(args.render_manifest.read_text(encoding="utf-8")) if args.render_manifest else {"pages": []}
    enriched = json.loads(args.enriched_common_ir.read_text(encoding="utf-8")) if args.enriched_common_ir else {"blocks": []}
    raw_artifact_ids = [str(args.native)]
    if args.render_manifest:
        raw_artifact_ids.append(str(args.render_manifest))
    if args.enriched_common_ir:
        raw_artifact_ids.append(str(args.enriched_common_ir))
    if args.ocr_layout_diagnostic:
        raw_artifact_ids.append(str(args.ocr_layout_diagnostic))
    if args.diagram_relations_pdf_base:
        raw_artifact_ids.append(str(args.diagram_relations_pdf_base))
    page_count = native.get("process_result", {}).get("page_count") or len(render.get("pages", []))
    # native's own "method"/"version" are the native extractor's identity
    # (e.g. "pdf_inspector"/"1.17.0") as captured at extraction time --
    # preserved independently of this adapter's own generator/generator_version
    # (see make_document). Absent in older/hand-built --native fixtures that
    # predate this capture; never guessed at here.
    doc = new_document_shell(
        document_id, "pdf", source_path, page_count, raw_artifact_ids, method="pdf_native_only",
        source_sha256=args.source_sha256,
        parser=native.get("method"), parser_version=native.get("version"),
    )
    doc["document"].update({
        "pdf_semantic_eligibility": eligibility,
        "pdf_semantic_reason": "substantive_native_text_available" if native_text_pages else "no_substantive_native_text",
        "native_text_page_count": len(native_text_pages),
    })

    relations, diagram_pdf_base_blocks = [], []
    if args.diagram_relations_pdf_base:
        diagram_pdf_base = json.loads(args.diagram_relations_pdf_base.read_text(encoding="utf-8"))
        if diagram_pdf_base.get("sidecar_schema_version") != "common_ir_v1_pdf_explicit_diagram_relations_v1":
            raise ValueError("diagram relation sidecar must use common_ir_v1_pdf_explicit_diagram_relations_v1")
        if diagram_pdf_base.get("source", {}).get("source_sha256") != doc["document"]["provenance"]["source_sha256"]:
            raise ValueError("diagram relation sidecar source_sha256 does not match the source PDF")
        relations = diagram_pdf_base.get("relations", [])
        if any(relation.get("kind") != "diagram_edge" or relation.get("inferred") is not False for relation in relations):
            raise ValueError("diagram relation sidecar may contain only explicit, non-inferred diagram_edge relations")
        diagram_pdf_base_blocks = diagram_pdf_base.get("blocks", [])

    table_diagram_blocks = carry_table_and_diagram_blocks(enriched, native["text_items"])
    table_claimed_indices = _table_and_candidate_claimed_native_indices(table_diagram_blocks)
    paragraph_blocks = build_paragraph_blocks(args.notice_id, native["text_items"], table_claimed_indices)

    registry = {o["occurrence_id"] for b in paragraph_blocks + table_diagram_blocks for o in b["occurrences"]}
    supplied = _ensure_cell_occurrences_exist(table_diagram_blocks, native["text_items"], registry)

    # Route A's own diagram_high evidence block (occ:surya:p{page}:diagram_high:{n})
    # is minted only inside --diagram-relations-pdf-base, never in
    # --enriched-common-ir; carry over just what a relation's evidence_ids
    # actually cites and isn't already registered, so explicit relations
    # resolve without duplicating anything Route B already gets via
    # --enriched-common-ir.
    extra_diagram_blocks = carry_referenced_diagram_evidence_blocks(diagram_pdf_base_blocks, relations, registry)
    ocr_layout_blocks = []
    if args.ocr_layout_diagnostic:
        ocr_layout = json.loads(args.ocr_layout_diagnostic.read_text(encoding="utf-8"))
        ocr_layout_blocks = carry_ocr_layout_diagnostic_blocks(
            ocr_layout, args.ocr_layout_diagnostic, doc["document"]["provenance"]["source_sha256"],
        )

    # Nested-table containment (see _pdf_nested_table_relations) is computed
    # last, after table_diagram_blocks is final, and appended rather than
    # merged into `relations` earlier so it never affects which diagram
    # evidence blocks carry_referenced_diagram_evidence_blocks() pulls in.
    nested_table_relations = _pdf_nested_table_relations(table_diagram_blocks)
    relations = relations + nested_table_relations

    all_blocks = paragraph_blocks + table_diagram_blocks + extra_diagram_blocks + ocr_layout_blocks
    all_blocks.sort(key=lambda b: b["_sort_key"])
    for order, block in enumerate(all_blocks):
        block["reading_order"] = order
        del block["_sort_key"]
        block.setdefault("cells", None)
        if block["cells"] is None:
            del block["cells"]

    doc["blocks"] = all_blocks
    doc["conflicts"] = []
    doc["relations"] = relations

    errors = validation_errors(doc)
    if errors:
        raise SystemExit("Common IR v1 validation failed:\n" + "\n".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "blocks": len(all_blocks),
        "paragraph_or_heading_blocks": len(paragraph_blocks), "table_or_diagram_blocks": len(table_diagram_blocks),
        "extra_diagram_evidence_blocks": len(extra_diagram_blocks),
        "ocr_layout_diagnostic_blocks": len(ocr_layout_blocks),
        "conflicts": 0,
        "relations": len(relations), "pdf_nested_table_relations": len(nested_table_relations),
        "supplementary_cell_occurrences_added": supplied, "validation_errors": 0,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
