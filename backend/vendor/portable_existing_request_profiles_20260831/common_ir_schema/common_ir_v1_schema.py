"""JSON Schema + cross-reference validation for Common IR v1.

v1 exists because v0.1/v0.2/v0.3 (config/common_ir_v0_1.schema.json) hard-
forbid inferred relations (`"inferred": {"const": false}`) and give a
relation only two possible endpoints (`from_id`/`to_id`, plain strings) --
there is no room to say "this LLM-proposed edge's endpoint matched two
identical-text native occurrences, here are both, unresolved" without either
silently picking one (forbidden) or dropping the relation (also forbidden).
v1 keeps v0.1's object shape (document/blocks/occurrences/cells/relations/
conflicts) and *adds* room for that, plus the block-level fields (`text`,
`text_occurrence_ids`, `reading_order`, `page`, `section_path`,
`boundary_markers`) needed for semantic (not one-native-fragment-per-block)
blocks. v0.1/v0.2/v0.3 files and their schemas are untouched by this module.

document.provenance lineage contract (validated by validation_errors() below,
not by the shared `provenance` $def -- see the comment on _PROVENANCE):
document.provenance must carry source_sha256/generator/generator_version/
schema_version/parser/parser_version, so a v1 document is self-describing and
reproducible without consulting anything outside the file itself.
`parser`/`parser_version` identify the *upstream* tool that produced the raw
IR this document was adapted from -- "rhwp" for HWP/HWPX, "pdf_inspector" for
PDF -- independently of `generator`/`generator_version` (which name this
adapter script, not the upstream parser); those two pairs answer different questions
("what shape did this get adapted into" vs "what actually read the original
file") and must never be conflated. `parser_core_version` is rhwp-specific
(its own native/core library version, distinct from the rhwp Python
package's own `parser_version`) and is carried only when the upstream
raw IR actually reports it -- deterministically: the literal value rhwp
itself returns, and simply absent (never guessed at, never a stale
placeholder) when unavailable. PDF documents never carry
parser_core_version; "core" is not a PDF concept.

Native PDF capability limits: a `source_kind=pdf` production document only
ever carries native (non-OCR) text -- pdf_semantic_eligibility is
`eligible_native_text` only when the source PDF has an extractable native
text layer, and `excluded_image_only` (empty blocks[]) otherwise; a
scanned/flattened PDF with no text layer can never become eligible_native_text
regardless of how good OCR might make it look elsewhere in this project.
`role=ocr_text` and any `rhwp_*` occurrence role are rejected outright on a
pdf document (see the source_kind == "pdf" checks below) -- OCR is legacy
structural evidence carried as textless `layout_region`, never a text source
for production PDF IR.

ID scope: block_id/occurrence_id/cell_id/relation_id are unique only *within
one Common IR v1 document* (that's what the checks below enforce) -- they
carry no cross-document uniqueness guarantee, and the same raw artifact
re-adapted into two different documents (e.g. a PDF re-run) is expected to
reproduce the same ids, not mint fresh ones.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator

_PROVENANCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["method", "page", "bbox", "coordinate_space", "source_location"],
    "properties": {
        "method": {"type": "string", "minLength": 1},
        "page": {"type": ["integer", "null"], "minimum": 1},
        "bbox": {"oneOf": [{"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}}, {"type": "null"}]},
        "coordinate_space": {"type": ["string", "null"]},
        "source_location": {"type": "string", "minLength": 1},
        # Optional generic source-text span. Adapters that can preserve a
        # source substring (for example Markdown fixtures) record Python
        # Unicode code-point offsets here. This is provenance, not a
        # Structured Profile value span.
        "source_start_char": {"type": "integer", "minimum": 0},
        "source_end_char": {"type": "integer", "minimum": 0},
        # document.provenance extras (§1 of the v1 contract): reproducibility
        # and lineage metadata. Optional here (in the shared $def) so
        # block/occurrence/relation/cell provenance don't have to carry
        # them; validation_errors() below enforces which of these
        # document.provenance itself must actually have.
        "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "generator": {"type": "string", "minLength": 1},
        "generator_version": {"type": "string", "minLength": 1},
        "schema_version": {"const": "common_ir_v1"},
        # Upstream parser identity -- independent of generator/generator_version
        # (this adapter script's own name/version). "rhwp" for HWP/HWPX,
        # "pdf_inspector" for PDF.
        "parser": {"type": "string", "minLength": 1},
        "parser_version": {"type": "string", "minLength": 1},
        # rhwp-only; present iff the raw rhwp IR reported it.
        "parser_core_version": {"type": "string", "minLength": 1},
    },
}

_OCCURRENCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["occurrence_id", "role", "provenance"],
    "properties": {
        "occurrence_id": {"type": "string", "minLength": 1},
        "role": {"type": "string", "enum": ["rhwp_cell", "native_text", "ocr_text", "layout_region", "rhwp_text", "markdown_text"]},
        "text": {"type": "string"},
        "producer_score": {"type": "number"},
        "provenance": {"$ref": "#/$defs/provenance"},
    },
}

_CELL = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cell_id", "evidence_ids", "provenance"],
    "properties": {
        "cell_id": {"type": "string", "minLength": 1},
        "evidence_ids": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "row_index": {"type": "integer", "minimum": 0},
        "col_index": {"type": "integer", "minimum": 0},
        "row_span": {"type": "integer", "minimum": 1},
        "col_span": {"type": "integer", "minimum": 1},
        "text_occurrence_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "provenance": {"$ref": "#/$defs/provenance"},
    },
}

_BOUNDARY_MARKER = {
    "type": "object", "additionalProperties": False,
    "required": ["marker", "matched_text"],
    "properties": {"marker": {"type": "string", "minLength": 1}, "matched_text": {"type": "string", "minLength": 1}},
}

_BLOCK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["block_id", "kind", "structure_status", "text", "text_occurrence_ids", "reading_order", "page", "section_path", "occurrences", "provenance"],
    "properties": {
        "block_id": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["paragraph", "heading", "table", "table_candidate", "diagram_candidate"]},
        "structure_status": {"type": "string", "enum": ["explicit", "partial"]},
        "text": {"type": "string"},
        "text_occurrence_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "reading_order": {"type": "integer", "minimum": 0},
        "page": {"type": ["integer", "null"], "minimum": 1},
        "section_path": {"type": "string"},
        "occurrences": {"type": "array", "items": {"$ref": "#/$defs/occurrence"}},
        "cells": {"type": "array", "items": {"$ref": "#/$defs/cell"}},
        "boundary_markers": {"type": "array", "items": {"$ref": "#/$defs/boundary_marker"}},
        "source_block_label": {"type": "string"},
        "provenance": {"$ref": "#/$defs/provenance"},
    },
}

_ENDPOINT_NODE = {
    "type": "object", "additionalProperties": False,
    "required": ["label", "mapping_status", "occurrence_ids"],
    "properties": {
        "label": {"type": "string", "minLength": 1},
        "mapping_status": {"type": "string", "enum": ["unique", "ambiguous", "unresolved"]},
        "occurrence_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
}

_RELATION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relation_id", "kind", "evidence_ids", "inferred", "provenance"],
    "properties": {
        "relation_id": {"type": "string", "minLength": 1},
        # table_contains is the one relation kind with intentionally
        # asymmetric endpoints: from_id names a *cell_id* (the parent
        # table's own cell, e.g. a PDF cell's provenance bbox containing a
        # nested table, or an rhwp cell holding a nested table) and to_id
        # names the nested child table/table_candidate's *block_id* -- never
        # a parent cell's occurrence_id and never the child's occurrence_id.
        # `inferred` describes endpoint-resolution ambiguity; the derivation
        # method itself is recorded in relation.provenance.method. See
        # validation_errors() for the matching reference check.
        "kind": {"type": "string", "enum": ["order", "parent_child", "diagram_edge", "table_continuation", "table_contains"]},
        # An explicit relation cites plain occurrence ids; an inferred one
        # (ambiguous/unresolved endpoints allowed) cites endpoint evidence
        # objects instead -- never both, never neither.
        "from_id": {"type": "string", "minLength": 1},
        "to_id": {"type": "string", "minLength": 1},
        "from_node": {"$ref": "#/$defs/endpoint_node"},
        "to_node": {"$ref": "#/$defs/endpoint_node"},
        # Evidence must cite a final occurrence id (e.g. "occ:surya:p2:b5"),
        # never a raw artifact/file path -- reject anything with a path
        # separator outright so a stray source_location doesn't slip in.
        "evidence_ids": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1, "pattern": "^[^/\\\\]+$"}},
        "inferred": {"type": "boolean"},
        "structure_status": {"type": "string", "enum": ["explicit", "partial"]},
        "review_status": {"type": "string"},
        "ambiguous_evidence": {"type": "boolean"},
        "observed_arrow": {"type": "string", "minLength": 1},
        "llm_provenance": {"type": "object"},
        "provenance": {"$ref": "#/$defs/provenance"},
    },
    "oneOf": [
        {"required": ["from_id", "to_id"]},
        {"required": ["from_node", "to_node"]},
    ],
    "allOf": [
        # inferred=false: explicit relation, plain occurrence endpoints.
        {
            "if": {"properties": {"inferred": {"const": False}}, "required": ["inferred"]},
            "then": {
                "required": ["from_id", "to_id", "structure_status"],
                "properties": {"structure_status": {"const": "explicit"}},
            },
        },
        # inferred=true: partial/needs_review relation, endpoint-node evidence
        # (ambiguous/unresolved occurrence_ids allowed on the node itself).
        {
            "if": {"properties": {"inferred": {"const": True}}, "required": ["inferred"]},
            "then": {
                "required": ["from_node", "to_node", "structure_status", "review_status"],
                "properties": {"structure_status": {"const": "partial"}, "review_status": {"const": "needs_review"}},
            },
        },
    ],
}

_CONFLICT = {
    "type": "object", "additionalProperties": False,
    "required": ["conflict_id", "kind", "occurrence_ids"],
    "properties": {
        "conflict_id": {"type": "string", "minLength": 1},
        "kind": {"const": "native_ocr"},
        "occurrence_ids": {"type": "array", "minItems": 2, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "note": {"type": "string"},
    },
}

_DOCUMENT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document_id", "source_kind", "artifact_role", "provenance"],
    "properties": {
        "document_id": {"type": "string", "minLength": 1},
        # markdown_fixture is a generic fixture/source-format capability. It
        # is deliberately not a Request-specific dialect: it uses the same
        # block/table/cell/occurrence contract as HWP/HWPX/PDF.
        "source_kind": {"type": "string", "enum": ["hwp", "hwpx", "pdf", "markdown_fixture"]},
        "artifact_role": {"const": "production"},
        "page_count": {"type": ["integer", "null"], "minimum": 0},
        "pdf_semantic_eligibility": {"type": "string", "enum": ["eligible_native_text", "excluded_image_only"]},
        "pdf_semantic_reason": {"type": "string", "enum": ["substantive_native_text_available", "no_substantive_native_text"]},
        "native_text_page_count": {"type": "integer", "minimum": 0},
        "raw_artifact_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "provenance": {"$ref": "#/$defs/provenance"},
    },
}


def schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "common_ir_v1.schema.json",
        "title": "공통 IR v1",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "document", "blocks"],
        "properties": {
            "schema_version": {"const": "common_ir_v1"},
            "document": {"$ref": "#/$defs/document"},
            "blocks": {"type": "array", "items": {"$ref": "#/$defs/block"}},
            "conflicts": {"type": "array", "items": {"$ref": "#/$defs/conflict"}},
            "relations": {"type": "array", "items": {"$ref": "#/$defs/relation"}},
        },
        "$defs": {
            "provenance": _PROVENANCE, "document": _DOCUMENT, "occurrence": _OCCURRENCE,
            "cell": _CELL, "boundary_marker": _BOUNDARY_MARKER, "block": _BLOCK,
            "endpoint_node": _ENDPOINT_NODE, "relation": _RELATION, "conflict": _CONFLICT,
        },
    }


def validation_errors(document: dict) -> list[str]:
    """Schema errors plus the same cross-reference checks v0.1/v0.3 apply:
    no duplicate block/occurrence ids, and every evidence_ids/from_id/to_id/
    from_node.occurrence_ids/to_node.occurrence_ids reference must resolve to
    a real occurrence (empty occurrence_ids on an unresolved endpoint is
    fine -- there's nothing to check). table_continuation is block_id-to-
    block_id, and table_contains is cell_id-to-block_id (see the `kind`
    schema comment above) -- every other relation kind is occurrence_id-to-
    occurrence_id, same as evidence_ids always is.

    This is a two-pass check so block order never matters: pass 1 collects
    every block_id/occurrence_id (and the block-level checks that don't need
    any other block's data); pass 2 resolves cell/block/relation/conflict
    references against the *complete* id sets from pass 1. A table block
    sorted before the paragraph block holding one of its cell's evidence
    occurrences must validate identically to the reverse order.
    """
    errors = [error.message for error in Draft202012Validator(schema()).iter_errors(document)]

    # document.provenance lineage contract (module docstring above): these
    # six are required regardless of source_kind. This is a targeted
    # document-level check, not a blanket addition to the shared
    # `provenance` $def's own `required`, because block/occurrence/relation/
    # cell provenance must NOT be forced to carry them.
    document_provenance = document.get("document", {}).get("provenance") or {}
    for key in ("source_sha256", "generator", "generator_version", "schema_version", "parser", "parser_version"):
        if key not in document_provenance:
            errors.append(f"document.provenance missing required field: {key}")

    source_kind = document.get("document", {}).get("source_kind")
    blocks = document.get("blocks", [])
    if source_kind == "pdf":
        metadata = document.get("document", {})
        required_pdf_metadata = {
            "pdf_semantic_eligibility", "pdf_semantic_reason", "native_text_page_count", "page_count",
        }
        for key in sorted(required_pdf_metadata - set(metadata)):
            errors.append(f"PDF production IR missing required semantic metadata: {key}")
        if metadata.get("pdf_semantic_eligibility") == "eligible_native_text" and metadata.get("native_text_page_count", 0) <= 0:
            errors.append("eligible_native_text requires native_text_page_count > 0")
        if metadata.get("pdf_semantic_eligibility") == "excluded_image_only" and metadata.get("native_text_page_count") not in (0, None):
            errors.append("excluded_image_only requires native_text_page_count == 0")
        expected_reason = {
            "eligible_native_text": "substantive_native_text_available",
            "excluded_image_only": "no_substantive_native_text",
        }.get(metadata.get("pdf_semantic_eligibility"))
        if expected_reason and metadata.get("pdf_semantic_reason") != expected_reason:
            errors.append("PDF semantic eligibility/reason pairing is inconsistent")
        if isinstance(metadata.get("native_text_page_count"), int) and isinstance(metadata.get("page_count"), int) and metadata["native_text_page_count"] > metadata["page_count"]:
            errors.append("native_text_page_count may not exceed page_count")

    # Pass 1: collect ids and run the checks that are local to one block.
    occurrence_ids, block_ids, cell_ids, reading_orders, relation_ids = set(), set(), set(), [], set()
    for block in blocks:
        block_id = block.get("block_id")
        if block_id in block_ids:
            errors.append(f"duplicate block_id: {block_id}")
        block_ids.add(block_id)
        if "reading_order" in block:
            reading_orders.append(block["reading_order"])
        if block.get("cells") and (block.get("kind") != "table" or block.get("structure_status") != "explicit"):
            errors.append(f"cells require table/explicit block: {block_id}")
        for cell in block.get("cells") or []:
            cell_id = cell.get("cell_id")
            if cell_id in cell_ids:
                errors.append(f"duplicate cell_id: {cell_id}")
            cell_ids.add(cell_id)
        for occurrence in block.get("occurrences", []):
            occurrence_id = occurrence.get("occurrence_id")
            if occurrence_id in occurrence_ids:
                errors.append(f"duplicate occurrence_id: {occurrence_id}")
            occurrence_ids.add(occurrence_id)
            if source_kind == "pdf" and occurrence.get("role", "").startswith("rhwp_"):
                errors.append(f"PDF production IR may not contain rhwp occurrence: {occurrence_id}")
            if source_kind == "pdf" and occurrence.get("role") == "ocr_text":
                errors.append(f"native-only PDF production IR may not contain ocr_text occurrence: {occurrence_id}")
            if source_kind == "pdf" and "text" in occurrence and occurrence.get("role") != "native_text":
                errors.append(f"native-only PDF production IR may only put text on native_text occurrences: {occurrence_id}")

    if reading_orders and sorted(reading_orders) != list(range(len(reading_orders))):
        errors.append("block reading_order must be dense and unique (0..N-1)")

    # Pass 2: resolve references against the complete id sets above.
    for block in blocks:
        for cell in block.get("cells") or []:
            for reference in (*cell.get("evidence_ids", []), *cell.get("text_occurrence_ids", [])):
                if reference not in occurrence_ids:
                    errors.append(f"cell occurrence reference does not exist: {reference}")
        for reference in block.get("text_occurrence_ids", []):
            if reference not in occurrence_ids:
                errors.append(f"block text_occurrence_ids reference does not exist: {reference}")

    for relation in document.get("relations", []):
        relation_id = relation.get("relation_id")
        if relation_id in relation_ids:
            errors.append(f"duplicate relation_id: {relation_id}")
        relation_ids.add(relation_id)
        for reference in relation.get("evidence_ids", []):
            if reference not in occurrence_ids:
                errors.append(f"relation evidence reference does not exist: {reference}")
        if relation.get("kind") == "table_continuation":
            for endpoint in (relation.get("from_id"), relation.get("to_id")):
                if endpoint is not None and endpoint not in block_ids:
                    errors.append(f"table_continuation block reference does not exist: {endpoint}")
        elif relation.get("kind") == "table_contains":
            from_id = relation.get("from_id")
            if from_id is not None and from_id not in cell_ids:
                errors.append(f"table_contains from_id must reference an existing cell_id: {from_id}")
            to_id = relation.get("to_id")
            if to_id is not None and to_id not in block_ids:
                errors.append(f"table_contains to_id must reference an existing block_id: {to_id}")
            elif to_id is not None:
                child_block = next((block for block in blocks if block.get("block_id") == to_id), None)
                if child_block is not None and child_block.get("kind") not in {"table", "table_candidate"}:
                    errors.append(f"table_contains to_id must reference a table or table_candidate block: {to_id}")
        else:
            for key in ("from_id", "to_id"):
                endpoint = relation.get(key)
                if endpoint is not None and endpoint not in occurrence_ids:
                    errors.append(f"relation occurrence reference does not exist: {endpoint}")
            for key in ("from_node", "to_node"):
                node = relation.get(key)
                if node:
                    for occurrence_id in node.get("occurrence_ids", []):
                        if occurrence_id not in occurrence_ids:
                            errors.append(f"relation node occurrence reference does not exist: {occurrence_id}")
    if source_kind == "pdf" and document.get("conflicts"):
        errors.append("native-only PDF production IR may not contain conflicts")
    for conflict in document.get("conflicts", []):
        for reference in conflict.get("occurrence_ids", []):
            if reference not in occurrence_ids:
                errors.append(f"conflict occurrence reference does not exist: {reference}")
    return errors
