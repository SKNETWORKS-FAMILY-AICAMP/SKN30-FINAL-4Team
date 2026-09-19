"""Textless, page-local table-grid proposals for PDF A4.3a.

The v1 sidecar is deliberately evaluation-only.  OpenDataLoader may propose
row/column/cell topology after its projected outer table box is independently
corroborated by exactly one strict Surya ``table`` region.  Native PDF text
occurrences remain atomic evidence owned by the reconstruction plan: this
module only records occurrence IDs as cell-membership proposals and never
copies text, geometry, confidence, or parser content into the sidecar.

Standalone validation proves internal consistency only.  Call
``validate_pdf_primary_table_grid_against_inputs`` at a trust boundary so all
source artifacts and local render bytes are deterministically replayed.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .coordinate_manifest import CoordinateManifestError, pixel_to_user_bbox
from .native_capture import (
    CAPTURE_SCHEMA_VERSION,
    NativeCaptureError,
    canonical_json_bytes as canonical_native_json_bytes,
    validate_native_capture,
)
from .reconstruction_plan import (
    SCHEMA_VERSION as RECONSTRUCTION_PLAN_SCHEMA_VERSION,
    PdfReconstructionPlanError,
    canonical_reconstruction_plan_json,
    validate_reconstruction_plan_against_inputs,
)
from .render_manifest import (
    SCHEMA_VERSION as RENDER_MANIFEST_SCHEMA_VERSION,
    PdfRenderManifest,
    PdfRenderManifestError,
    validate_render_manifest_files,
)
from .structure_candidates import (
    SCHEMA_VERSION as STRUCTURE_CANDIDATES_SCHEMA_VERSION,
    PdfStructureCandidatesError,
    canonical_structure_candidates_json,
    validate_structure_candidates,
)
from .surya_layout_artifact import (
    MAX_TOTAL_REGIONS,
    SCHEMA_VERSION as SURYA_SCHEMA_VERSION,
    SuryaLayoutArtifactError,
    SuryaProducerIdentity,
    validate_surya_layout_artifact,
)


SCHEMA_VERSION = "pdf_primary_table_grid/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"
POLICY_VERSION = "primary_page_local_table_grid/v1"

MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 4_000_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_TABLES = MAX_TOTAL_REGIONS
MAX_ROWS_PER_TABLE = 1_000
MAX_COLUMNS_PER_TABLE = 1_000
MAX_CELLS = 200_000
MAX_OCCURRENCES_PER_CELL = 4_096
MAX_OCCURRENCES_PER_TABLE = 4_096
MAX_TABLE_REGION_COMPARISONS = 5_000_000
MIN_OUTER_TABLE_IOU = 0.90
CENTER_CONTAINMENT_TOLERANCE_PT = 1e-5

_SHA = frozenset("0123456789abcdef")
_CANDIDATE_ID = re.compile(r"^odl-[0-9a-f]{64}$")
_OCCURRENCE_ID = re.compile(
    r"^occ:inspector:p(?P<page>[1-9][0-9]{0,2}):"
    r"t(?P<index>0|[1-9][0-9]{0,4}|[1-4][0-9]{5})$"
)
_TABLE_GRID_ID = re.compile(r"^table-grid-[0-9a-f]{64}$")
_ROW_ID = re.compile(r"^table-row-[0-9a-f]{64}$")
_CELL_ID = re.compile(r"^table-cell-[0-9a-f]{64}$")
_SURYA_TABLE_ID = re.compile(r"^p(?P<page>[0-9]{4})-table-(?P<ordinal>[0-9]{4})$")

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "policy",
        "notice_id",
        "source_pdf_sha256",
        "page_scope",
        "input_artifacts",
        "tables",
    }
)
_POLICY_KEYS = frozenset({"policy_version"})
_INPUT_KEYS = frozenset(
    {
        "native_capture_schema_version",
        "native_capture_sha256",
        "render_manifest_schema_version",
        "render_manifest_sha256",
        "structure_candidates_schema_version",
        "structure_candidates_sha256",
        "reconstruction_plan_schema_version",
        "reconstruction_plan_sha256",
        "surya_layout_artifact_schema_version",
        "surya_layout_artifact_sha256",
    }
)
_TABLE_KEYS = frozenset(
    {
        "table_grid_id",
        "page",
        "row_count",
        "column_count",
        "source_table_candidate_id",
        "surya_region_id",
        "rows",
    }
)
_ROW_KEYS = frozenset({"row_id", "row_index", "cells"})
_CELL_KEYS = frozenset(
    {
        "cell_id",
        "row_index",
        "column_index",
        "row_span",
        "column_span",
        "source_cell_candidate_id",
        "occurrence_ids",
    }
)


class PdfPrimaryTableGridError(ValueError):
    """Raised when an A4.3a table-grid proposal is unsafe or inconsistent."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfPrimaryTableGridError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys) or keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise PdfPrimaryTableGridError(
            f"{name} keys are invalid ({'; '.join(details)})"
        )
    return value


