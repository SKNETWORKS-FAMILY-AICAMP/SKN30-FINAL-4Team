"""Offline structural projection for a bound OpenDataLoader document.

The OpenDataLoader parser is useful for proposing document shape, especially
for nested lists and tables.  It is not a text authority and its coordinates
have not been calibrated to the source PDF.  This module deliberately emits a
small, deterministic, *non-promotable* sidecar: it contains only structural
identities, hierarchy, ordering, page mapping, and unverified node-local
geometry.  It cannot create Common IR blocks, native anchors, or facts.
"""
from __future__ import annotations

from hashlib import sha256
import math
from typing import Any, Mapping, Sequence

from .opendataloader_artifact import (
    MAX_ABSOLUTE_COORDINATE,
    MAX_JSON_DEPTH,
    MAX_OBJECTS,
    OpenDataLoaderArtifact,
    OpenDataLoaderArtifactError,
    canonical_json_bytes,
    validate_opendataloader_artifact,
    SCHEMA_VERSION as STRICT_BINDING_SCHEMA_VERSION,
)
from .legacy_opendataloader_evaluation import (
    LegacyOpenDataLoaderEvaluation,
    LegacyOpenDataLoaderEvaluationError,
    validate_legacy_opendataloader_evaluation,
    SCHEMA_VERSION as LEGACY_BINDING_SCHEMA_VERSION,
)


SCHEMA_VERSION = "pdf_structure_candidates/v1"
COORDINATE_STATUS = "odl_pdf_points_unverified"
ORDERING_STATUS = "odl_traversal_unverified"
MAX_CANDIDATES = MAX_OBJECTS
MAX_TRAVERSAL_DEPTH = MAX_JSON_DEPTH

_NODE_KIND = {
    "heading": "heading",
    "paragraph": "paragraph",
    "list": "list",
    "list item": "list_item",
    "table": "table",
    "table row": "table_row",
    "table cell": "table_cell",
    "text block": "text_block",
    "image": "image",
    "caption": "caption",
}
_CHILD_FIELDS = frozenset({"kids", "list items", "rows", "cells"})
_ALLOWED_CHILD_FIELDS = {
    "list": frozenset({"kids", "list items"}),
    "table": frozenset({"kids", "rows"}),
    "table row": frozenset({"kids", "cells"}),
    "table cell": frozenset({"kids"}),
    "heading": frozenset({"kids"}),
    "paragraph": frozenset({"kids"}),
    "list item": frozenset({"kids"}),
    "text block": frozenset({"kids"}),
    "image": frozenset({"kids"}),
    "caption": frozenset({"kids"}),
}
_REQUIRED_CHILD_TYPE = {
    "list items": "list item",
    "rows": "table row",
    "cells": "table cell",
}
_TABLE_INT_FIELDS = {
    "number of rows": "declared_row_count",
    "number of columns": "declared_column_count",
    "row number": "row_number",
    "column number": "column_number",
    "row span": "row_span",
    "column span": "column_span",
}
_ROOT_KEYS = frozenset({
    "schema_version", "notice_id", "source_pdf_sha256", "opendataloader_raw_json_sha256",
    "opendataloader_canonical_json_sha256", "input_binding_schema_version", "input_binding_sha256", "source_page_count",
    "odl_page_count", "page_scope", "odl_page_to_source_page", "coordinate_status",
    "ordering_status", "evaluation_only", "non_promotable", "candidates",
})
_PAGE_MAP_KEYS = frozenset({"odl_page", "source_page"})
_CANDIDATE_REQUIRED_KEYS = frozenset({
    "candidate_id", "parent_candidate_id", "odl_page", "source_page", "raw_type", "normalized_kind",
    "traversal_order", "traversal_path",
})
_CANDIDATE_OPTIONAL_KEYS = frozenset({"bbox_odl_pdf_points_unverified", "table_metadata"})
_TABLE_METADATA_BY_TYPE = {
    "table": frozenset({"declared_row_count", "declared_column_count"}),
    "table row": frozenset({"row_number"}),
    "table cell": frozenset({"row_number", "column_number", "row_span", "column_span"}),
}


