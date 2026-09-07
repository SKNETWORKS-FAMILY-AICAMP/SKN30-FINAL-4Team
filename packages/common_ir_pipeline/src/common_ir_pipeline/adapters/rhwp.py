#!/usr/bin/env python3
"""Create standalone Common IR v1 from one rhwp raw IR (HWP/HWPX).

One source file -> one output; no paired PDF is read or required. rhwp
already parses HWP/HWPX into paragraph/table units (not glyph fragments), so
this adapter mostly reshapes that structure into v1's block/occurrence/cell
contract rather than re-merging anything:
  - blank paragraphs, picture items, and currently unsupported rhwp block
    kinds are skipped (v1's block.kind has no slot for images; "meaningful
    paragraph/table blocks" excludes empty ones).  The output summary counts
    these omissions across both top-level and nested table-cell content.
  - reading_order is assigned over the *emitted* blocks (0-based), so it
    stays a dense, gap-free document order regardless of skipped items.
  - section_path is built from rhwp's own section_idx/para_idx provenance
    (source structural path); page is always null (HWP/HWPX has no native
    page concept -- v1 requires the key but allows a null value).
  - [서식]/【붙임】/[별첨] boundary markers are detected on each block's own
    text (metadata only; never changes structure_status).
  - nested tables: a cell's own text (flatten_blocks) is its *direct*
    paragraph content only -- a nested table inside that cell is never
    inlined into it. The nested table is emitted as its own separate
    block (emit_table recurses), and a `table_contains` relation
    (from_id=parent cell_id, to_id=child table block_id, inferred=false,
    structure_status=explicit -- direct endpoint resolution is unambiguous;
    PDF bbox-derived containment is distinguished by provenance.method)
    records the
    relationship instead, so the same text is never carried by two blocks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common_ir_pipeline.schema import validation_errors
from common_ir_pipeline.shared import detect_boundary_markers, make_provenance, new_document_shell

TEXTUAL_KINDS = {"paragraph", "list_item", "field", "caption"}


def flatten_blocks(blocks: list[dict]) -> str:
    """Direct textual content of `blocks` -- paragraphs only. A nested
    table's own text is deliberately excluded: it is carried solely by its
    own separately emitted table block, addressed from here via a
    `table_contains` relation (see emit_table), so inlining it would
    duplicate the same content across two blocks."""
    parts = [block.get("text", "") for block in blocks if block.get("kind") in TEXTUAL_KINDS]
    return "\n".join(part for part in parts if part.strip())


def section_path_of(prov: dict) -> str:
    section = prov.get("section_idx")
    para = prov.get("para_idx")
    parts = []
    if section is not None:
        parts.append(f"section[{section}]")
    if para is not None:
        parts.append(f"para[{para}]")
    return "/".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--rhwp", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("hwp", "hwpx"), required=True)
    parser.add_argument("--source-path", type=Path, required=True, help="original HWP/HWPX file; document provenance source")
    parser.add_argument("--source-sha256", required=True, help="SHA-256 of --source-path; validated against the readable original")
    parser.add_argument("--source-location-base", help="stable source-location prefix for raw IR pointers")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    raw = json.loads(args.rhwp.read_text(encoding="utf-8"))
    if not args.source_path.is_file():
        raise SystemExit(f"source file does not exist: {args.source_path}")
    raw_location = args.source_location_base or str(args.rhwp)
    original_source = args.source_path
    document_id = f"{args.source_kind}:{args.notice_id}"
    # raw's own "version"/"core_version" are exactly what run_rhwp_to_common_ir_v1_e2e.py
    # (or any other caller building --rhwp) captured from rhwp.version() /
    # rhwp.rhwp_core_version() at parse time -- preserved here independently
    # of this adapter's own generator/generator_version (see make_document).
    doc = new_document_shell(
        document_id,
        args.source_kind,
        original_source,
        raw.get("page_count"),
        [raw_location],
        method="rhwp",
        source_sha256=args.source_sha256,
        parser="rhwp",
        parser_version=raw.get("version"),
        parser_core_version=raw.get("core_version"),
    )
    blocks = doc["blocks"]
    relations = doc["relations"]
    order = 0
    skipped_empty = skipped_picture = 0
    skipped_other: dict[str, int] = {}
    skipped_furniture: dict[str, int] = {}

    def next_order() -> int:
        nonlocal order
        value = order
        order += 1
        return value

    def emit_table(table: dict, table_path: str, location: str, section_path: str) -> None:
        nonlocal skipped_empty, skipped_picture, skipped_other
        table_provenance = make_provenance("rhwp", None, None, None, location)
        cell_occurrences, cells = [], []
        for cell_index, cell in enumerate(table.get("cells", [])):
            cell_location = f"{location}/cells/{cell_index}"
            occurrence_id = f"occ:rhwp:t{table_path}:c{cell_index}"
            cell_blocks = cell.get("blocks", [])
            text = flatten_blocks(cell_blocks)
            for child in cell_blocks:
                child_kind = child.get("kind")
                if child_kind == "paragraph" and not (child.get("text") or "").strip():
                    skipped_empty += 1
                elif child_kind == "picture":
                    skipped_picture += 1
                elif child_kind not in TEXTUAL_KINDS | {"table"}:
                    skipped_other[child_kind or "missing_kind"] = skipped_other.get(child_kind or "missing_kind", 0) + 1
            cell_occurrences.append({"occurrence_id": occurrence_id, "role": "rhwp_cell", "text": text, "provenance": make_provenance("rhwp", None, None, None, cell_location)})
            cells.append({
                "cell_id": f"{args.source_kind}:t{table_path}:c{cell_index}", "evidence_ids": [occurrence_id],
                "row_index": cell["row"], "col_index": cell["col"], "row_span": cell["row_span"], "col_span": cell["col_span"],
                "text_occurrence_ids": [occurrence_id], "provenance": make_provenance("rhwp", None, None, None, cell_location),
            })
        # Derive the table text from direct paragraph content only.  rhwp's
        # optional table["text"] may flatten nested-table text into the
        # parent, which would violate the non-duplication contract: nested
        # table content is carried by its own emitted block and connected via
        # table_contains below.
        table_text = flatten_blocks([child for cell in table.get("cells", []) for child in cell.get("blocks", [])])
        table_occurrence_id = f"occ:rhwp:t{table_path}"
        blocks.append({
            "block_id": f"{args.source_kind}:t{table_path}", "kind": "table", "structure_status": "explicit",
            "text": table_text, "text_occurrence_ids": [table_occurrence_id],
            "reading_order": next_order(), "page": None, "section_path": section_path,
            "occurrences": [{"occurrence_id": table_occurrence_id, "role": "rhwp_text", "text": table_text, "provenance": table_provenance}] + cell_occurrences,
            "cells": cells, "boundary_markers": detect_boundary_markers(table_text),
            "provenance": table_provenance,
        })
        # Nested tables are preserved as separately addressable blocks (the
        # containing cell's own text above is its direct paragraphs only,
        # see flatten_blocks); a table_contains relation records the
        # containment instead of inlining the child's text into the parent
        # cell -- from_id is the parent *cell_id* (not its occurrence_id),
        # to_id is the child table's block_id.
        for cell_index, cell in enumerate(table.get("cells", [])):
            parent_cell_id = f"{args.source_kind}:t{table_path}:c{cell_index}"
            for child_index, child in enumerate(cell.get("blocks", [])):
                if child.get("kind") != "table":
                    continue
                child_table_path = f"{table_path}.c{cell_index}.b{child_index}"
                child_location = f"{location}/cells/{cell_index}/blocks/{child_index}"
                child_block_id = f"{args.source_kind}:t{child_table_path}"
                relations.append({
                    "relation_id": f"relation:table_contains:{parent_cell_id}:{child_block_id}",
                    "kind": "table_contains", "from_id": parent_cell_id, "to_id": child_block_id,
                    "evidence_ids": [f"occ:rhwp:t{table_path}:c{cell_index}"],
                    "inferred": False, "structure_status": "explicit",
                    "provenance": make_provenance("rhwp", None, None, None, child_location),
                })
                emit_table(child, child_table_path, child_location, section_path)

    for index, item in enumerate(raw["ir"]["body"]):
        location = f"{raw_location}#/ir/body/{index}"
        kind = item.get("kind")
        if kind in TEXTUAL_KINDS:
            text = item.get("text") or ""
            if not text.strip():
                skipped_empty += 1
                continue
            prov = make_provenance("rhwp", None, None, None, location)
            occurrence_id = f"occ:rhwp:p{index}"
            section_path = section_path_of(item.get("prov", {}))
            blocks.append({
                "block_id": f"{args.source_kind}:b{index}", "kind": "paragraph", "structure_status": "explicit",
                "text": text, "text_occurrence_ids": [occurrence_id],
                "reading_order": next_order(), "page": None, "section_path": section_path,
                "occurrences": [{"occurrence_id": occurrence_id, "role": "rhwp_text", "text": text, "provenance": prov}],
                "boundary_markers": detect_boundary_markers(text),
                "source_block_label": kind,
                "provenance": prov,
            })
        elif kind == "table":
            emit_table(item, str(index), location, section_path_of(item.get("prov", {})))
        elif kind == "picture":
            skipped_picture += 1
        else:
            skipped_other[kind or "missing_kind"] = skipped_other.get(kind or "missing_kind", 0) + 1
        # Unsupported rhwp kinds are intentionally left unrepresented in v1
        # today rather than guessed at; the per-kind output summary records
        # each omission.

    for furniture in raw.get("ir", {}).get("furniture", []):
        furniture_kind = furniture.get("kind") if isinstance(furniture, dict) else type(furniture).__name__
        skipped_furniture[furniture_kind or "missing_kind"] = skipped_furniture.get(furniture_kind or "missing_kind", 0) + 1

    errors = validation_errors(doc)
    if errors:
        raise SystemExit("Common IR v1 validation failed:\n" + "\n".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "blocks": len(blocks), "relations": len(relations), "skipped_empty_paragraphs": skipped_empty, "skipped_pictures": skipped_picture, "skipped_other_blocks_by_kind": skipped_other, "skipped_furniture_by_kind": skipped_furniture, "validation_errors": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