def _string(name: str, value: object, *, maximum_bytes: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise PdfPrimaryTableGridError(
            f"{name} must be a bounded non-empty trimmed string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PdfPrimaryTableGridError(f"{name} contains invalid Unicode") from error
    if len(encoded) > maximum_bytes:
        raise PdfPrimaryTableGridError(
            f"{name} must be a bounded non-empty trimmed string"
        )
    return value


def _sha(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA for character in value)
    ):
        raise PdfPrimaryTableGridError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return value


def _positive(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PdfPrimaryTableGridError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _candidate_id(name: str, value: object) -> str:
    if not isinstance(value, str) or _CANDIDATE_ID.fullmatch(value) is None:
        raise PdfPrimaryTableGridError(f"{name} is not an ODL candidate ID")
    return value


def _occurrence_parts(name: str, value: object) -> tuple[str, int, int]:
    if not isinstance(value, str):
        raise PdfPrimaryTableGridError(f"{name} is not a native occurrence ID")
    matched = _OCCURRENCE_ID.fullmatch(value)
    if matched is None:
        raise PdfPrimaryTableGridError(f"{name} is not a native occurrence ID")
    page, source_index = int(matched.group("page")), int(matched.group("index"))
    if not 1 <= page <= MAX_PAGES:
        raise PdfPrimaryTableGridError(f"{name} page is outside the safety cap")
    return value, page, source_index


def _surya_table_id(name: str, value: object, *, page: int) -> str:
    if not isinstance(value, str):
        raise PdfPrimaryTableGridError(f"{name} is not a Surya table region ID")
    matched = _SURYA_TABLE_ID.fullmatch(value)
    if matched is None or int(matched.group("page")) != page:
        raise PdfPrimaryTableGridError(f"{name} is not a Surya table region ID")
    ordinal = int(matched.group("ordinal"))
    if not 1 <= ordinal <= MAX_TOTAL_REGIONS:
        raise PdfPrimaryTableGridError(f"{name} ordinal exceeds the safety cap")
    return value


def _assert_json_limits(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise PdfPrimaryTableGridError("table grid exceeds the JSON node cap")
        if depth > MAX_JSON_DEPTH:
            raise PdfPrimaryTableGridError("table grid exceeds the JSON depth cap")
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise PdfPrimaryTableGridError(
                    "table grid contains a non-finite number"
                )
            continue
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise PdfPrimaryTableGridError(
                    "table grid contains a surrogate code point"
                )
            try:
                encoded = current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PdfPrimaryTableGridError(
                    "table grid contains invalid Unicode"
                ) from error
            if len(encoded) > MAX_STRING_BYTES:
                raise PdfPrimaryTableGridError(
                    "table grid contains an oversized string"
                )
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise PdfPrimaryTableGridError(
                        "table grid object keys must be strings"
                    )
                stack.append((key, depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
            continue
        raise PdfPrimaryTableGridError(
            f"table grid contains unsupported type {type(current).__name__}"
        )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain(nested) for nested in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(nested) for key, nested in value.items()})
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
        raise PdfPrimaryTableGridError(
            "table grid is not canonical UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryTableGridError("table grid exceeds the artifact byte cap")
    return encoded


def _content_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return prefix + sha256(_canonical_bytes(payload)).hexdigest()


def _table_grid_id(
    *,
    source_pdf_sha256: str,
    input_artifacts: Mapping[str, Any],
    page: int,
    source_table_candidate_id: str,
    surya_region_id: str,
) -> str:
    return _content_id(
        "table-grid-",
        {
            "input_artifacts": input_artifacts,
            "page": page,
            "policy_version": POLICY_VERSION,
            "source_pdf_sha256": source_pdf_sha256,
            "source_table_candidate_id": source_table_candidate_id,
            "surya_region_id": surya_region_id,
        },
    )


def _row_id(*, table_grid_id: str, row_index: int) -> str:
    return _content_id(
        "table-row-",
        {
            "row_index": row_index,
            "table_grid_id": table_grid_id,
        },
    )


def _cell_id(
    *,
    table_grid_id: str,
    row_index: int,
    column_index: int,
    row_span: int,
    column_span: int,
    source_cell_candidate_id: str,
    occurrence_ids: Sequence[str],
) -> str:
    return _content_id(
        "table-cell-",
        {
            "column_index": column_index,
            "column_span": column_span,
            "occurrence_ids": list(occurrence_ids),
            "row_index": row_index,
            "row_span": row_span,
            "source_cell_candidate_id": source_cell_candidate_id,
            "table_grid_id": table_grid_id,
        },
    )


def validate_pdf_primary_table_grid(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate standalone consistency and return a detached canonical copy."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="primary table grid")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfPrimaryTableGridError("schema_version is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfPrimaryTableGridError(
            "primary table grid must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PdfPrimaryTableGridError("standalone validation scope is invalid")
    policy = _exact_keys(root["policy"], _POLICY_KEYS, name="policy")
    if policy["policy_version"] != POLICY_VERSION:
        raise PdfPrimaryTableGridError("policy.policy_version is invalid")
    notice_id = _string("notice_id", root["notice_id"])
    source_pdf_sha256 = _sha("source_pdf_sha256", root["source_pdf_sha256"])

    raw_pages = root["page_scope"]
    if not isinstance(raw_pages, list) or not raw_pages or len(raw_pages) > MAX_PAGES:
        raise PdfPrimaryTableGridError("page_scope must be a bounded non-empty array")
    pages = [
        _positive(f"page_scope[{index}]", page, maximum=MAX_PAGES)
        for index, page in enumerate(raw_pages)
    ]
    if pages != sorted(set(pages)):
        raise PdfPrimaryTableGridError("page_scope must be sorted and unique")

    inputs = _exact_keys(root["input_artifacts"], _INPUT_KEYS, name="input_artifacts")
    schema_versions = {
        "native_capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "render_manifest_schema_version": RENDER_MANIFEST_SCHEMA_VERSION,
        "structure_candidates_schema_version": STRUCTURE_CANDIDATES_SCHEMA_VERSION,
        "reconstruction_plan_schema_version": RECONSTRUCTION_PLAN_SCHEMA_VERSION,
        "surya_layout_artifact_schema_version": SURYA_SCHEMA_VERSION,
    }
    checked_inputs: dict[str, Any] = {}
    for name, expected in schema_versions.items():
        if inputs[name] != expected:
            raise PdfPrimaryTableGridError(f"{name} is invalid")
        checked_inputs[name] = expected
        digest_name = name.removesuffix("_schema_version") + "_sha256"
        checked_inputs[digest_name] = _sha(
            f"input_artifacts.{digest_name}", inputs[digest_name]
        )

    raw_tables = root["tables"]
    if not isinstance(raw_tables, list) or len(raw_tables) > MAX_TABLES:
        raise PdfPrimaryTableGridError("tables must be a bounded array")
    tables: list[dict[str, Any]] = []
    seen_grid_ids: set[str] = set()
    seen_table_candidates: set[str] = set()
    seen_row_ids: set[str] = set()
    seen_cell_ids: set[str] = set()
    seen_cell_candidates: set[str] = set()
    seen_surya_regions: set[str] = set()
    seen_occurrences: set[str] = set()
    total_cells = 0
    previous_table_key: tuple[int, str, str] | None = None
    for table_offset, raw_table in enumerate(raw_tables):
        table_name = f"tables[{table_offset}]"
        table = _exact_keys(raw_table, _TABLE_KEYS, name=table_name)
        table_grid_id = table["table_grid_id"]
        if (
            not isinstance(table_grid_id, str)
            or _TABLE_GRID_ID.fullmatch(table_grid_id) is None
            or table_grid_id in seen_grid_ids
        ):
            raise PdfPrimaryTableGridError(
                f"{table_name}.table_grid_id is invalid or duplicated"
            )
        page = _positive(f"{table_name}.page", table["page"], maximum=MAX_PAGES)
        if page not in pages:
            raise PdfPrimaryTableGridError(f"{table_name}.page is outside page_scope")
        row_count = _positive(
            f"{table_name}.row_count", table["row_count"], maximum=MAX_ROWS_PER_TABLE
        )
        column_count = _positive(
            f"{table_name}.column_count",
            table["column_count"],
            maximum=MAX_COLUMNS_PER_TABLE,
        )
        source_table_id = _candidate_id(
            f"{table_name}.source_table_candidate_id",
            table["source_table_candidate_id"],
        )
        surya_region_id = _surya_table_id(
            f"{table_name}.surya_region_id", table["surya_region_id"], page=page
        )
        table_key = (page, surya_region_id, source_table_id)
        if previous_table_key is not None and table_key <= previous_table_key:
            raise PdfPrimaryTableGridError("tables must be in canonical order")
        previous_table_key = table_key
        if source_table_id in seen_table_candidates:
            raise PdfPrimaryTableGridError("source table candidate is duplicated")
        if surya_region_id in seen_surya_regions:
            raise PdfPrimaryTableGridError("Surya table region is reused")
        expected_grid_id = _table_grid_id(
            source_pdf_sha256=source_pdf_sha256,
            input_artifacts=checked_inputs,
            page=page,
            source_table_candidate_id=source_table_id,
            surya_region_id=surya_region_id,
        )
        if table_grid_id != expected_grid_id:
            raise PdfPrimaryTableGridError(
                f"{table_name}.table_grid_id does not bind canonical content"
            )

        raw_rows = table["rows"]
        if not isinstance(raw_rows, list) or len(raw_rows) != row_count:
            raise PdfPrimaryTableGridError(
                f"{table_name}.rows must exactly cover row_count"
            )
        rows: list[dict[str, Any]] = []
        table_occurrence_count = 0
        for row_offset, raw_row in enumerate(raw_rows):
            row_name = f"{table_name}.rows[{row_offset}]"
            row = _exact_keys(raw_row, _ROW_KEYS, name=row_name)
            row_index = _positive(
                f"{row_name}.row_index", row["row_index"], maximum=row_count
            )
            if row_index != row_offset + 1:
                raise PdfPrimaryTableGridError("rows must be consecutive by row_index")
            row_id = row["row_id"]
            if (
                not isinstance(row_id, str)
                or _ROW_ID.fullmatch(row_id) is None
                or row_id in seen_row_ids
            ):
                raise PdfPrimaryTableGridError(
                    f"{row_name} row identity is invalid or duplicated"
                )
            expected_row_id = _row_id(
                table_grid_id=table_grid_id,
                row_index=row_index,
            )
            if row_id != expected_row_id:
                raise PdfPrimaryTableGridError(
                    f"{row_name}.row_id does not bind canonical content"
                )

            raw_cells = row["cells"]
            if not isinstance(raw_cells, list) or len(raw_cells) != column_count:
                raise PdfPrimaryTableGridError(
                    f"{row_name}.cells must exactly cover column_count"
                )
            total_cells += len(raw_cells)
            if total_cells > MAX_CELLS:
                raise PdfPrimaryTableGridError("table cell count exceeds the safety cap")
            cells: list[dict[str, Any]] = []
            for cell_offset, raw_cell in enumerate(raw_cells):
                cell_name = f"{row_name}.cells[{cell_offset}]"
                cell = _exact_keys(raw_cell, _CELL_KEYS, name=cell_name)
                cell_row = _positive(
                    f"{cell_name}.row_index", cell["row_index"], maximum=row_count
                )
                column_index = _positive(
                    f"{cell_name}.column_index",
                    cell["column_index"],
                    maximum=column_count,
                )
                if cell_row != row_index or column_index != cell_offset + 1:
                    raise PdfPrimaryTableGridError(
                        "cells must form a consecutive page-local row/column grid"
                    )
                row_span = _positive(
                    f"{cell_name}.row_span",
                    cell["row_span"],
                    maximum=MAX_ROWS_PER_TABLE,
                )
                column_span = _positive(
                    f"{cell_name}.column_span",
                    cell["column_span"],
                    maximum=MAX_COLUMNS_PER_TABLE,
                )
                if row_span != 1 or column_span != 1:
                    raise PdfPrimaryTableGridError("v1 supports only 1x1 cells")
                source_cell_id = _candidate_id(
                    f"{cell_name}.source_cell_candidate_id",
                    cell["source_cell_candidate_id"],
                )
                cell_id = cell["cell_id"]
                if (
                    not isinstance(cell_id, str)
                    or _CELL_ID.fullmatch(cell_id) is None
                    or cell_id in seen_cell_ids
                    or source_cell_id in seen_cell_candidates
                ):
                    raise PdfPrimaryTableGridError(
                        f"{cell_name} cell identity is invalid or duplicated"
                    )
                raw_occurrences = cell["occurrence_ids"]
                if (
                    not isinstance(raw_occurrences, list)
                    or len(raw_occurrences) > MAX_OCCURRENCES_PER_CELL
                ):
                    raise PdfPrimaryTableGridError(
                        f"{cell_name}.occurrence_ids must be a bounded array"
                    )
                occurrences: list[str] = []
                table_occurrence_count += len(raw_occurrences)
                if table_occurrence_count > MAX_OCCURRENCES_PER_TABLE:
                    raise PdfPrimaryTableGridError(
                        "table occurrence count exceeds the evaluation safety cap"
                    )
                previous_source_index = -1
                for occurrence_offset, raw_occurrence in enumerate(raw_occurrences):
                    occurrence, occurrence_page, source_index = _occurrence_parts(
                        f"{cell_name}.occurrence_ids[{occurrence_offset}]",
                        raw_occurrence,
                    )
                    if occurrence_page != page or source_index <= previous_source_index:
                        raise PdfPrimaryTableGridError(
                            "cell occurrences must be same-page and source-index ordered"
                        )
                    if occurrence in seen_occurrences:
                        raise PdfPrimaryTableGridError(
                            "native occurrence has duplicate table-cell membership"
                        )
                    previous_source_index = source_index
                    seen_occurrences.add(occurrence)
                    occurrences.append(occurrence)
                expected_cell_id = _cell_id(
                    table_grid_id=table_grid_id,
                    row_index=row_index,
                    column_index=column_index,
                    row_span=row_span,
                    column_span=column_span,
                    source_cell_candidate_id=source_cell_id,
                    occurrence_ids=occurrences,
                )
                if cell_id != expected_cell_id:
                    raise PdfPrimaryTableGridError(
                        f"{cell_name}.cell_id does not bind canonical content"
                    )
                seen_cell_ids.add(cell_id)
                seen_cell_candidates.add(source_cell_id)
                cells.append(
                    {
                        "cell_id": cell_id,
                        "row_index": row_index,
                        "column_index": column_index,
                        "row_span": 1,
                        "column_span": 1,
                        "source_cell_candidate_id": source_cell_id,
                        "occurrence_ids": occurrences,
                    }
                )
            seen_row_ids.add(row_id)
            rows.append(
                {
                    "row_id": row_id,
                    "row_index": row_index,
                    "cells": cells,
                }
            )
        if table_occurrence_count == 0:
            raise PdfPrimaryTableGridError(
                f"{table_name} must contain at least one Native occurrence"
            )
        seen_grid_ids.add(table_grid_id)
        seen_table_candidates.add(source_table_id)
        seen_surya_regions.add(surya_region_id)
        tables.append(
            {
                "table_grid_id": table_grid_id,
                "page": page,
                "row_count": row_count,
                "column_count": column_count,
                "source_table_candidate_id": source_table_id,
                "surya_region_id": surya_region_id,
                "rows": rows,
            }
        )

    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "policy": {"policy_version": POLICY_VERSION},
        "notice_id": notice_id,
        "source_pdf_sha256": source_pdf_sha256,
        "page_scope": pages,
        "input_artifacts": checked_inputs,
        "tables": tables,
    }
    _canonical_bytes(result)
    return result


def canonical_pdf_primary_table_grid_json(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_pdf_primary_table_grid(value))


def _area(bbox: Sequence[float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection_area(left: Sequence[float], right: Sequence[float]) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _iou(left: Sequence[float], right: Sequence[float]) -> float:
    intersection = _intersection_area(left, right)
    union = _area(left) + _area(right) - intersection
    return 0.0 if union <= 0.0 else intersection / union


def _center_inside(
    outer: Sequence[float],
    inner: Sequence[float],
    *,
    tolerance: float = CENTER_CONTAINMENT_TOLERANCE_PT,
) -> bool:
    center_x = (inner[0] + inner[2]) / 2.0
    center_y = (inner[1] + inner[3]) / 2.0
    return (
        outer[0] - tolerance <= center_x <= outer[2] + tolerance
        and outer[1] - tolerance <= center_y <= outer[3] + tolerance
    )


def _strict_rectangle(region: Any) -> bool:
    x0, y0, x1, y1 = region.bbox_px
    expected = {(x0, y0), (x1, y0), (x1, y1), (x0, y1)}
    # The strict Surya artifact canonicalizes polygon winding, so rectangle
    # recognition must not depend on the producer's clockwise start vertex.
    return len(region.polygon_px) == 4 and set(region.polygon_px) == expected


def build_pdf_primary_table_grid(
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    structure_candidates: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    render_artifact_root: str | Path,
    calibration_proof: Mapping[str, Any],
    expected_calibration_proof_sha256: str,
    reconstruction_plan: Mapping[str, Any],
    surya_layout_artifact: Mapping[str, Any],
    expected_surya_producer: SuryaProducerIdentity,
    expected_surya_logical_compute_key: str,
    expected_surya_pages: Sequence[int],
) -> dict[str, Any]:
    """Replay raw inputs and build page-local simple-grid proposals."""

    if not isinstance(surya_layout_artifact, Mapping):
        raise PdfPrimaryTableGridError(
            "Surya layout input must be the full raw artifact object"
        )
    try:
        candidates = validate_structure_candidates(structure_candidates)
        manifest = validate_render_manifest_files(
            render_manifest, artifact_root=render_artifact_root
        )
        native = validate_native_capture(
            native_capture,
            source_pdf=source_pdf,
            expected_notice_id=candidates["notice_id"],
        )
        plan = validate_reconstruction_plan_against_inputs(
            reconstruction_plan,
            source_pdf=source_pdf,
            native_capture=native_capture,
            structure_candidates=structure_candidates,
            render_manifest=manifest,
            calibration_proof=calibration_proof,
            expected_calibration_proof_sha256=expected_calibration_proof_sha256,
        )
    except (
        PdfStructureCandidatesError,
        PdfRenderManifestError,
        NativeCaptureError,
        PdfReconstructionPlanError,
    ) as error:
        raise PdfPrimaryTableGridError(f"A4.3a source replay failed: {error}") from error

    page_scope = tuple(plan["page_scope"])
    if tuple(candidates["page_scope"]) != page_scope:
        raise PdfPrimaryTableGridError("candidate and reconstruction page scopes disagree")
    if isinstance(expected_surya_pages, (str, bytes)):
        raise PdfPrimaryTableGridError("expected Surya pages must equal page_scope")
    try:
        expected_pages = tuple(expected_surya_pages)
    except TypeError as error:
        raise PdfPrimaryTableGridError(
            "expected Surya pages must be a sequence"
        ) from error
    if expected_pages != page_scope:
        raise PdfPrimaryTableGridError("expected Surya pages must equal page_scope")
    try:
        surya = validate_surya_layout_artifact(
            surya_layout_artifact,
            render_manifest=manifest,
            expected_logical_compute_key=expected_surya_logical_compute_key,
            expected_producer=expected_surya_producer,
            expected_requested_pages=expected_pages,
        )
    except SuryaLayoutArtifactError as error:
        raise PdfPrimaryTableGridError(
            f"Surya layout artifact replay failed: {error}"
        ) from error

    if (
        native["source_sha256"] != plan["source_pdf_sha256"]
        or candidates["source_pdf_sha256"] != plan["source_pdf_sha256"]
    ):
        raise PdfPrimaryTableGridError("replayed artifacts do not bind one source PDF")

    input_artifacts = {
        "native_capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "native_capture_sha256": sha256(
            canonical_native_json_bytes(native)
        ).hexdigest(),
        "render_manifest_schema_version": RENDER_MANIFEST_SCHEMA_VERSION,
        "render_manifest_sha256": manifest.manifest_sha256(),
        "structure_candidates_schema_version": STRUCTURE_CANDIDATES_SCHEMA_VERSION,
        "structure_candidates_sha256": sha256(
            canonical_structure_candidates_json(candidates)
        ).hexdigest(),
        "reconstruction_plan_schema_version": RECONSTRUCTION_PLAN_SCHEMA_VERSION,
        "reconstruction_plan_sha256": sha256(
            canonical_reconstruction_plan_json(plan)
        ).hexdigest(),
        "surya_layout_artifact_schema_version": SURYA_SCHEMA_VERSION,
        "surya_layout_artifact_sha256": surya.artifact_sha256(),
    }

    candidate_by_id = {
        candidate["candidate_id"]: candidate for candidate in candidates["candidates"]
    }
    children_by_parent: dict[str, list[Mapping[str, Any]]] = {}
    for candidate in candidates["candidates"]:
        parent_id = candidate["parent_candidate_id"]
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(candidate)
    units_by_candidate = {
        unit["source_candidate_id"]: unit
        for unit in plan["units"]
        if unit["origin"] == "opendataloader"
    }
    ledger_by_id = {
        entry["occurrence_id"]: entry for entry in plan["native_occurrence_ledger"]
    }
    coordinates = {page.page: page.coordinate_manifest for page in manifest.pages}
    table_regions_by_page: dict[int, list[tuple[Any, tuple[float, float, float, float]]]] = {
        page: [] for page in page_scope
    }
    for surya_page in surya.pages:
        coordinate = coordinates.get(surya_page.page)
        if coordinate is None:
            raise PdfPrimaryTableGridError(
                "Surya page is absent from the trusted render manifest"
            )
        for region in surya_page.regions:
            if region.label != "table" or not _strict_rectangle(region):
                continue
            try:
                bbox = pixel_to_user_bbox(coordinate, region.bbox_px)
            except CoordinateManifestError as error:
                raise PdfPrimaryTableGridError(
                    f"Surya coordinate conversion failed: {error}"
                ) from error
            table_regions_by_page[surya_page.page].append((region, bbox))

    table_candidates = [
        candidate
        for candidate in candidates["candidates"]
        if candidate["normalized_kind"] == "table"
    ]
    comparison_count = sum(
        len(table_regions_by_page.get(candidate["source_page"], ()))
        for candidate in table_candidates
    )
    if comparison_count > MAX_TABLE_REGION_COMPARISONS:
        raise PdfPrimaryTableGridError(
            "table/Surya comparison count exceeds the safety cap"
        )

    proposed: list[dict[str, Any]] = []
    used_surya_regions: set[str] = set()
    globally_used_occurrences: set[str] = set()
    for table_candidate in table_candidates:
        table_id = table_candidate["candidate_id"]
        metadata = table_candidate.get("table_metadata", {})
        row_count = metadata.get("declared_row_count")
        column_count = metadata.get("declared_column_count")
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or isinstance(column_count, bool)
            or not isinstance(column_count, int)
            or not 1 <= row_count <= MAX_ROWS_PER_TABLE
            or not 1 <= column_count <= MAX_COLUMNS_PER_TABLE
        ):
            raise PdfPrimaryTableGridError(
                "ODL table is missing bounded declared row/column counts"
            )
        rows = [
            child
            for child in children_by_parent.get(table_id, ())
            if child["normalized_kind"] == "table_row"
        ]
        if len(rows) != row_count or any(
            child["normalized_kind"] != "table_row"
            for child in children_by_parent.get(table_id, ())
        ):
            raise PdfPrimaryTableGridError(
                "ODL table rows are orphaned, incomplete, or ambiguous"
            )
        rows_by_index: dict[int, Mapping[str, Any]] = {}
        unsupported_span = False
        topology: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
        for row in rows:
            row_index = row.get("table_metadata", {}).get("row_number")
            if (
                isinstance(row_index, bool)
                or not isinstance(row_index, int)
                or not 1 <= row_index <= row_count
                or row_index in rows_by_index
            ):
                raise PdfPrimaryTableGridError(
                    "ODL table row numbers are missing, duplicated, or out of range"
                )
            rows_by_index[row_index] = row
            cells = list(children_by_parent.get(row["candidate_id"], ()))
            if any(cell["normalized_kind"] != "table_cell" for cell in cells):
                raise PdfPrimaryTableGridError(
                    "ODL table row contains an ambiguous non-cell child"
                )
            cells_by_column: dict[int, Mapping[str, Any]] = {}
            for cell in cells:
                cell_metadata = cell.get("table_metadata", {})
                if cell_metadata.get("row_number") != row_index:
                    raise PdfPrimaryTableGridError(
                        "ODL cell row number disagrees with its parent row"
                    )
                column_index = cell_metadata.get("column_number")
                if (
                    isinstance(column_index, bool)
                    or not isinstance(column_index, int)
                    or not 1 <= column_index <= column_count
                    or column_index in cells_by_column
                ):
                    raise PdfPrimaryTableGridError(
                        "ODL cell columns are missing, duplicated, or out of range"
                    )
                row_span = cell_metadata.get("row_span")
                column_span = cell_metadata.get("column_span")
                if (
                    isinstance(row_span, bool)
                    or not isinstance(row_span, int)
                    or not 1 <= row_span <= MAX_ROWS_PER_TABLE
                    or isinstance(column_span, bool)
                    or not isinstance(column_span, int)
                    or not 1 <= column_span <= MAX_COLUMNS_PER_TABLE
                ):
                    raise PdfPrimaryTableGridError(
                        "ODL cell span metadata is missing or malformed"
                    )
                if (
                    row_index + row_span - 1 > row_count
                    or column_index + column_span - 1 > column_count
                ):
                    raise PdfPrimaryTableGridError(
                        "ODL cell span extends outside the declared table grid"
                    )
                if row_span != 1 or column_span != 1:
                    unsupported_span = True
                cells_by_column[column_index] = cell
            # A spanning table legitimately omits covered slots from later
            # rows.  Detect that unsupported feature before demanding a dense
            # v1 grid, then skip the whole table below.
            if not unsupported_span and (
                len(cells) != column_count
                or set(cells_by_column) != set(range(1, column_count + 1))
            ):
                raise PdfPrimaryTableGridError(
                    "ODL simple table cells are incomplete or ambiguous"
                )
            topology.append(
                (row, [cells_by_column[index] for index in sorted(cells_by_column)])
            )
        if set(rows_by_index) != set(range(1, row_count + 1)):
            raise PdfPrimaryTableGridError("ODL table row grid is not complete")
        if unsupported_span:
            continue
        topology.sort(key=lambda item: item[0]["table_metadata"]["row_number"])

        table_unit = units_by_candidate.get(table_id)
        if table_unit is None or table_unit["bbox_pdf_user_space"] is None:
            continue
        page = table_candidate["source_page"]
        matching_regions = [
            (region, region_bbox)
            for region, region_bbox in table_regions_by_page.get(page, ())
            if _iou(table_unit["bbox_pdf_user_space"], region_bbox)
            >= MIN_OUTER_TABLE_IOU
        ]
        if len(matching_regions) > 1:
            raise PdfPrimaryTableGridError(
                "ODL table has ambiguous Surya outer-region corroboration"
            )
        if not matching_regions:
            continue
        matched_region, matched_surya_bbox = matching_regions[0]
        surya_region_id = matched_region.region_id
        if surya_region_id in used_surya_regions:
            raise PdfPrimaryTableGridError(
                "one Surya table region corroborates multiple ODL tables"
            )

        table_occurrences = list(table_unit["reference_occurrence_ids"])
        if len(table_occurrences) != len(set(table_occurrences)):
            raise PdfPrimaryTableGridError("ODL table occurrence references are duplicated")
        if len(table_occurrences) > MAX_OCCURRENCES_PER_TABLE:
            raise PdfPrimaryTableGridError(
                "table occurrence count exceeds the evaluation safety cap"
            )
        for occurrence in table_occurrences:
            ledger = ledger_by_id.get(occurrence)
            native_bbox = None if ledger is None else ledger["bbox_pdf_user_space"]
            if (
                ledger is None
                or ledger["page"] != page
                or ledger["disposition"] != "owned_atomic"
                or ledger["primary_owner_unit_id"] is None
                or native_bbox is None
            ):
                raise PdfPrimaryTableGridError(
                    "table membership does not reference bounded atomic Native evidence"
                )
            if not _center_inside(table_unit["bbox_pdf_user_space"], native_bbox):
                raise PdfPrimaryTableGridError(
                    "Native occurrence center is outside the projected ODL table"
                )
            if not _center_inside(matched_surya_bbox, native_bbox):
                raise PdfPrimaryTableGridError(
                    "Native occurrence center is outside the corroborating Surya table"
                )
        table_occurrence_set = set(table_occurrences)
        emitted_occurrences: set[str] = set()
        cell_memberships: dict[str, list[str]] = {}
        for row, cells in topology:
            for cell in cells:
                unit = units_by_candidate.get(cell["candidate_id"])
                if unit is None:
                    raise PdfPrimaryTableGridError(
                        "ODL table cell is orphaned from the reconstruction plan"
                    )
                occurrences = list(unit["reference_occurrence_ids"])
                occurrence_parts = [
                    _occurrence_parts("cell occurrence", occurrence)
                    for occurrence in occurrences
                ]
                ordered = sorted(occurrence_parts, key=lambda item: item[2])
                if occurrence_parts != ordered or any(item[1] != page for item in ordered):
                    raise PdfPrimaryTableGridError(
                        "cell occurrences must preserve same-page native source order"
                    )
                for occurrence, _, _ in ordered:
                    ledger = ledger_by_id.get(occurrence)
                    if (
                        ledger is None
                        or ledger["page"] != page
                        or ledger["disposition"] != "owned_atomic"
                        or ledger["primary_owner_unit_id"] is None
                    ):
                        raise PdfPrimaryTableGridError(
                            "cell membership does not reference atomic Native evidence"
                        )
                    if occurrence in emitted_occurrences:
                        raise PdfPrimaryTableGridError(
                            "native occurrence has duplicate cells in one ODL table"
                        )
                    emitted_occurrences.add(occurrence)
                cell_memberships[cell["candidate_id"]] = [
                    item[0] for item in ordered
                ]
        if emitted_occurrences != table_occurrence_set:
            raise PdfPrimaryTableGridError(
                "ODL cells do not exactly partition their table occurrences"
            )
        # A fully textless ODL/Surya match has no Native evidence with which
        # A4.3a can evaluate cell ownership.  It is an unsupported table for
        # this Native-grounded slice, not a malformed source artifact.  Keep
        # partial emptiness fail-closed via the exact-partition check above.
        if not table_occurrence_set:
            continue
        if emitted_occurrences & globally_used_occurrences:
            raise PdfPrimaryTableGridError(
                "native occurrence has duplicate membership across ODL tables"
            )

        table_grid_id = _table_grid_id(
            source_pdf_sha256=plan["source_pdf_sha256"],
            input_artifacts=input_artifacts,
            page=page,
            source_table_candidate_id=table_id,
            surya_region_id=surya_region_id,
        )
        output_rows: list[dict[str, Any]] = []
        for row, cells in topology:
            row_index = row["table_metadata"]["row_number"]
            row_id = _row_id(
                table_grid_id=table_grid_id,
                row_index=row_index,
            )
            output_cells: list[dict[str, Any]] = []
            for cell in cells:
                column_index = cell["table_metadata"]["column_number"]
                occurrence_ids = cell_memberships[cell["candidate_id"]]
                output_cells.append(
                    {
                        "cell_id": _cell_id(
                            table_grid_id=table_grid_id,
                            row_index=row_index,
                            column_index=column_index,
                            row_span=1,
                            column_span=1,
                            source_cell_candidate_id=cell["candidate_id"],
                            occurrence_ids=occurrence_ids,
                        ),
                        "row_index": row_index,
                        "column_index": column_index,
                        "row_span": 1,
                        "column_span": 1,
                        "source_cell_candidate_id": cell["candidate_id"],
                        "occurrence_ids": occurrence_ids,
                    }
                )
            output_rows.append(
                {
                    "row_id": row_id,
                    "row_index": row_index,
                    "cells": output_cells,
                }
            )
        proposed.append(
            {
                "table_grid_id": table_grid_id,
                "page": page,
                "row_count": row_count,
                "column_count": column_count,
                "source_table_candidate_id": table_id,
                "surya_region_id": surya_region_id,
                "rows": output_rows,
            }
        )
        used_surya_regions.add(surya_region_id)
        globally_used_occurrences.update(emitted_occurrences)

    # A table-cell outside a direct table-row parent, or a table-row outside a
    # direct table parent, is unsafe parser topology even if it did not happen
    # to overlap a Surya region.
    for candidate in candidates["candidates"]:
        if candidate["normalized_kind"] not in {"table_row", "table_cell"}:
            continue
        parent = candidate_by_id.get(candidate["parent_candidate_id"])
        expected_parent_kind = (
            "table" if candidate["normalized_kind"] == "table_row" else "table_row"
        )
        if parent is None or parent["normalized_kind"] != expected_parent_kind:
            raise PdfPrimaryTableGridError("ODL table topology contains an orphan node")

    proposed.sort(
        key=lambda table: (
            table["page"],
            table["surya_region_id"],
            table["source_table_candidate_id"],
        )
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "policy": {"policy_version": POLICY_VERSION},
        "notice_id": candidates["notice_id"],
        "source_pdf_sha256": plan["source_pdf_sha256"],
        "page_scope": list(page_scope),
        "input_artifacts": input_artifacts,
        "tables": proposed,
    }
    return validate_pdf_primary_table_grid(result)


def validate_pdf_primary_table_grid_against_inputs(
    value: Mapping[str, Any] | "PrimaryTableGridFixture", **kwargs: Any
) -> dict[str, Any]:
    """Replay every raw input and require exact canonical candidate identity."""

    fixture = (
        value
        if type(value) is PrimaryTableGridFixture
        else PrimaryTableGridFixture.from_dict(value)
    )
    expected = build_pdf_primary_table_grid(**kwargs)
    if fixture.canonical_json() != canonical_pdf_primary_table_grid_json(expected):
        raise PdfPrimaryTableGridError(
            "primary table grid does not match deterministic replay of raw inputs"
        )
    return fixture.to_dict()


@dataclass(frozen=True, slots=True)
class PrimaryTableGridFixture:
    """Immutable standalone sidecar; never evidence of source replay."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise PdfPrimaryTableGridError("primary table grid root must be an object")
        canonical = validate_pdf_primary_table_grid(self.payload)
        object.__setattr__(self, "payload", _freeze(canonical))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PrimaryTableGridFixture":
        return cls(value)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self.payload)

    def canonical_json(self) -> bytes:
        return _canonical_bytes(self.payload)

    @property
    def canonical_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PdfPrimaryTableGridError(
                f"primary table grid contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PdfPrimaryTableGridError(
        f"primary table grid contains non-finite JSON number {value!r}"
    )


def _bounded_integer(value: str) -> int:
    if len(value.lstrip("-")) > 7:
        raise PdfPrimaryTableGridError(
            "primary table grid contains an oversized integer"
        )
    return int(value)


def parse_pdf_primary_table_grid_bytes(raw: bytes) -> PrimaryTableGridFixture:
    """Parse bounded, duplicate-free, exact canonical UTF-8 JSON."""

    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryTableGridError("primary table grid exceeds the artifact byte cap")
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PdfPrimaryTableGridError(
                "primary table grid must not contain a UTF-8 BOM"
            )
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
            parse_int=_bounded_integer,
            parse_float=lambda _: (_ for _ in ()).throw(
                PdfPrimaryTableGridError(
                    "primary table grid must not contain floating-point numbers"
                )
            ),
        )
    except PdfPrimaryTableGridError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PdfPrimaryTableGridError(
            "primary table grid is not valid UTF-8 JSON"
        ) from error
    _assert_json_limits(value)
    if not isinstance(value, Mapping):
        raise PdfPrimaryTableGridError("primary table grid root must be an object")
    fixture = PrimaryTableGridFixture.from_dict(value)
    if raw != fixture.canonical_json():
        raise PdfPrimaryTableGridError(
            "primary table grid bytes are not canonical UTF-8 JSON"
        )
    return fixture


def load_pdf_primary_table_grid_file(path: str | Path) -> PrimaryTableGridFixture:
    """Safely load one regular, non-symlink table-grid sidecar."""

    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PdfPrimaryTableGridError(
                "primary table grid input must be a regular non-symlink file"
            )
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise PdfPrimaryTableGridError(
                "primary table grid exceeds the artifact byte cap"
            )
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
                raise PdfPrimaryTableGridError(
                    "primary table grid changed during safe open"
                )
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
                raise PdfPrimaryTableGridError(
                    "primary table grid changed while being read"
                )
    except PdfPrimaryTableGridError:
        raise
    except OSError as error:
        raise PdfPrimaryTableGridError(
            "primary table grid could not be opened safely"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_pdf_primary_table_grid_bytes(raw)