class PdfStructureCandidatesError(ValueError):
    """Raised when a structural projection would be ambiguous or unsafe."""


def _positive_int(name: str, value: object, *, maximum: int = MAX_CANDIDATES) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise PdfStructureCandidatesError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _node_id(value: object) -> str:
    if isinstance(value, bool):
        raise PdfStructureCandidatesError("ODL node id must be a positive integer or bounded string")
    if isinstance(value, int):
        if value < 1:
            raise PdfStructureCandidatesError("ODL node id must be positive")
        return f"int:{value}"
    if isinstance(value, str):
        if not value or value != value.strip() or len(value.encode("utf-8")) > 4096:
            raise PdfStructureCandidatesError("ODL node id must be a bounded trimmed string")
        return f"str:{value}"
    raise PdfStructureCandidatesError("ODL node id must be a positive integer or bounded string")


def _candidate_id(odl_page: int, node_id: str) -> str:
    # A digest keeps arbitrary parser IDs out of output paths while binding the
    # candidate to both the ODL page and the typed ODL object identity.
    identity = canonical_json_bytes({"odl_page": odl_page, "node_id": node_id})
    return "odl-" + sha256(identity).hexdigest()


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PdfStructureCandidatesError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or abs(result) > MAX_ABSOLUTE_COORDINATE:
        raise PdfStructureCandidatesError(f"{name} must be a bounded finite number")
    return 0.0 if result == 0.0 else result


def _node_bbox(node: Mapping[str, Any]) -> list[float] | None:
    present = [name for name in ("bounding box", "bbox") if name in node]
    if len(present) > 1:
        raise PdfStructureCandidatesError("ODL node must not provide both bounding box and bbox")
    if not present:
        return None
    value = node[present[0]]
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise PdfStructureCandidatesError("ODL node bbox must contain exactly four coordinates")
    x0, y0, x1, y1 = (_finite(f"ODL node bbox[{index}]", item) for index, item in enumerate(value))
    if not x0 < x1 or not y0 < y1:
        raise PdfStructureCandidatesError("ODL node bbox must have strictly increasing x/y bounds")
    return [x0, y0, x1, y1]


def _child_array(node: Mapping[str, Any], field: str, *, raw_type: str) -> Sequence[Mapping[str, Any]]:
    if field not in node:
        return ()
    if field not in _ALLOWED_CHILD_FIELDS[raw_type]:
        raise PdfStructureCandidatesError(f"{raw_type!r} must not contain child field {field!r}")
    value = node[field]
    if not isinstance(value, (tuple, list)):
        raise PdfStructureCandidatesError(f"ODL child field {field!r} must be an array")
    children: list[Mapping[str, Any]] = []
    for index, child in enumerate(value):
        if not isinstance(child, Mapping):
            raise PdfStructureCandidatesError(f"ODL child field {field!r} entry {index} must be an object")
        expected = _REQUIRED_CHILD_TYPE.get(field)
        if expected is not None and child.get("type") != expected:
            raise PdfStructureCandidatesError(f"ODL child field {field!r} entry {index} must have type {expected!r}")
        children.append(child)
    return children


def _table_metadata(node: Mapping[str, Any]) -> dict[str, int]:
    metadata: dict[str, int] = {}
    for raw_name, output_name in _TABLE_INT_FIELDS.items():
        if raw_name in node:
            raw_type = node.get("type")
            if output_name not in _TABLE_METADATA_BY_TYPE.get(raw_type, frozenset()):
                raise PdfStructureCandidatesError(f"ODL {raw_type!r} must not contain table metadata {raw_name!r}")
            metadata[output_name] = _positive_int(f"ODL {raw_name}", node[raw_name])
    return metadata


def _as_error(error: OpenDataLoaderArtifactError) -> PdfStructureCandidatesError:
    return PdfStructureCandidatesError(str(error))


def _legacy_as_error(error: LegacyOpenDataLoaderEvaluationError) -> PdfStructureCandidatesError:
    return PdfStructureCandidatesError(str(error))


