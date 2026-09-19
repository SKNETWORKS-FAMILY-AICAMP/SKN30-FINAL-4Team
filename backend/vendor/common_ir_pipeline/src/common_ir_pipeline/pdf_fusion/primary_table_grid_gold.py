"""Strict, textless partial Gold for page-local PDF table grids.

``pdf_primary_table_grid_gold/v1`` records only reviewer-defined table
segments, logical grid topology, native occurrence ownership, and explicit
outside-table anchors.  It never records text, geometry, parser candidate
IDs, OCR region IDs, or reconstructed values.

Standalone validation proves internal consistency only.  Every public
quality-gate boundary must call
:func:`validate_primary_table_grid_gold_against_inputs` so the source PDF,
native capture, and canonical page renders are replayed in that call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .native_capture import (
    CAPTURE_SCHEMA_VERSION as NATIVE_CAPTURE_SCHEMA_VERSION,
    PINNED_PDF_INSPECTOR_VERSION,
    NativeCaptureError,
    canonical_json_bytes as canonical_native_json_bytes,
    validate_native_capture,
)
from .render_manifest import (
    PdfRenderManifest,
    PdfRenderManifestError,
    validate_render_manifest_files,
)


SCHEMA_VERSION = "pdf_primary_table_grid_gold/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 18
MAX_JSON_NODES = 50_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_TEXT_ITEM_INDEX = 499_999
MAX_REVIEWED_SCOPES = 256
MAX_OCCURRENCES_PER_SCOPE = 4_096
MAX_ROWS_PER_SCOPE = 1_024
MAX_COLUMNS_PER_SCOPE = 1_024
MAX_CELLS_PER_SCOPE = 16_384
MAX_GRID_SLOTS_PER_SCOPE = 1_000_000
MAX_HARD_NEGATIVES_PER_SCOPE = 4_096
MAX_RENDER_MANIFEST_TOTAL_BYTES = 512 * 1024 * 1024

# A human-confirmed claim becomes quality-gate eligible only after its exact
# canonical digest is added through a separately reviewed code change.
TRUSTED_CONFIRMED_TABLE_GRID_GOLD_SHA256S: frozenset[str] = frozenset(
    {
        "548f3fd6ce803e15439f4c4e0e5abaa7fa295db76b6ceb71aa61dcc77bf6d725",
    }
)
_REPLAY_CONSTRUCTION_TOKEN = object()

_SHA = frozenset("0123456789abcdef")
_OCCURRENCE = re.compile(
    r"^occ:inspector:p(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6]):"
    r"t(?:0|[1-9][0-9]{0,4}|[1-4][0-9]{5})$"
)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_REVIEWER_REF = re.compile(r"^reviewer:[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_UTC_TIMESTAMP = re.compile(
    r"^(?:[0-9]{4})-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)
_NOTICE_ID = re.compile(r"^PBLN_[0-9]{15}$")

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "notice_id",
        "source",
        "page_scope",
        "reviewed_scopes",
    }
)
_SOURCE_KEYS = frozenset(
    {"source_pdf_sha256", "canonical_page_renders", "native_capture"}
)
_PAGE_RENDER_KEYS = frozenset({"physical_page", "canonical_render_sha256"})
_NATIVE_CAPTURE_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "extractor_version"}
)
_SCOPE_KEYS = frozenset(
    {
        "scope_id",
        "physical_page",
        "segment_id",
        "review_status",
        "reviewer_ref",
        "confirmed_at",
        "occurrence_ids",
        "row_ids",
        "column_ids",
        "cells",
        "hard_negatives",
    }
)
_CELL_KEYS = frozenset(
    {"cell_id", "row_ids", "column_ids", "content_status", "occurrence_ids"}
)
_HARD_NEGATIVE_KEYS = frozenset(
    {"kind", "occurrence_id", "anchor_position"}
)
_REVIEW_STATUSES = frozenset(
    {"pending_human_confirmation", "human_confirmed"}
)
_CONTENT_STATUSES = frozenset({"populated", "empty"})
_ANCHOR_POSITIONS = frozenset({"before_segment", "after_segment"})


class PrimaryTableGridGoldError(ValueError):
    """Raised when page-local table-grid Gold is unsafe or ambiguous."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrimaryTableGridGoldError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PrimaryTableGridGoldError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        if extra:
            detail.append("unexpected keys: " + ", ".join(extra))
        raise PrimaryTableGridGoldError(
            f"{name} keys are invalid ({'; '.join(detail)})"
        )
    return value


