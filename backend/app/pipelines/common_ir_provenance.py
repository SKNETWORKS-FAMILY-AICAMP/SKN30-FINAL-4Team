"""Fail-closed provenance checks for model-safe Common IR inputs.

The guard intentionally does not inspect ordinary source-text payloads.  A
notice may legitimately use words such as ``adjudication`` in its body; only
metadata and provenance may establish manual-oracle contamination.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


MAX_METADATA_SCAN_DEPTH = 128
_TEXT_PAYLOAD_KEYS = frozenset(
    {"content", "excerpt", "normalized_text", "raw_text", "text", "value_raw"}
)
_PROVENANCE_MARKER_KEYS = frozenset(
    {
        "generator",
        "lineage",
        "method",
        "origin",
        "parser",
        "producer",
        "source_location",
        "transform",
    }
)
_PROVENANCE_CONTAINER_KEYS = frozenset({"llm_provenance", "provenance"})
_ADJUDICATION_LABEL_KEYS = frozenset({"source_block_label"})
_ADJUDICATION_MARKER_FRAGMENTS = (
    "adjudicat",
    "gold_patch",
    "goldpatch",
    "manual_gold",
)


class CommonIrProvenanceError(ValueError):
    """A Common IR document carries unsafe manual-adjudication lineage."""


def contains_manual_adjudication_metadata(document: Mapping[str, Any]) -> bool:
    """Return whether metadata, rather than source text, names a manual oracle."""

    def marker(value: str) -> bool:
        lowered = value.strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
        return lowered.startswith("adj:") or any(
            fragment in normalized for fragment in _ADJUDICATION_MARKER_FRAGMENTS
        )

    def visit(
        value: Any,
        *,
        parent_key: str = "",
        provenance_context: bool = False,
        depth: int = 0,
    ) -> bool:
        if depth > MAX_METADATA_SCAN_DEPTH:
            raise CommonIrProvenanceError("Common IR metadata nesting exceeds limit")
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key).strip().lower()
                child_provenance_context = provenance_context or key in _PROVENANCE_CONTAINER_KEYS
                if key in _TEXT_PAYLOAD_KEYS and isinstance(item, str) and not child_provenance_context:
                    continue
                if marker(key):
                    return True
                if isinstance(item, str):
                    if (
                        child_provenance_context
                        or key.endswith("_id")
                        or key in _PROVENANCE_MARKER_KEYS
                        or key in _ADJUDICATION_LABEL_KEYS
                    ) and marker(item):
                        return True
                elif isinstance(item, list) and key.endswith("_ids"):
                    if any(isinstance(member, str) and marker(member) for member in item):
                        return True
                elif isinstance(item, list) and (
                    child_provenance_context or key in _PROVENANCE_MARKER_KEYS
                ):
                    if any(isinstance(member, str) and marker(member) for member in item):
                        return True
                if visit(
                    item,
                    parent_key=key,
                    provenance_context=child_provenance_context,
                    depth=depth + 1,
                ):
                    return True
            return False
        if isinstance(value, list):
            if (parent_key.endswith("_ids") or provenance_context) and any(
                isinstance(member, str) and marker(member) for member in value
            ):
                return True
            return any(
                visit(
                    item,
                    parent_key=parent_key,
                    provenance_context=provenance_context,
                    depth=depth + 1,
                )
                for item in value
            )
        return False

    try:
        return visit(document)
    except RecursionError as error:
        raise CommonIrProvenanceError("Common IR metadata nesting exceeds limit") from error


def require_automatic_common_ir(document: Mapping[str, Any]) -> None:
    """Reject manual-adjudication provenance before a document is frozen or used."""

    if contains_manual_adjudication_metadata(document):
        raise CommonIrProvenanceError("Common IR contains manual adjudication provenance")
