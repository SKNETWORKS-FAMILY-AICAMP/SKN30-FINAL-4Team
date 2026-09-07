"""Validation helpers for Common IR v0.2.

v0.2 keeps the v0.1 object contract and adds an explicit PDF multi-page-table
relation.  It also permits evidence-backed cell fragments in a partial table;
that permission does not imply that a full grid is known.
"""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

PACKAGE_SCHEMA = Path(__file__).resolve().parent / "config" / "common_ir_v0_1.schema.json"
LEGACY_SCHEMA = Path("exploratory_study/results/runpod_hybrid_ir_validation/config/common_ir_v0_1.schema.json")


def schema() -> dict:
    source = PACKAGE_SCHEMA if PACKAGE_SCHEMA.is_file() else LEGACY_SCHEMA
    value = json.loads(source.read_text(encoding="utf-8"))
    value["$id"] = "common_ir_v0_2.schema.json"
    value["title"] = "공통 IR v0.2"
    value["properties"]["schema_version"]["const"] = "common_ir_v0_2"
    value["$defs"]["relation"]["properties"]["kind"]["enum"].append("table_continuation")
    return value


def validation_errors(document: dict) -> list[str]:
    errors = [error.message for error in Draft202012Validator(schema()).iter_errors(document)]
    doc = document.get("document", {})
    source_kind, artifact_role = doc.get("source_kind"), doc.get("artifact_role")
    if source_kind == "pdf" and artifact_role != "production":
        errors.append("PDF Common IR must have artifact_role=production")
    occurrence_ids, block_ids, duplicate_occurrences, duplicate_blocks = set(), set(), set(), set()
    blocks_by_id = {}
    for block in document.get("blocks", []):
        block_id = block.get("block_id")
        if block_id in block_ids: duplicate_blocks.add(block_id)
        block_ids.add(block_id); blocks_by_id[block_id] = block
        for occurrence in block.get("occurrences", []):
            occurrence_id = occurrence.get("occurrence_id")
            if occurrence_id in occurrence_ids: duplicate_occurrences.add(occurrence_id)
            occurrence_ids.add(occurrence_id)
            if source_kind == "pdf" and occurrence.get("role", "").startswith("rhwp_"):
                errors.append(f"PDF production IR may not contain rhwp occurrence: {occurrence_id}")
    for value in sorted(duplicate_blocks): errors.append(f"duplicate block_id: {value}")
    for value in sorted(duplicate_occurrences): errors.append(f"duplicate occurrence_id: {value}")
    for block in document.get("blocks", []):
        # v0.2 permits evidence-backed fragments on partial tables/candidates.
        for cell in block.get("cells", []):
            for reference in (*cell.get("evidence_ids", []), *cell.get("text_occurrence_ids", [])):
                if reference not in occurrence_ids: errors.append(f"cell occurrence reference does not exist: {reference}")
    for relation in document.get("relations", []):
        for reference in (*relation.get("evidence_ids", []),):
            if reference not in occurrence_ids: errors.append(f"relation evidence reference does not exist: {reference}")
        endpoints = (relation.get("from_id"), relation.get("to_id"))
        if relation.get("kind") == "table_continuation":
            for endpoint in endpoints:
                if endpoint not in block_ids: errors.append(f"table_continuation block reference does not exist: {endpoint}")
                elif blocks_by_id[endpoint].get("kind") not in {"table", "table_candidate"}: errors.append(f"table_continuation endpoint is not a table: {endpoint}")
        else:
            for endpoint in endpoints:
                if endpoint not in occurrence_ids: errors.append(f"relation reference does not exist: {endpoint}")
    return errors
