"""Validation helpers for the published common_ir_v0_1 contract."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


PACKAGE_SCHEMA = Path(__file__).resolve().parent / "config" / "common_ir_v0_1.schema.json"
LEGACY_SCHEMA = Path("exploratory_study/results/runpod_hybrid_ir_validation/config/common_ir_v0_1.schema.json")


def schema() -> dict:
    source = PACKAGE_SCHEMA if PACKAGE_SCHEMA.is_file() else LEGACY_SCHEMA
    return json.loads(source.read_text(encoding="utf-8"))


def validation_errors(document: dict) -> list[str]:
    """Return schema and cross-reference errors without mutating the document."""
    errors = [error.message for error in Draft202012Validator(schema()).iter_errors(document)]
    doc = document.get("document", {})
    source_kind, artifact_role = doc.get("source_kind"), doc.get("artifact_role")
    if source_kind == "pdf" and artifact_role != "production":
        errors.append("PDF Common IR must have artifact_role=production")

    occurrence_ids, block_ids, duplicate_occurrences, duplicate_blocks = set(), set(), set(), set()
    for block in document.get("blocks", []):
        block_id = block.get("block_id")
        if block_id in block_ids:
            duplicate_blocks.add(block_id)
        block_ids.add(block_id)
        for occurrence in block.get("occurrences", []):
            occurrence_id = occurrence.get("occurrence_id")
            if occurrence_id in occurrence_ids:
                duplicate_occurrences.add(occurrence_id)
            occurrence_ids.add(occurrence_id)
            if source_kind == "pdf" and occurrence.get("role", "").startswith("rhwp_"):
                errors.append(f"PDF production IR may not contain rhwp occurrence: {occurrence_id}")
    for value in sorted(duplicate_blocks):
        errors.append(f"duplicate block_id: {value}")
    for value in sorted(duplicate_occurrences):
        errors.append(f"duplicate occurrence_id: {value}")

    for block in document.get("blocks", []):
        if block.get("cells") and (block.get("kind") != "table" or block.get("structure_status") != "explicit"):
            errors.append(f"cells require table/explicit block: {block.get('block_id')}")
        for cell in block.get("cells", []):
            for reference in (*cell.get("evidence_ids", []), *cell.get("text_occurrence_ids", [])):
                if reference not in occurrence_ids:
                    errors.append(f"cell occurrence reference does not exist: {reference}")
    for relation in document.get("relations", []):
        for reference in (relation.get("from_id"), relation.get("to_id"), *relation.get("evidence_ids", [])):
            if reference not in occurrence_ids:
                errors.append(f"relation reference does not exist: {reference}")
    return errors
