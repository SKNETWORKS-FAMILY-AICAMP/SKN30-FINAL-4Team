"""Read-only projection from Common IR v0.3 into semantic-source blocks.

The projection never rewrites Common IR.  It selects one exact occurrence per
block for semantic reading, keeps the original ``block_id`` as evidence, and
removes only geometrically matching PDF native/OCR duplicates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import SourceBlock, SourceRelation


_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class ProjectionResult:
    notice_id: str
    source_kind: str
    blocks: list[SourceBlock]
    table_cell_blocks: list[SourceBlock]
    selected_occurrence_ids: dict[str, list[str]]
    dropped_duplicate_ocr_block_ids: list[str]
    suppressed_nested_wrapper_block_ids: list[str]


def _text_occurrences(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [occurrence for occurrence in block.get("occurrences", []) if occurrence.get("text", "").strip()]


def _preferred_occurrences(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer source-native text without discarding OCR fallback blocks."""

    occurrences = _text_occurrences(block)
    for role in ("rhwp_text", "rhwp_cell", "native_text", "ocr_text"):
        selected = [occurrence for occurrence in occurrences if occurrence.get("role") == role]
        if selected:
            return selected
    return occurrences


def _occurrence_index(document: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for source_order, block in enumerate(document["blocks"]):
        for occurrence in _text_occurrences(block):
            index[occurrence["occurrence_id"]] = occurrence["text"]
    return index


def _table_text(block: dict[str, Any], occurrence_text: dict[str, str]) -> tuple[str, list[str]]:
    """Flatten only explicit cell coordinates; retain every source occurrence id."""

    cells = block.get("cells", [])
    if not cells:
        selected = _preferred_occurrences(block)
        return "\n".join(occurrence["text"] for occurrence in selected), [
            occurrence["occurrence_id"] for occurrence in selected
        ]

    parts: list[str] = []
    selected_ids: list[str] = []
    for cell in sorted(cells, key=lambda item: (item.get("row_index", 0), item.get("col_index", 0), item.get("cell_id", ""))):
        ids = cell.get("text_occurrence_ids", [])
        text = " ".join(occurrence_text[item] for item in ids if item in occurrence_text).strip()
        if text:
            parts.append(f"r{cell.get('row_index', 0)}c{cell.get('col_index', 0)}: {text}")
            selected_ids.extend(item for item in ids if item in occurrence_text)
    return (f"[table] " + " | ".join(parts)) if parts else "", selected_ids


def _table_cell_blocks(block: dict[str, Any], occurrence_text: dict[str, str], source_order: int) -> list[tuple[SourceBlock, list[str]]]:
    """Expose exact Common-IR table occurrences as A-candidate cell blocks.

    A cell may contain several text occurrences.  They remain separate source
    blocks so ``value_raw`` can always be recovered from one exact occurrence,
    rather than a server-created cell concatenation.
    """

    result: list[tuple[SourceBlock, list[str]]] = []
    for cell in sorted(block.get("cells", []), key=lambda item: (item.get("row_index", 0), item.get("col_index", 0), item.get("cell_id", ""))):
        row, col = cell.get("row_index", 0), cell.get("col_index", 0)
        for paragraph, occurrence_id in enumerate(cell.get("text_occurrence_ids", [])):
            text = occurrence_text.get(occurrence_id, "").strip()
            if not text:
                continue
            result.append(
                (
                    SourceBlock(
                        block_id=f"{block['block_id']}#r{row}c{col}p{paragraph}",
                        text=text,
                        relation=SourceRelation.CANDIDATE,
                        block_kind="table_cell",
                        source_order=source_order,
                        source_occurrence_ids=[occurrence_id],
                    ),
                    [occurrence_id],
                )
            )
    return result


def _normal_text(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _bbox(block: dict[str, Any]) -> list[float] | None:
    bbox = block.get("provenance", {}).get("bbox")
    return bbox if isinstance(bbox, list) and len(bbox) == 4 else None


def _cell_text(cell: dict[str, Any], occurrence_text: dict[str, str]) -> str:
    return " ".join(occurrence_text[item] for item in cell.get("text_occurrence_ids", []) if item in occurrence_text).strip()


def _nested_table_wrapper_ids(blocks: list[dict[str, Any]], occurrence_text: dict[str, str]) -> set[str]:
    """Return 1×1 table wrappers whose real grid is a child table block.

    HWP/HWPX commonly represents a visual table inside the sole cell of an
    outer layout table.  Common IR preserves both blocks: e.g. ``hwpx:t131``
    and ``hwpx:t131.c0.b1``.  Keeping both in a semantic projection duplicates
    the whole table once as tab-separated wrapper text and once as explicit
    cells.  Suppress only the wrapper here; the source Common IR remains
    untouched and the child retains exact cell evidence.
    """

    table_by_id = {block.get("block_id", ""): block for block in blocks if block.get("kind") == "table"}
    wrappers: set[str] = set()
    for block in blocks:
        if block.get("kind") != "table" or block.get("structure_status") != "explicit" or len(block.get("cells", [])) != 1:
            continue
        block_id = block.get("block_id", "")
        cell_id = block["cells"][0].get("cell_id", "")
        cell_match = re.search(r":c(\d+)$", cell_id)
        if not cell_match:
            continue
        child_prefix = f"{block_id}.c{cell_match.group(1)}.b"
        children = [candidate for candidate_id, candidate in table_by_id.items() if candidate_id.startswith(child_prefix)]
        wrapper_text = _normal_text("\n".join(item["text"] for item in _preferred_occurrences(block)))
        child_text = _normal_text("\n".join(
            _cell_text(cell, occurrence_text)
            for child in children
            for cell in sorted(child.get("cells", []), key=lambda item: (item.get("row_index", 0), item.get("col_index", 0), item.get("cell_id", "")))
            if _cell_text(cell, occurrence_text)
        ))
        # A wrapper may also contain a caption or sibling paragraph.  Suppress
        # it only when the explicit child grid completely accounts for its
        # meaningful text; otherwise retain both rather than losing evidence.
        if children and child_text and wrapper_text == child_text:
            wrappers.add(block_id)
    return wrappers


def _iou(left: list[float] | None, right: list[float] | None) -> float:
    if left is None or right is None:
        return 0.0
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    overlap = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if not overlap:
        return 0.0
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return overlap / (left_area + right_area - overlap) if left_area > 0 and right_area > 0 else 0.0


def project_common_ir_v03(document: dict[str, Any]) -> ProjectionResult:
    """Produce a loss-aware semantic projection for one Common IR v0.3 file.

    PDF OCR paragraphs are dropped only if a same-page native paragraph has
    identical normalized text and a substantially overlapping bounding box.
    OCR-only paragraphs, partial tables, and diagram candidates are retained;
    downstream A/B/C policy decides their eligibility.
    """

    if document.get("schema_version") != "common_ir_v0_3":
        raise ValueError("expected common_ir_v0_3")
    metadata = document["document"]
    document_id = metadata["document_id"]
    try:
        _prefix, notice_id = document_id.split(":", 1)
    except ValueError as error:
        raise ValueError("document_id must be '<source_kind>:<notice_id>'") from error

    occurrence_text = _occurrence_index(document)
    nested_table_wrappers = _nested_table_wrapper_ids(document["blocks"], occurrence_text)
    native_paragraphs: dict[tuple[int | None, str], list[dict[str, Any]]] = {}
    for source_order, block in enumerate(document["blocks"]):
        if block.get("kind") != "paragraph":
            continue
        native = [occurrence for occurrence in _text_occurrences(block) if occurrence.get("role") == "native_text"]
        if native:
            page = block.get("provenance", {}).get("page")
            native_paragraphs.setdefault((page, _normal_text(native[0]["text"])), []).append(block)

    projected: list[SourceBlock] = []
    projected_table_cells: list[SourceBlock] = []
    selected_occurrence_ids: dict[str, list[str]] = {}
    dropped: list[str] = []
    for source_order, block in enumerate(document["blocks"]):
        kind = block.get("kind")
        if block.get("block_id") in nested_table_wrappers:
            continue
        selected = _preferred_occurrences(block)
        if not selected and kind != "table":
            continue

        if metadata.get("source_kind") == "pdf" and kind == "paragraph" and selected[0].get("role") == "ocr_text":
            page = block.get("provenance", {}).get("page")
            candidates = native_paragraphs.get((page, _normal_text(selected[0]["text"])), [])
            # OCR/layout and native bounding boxes differ by font metrics; 0.7
            # still requires the same physical text region when text also
            # matches exactly.
            if any(_iou(_bbox(block), _bbox(candidate)) >= 0.7 for candidate in candidates):
                dropped.append(block["block_id"])
                continue

        if kind == "table":
            text, occurrence_ids = _table_text(block, occurrence_text)
        else:
            text = "\n".join(occurrence["text"] for occurrence in selected).strip()
            occurrence_ids = [occurrence["occurrence_id"] for occurrence in selected]
        if not text.strip():
            continue
        projected.append(
            SourceBlock(
                block_id=block["block_id"],
                text=text,
                relation=SourceRelation.CANDIDATE,
                block_kind=kind,
                source_order=source_order,
                source_occurrence_ids=occurrence_ids,
            )
        )
        selected_occurrence_ids[block["block_id"]] = occurrence_ids
        if kind == "table":
            for cell_block, cell_occurrence_ids in _table_cell_blocks(block, occurrence_text, source_order):
                projected_table_cells.append(cell_block)
                selected_occurrence_ids[cell_block.block_id] = cell_occurrence_ids

    return ProjectionResult(
        notice_id=notice_id,
        source_kind=metadata["source_kind"],
        blocks=projected,
        table_cell_blocks=projected_table_cells,
        selected_occurrence_ids=selected_occurrence_ids,
        dropped_duplicate_ocr_block_ids=dropped,
        suppressed_nested_wrapper_block_ids=sorted(nested_table_wrappers),
    )
