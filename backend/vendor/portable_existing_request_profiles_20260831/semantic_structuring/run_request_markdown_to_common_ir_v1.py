#!/usr/bin/env python3
"""Convert the five Request Markdown fixtures into Common IR v1.

This is a fixture adapter, not a Request-specific Common IR dialect. It uses
the same document/block/table/cell/occurrence vocabulary as the production
HWP/HWPX/PDF adapters and deliberately performs no business interpretation.
Only ``semantic_input/*.input.md`` and its request manifest are read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "exploratory_study/input/common_ir_transfer_20260827/transfer_common_ir_20260827/study/results/scripts"
sys.path.insert(0, str(SCHEMA_DIR))
from common_ir_v1_schema import validation_errors  # noqa: E402


SCHEMA_VERSION = "common_ir_v1"
GENERATOR = "semantic_structuring.request_markdown_common_ir_v1"
GENERATOR_VERSION = "1.1.0"
PARSER = "markdown_fixture_parser"
PARSER_VERSION = "1.1.0"
SOURCE_KIND = "markdown_fixture"
THEMATIC_BREAK = re.compile(r"^\s{0,3}(?:---+|\*\*\*+|___+)\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LIST_ITEM = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
TABLE_DIVIDER = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
INLINE_PRESENTATION_MARKER = re.compile(r"\*\*|__")
PRESENTATION_CLEANING_VERSION = "markdown_inline_presentation_clean_v1"


@dataclass(frozen=True)
class SourceSlice:
    text: str
    start: int
    end: int


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_presentation_markup(source: str) -> str:
    """Remove inline visual decoration while preserving Markdown structure.

    The fixture parser still needs heading/list/table syntax to reconstruct the
    document's explicit structure. Only paired inline bold/strong delimiters
    are presentation markup here; the cleaned text becomes a separate source
    artifact, so every Common IR occurrence remains an exact span of the text
    it actually consumed.
    """

    return INLINE_PRESENTATION_MARKER.sub("", source)


def materialize_cleaned_input(
    original_input_dir: Path, source_manifest: dict[str, Any], output_root: Path,
) -> tuple[Path, dict[str, dict[str, str]]]:
    """Create a separate clean semantic-input copy without touching corpus/Gold."""

    cleaned_dir = output_root / "cleaned_semantic_input"
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    lineage: dict[str, dict[str, str]] = {}
    for item in source_manifest.get("documents", []):
        name = item["input_file"]
        original_path = (original_input_dir / name).resolve()
        cleaned_path = (cleaned_dir / name).resolve()
        original_text = original_path.read_text(encoding="utf-8")
        cleaned_path.write_text(clean_presentation_markup(original_text), encoding="utf-8")
        lineage[item["doc_id"]] = {
            "original_source_path": str(original_path),
            "original_source_sha256": sha256(original_path),
            "cleaned_source_path": str(cleaned_path),
            "cleaned_source_sha256": sha256(cleaned_path),
        }
    cleaned_manifest = {
        **source_manifest,
        "presentation_cleaning": {
            "enabled": True,
            "version": PRESENTATION_CLEANING_VERSION,
            "purpose": "remove inline Markdown presentation markers from semantic text",
            "original_input_root": str(original_input_dir.resolve()),
            "rules": ["remove ** and __ inline strong delimiters", "preserve heading/list/table syntax"],
        },
    }
    (cleaned_dir / "request_manifest.json").write_text(
        json.dumps(cleaned_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return cleaned_dir, lineage


def line_slices(source: str) -> list[SourceSlice]:
    slices: list[SourceSlice] = []
    offset = 0
    for part in source.splitlines(keepends=True):
        text = part[:-1] if part.endswith("\n") else part
        if text.endswith("\r"):
            text = text[:-1]
        slices.append(SourceSlice(text=text, start=offset, end=offset + len(text)))
        offset += len(part)
    if not slices and source == "":
        return []
    return slices


def provenance(source_path: Path, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "method": "markdown_fixture_source_span",
        "page": None,
        "bbox": None,
        "coordinate_space": "unicode_codepoint_offset",
        "source_location": str(source_path.resolve()),
    }
    if start is not None:
        value["source_start_char"] = start
        value["source_end_char"] = end
    return value


def split_table_cells(line: SourceSlice) -> list[SourceSlice]:
    """Return content slices for a Markdown table row, excluding pipe syntax."""
    text = line.text
    left = 1 if text.startswith("|") else 0
    right = len(text) - 1 if text.endswith("|") else len(text)
    segments: list[SourceSlice] = []
    cursor = left
    while cursor <= right:
        next_pipe = text.find("|", cursor, right)
        segment_end = right if next_pipe == -1 else next_pipe
        raw = text[cursor:segment_end]
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw) - len(raw.rstrip())
        content_start = cursor + leading
        content_end = segment_end - trailing
        segments.append(SourceSlice(text=text[content_start:content_end], start=line.start + content_start, end=line.start + content_end))
        if next_pipe == -1:
            break
        cursor = next_pipe + 1
        if cursor == right + 1:
            break
    return segments


def is_table_row(line: SourceSlice) -> bool:
    return "|" in line.text and line.text.strip().startswith("|") and line.text.strip().endswith("|")


def make_occurrence(occurrence_id: str, source: SourceSlice, source_path: Path) -> dict[str, Any]:
    return {
        "occurrence_id": occurrence_id,
        "role": "markdown_text",
        "text": source.text,
        "provenance": provenance(source_path, source.start, source.end),
    }


def parse_markdown(source_path: Path, document_id: str) -> list[dict[str, Any]]:
    source = source_path.read_text(encoding="utf-8")
    lines = line_slices(source)
    blocks: list[dict[str, Any]] = []
    # Heading levels can be skipped in Markdown (for example h1 -> h3), so a
    # positional list cannot safely represent the current hierarchy.  Keep
    # headings keyed by their actual level instead: a new heading replaces
    # only itself and deeper levels, while same-level siblings never become
    # parents of one another.
    heading_by_level: dict[int, str] = {}
    index = 0

    def current_section_path() -> str:
        return "/".join(heading_by_level[level] for level in sorted(heading_by_level))

    def add_text_block(kind: str, source_slice: SourceSlice, *, label: str | None = None) -> None:
        order = len(blocks)
        block_id = f"markdown:{document_id}:b{order}"
        occurrence_id = f"markdown:{document_id}:occ:b{order}"
        block: dict[str, Any] = {
            "block_id": block_id,
            "kind": kind,
            "structure_status": "explicit",
            "text": source_slice.text,
            "text_occurrence_ids": [occurrence_id],
            "reading_order": order,
            "page": None,
            "section_path": current_section_path(),
            "occurrences": [make_occurrence(occurrence_id, source_slice, source_path)],
            "provenance": provenance(source_path, source_slice.start, source_slice.end),
        }
        if label:
            block["source_block_label"] = label
        blocks.append(block)

    while index < len(lines):
        line = lines[index]
        if not line.text.strip() or THEMATIC_BREAK.match(line.text):
            index += 1
            continue

        heading = HEADING.match(line.text)
        if heading:
            level = len(heading.group(1))
            content_start = line.start + heading.start(2)
            content_end = line.start + heading.end(2)
            content = SourceSlice(line.text[heading.start(2):heading.end(2)], content_start, content_end)
            for active_level in tuple(heading_by_level):
                if active_level >= level:
                    del heading_by_level[active_level]
            heading_by_level[level] = content.text
            add_text_block("heading", content, label=f"markdown_heading_h{level}")
            index += 1
            continue

        # A table begins with a pipe row followed by Markdown's delimiter row.
        if index + 1 < len(lines) and is_table_row(line) and TABLE_DIVIDER.match(lines[index + 1].text):
            start_index = index
            row_indices = [index]
            index += 2  # skip the delimiter syntax; it carries no cell text.
            while index < len(lines) and is_table_row(lines[index]) and not TABLE_DIVIDER.match(lines[index].text):
                row_indices.append(index)
                index += 1
            table_start = lines[start_index].start
            table_end = lines[row_indices[-1]].end
            raw_table = SourceSlice(source[table_start:table_end], table_start, table_end)
            order = len(blocks)
            block_id = f"markdown:{document_id}:t{order}"
            table_occurrence_id = f"markdown:{document_id}:occ:t{order}"
            occurrences = [make_occurrence(table_occurrence_id, raw_table, source_path)]
            cells: list[dict[str, Any]] = []
            for row_index, line_index in enumerate(row_indices):
                for col_index, cell_slice in enumerate(split_table_cells(lines[line_index])):
                    occurrence_id = f"markdown:{document_id}:occ:t{order}:r{row_index}:c{col_index}"
                    cell_id = f"markdown:{document_id}:t{order}:r{row_index}:c{col_index}"
                    occurrences.append(make_occurrence(occurrence_id, cell_slice, source_path))
                    cells.append({
                        "cell_id": cell_id,
                        "row_index": row_index,
                        "col_index": col_index,
                        "row_span": 1,
                        "col_span": 1,
                        "text_occurrence_ids": [occurrence_id],
                        "evidence_ids": [occurrence_id],
                        "provenance": provenance(source_path, cell_slice.start, cell_slice.end),
                    })
            blocks.append({
                "block_id": block_id,
                "kind": "table",
                "structure_status": "explicit",
                "text": raw_table.text,
                "text_occurrence_ids": [table_occurrence_id],
                "reading_order": order,
                "page": None,
                "section_path": current_section_path(),
                "occurrences": occurrences,
                "cells": cells,
                "source_block_label": "markdown_table",
                "provenance": provenance(source_path, table_start, table_end),
            })
            continue

        list_item = LIST_ITEM.match(line.text)
        if list_item:
            content_start = line.start + list_item.start(2)
            content_end = line.start + list_item.end(2)
            add_text_block("paragraph", SourceSlice(line.text[list_item.start(2):list_item.end(2)], content_start, content_end), label="markdown_list_item")
            index += 1
            continue

        # Keep literal Markdown text intact for exact source-span provenance.
        # Consecutive ordinary lines form one paragraph block including source
        # newlines; this avoids manufacturing a normalized text basis.
        start_index = index
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if (not candidate.text.strip() or THEMATIC_BREAK.match(candidate.text) or HEADING.match(candidate.text)
                    or LIST_ITEM.match(candidate.text)
                    or (index + 1 < len(lines) and is_table_row(candidate) and TABLE_DIVIDER.match(lines[index + 1].text))):
                break
            index += 1
        start = lines[start_index].start
        end = lines[index - 1].end
        add_text_block("paragraph", SourceSlice(source[start:end], start, end), label="markdown_paragraph")

    return blocks


def build_document(source_path: Path, doc_id: str) -> dict[str, Any]:
    source_hash = sha256(source_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "document": {
            "document_id": f"request:{doc_id}",
            "source_kind": SOURCE_KIND,
            "artifact_role": "production",
            "page_count": None,
            "raw_artifact_ids": [],
            "provenance": {
                **provenance(source_path),
                "method": "markdown_fixture_adapter",
                "source_sha256": source_hash,
                "generator": GENERATOR,
                "generator_version": GENERATOR_VERSION,
                "schema_version": SCHEMA_VERSION,
                "parser": PARSER,
                "parser_version": PARSER_VERSION,
            },
        },
        "blocks": parse_markdown(source_path, doc_id),
        "conflicts": [],
        "relations": [],
    }


def table_summary(document: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for block in document["blocks"]:
        if block["kind"] != "table":
            continue
        cells = block.get("cells", [])
        output.append({
            "block_id": block["block_id"],
            "row_count": 1 + max((cell["row_index"] for cell in cells), default=-1),
            "column_count": 1 + max((cell["col_index"] for cell in cells), default=-1),
            "cell_count": len(cells),
            "structure_status": block["structure_status"],
        })
    return output


def assert_fixture_integrity(document: dict[str, Any], source_path: Path) -> list[str]:
    errors = list(validation_errors(document))
    source = source_path.read_text(encoding="utf-8")
    doc_provenance = document["document"]["provenance"]
    if doc_provenance["source_sha256"] != sha256(source_path):
        errors.append("source SHA-256 does not match source file")
    for block in document["blocks"]:
        for occurrence in block["occurrences"]:
            occurrence_provenance = occurrence["provenance"]
            start, end = occurrence_provenance.get("source_start_char"), occurrence_provenance.get("source_end_char")
            if start is None or end is None:
                errors.append(f"occurrence has no source span: {occurrence['occurrence_id']}")
            elif source[start:end] != occurrence.get("text", ""):
                errors.append(f"occurrence source span does not round-trip: {occurrence['occurrence_id']}")
    return errors


def write_handoff(
    output_root: Path, manifest_rows: list[dict[str, Any]], table_summaries: dict[str, list[dict[str, Any]]],
    *, presentation_cleaning: dict[str, Any] | None = None,
) -> None:
    rows = "\n".join(
        f"| `{row['document_id']}` | `{row['source_path']}` | `{row['common_ir_path']}` | `{row['reference_notice_id']}` |"
        for row in manifest_rows
    )
    table_notes = "\n".join(
        f"- `{doc_id}`: " + (", ".join(f"{item['row_count']}행×{item['column_count']}열 ({item['cell_count']}셀)" for item in tables) if tables else "Markdown 표 없음")
        for doc_id, tables in table_summaries.items()
    )
    cleaning_note = ""
    if presentation_cleaning:
        cleaning_note = f"""