def _string(
    name: str,
    value: object,
    *,
    maximum: int = MAX_STRING_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise PrimaryTableGridGoldError(f"{name} must be a string")
    if (not allow_empty and not value) or value != value.strip():
        raise PrimaryTableGridGoldError(f"{name} must be a trimmed string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PrimaryTableGridGoldError(
            f"{name} must not contain surrogate code points"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PrimaryTableGridGoldError(f"{name} exceeds its string safety cap")
    return value


def _identifier(name: str, value: object) -> str:
    checked = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(checked) is None:
        raise PrimaryTableGridGoldError(f"{name} is not a bounded identifier")
    return checked


def _sha256(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if len(checked) != 64 or any(character not in _SHA for character in checked):
        raise PrimaryTableGridGoldError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return checked


def _positive_int(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PrimaryTableGridGoldError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _occurrence_parts(name: str, value: object) -> tuple[str, int, int]:
    occurrence_id = _string(name, value, maximum=64)
    if _OCCURRENCE.fullmatch(occurrence_id) is None:
        raise PrimaryTableGridGoldError(
            f"{name} is not a bounded occurrence ID"
        )
    page_and_index = occurrence_id.removeprefix("occ:inspector:p").split(
        ":t", 1
    )
    page, item_index = int(page_and_index[0]), int(page_and_index[1])
    if page > MAX_PAGES or item_index > MAX_TEXT_ITEM_INDEX:
        raise PrimaryTableGridGoldError(f"{name} exceeds occurrence bounds")
    return occurrence_id, page, item_index


def _strictly_increasing(values: Sequence[int]) -> bool:
    return all(left < right for left, right in zip(values, values[1:]))


def _assert_json_limits(
    value: object, *, depth: int = 1, counter: list[int] | None = None
) -> None:
    counter = [0] if counter is None else counter
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise PrimaryTableGridGoldError("JSON exceeds the node safety cap")
    if depth > MAX_JSON_DEPTH:
        raise PrimaryTableGridGoldError("JSON exceeds the nesting depth cap")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PrimaryTableGridGoldError("JSON contains a non-finite number")
        return
    if isinstance(value, str):
        _string("JSON string", value, allow_empty=True)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _string("JSON object key", key)
            _assert_json_limits(nested, depth=depth + 1, counter=counter)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_json_limits(nested, depth=depth + 1, counter=counter)
        return
    raise PrimaryTableGridGoldError(
        f"JSON contains unsupported type {type(value).__name__}"
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain(nested) for nested in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze(nested) for key, nested in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(nested) for nested in value)
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    _assert_json_limits(value)
    try:
        encoded = json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PrimaryTableGridGoldError(
            "table-grid Gold is not canonically serializable UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PrimaryTableGridGoldError(
            "table-grid Gold exceeds the artifact byte cap"
        )
    return encoded


def _review_metadata(scope: Mapping[str, Any], *, name: str) -> None:
    status = scope["review_status"]
    if status not in _REVIEW_STATUSES:
        raise PrimaryTableGridGoldError(f"{name}.review_status is unsupported")
    reviewer_ref = scope["reviewer_ref"]
    confirmed_at = scope["confirmed_at"]
    if status == "pending_human_confirmation":
        if reviewer_ref is not None or confirmed_at is not None:
            raise PrimaryTableGridGoldError(
                f"{name} pending review metadata must be null"
            )
        return
    if (
        not isinstance(reviewer_ref, str)
        or _REVIEWER_REF.fullmatch(reviewer_ref) is None
    ):
        raise PrimaryTableGridGoldError(
            f"{name}.reviewer_ref must be a non-identifying reviewer reference"
        )
    timestamp = _string(f"{name}.confirmed_at", confirmed_at, maximum=20)
    if _UTC_TIMESTAMP.fullmatch(timestamp) is None:
        raise PrimaryTableGridGoldError(
            f"{name}.confirmed_at must be second-precision RFC3339 UTC"
        )
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise PrimaryTableGridGoldError(
            f"{name}.confirmed_at is not a real UTC timestamp"
        ) from error


def _identifier_list(
    name: str, value: object, *, maximum: int
) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise PrimaryTableGridGoldError(
            f"{name} must be a bounded non-empty list"
        )
    checked = [
        _identifier(f"{name}[{index}]", item) for index, item in enumerate(value)
    ]
    if len(set(checked)) != len(checked):
        raise PrimaryTableGridGoldError(f"{name} must contain unique IDs")
    return checked


def _contiguous_slice(
    *, name: str, members: list[str], axis_index: Mapping[str, int]
) -> tuple[int, int]:
    try:
        indices = [axis_index[member] for member in members]
    except KeyError as error:
        raise PrimaryTableGridGoldError(
            f"{name} references an ID outside its scope axis"
        ) from error
    if indices != list(range(indices[0], indices[0] + len(indices))):
        raise PrimaryTableGridGoldError(
            f"{name} must be a contiguous scope-axis slice in declared order"
        )
    return indices[0], indices[-1]


def validate_primary_table_grid_gold(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach one internally consistent v1 Gold artifact."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="table-grid Gold")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PrimaryTableGridGoldError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PrimaryTableGridGoldError(
            "table-grid Gold must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PrimaryTableGridGoldError(
            "standalone validation must disclose internal-consistency-only scope"
        )
    notice_id = _string("notice_id", root["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PrimaryTableGridGoldError(
            "notice_id must use the canonical PBLN_ plus 15 digits form"
        )

    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    source_pdf_sha256 = _sha256(
        "source.source_pdf_sha256", source["source_pdf_sha256"]
    )
    capture = _exact_keys(
        source["native_capture"], _NATIVE_CAPTURE_KEYS, name="source.native_capture"
    )
    if capture["schema_version"] != NATIVE_CAPTURE_SCHEMA_VERSION:
        raise PrimaryTableGridGoldError(
            "source.native_capture.schema_version is not the strict native contract"
        )
    native_capture_sha256 = _sha256(
        "source.native_capture.canonical_sha256", capture["canonical_sha256"]
    )
    if capture["extractor_version"] != PINNED_PDF_INSPECTOR_VERSION:
        raise PrimaryTableGridGoldError(
            "source.native_capture.extractor_version is not the pinned extractor"
        )

    page_scope_raw = root["page_scope"]
    if (
        not isinstance(page_scope_raw, list)
        or not page_scope_raw
        or len(page_scope_raw) > MAX_PAGES
    ):
        raise PrimaryTableGridGoldError(
            "page_scope must be a bounded non-empty list"
        )
    page_scope = [
        _positive_int(f"page_scope[{index}]", page, maximum=MAX_PAGES)
        for index, page in enumerate(page_scope_raw)
    ]
    if not _strictly_increasing(page_scope):
        raise PrimaryTableGridGoldError("page_scope must be unique and ascending")

    renders_raw = source["canonical_page_renders"]
    if not isinstance(renders_raw, list) or len(renders_raw) != len(page_scope):
        raise PrimaryTableGridGoldError(
            "canonical_page_renders must bind every scoped page exactly once"
        )
    renders: list[dict[str, Any]] = []
    for index, raw_render in enumerate(renders_raw):
        render = _exact_keys(
            raw_render,
            _PAGE_RENDER_KEYS,
            name=f"canonical_page_renders[{index}]",
        )
        physical_page = _positive_int(
            f"canonical_page_renders[{index}].physical_page",
            render["physical_page"],
            maximum=MAX_PAGES,
        )
        renders.append(
            {
                "physical_page": physical_page,
                "canonical_render_sha256": _sha256(
                    f"canonical_page_renders[{index}].canonical_render_sha256",
                    render["canonical_render_sha256"],
                ),
            }
        )
    if [render["physical_page"] for render in renders] != page_scope:
        raise PrimaryTableGridGoldError(
            "canonical_page_renders must use page_scope order without gaps"
        )

    scopes_raw = root["reviewed_scopes"]
    if (
        not isinstance(scopes_raw, list)
        or not scopes_raw
        or len(scopes_raw) > MAX_REVIEWED_SCOPES
    ):
        raise PrimaryTableGridGoldError(
            "reviewed_scopes must be a bounded non-empty list"
        )
    scopes: list[dict[str, Any]] = []
    seen_scope_ids: set[str] = set()
    seen_segment_ids: set[str] = set()
    seen_row_ids: set[str] = set()
    seen_column_ids: set[str] = set()
    seen_cell_ids: set[str] = set()
    reviewed_occurrence_owners: set[str] = set()
    seen_scope_pages: set[int] = set()
    prior_scope_key: tuple[int, str] | None = None
    for scope_index, raw_scope in enumerate(scopes_raw):
        name = f"reviewed_scopes[{scope_index}]"
        scope = _exact_keys(raw_scope, _SCOPE_KEYS, name=name)
        scope_id = _identifier(f"{name}.scope_id", scope["scope_id"])
        physical_page = _positive_int(
            f"{name}.physical_page", scope["physical_page"], maximum=MAX_PAGES
        )
        if physical_page not in page_scope:
            raise PrimaryTableGridGoldError(f"{name} is outside page_scope")
        scope_key = (physical_page, scope_id)
        if scope_id in seen_scope_ids or (
            prior_scope_key is not None and scope_key <= prior_scope_key
        ):
            raise PrimaryTableGridGoldError(
                "reviewed_scopes must have unique IDs and canonical page/ID order"
            )
        prior_scope_key = scope_key
        seen_scope_ids.add(scope_id)
        seen_scope_pages.add(physical_page)

        segment_id = _identifier(f"{name}.segment_id", scope["segment_id"])
        if segment_id in seen_segment_ids:
            raise PrimaryTableGridGoldError("segment IDs must be artifact-unique")
        seen_segment_ids.add(segment_id)
        _review_metadata(scope, name=name)

        occurrence_values = scope["occurrence_ids"]
        if (
            not isinstance(occurrence_values, list)
            or not occurrence_values
            or len(occurrence_values) > MAX_OCCURRENCES_PER_SCOPE
        ):
            raise PrimaryTableGridGoldError(
                f"{name}.occurrence_ids must be a bounded non-empty list"
            )
        occurrences: list[str] = []
        source_indices: list[int] = []
        for occurrence_index, raw_occurrence in enumerate(occurrence_values):
            occurrence_id, page, item_index = _occurrence_parts(
                f"{name}.occurrence_ids[{occurrence_index}]", raw_occurrence
            )
            if page != physical_page:
                raise PrimaryTableGridGoldError(
                    f"{name}.occurrence_ids must remain on the scope page"
                )
            occurrences.append(occurrence_id)
            source_indices.append(item_index)
        if not _strictly_increasing(source_indices):
            raise PrimaryTableGridGoldError(
                f"{name}.occurrence_ids must be unique and in native source order"
            )
        occurrence_set = set(occurrences)
        if reviewed_occurrence_owners.intersection(occurrence_set):
            raise PrimaryTableGridGoldError(
                "an occurrence cannot be adjudicated by two reviewed scopes"
            )
        reviewed_occurrence_owners.update(occurrence_set)

        row_ids = _identifier_list(
            f"{name}.row_ids", scope["row_ids"], maximum=MAX_ROWS_PER_SCOPE
        )
        column_ids = _identifier_list(
            f"{name}.column_ids",
            scope["column_ids"],
            maximum=MAX_COLUMNS_PER_SCOPE,
        )
        row_index_by_id = {
            row_id: row_index for row_index, row_id in enumerate(row_ids)
        }
        column_index_by_id = {
            column_id: column_index
            for column_index, column_id in enumerate(column_ids)
        }
        if len(row_ids) * len(column_ids) > MAX_GRID_SLOTS_PER_SCOPE:
            raise PrimaryTableGridGoldError(
                f"{name} logical grid exceeds the slot safety cap"
            )
        for axis_name, identifiers, global_seen in (
            ("row", row_ids, seen_row_ids),
            ("column", column_ids, seen_column_ids),
        ):
            if global_seen.intersection(identifiers):
                raise PrimaryTableGridGoldError(
                    f"{axis_name} IDs must be artifact-unique"
                )
            global_seen.update(identifiers)

        cells_raw = scope["cells"]
        if (
            not isinstance(cells_raw, list)
            or not cells_raw
            or len(cells_raw) > MAX_CELLS_PER_SCOPE
        ):
            raise PrimaryTableGridGoldError(
                f"{name}.cells must be a bounded non-empty list"
            )
        cells: list[dict[str, Any]] = []
        owned_slots: dict[tuple[int, int], str] = {}
        positive_occurrences: set[str] = set()
        prior_cell_key: tuple[int, int, int, int, str] | None = None
        expanded_slot_work = 0
        for cell_index, raw_cell in enumerate(cells_raw):
            cell_name = f"{name}.cells[{cell_index}]"
            cell = _exact_keys(raw_cell, _CELL_KEYS, name=cell_name)
            cell_id = _identifier(f"{cell_name}.cell_id", cell["cell_id"])
            if cell_id in seen_cell_ids:
                raise PrimaryTableGridGoldError("cell IDs must be artifact-unique")
            cell_rows = _identifier_list(
                f"{cell_name}.row_ids", cell["row_ids"], maximum=len(row_ids)
            )
            cell_columns = _identifier_list(
                f"{cell_name}.column_ids",
                cell["column_ids"],
                maximum=len(column_ids),
            )
            first_row, last_row = _contiguous_slice(
                name=f"{cell_name}.row_ids",
                members=cell_rows,
                axis_index=row_index_by_id,
            )
            first_column, last_column = _contiguous_slice(
                name=f"{cell_name}.column_ids",
                members=cell_columns,
                axis_index=column_index_by_id,
            )
            cell_key = (
                first_row,
                first_column,
                last_row,
                last_column,
                cell_id,
            )
            if prior_cell_key is not None and cell_key <= prior_cell_key:
                raise PrimaryTableGridGoldError(
                    f"{name}.cells must use canonical grid/cell-ID order"
                )
            prior_cell_key = cell_key
            seen_cell_ids.add(cell_id)
            expanded_slot_work += len(cell_rows) * len(cell_columns)
            if expanded_slot_work > MAX_GRID_SLOTS_PER_SCOPE:
                raise PrimaryTableGridGoldError(
                    f"{name}.cells exceed the expanded-slot work cap"
                )
            for row_index in range(first_row, last_row + 1):
                for column_index in range(first_column, last_column + 1):
                    slot = (row_index, column_index)
                    if slot in owned_slots:
                        raise PrimaryTableGridGoldError(
                            f"{cell_name} overlaps cell {owned_slots[slot]!r}"
                        )
                    owned_slots[slot] = cell_id

            content_status = cell["content_status"]
            if content_status not in _CONTENT_STATUSES:
                raise PrimaryTableGridGoldError(
                    f"{cell_name}.content_status is unsupported"
                )
            cell_occurrences_raw = cell["occurrence_ids"]
            if (
                not isinstance(cell_occurrences_raw, list)
                or len(cell_occurrences_raw) > MAX_OCCURRENCES_PER_SCOPE
            ):
                raise PrimaryTableGridGoldError(
                    f"{cell_name}.occurrence_ids exceeds the safety cap"
                )
            cell_occurrences: list[str] = []
            cell_indices: list[int] = []
            for occurrence_index, raw_occurrence in enumerate(
                cell_occurrences_raw
            ):
                occurrence_id, page, item_index = _occurrence_parts(
                    f"{cell_name}.occurrence_ids[{occurrence_index}]",
                    raw_occurrence,
                )
                if page != physical_page or occurrence_id not in occurrence_set:
                    raise PrimaryTableGridGoldError(
                        f"{cell_name} references an occurrence outside its scope"
                    )
                cell_occurrences.append(occurrence_id)
                cell_indices.append(item_index)
            if cell_indices and not _strictly_increasing(cell_indices):
                raise PrimaryTableGridGoldError(
                    f"{cell_name}.occurrence_ids must be unique and in "
                    "native source order"
                )
            if content_status == "empty" and cell_occurrences:
                raise PrimaryTableGridGoldError(
                    f"{cell_name} empty cells cannot own occurrences"
                )
            if content_status == "populated" and not cell_occurrences:
                raise PrimaryTableGridGoldError(
                    f"{cell_name} populated cells must own occurrences"
                )
            if positive_occurrences.intersection(cell_occurrences):
                raise PrimaryTableGridGoldError(
                    f"{name} occurrence cannot belong to two cells"
                )
            positive_occurrences.update(cell_occurrences)
            cells.append(
                {
                    "cell_id": cell_id,
                    "row_ids": cell_rows,
                    "column_ids": cell_columns,
                    "content_status": content_status,
                    "occurrence_ids": cell_occurrences,
                }
            )

        expected_slots = {
            (row_index, column_index)
            for row_index in range(len(row_ids))
            for column_index in range(len(column_ids))
        }
        if set(owned_slots) != expected_slots:
            raise PrimaryTableGridGoldError(
                f"{name}.cells must partition every logical grid slot exactly once"
            )
        if not positive_occurrences:
            raise PrimaryTableGridGoldError(
                f"{name} must contain at least one populated cell"
            )

        negatives_raw = scope["hard_negatives"]
        if (
            not isinstance(negatives_raw, list)
            or not negatives_raw
            or len(negatives_raw) > MAX_HARD_NEGATIVES_PER_SCOPE
        ):
            raise PrimaryTableGridGoldError(
                f"{name}.hard_negatives must be bounded and non-empty"
            )
        negatives: list[dict[str, Any]] = []
        negative_occurrences: set[str] = set()
        prior_negative_index = -1
        positive_indices = [
            int(item.rsplit(":t", 1)[1]) for item in positive_occurrences
        ]
        first_positive, last_positive = min(positive_indices), max(positive_indices)
        for negative_index, raw_negative in enumerate(negatives_raw):
            negative_name = f"{name}.hard_negatives[{negative_index}]"
            negative = _exact_keys(
                raw_negative, _HARD_NEGATIVE_KEYS, name=negative_name
            )
            if negative["kind"] != "forbidden_segment_membership":
                raise PrimaryTableGridGoldError(
                    f"{negative_name}.kind must be forbidden_segment_membership"
                )
            occurrence_id, page, item_index = _occurrence_parts(
                f"{negative_name}.occurrence_id", negative["occurrence_id"]
            )
            anchor_position = negative["anchor_position"]
            if anchor_position not in _ANCHOR_POSITIONS:
                raise PrimaryTableGridGoldError(
                    f"{negative_name}.anchor_position is unsupported"
                )
            if page != physical_page or occurrence_id not in occurrence_set:
                raise PrimaryTableGridGoldError(
                    f"{negative_name} must bind an in-scope occurrence"
                )
            if occurrence_id in positive_occurrences:
                raise PrimaryTableGridGoldError(
                    f"{negative_name} occurrence is already owned by a cell"
                )
            if (
                occurrence_id in negative_occurrences
                or item_index <= prior_negative_index
            ):
                raise PrimaryTableGridGoldError(
                    f"{name}.hard_negatives must be unique and in native source order"
                )
            if anchor_position == "before_segment" and item_index >= first_positive:
                raise PrimaryTableGridGoldError(
                    f"{negative_name} before-segment anchor must precede all "
                    "cell content"
                )
            if anchor_position == "after_segment" and item_index <= last_positive:
                raise PrimaryTableGridGoldError(
                    f"{negative_name} after-segment anchor must follow all cell content"
                )
            prior_negative_index = item_index
            negative_occurrences.add(occurrence_id)
            negatives.append(
                {
                    "kind": "forbidden_segment_membership",
                    "occurrence_id": occurrence_id,
                    "anchor_position": anchor_position,
                }
            )
        if positive_occurrences | negative_occurrences != occurrence_set:
            raise PrimaryTableGridGoldError(
                f"{name}.occurrence_ids must be exactly partitioned by cells "
                "and hard negatives"
            )

        scopes.append(
            {
                "scope_id": scope_id,
                "physical_page": physical_page,
                "segment_id": segment_id,
                "review_status": scope["review_status"],
                "reviewer_ref": scope["reviewer_ref"],
                "confirmed_at": scope["confirmed_at"],
                "occurrence_ids": occurrences,
                "row_ids": row_ids,
                "column_ids": column_ids,
                "cells": cells,
                "hard_negatives": negatives,
            }
        )

    if seen_scope_pages != set(page_scope):
        raise PrimaryTableGridGoldError(
            "every page_scope page must have at least one reviewed scope"
        )

    canonical: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": notice_id,
        "source": {
            "source_pdf_sha256": source_pdf_sha256,
            "canonical_page_renders": renders,
            "native_capture": {
                "schema_version": NATIVE_CAPTURE_SCHEMA_VERSION,
                "canonical_sha256": native_capture_sha256,
                "extractor_version": PINNED_PDF_INSPECTOR_VERSION,
            },
        },
        "page_scope": page_scope,
        "reviewed_scopes": scopes,
    }
    _canonical_bytes(canonical)
    return canonical


def canonical_primary_table_grid_gold_json(value: Mapping[str, Any]) -> bytes:
    """Return canonical UTF-8 JSON after strict semantic validation."""

    return _canonical_bytes(validate_primary_table_grid_gold(value))


@dataclass(frozen=True, slots=True)
class PrimaryTableGridGoldFixture:
    """Immutable canonical page-local table-grid Gold."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise PrimaryTableGridGoldError("Gold root must be an object")
        canonical = validate_primary_table_grid_gold(self.payload)
        object.__setattr__(self, "payload", _freeze(canonical))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PrimaryTableGridGoldFixture":
        return cls(value)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self.payload)

    def canonical_json(self) -> bytes:
        return _canonical_bytes(self.payload)

    @property
    def canonical_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()

    @property
    def has_trusted_confirmation(self) -> bool:
        return (
            all(
                scope["review_status"] == "human_confirmed"
                for scope in self.payload["reviewed_scopes"]
            )
            and self.canonical_sha256
            in TRUSTED_CONFIRMED_TABLE_GRID_GOLD_SHA256S
        )

    @property
    def is_evaluable(self) -> bool:
        return False

    @property
    def quality_gate_status(self) -> str:
        if any(
            scope["review_status"] == "pending_human_confirmation"
            for scope in self.payload["reviewed_scopes"]
        ):
            return "not_evaluable_gold_pending"
        if not self.has_trusted_confirmation:
            return "not_evaluable_untrusted_confirmation"
        return "not_evaluable_input_replay_required"


@dataclass(frozen=True, slots=True, init=False)
class ReplayedPrimaryTableGridGold:
    """Cooperative receipt returned after complete raw-input replay."""

    fixture: PrimaryTableGridGoldFixture
    _replayed_canonical_sha256: str

    def __init__(
        self,
        fixture: PrimaryTableGridGoldFixture,
        *,
        _construction_token: object,
        replayed_canonical_sha256: str,
    ) -> None:
        if (
            _construction_token is not _REPLAY_CONSTRUCTION_TOKEN
            or type(fixture) is not PrimaryTableGridGoldFixture
            or replayed_canonical_sha256 != fixture.canonical_sha256
        ):
            raise PrimaryTableGridGoldError(
                "Gold replay receipt requires matching internal replay inputs"
            )
        object.__setattr__(self, "fixture", fixture)
        object.__setattr__(
            self, "_replayed_canonical_sha256", replayed_canonical_sha256
        )

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.fixture.payload

    def to_dict(self) -> dict[str, Any]:
        return self.fixture.to_dict()

    def canonical_json(self) -> bytes:
        return self.fixture.canonical_json()

    @property
    def canonical_sha256(self) -> str:
        return self._replayed_canonical_sha256

    @property
    def has_trusted_confirmation(self) -> bool:
        return self.fixture.has_trusted_confirmation

    @property
    def is_replay_receipt(self) -> bool:
        return True


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise PrimaryTableGridGoldError(
                f"Gold contains duplicate JSON key {key!r}"
            )
        value[key] = nested
    return value


def _reject_constant(value: str) -> None:
    raise PrimaryTableGridGoldError(
        f"Gold contains non-finite JSON number {value!r}"
    )


def _bounded_integer(value: str) -> int:
    if len(value.lstrip("-")) > 7:
        raise PrimaryTableGridGoldError("Gold contains an oversized integer")
    return int(value)


def _reject_float(_: str) -> float:
    raise PrimaryTableGridGoldError(
        "Gold must not contain floating-point numbers"
    )


def parse_primary_table_grid_gold_bytes(raw: bytes) -> PrimaryTableGridGoldFixture:
    """Parse exact canonical UTF-8 JSON with bounded fail-closed semantics."""

    if not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES:
        raise PrimaryTableGridGoldError("Gold exceeds the artifact byte cap")
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PrimaryTableGridGoldError("Gold must not include a UTF-8 BOM")
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
            parse_int=_bounded_integer,
            parse_float=_reject_float,
        )
    except PrimaryTableGridGoldError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise PrimaryTableGridGoldError("Gold is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise PrimaryTableGridGoldError("Gold root must be an object")
    fixture = PrimaryTableGridGoldFixture.from_dict(value)
    if raw != fixture.canonical_json():
        raise PrimaryTableGridGoldError("Gold bytes are not canonical UTF-8 JSON")
    return fixture


def load_primary_table_grid_gold_file(
    path: str | Path,
) -> PrimaryTableGridGoldFixture:
    """Read one regular canonical Gold file without following symlinks."""

    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PrimaryTableGridGoldError(
                "Gold input must be a regular non-symlink file"
            )
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise PrimaryTableGridGoldError("Gold exceeds the artifact byte cap")
        descriptor = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (before.st_dev, before.st_ino):
                raise PrimaryTableGridGoldError("Gold changed while opening")
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(stream.fileno())
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            len(raw) > MAX_ARTIFACT_BYTES
            or len(raw) != opened.st_size
            or opened_identity != after_identity
        ):
            raise PrimaryTableGridGoldError("Gold changed while reading")
    except PrimaryTableGridGoldError:
        raise
    except OSError as error:
        raise PrimaryTableGridGoldError(
            "Gold input cannot be safely opened"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_primary_table_grid_gold_bytes(raw)


def _native_occurrence_is_visible(
    item: Mapping[str, Any],
    *,
    crop_box: Sequence[float],
    tolerance: float = 1e-5,
) -> bool:
    x0 = float(item["x"])
    y0 = float(item["y"])
    x1 = x0 + float(item["width"])
    y1 = y0 + float(item["height"])
    crop_x0, crop_y0, crop_x1, crop_y1 = (float(value) for value in crop_box)
    return (
        crop_x0 - tolerance <= x0
        and crop_y0 - tolerance <= y0
        and x1 <= crop_x1 + tolerance
        and y1 <= crop_y1 + tolerance
    )


def validate_primary_table_grid_gold_against_inputs(
    gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    render_artifact_root: str | Path,
) -> ReplayedPrimaryTableGridGold:
    """Replay all raw source, native namespace, and render bindings."""

    fixture = (
        gold
        if type(gold) is PrimaryTableGridGoldFixture
        else PrimaryTableGridGoldFixture.from_dict(gold)
    )
    payload = fixture.to_dict()
    source = payload["source"]
    try:
        capture = validate_native_capture(
            native_capture,
            source_pdf=source_pdf,
            expected_notice_id=payload["notice_id"],
        )
    except NativeCaptureError as error:
        raise PrimaryTableGridGoldError(
            "native capture did not pass strict source replay"
        ) from error
    if capture["capture_schema_version"] != source["native_capture"]["schema_version"]:
        raise PrimaryTableGridGoldError("native capture schema binding mismatch")
    if capture["version"] != source["native_capture"]["extractor_version"]:
        raise PrimaryTableGridGoldError("native capture extractor binding mismatch")
    if capture["source_sha256"] != source["source_pdf_sha256"]:
        raise PrimaryTableGridGoldError("source PDF binding mismatch")
    capture_sha256 = sha256(canonical_native_json_bytes(capture)).hexdigest()
    if capture_sha256 != source["native_capture"]["canonical_sha256"]:
        raise PrimaryTableGridGoldError("native capture canonical hash mismatch")

    try:
        if isinstance(render_manifest, Mapping):
            raw_page_count = render_manifest.get("page_count")
            raw_pages = render_manifest.get("pages")
            if (
                isinstance(raw_page_count, bool)
                or not isinstance(raw_page_count, int)
                or raw_page_count < 1
                or raw_page_count > MAX_PAGES
                or not isinstance(raw_pages, list)
                or not raw_pages
                or len(raw_pages) > MAX_PAGES
            ):
                raise PdfRenderManifestError(
                    "render manifest exceeds the preflight page cap"
                )
        manifest = (
            PdfRenderManifest.from_dict(render_manifest)
            if isinstance(render_manifest, Mapping)
            else render_manifest
        )
        if not isinstance(manifest, PdfRenderManifest):
            raise PdfRenderManifestError(
                "render manifest must be a PdfRenderManifest or object"
            )
        if manifest.page_count > MAX_PAGES:
            raise PdfRenderManifestError("render manifest exceeds the page cap")
        if (
            sum(page.image_size_bytes for page in manifest.pages)
            > MAX_RENDER_MANIFEST_TOTAL_BYTES
        ):
            raise PdfRenderManifestError(
                "render manifest exceeds the aggregate PNG byte cap"
            )
        checked_manifest = validate_render_manifest_files(
            manifest, artifact_root=render_artifact_root
        )
    except PdfRenderManifestError as error:
        raise PrimaryTableGridGoldError(
            "render manifest did not pass strict artifact replay"
        ) from error
    if checked_manifest.source_pdf_sha256 != source["source_pdf_sha256"]:
        raise PrimaryTableGridGoldError("render manifest source binding mismatch")
    if checked_manifest.page_count != capture["process_result"]["page_count"]:
        raise PrimaryTableGridGoldError(
            "native capture and render manifest page counts disagree"
        )
    expected_renders = {
        item["physical_page"]: item["canonical_render_sha256"]
        for item in source["canonical_page_renders"]
    }
    supplied_renders = {
        page.page: page.image_sha256
        for page in checked_manifest.pages
        if page.page in expected_renders
    }
    if supplied_renders != expected_renders:
        raise PrimaryTableGridGoldError("canonical page-render binding mismatch")

    coordinate_by_page = {
        page.page: page.coordinate_manifest for page in checked_manifest.pages
    }
    capture_occurrences = {
        f"occ:inspector:p{item['page']}:t{index}": item
        for index, item in enumerate(capture["text_items"])
    }
    reviewed_occurrences = {
        occurrence_id
        for scope in payload["reviewed_scopes"]
        for occurrence_id in scope["occurrence_ids"]
    }
    if reviewed_occurrences - capture_occurrences.keys():
        raise PrimaryTableGridGoldError(
            "reviewed occurrence namespace is not present in native capture"
        )
    invalid_occurrences = {
        occurrence_id
        for occurrence_id in reviewed_occurrences
        if (
            capture_occurrences[occurrence_id]["item_type"] != "text"
            or not capture_occurrences[occurrence_id]["text"].strip()
            or capture_occurrences[occurrence_id]["width"] <= 0
            or capture_occurrences[occurrence_id]["height"] <= 0
            or not _native_occurrence_is_visible(
                capture_occurrences[occurrence_id],
                crop_box=coordinate_by_page[
                    capture_occurrences[occurrence_id]["page"]
                ].crop_box,
            )
        )
    }
    if invalid_occurrences:
        raise PrimaryTableGridGoldError(
            "table-grid Gold may reference only substantive visible native "
            "text occurrences"
        )
    return ReplayedPrimaryTableGridGold(
        fixture,
        _construction_token=_REPLAY_CONSTRUCTION_TOKEN,
        replayed_canonical_sha256=fixture.canonical_sha256,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_RENDER_MANIFEST_TOTAL_BYTES",
    "PrimaryTableGridGoldError",
    "PrimaryTableGridGoldFixture",
    "ReplayedPrimaryTableGridGold",
    "SCHEMA_VERSION",
    "TRUSTED_CONFIRMED_TABLE_GRID_GOLD_SHA256S",
    "canonical_primary_table_grid_gold_json",
    "load_primary_table_grid_gold_file",
    "parse_primary_table_grid_gold_bytes",
    "validate_primary_table_grid_gold",
    "validate_primary_table_grid_gold_against_inputs",
]