def _binding_sha256(binding: OpenDataLoaderArtifact | LegacyOpenDataLoaderEvaluation) -> str:
    if isinstance(binding, OpenDataLoaderArtifact):
        return binding.artifact_sha256()
    return sha256(binding.canonical_json()).hexdigest()


def _bind_input(
    binding: OpenDataLoaderArtifact | LegacyOpenDataLoaderEvaluation | Mapping[str, Any],
    raw_odl: Mapping[str, Any], *, raw_bytes: bytes, actual_odl_page_count: int | None,
) -> OpenDataLoaderArtifact | LegacyOpenDataLoaderEvaluation:
    """Validate strict or legacy input without making the latter look strict."""
    if isinstance(binding, Mapping):
        schema_version = binding.get("schema_version")
        try:
            if schema_version == STRICT_BINDING_SCHEMA_VERSION:
                binding = OpenDataLoaderArtifact.from_dict(binding)
            elif schema_version == LEGACY_BINDING_SCHEMA_VERSION:
                binding = LegacyOpenDataLoaderEvaluation.from_dict(binding)
            else:
                raise PdfStructureCandidatesError("input binding schema_version is unsupported")
        except OpenDataLoaderArtifactError as error:
            raise _as_error(error) from error
        except LegacyOpenDataLoaderEvaluationError as error:
            raise _legacy_as_error(error) from error
    if isinstance(binding, OpenDataLoaderArtifact):
        if actual_odl_page_count is None:
            raise PdfStructureCandidatesError("strict OpenDataLoader input requires actual_odl_page_count")
        try:
            return validate_opendataloader_artifact(
                binding, raw_odl, raw_bytes=raw_bytes, actual_odl_page_count=actual_odl_page_count,
            )
        except OpenDataLoaderArtifactError as error:
            raise _as_error(error) from error
    if isinstance(binding, LegacyOpenDataLoaderEvaluation):
        if actual_odl_page_count is not None and actual_odl_page_count != binding.odl_page_count:
            raise PdfStructureCandidatesError("actual ODL page count disagrees with legacy evaluation envelope")
        try:
            return validate_legacy_opendataloader_evaluation(binding, raw_odl, raw_bytes=raw_bytes)
        except LegacyOpenDataLoaderEvaluationError as error:
            raise _legacy_as_error(error) from error
    raise PdfStructureCandidatesError("input binding must be strict or legacy OpenDataLoader metadata")