## Cleaned semantic-input lineage

- Purpose: `{presentation_cleaning['purpose']}`
- Cleaning version: `{presentation_cleaning['version']}`
- Original fixture input root (read only): `{presentation_cleaning['original_input_root']}`
- Cleaned source copies: `{presentation_cleaning['cleaned_input_root']}`
- Rules: inline `**` / `__` presentation delimiters are removed; heading, list,
  and table syntax is retained so explicit structure remains available.
- Gold and the original corpus were not changed. Common IR occurrence offsets
  refer to each cleaned source copy, whose path and SHA-256 are in the manifest.
"""
    source_span_basis = "cleaned `.input.md` source copies" if presentation_cleaning else "original `.input.md` source"
    text = f"""# Request Markdown fixture → Common IR v1 handoff

## Contract

- Schema/version: `{SCHEMA_VERSION}`
- Source format: `{SOURCE_KIND}` (initial test-fixture input capability; not a Request-specific Common IR dialect or permanent representative source path)
- Parser: `{PARSER}` `{PARSER_VERSION}`
- Generator: `{GENERATOR}` `{GENERATOR_VERSION}`
- No LLM, OCR, PDF processing, Gold/score input, or Request business-semantic extraction was used.

## Input/output mapping

| document ID | Markdown source | Common IR output | reference notice ID (sidecar only) |
|---|---|---|---|
{rows}

`reference_notice_id` is stored only in `request_common_ir_manifest.json`; it is not inserted into Common IR blocks, occurrences, relations, or document text.

## Markdown mapping

- ATX headings become `heading` blocks; heading level/order is preserved in `source_block_label` and `section_path`.
- Ordinary paragraphs and list items become `paragraph` blocks. List marker syntax is structural; each list item's text occurrence points to its exact source substring.
- Markdown tables become `table` blocks with explicit `cells`. Header and data rows preserve row/column indexes, spans, cell occurrence IDs, and exact source-text spans. The Markdown divider row is syntax only and is not a semantic cell.
- All occurrence provenance uses Python Unicode code-point offsets, start-inclusive/end-exclusive, against the {source_span_basis}.

{cleaning_note}

## Table summaries

{table_notes}

## Validation

Every output passed Common IR v1 schema/cross-reference validation, source SHA-256 verification, occurrence source-span round-trip verification, and lineage join through `document_id` plus source hash. Only the declared fixture semantic input and its request manifest were read.

## Remaining limitation

This adapter preserves Markdown's explicit structure only. It does not infer merged cells, checkbox meaning, business fields, Request type canonical codes, comparison facts, or Gold/assessment results.
"""
    (output_root / "HANDOFF.md").write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=ROOT / "docs/PreReview_Request_Profile/test_request_corpus_v0.1/semantic_input")
    parser.add_argument("--output-root", type=Path, default=ROOT / "exploratory_study/results/request_profile_test_corpus_common_ir_v1_20260831")
    parser.add_argument(
        "--presentation-clean", action="store_true",
        help="materialize separate cleaned fixture inputs removing inline **/__ presentation markers",
    )
    args = parser.parse_args()
    original_input_dir = args.input_dir.resolve()
    output_root = args.output_root.resolve()
    source_manifest_path = original_input_dir / "request_manifest.json"
    manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    presentation_cleaning: dict[str, Any] | None = None
    cleaned_lineage: dict[str, dict[str, str]] = {}
    if args.presentation_clean:
        input_dir, cleaned_lineage = materialize_cleaned_input(original_input_dir, manifest, output_root)
        presentation_cleaning = {
            "enabled": True,
            "version": PRESENTATION_CLEANING_VERSION,
            "purpose": "remove inline Markdown presentation markers from semantic text",
            "original_input_root": str(original_input_dir),
            "cleaned_input_root": str(input_dir),
            "rules": ["remove ** and __ inline strong delimiters", "preserve heading/list/table syntax"],
        }
        source_manifest_path = input_dir / "request_manifest.json"
        manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    else:
        input_dir = original_input_dir
    documents = manifest.get("documents", [])
    if len(documents) != 5:
        raise ValueError("expected exactly five fixture documents")
    output_dir = output_root / "common_ir"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_rows: list[dict[str, Any]] = []
    summaries: dict[str, list[dict[str, Any]]] = {}
    validation_by_document: dict[str, Any] = {}
    for item in documents:
        doc_id = item["doc_id"]
        source_path = (input_dir / item["input_file"]).resolve()
        if source_path.parent != input_dir or source_path.suffix != ".md" or not source_path.name.endswith(".input.md"):
            raise ValueError(f"not an allowed semantic input file: {source_path}")
        document = build_document(source_path, doc_id)
        errors = assert_fixture_integrity(document, source_path)
        if errors:
            raise ValueError(f"{doc_id} Common IR validation failed: {errors}")
        output_path = output_dir / f"{doc_id}.common_ir_v1.json"
        output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output_row = {
            "document_id": document["document"]["document_id"],
            "common_ir_path": str(output_path),
            "source_path": str(source_path),
            "source_sha256": document["document"]["provenance"]["source_sha256"],
            "reference_notice_id": item.get("reference_notice_id"),
            "is_synthetic_fixture": bool(item.get("is_synthetic_fixture")),
        }
        if args.presentation_clean:
            output_row["source_presentation_cleaning"] = PRESENTATION_CLEANING_VERSION
            output_row.update(cleaned_lineage[doc_id])
        output_rows.append(output_row)
        summaries[document["document"]["document_id"]] = table_summary(document)
        validation_by_document[document["document"]["document_id"]] = {
            "schema_errors": [], "dangling_references": 0,
            "source_span_round_trip_errors": 0,
            "gold_or_scoring_input_used": False,
            "table_summary": summaries[document["document"]["document_id"]],
        }

    request_manifest = {"corpus_version": manifest.get("corpus_version"), "entries": output_rows}
    (output_root / "request_common_ir_manifest.json").write_text(json.dumps(request_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_manifest = {
        "schema_version": SCHEMA_VERSION,
        "adapter": {"generator": GENERATOR, "generator_version": GENERATOR_VERSION, "parser": PARSER, "parser_version": PARSER_VERSION},
        "input_root": str(input_dir),
        "allowed_inputs": [str((input_dir / item["input_file"]).resolve()) for item in documents] + [str(source_manifest_path.resolve())],
        "declared_constraints": {"llm_called": False, "ocr_called": False, "pdf_processed": False, "gold_or_scoring_input_used": False},
        "validation": validation_by_document,
    }
    if presentation_cleaning:
        run_manifest["presentation_cleaning"] = presentation_cleaning
        run_manifest["cleaned_source_lineage"] = cleaned_lineage
        run_manifest["source_materialization_input_paths"] = [
            str((original_input_dir / item["input_file"]).resolve()) for item in documents
        ] + [str((original_input_dir / "request_manifest.json").resolve())]
        run_manifest["common_ir_input_paths"] = run_manifest["allowed_inputs"]
    (output_root / "run_manifest.json").write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_handoff(output_root, output_rows, summaries, presentation_cleaning=presentation_cleaning)
    print(json.dumps({"output_root": str(output_root), "documents": len(output_rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