def project_structure_candidates(
    binding: OpenDataLoaderArtifact | LegacyOpenDataLoaderEvaluation | Mapping[str, Any],
    raw_odl: Mapping[str, Any],
    *,
    raw_bytes: bytes,
    actual_odl_page_count: int | None = None,
) -> dict[str, Any]:
    """Produce the deterministic evaluation-only structural sidecar.

    ``raw_odl`` must be the immutable mapping returned by the ODL reader (or
    an equivalently safe mapping). A strict ``opendataloader_artifact/v1``
    requires ``actual_odl_page_count``, a validated parser-boundary input.
    A legacy evaluation already binds the raw root count and accepts the
    argument only as a matching cross-check. If the raw root declares ``number
    of pages``, this function makes it agree with the selected binding. The
    strict/legacy validation is repeated at this boundary so callers cannot
    accidentally project a mismatched payload or mislabel legacy provenance.
    """
    try:
        bound = _bind_input(binding, raw_odl, raw_bytes=raw_bytes, actual_odl_page_count=actual_odl_page_count)
    except PdfStructureCandidatesError:
        raise

    root_kids = raw_odl.get("kids")
    if not isinstance(root_kids, (tuple, list)):
        raise PdfStructureCandidatesError("ODL root must contain a kids array")
    if "number of pages" in raw_odl:
        declared_count = _positive_int("ODL root number of pages", raw_odl["number of pages"], maximum=bound.odl_page_count)
        if declared_count != bound.odl_page_count:
            raise PdfStructureCandidatesError("ODL root number of pages disagrees with input binding")

    candidates: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    seen_node_objects: set[int] = set()
    # item: node, parent candidate, inherited ODL page, traversal path, depth
    stack: list[tuple[Mapping[str, Any], str | None, int | None, tuple[str, ...], int]] = []
    for index in reversed(range(len(root_kids))):
        node = root_kids[index]
        if not isinstance(node, Mapping):
            raise PdfStructureCandidatesError(f"ODL root kids entry {index} must be an object")
        stack.append((node, None, None, (f"kids[{index}]",), 1))

    while stack:
        node, parent_id, inherited_odl_page, path, depth = stack.pop()
        if depth > MAX_TRAVERSAL_DEPTH:
            raise PdfStructureCandidatesError("ODL structural traversal exceeds the depth cap")
        object_identity = id(node)
        if object_identity in seen_node_objects:
            raise PdfStructureCandidatesError("ODL structural traversal forbids object alias reuse or cycles")
        seen_node_objects.add(object_identity)
        if len(candidates) >= MAX_CANDIDATES:
            raise PdfStructureCandidatesError("ODL structural candidate count exceeds the cap")

        raw_type = node.get("type")
        if not isinstance(raw_type, str) or raw_type not in _NODE_KIND:
            raise PdfStructureCandidatesError("ODL node type is unsupported")
        if "id" not in node:
            raise PdfStructureCandidatesError("ODL structural node must contain an id")
        node_id = _node_id(node["id"])

        direct_page = node.get("page number")
        if direct_page is None:
            if raw_type != "table row" or inherited_odl_page is None:
                raise PdfStructureCandidatesError("ODL structural node must contain a mapped page number")
            odl_page = inherited_odl_page
            node_has_direct_page = False
        else:
            odl_page = _positive_int("ODL node page number", direct_page, maximum=bound.odl_page_count)
            node_has_direct_page = True
        try:
            source_page = bound.source_page_for_odl_page(odl_page)
        except (OpenDataLoaderArtifactError, LegacyOpenDataLoaderEvaluationError) as error:
            raise PdfStructureCandidatesError("ODL structural node page is outside the artifact page scope") from error

        candidate_id = _candidate_id(odl_page, node_id)
        if candidate_id in candidate_ids:
            raise PdfStructureCandidatesError("ODL structural traversal found duplicate page/object identity")
        candidate_ids.add(candidate_id)
        candidate: dict[str, Any] = {
            "candidate_id": candidate_id,
            "parent_candidate_id": parent_id,
            "odl_page": odl_page,
            "source_page": source_page,
            "raw_type": raw_type,
            "normalized_kind": _NODE_KIND[raw_type],
            "traversal_order": len(candidates) + 1,
            "traversal_path": list(path),
        }
        # Rows without an explicit page may inherit source-page *scope* only.
        # Their geometry is too ambiguous to emit, even when malformed input
        # happens to contain a bbox-like value.
        if node_has_direct_page:
            bbox = _node_bbox(node)
            if bbox is not None:
                candidate["bbox_odl_pdf_points_unverified"] = bbox
        metadata = _table_metadata(node)
        if metadata:
            candidate["table_metadata"] = metadata
        candidates.append(candidate)

        ordered_children: list[tuple[str, int, Mapping[str, Any]]] = []
        for field in ("kids", "list items", "rows", "cells"):
            if field in node:
                for index, child in enumerate(_child_array(node, field, raw_type=raw_type)):
                    ordered_children.append((field, index, child))
        for field, index, child in reversed(ordered_children):
            stack.append((child, candidate_id, odl_page, path + (f"{field}[{index}]",), depth + 1))

    return {
        "schema_version": SCHEMA_VERSION,
        "notice_id": bound.notice_id,
        "source_pdf_sha256": bound.source_pdf_sha256,
        "opendataloader_raw_json_sha256": bound.raw_json_sha256,
        "opendataloader_canonical_json_sha256": bound.canonical_json_sha256,
        "input_binding_schema_version": bound.schema_version,
        "input_binding_sha256": _binding_sha256(bound),
        "source_page_count": bound.source_page_count,
        "odl_page_count": bound.odl_page_count,
        "page_scope": list(bound.page_scope),
        "odl_page_to_source_page": [item.to_dict() for item in bound.odl_page_to_source_page],
        "coordinate_status": COORDINATE_STATUS,
        "ordering_status": ORDERING_STATUS,
        "evaluation_only": True,
        "non_promotable": True,
        "candidates": candidates,
    }


def _exact_keys(value: object, expected: frozenset[str], *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfStructureCandidatesError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys) or keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        details = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise PdfStructureCandidatesError(f"{name} keys are invalid ({'; '.join(details)})")
    return value


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PdfStructureCandidatesError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _trimmed_string(name: str, value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise PdfStructureCandidatesError(f"{name} must be a bounded non-empty trimmed string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise PdfStructureCandidatesError(f"{name} must not contain a surrogate code point")
    return value


def _candidate_bbox(value: object) -> None:
    if not isinstance(value, list) or len(value) != 4:
        raise PdfStructureCandidatesError("candidate bbox must contain exactly four coordinates")
    x0, y0, x1, y1 = (_finite(f"candidate bbox[{index}]", item) for index, item in enumerate(value))
    if not x0 < x1 or not y0 < y1:
        raise PdfStructureCandidatesError("candidate bbox must have strictly increasing x/y bounds")


def validate_structure_candidates(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Fail-closed runtime validation matching the v1 JSON Schema.

    The serializer calls this rather than trusting that a mapping once came
    from :func:`project_structure_candidates`; sidecars are files/artifacts
    and must remain safe after any deserialize/mutate/re-serialize boundary.
    """
    root = _exact_keys(value, _ROOT_KEYS, name="structure candidates")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfStructureCandidatesError("structure candidates schema_version is invalid")
    _trimmed_string("notice_id", root["notice_id"])
    for name in ("source_pdf_sha256", "opendataloader_raw_json_sha256", "opendataloader_canonical_json_sha256", "input_binding_sha256"):
        _sha(name, root[name])
    if root["input_binding_schema_version"] not in {
        STRICT_BINDING_SCHEMA_VERSION, LEGACY_BINDING_SCHEMA_VERSION,
    }:
        raise PdfStructureCandidatesError("input_binding_schema_version is invalid")
    source_page_count = _positive_int("source_page_count", root["source_page_count"], maximum=256)
    odl_page_count = _positive_int("odl_page_count", root["odl_page_count"], maximum=256)
    if not isinstance(root["page_scope"], list) or not root["page_scope"]:
        raise PdfStructureCandidatesError("page_scope must be a non-empty array")
    page_scope = tuple(_positive_int(f"page_scope[{index}]", item, maximum=source_page_count) for index, item in enumerate(root["page_scope"]))
    if page_scope != tuple(sorted(set(page_scope))):
        raise PdfStructureCandidatesError("page_scope must be sorted and unique")
    if not isinstance(root["odl_page_to_source_page"], list) or not root["odl_page_to_source_page"]:
        raise PdfStructureCandidatesError("odl_page_to_source_page must be a non-empty array")
    page_map: dict[int, int] = {}
    source_pages: set[int] = set()
    previous_odl_page = 0
    for index, mapping in enumerate(root["odl_page_to_source_page"]):
        entry = _exact_keys(mapping, _PAGE_MAP_KEYS, name=f"odl_page_to_source_page[{index}]")
        odl_page = _positive_int(f"odl_page_to_source_page[{index}].odl_page", entry["odl_page"], maximum=odl_page_count)
        source_page = _positive_int(f"odl_page_to_source_page[{index}].source_page", entry["source_page"], maximum=source_page_count)
        if odl_page <= previous_odl_page or odl_page in page_map:
            raise PdfStructureCandidatesError("odl_page_to_source_page odl pages must be sorted and unique")
        previous_odl_page = odl_page
        page_map[odl_page] = source_page
        source_pages.add(source_page)
    if source_pages != set(page_scope) or len(source_pages) != len(page_map):
        raise PdfStructureCandidatesError("odl_page_to_source_page must bijectively cover page_scope")
    if root["coordinate_status"] != COORDINATE_STATUS:
        raise PdfStructureCandidatesError("structure candidates coordinate status is invalid")
    if root["ordering_status"] != ORDERING_STATUS:
        raise PdfStructureCandidatesError("structure candidates ordering status is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfStructureCandidatesError("structure candidates must remain evaluation-only and non-promotable")
    if not isinstance(root["candidates"], list) or len(root["candidates"]) > MAX_CANDIDATES:
        raise PdfStructureCandidatesError("candidates must be a bounded array")

    seen_ids: dict[str, tuple[str, ...]] = {}
    for index, candidate_value in enumerate(root["candidates"]):
        if not isinstance(candidate_value, Mapping):
            raise PdfStructureCandidatesError(f"candidates[{index}] must be an object")
        candidate = _exact_keys(candidate_value, _CANDIDATE_REQUIRED_KEYS | _CANDIDATE_OPTIONAL_KEYS.intersection(candidate_value.keys()), name=f"candidates[{index}]")
        candidate_id = candidate["candidate_id"]
        if not isinstance(candidate_id, str) or len(candidate_id) != 68 or not candidate_id.startswith("odl-") or any(character not in "0123456789abcdef" for character in candidate_id[4:]):
            raise PdfStructureCandidatesError(f"candidates[{index}].candidate_id is invalid")
        if candidate_id in seen_ids:
            raise PdfStructureCandidatesError("candidate ids must be unique")
        parent_id = candidate["parent_candidate_id"]
        if parent_id is not None:
            if not isinstance(parent_id, str) or parent_id not in seen_ids:
                raise PdfStructureCandidatesError("parent_candidate_id must refer to an earlier candidate")
        odl_page = _positive_int(f"candidates[{index}].odl_page", candidate["odl_page"], maximum=odl_page_count)
        source_page = _positive_int(f"candidates[{index}].source_page", candidate["source_page"], maximum=source_page_count)
        if page_map.get(odl_page) != source_page:
            raise PdfStructureCandidatesError("candidate page mapping disagrees with the artifact page map")
        raw_type = candidate["raw_type"]
        if not isinstance(raw_type, str) or raw_type not in _NODE_KIND or candidate["normalized_kind"] != _NODE_KIND[raw_type]:
            raise PdfStructureCandidatesError("candidate raw type and normalized kind disagree")
        if _positive_int(f"candidates[{index}].traversal_order", candidate["traversal_order"]) != index + 1:
            raise PdfStructureCandidatesError("candidate traversal order must be consecutive pre-order")
        path_value = candidate["traversal_path"]
        if not isinstance(path_value, list) or not path_value or len(path_value) > MAX_TRAVERSAL_DEPTH:
            raise PdfStructureCandidatesError("candidate traversal path must be a bounded non-empty array")
        path = tuple(_trimmed_string(f"candidates[{index}].traversal_path", item) for item in path_value)
        if parent_id is not None:
            parent_path = seen_ids[parent_id]
            if len(path) <= len(parent_path) or path[:len(parent_path)] != parent_path:
                raise PdfStructureCandidatesError("candidate traversal path must extend its parent path")
        if "bbox_odl_pdf_points_unverified" in candidate:
            _candidate_bbox(candidate["bbox_odl_pdf_points_unverified"])
        if "table_metadata" in candidate:
            metadata = candidate["table_metadata"]
            allowed_metadata = _TABLE_METADATA_BY_TYPE.get(raw_type)
            if allowed_metadata is None:
                raise PdfStructureCandidatesError("non-table candidate must not contain table metadata")
            if not isinstance(metadata, Mapping) or not metadata:
                raise PdfStructureCandidatesError("table_metadata must be a non-empty object")
            metadata_keys = frozenset(metadata.keys())
            if any(not isinstance(key, str) for key in metadata_keys) or not metadata_keys <= allowed_metadata:
                raise PdfStructureCandidatesError("table metadata keys are invalid for candidate type")
            for name, item in metadata.items():
                _positive_int(f"candidates[{index}].table_metadata.{name}", item)
        seen_ids[candidate_id] = path
    return root


def canonical_structure_candidates_json(value: Mapping[str, Any]) -> bytes:
    """Validate and serialize a sidecar using canonical UTF-8 JSON."""
    validated = validate_structure_candidates(value)
    try:
        return canonical_json_bytes(validated)
    except OpenDataLoaderArtifactError as error:
        raise _as_error(error) from error
