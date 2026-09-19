"""Textless cross-page table-continuation proposals for PDF A4.3b.

The candidate is deliberately evaluation-only and non-promotable.  It first
replays the complete A4.3a page-local grid against all raw inputs, then proposes
only uniquely corroborated relations across adjacent physical pages.  Native
occurrence ownership and page-local grids are never changed here.

OpenDataLoader geometry is used only through the source-bound reconstruction
plan.  Gold is intentionally outside this module's import and data flow.
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

from .native_capture import CAPTURE_SCHEMA_VERSION as NATIVE_CAPTURE_SCHEMA_VERSION
from .primary_table_grid import (
    SCHEMA_VERSION as PRIMARY_TABLE_GRID_SCHEMA_VERSION,
    PdfPrimaryTableGridError,
    PrimaryTableGridFixture,
    build_pdf_primary_table_grid,
    canonical_pdf_primary_table_grid_json,
)
from .reconstruction_plan import (
    SCHEMA_VERSION as RECONSTRUCTION_PLAN_SCHEMA_VERSION,
    PdfReconstructionPlanError,
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
)
from .surya_layout_artifact import (
    SCHEMA_VERSION as SURYA_LAYOUT_ARTIFACT_SCHEMA_VERSION,
    SuryaProducerIdentity,
)


SCHEMA_VERSION = "pdf_primary_table_continuation/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"
POLICY_VERSION = "primary_adjacent_page_table_continuation/v1"

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 1_000_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_CONTINUATIONS = 10_000
MAX_COLUMNS = 1_000
MAX_MAPPING_WORK = 200_000

# Conservative proposal gates.  They are policy-versioned above and use
# normalized canonical page coordinates, not document-specific point values.
MAX_PREDECESSOR_BOTTOM_GAP_FRACTION = 0.15
MAX_SUCCESSOR_TOP_GAP_FRACTION = 0.15
MAX_HORIZONTAL_EDGE_DELTA_FRACTION = 0.02
MIN_OUTER_INTERVAL_IOU = 0.90
MIN_COLUMN_INTERVAL_IOU = 0.90
GEOMETRY_TOLERANCE_PT = 1e-5
PPM_SCALE = 1_000_000

_SHA = frozenset("0123456789abcdef")
_TABLE_GRID_ID = re.compile(r"^table-grid-[0-9a-f]{64}$")
_CONTINUATION_ID = re.compile(r"^table-continuation-[0-9a-f]{64}$")

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
        "continuations",
    }
)
_POLICY_KEYS = frozenset({"policy_version"})
_INPUT_KEYS = frozenset(
    {
        "primary_table_grid_schema_version",
        "primary_table_grid_sha256",
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
_CONTINUATION_KEYS = frozenset(
    {
        "continuation_id",
        "relation_kind",
        "predecessor_table_grid_id",
        "predecessor_page",
        "successor_table_grid_id",
        "successor_page",
        "column_mapping",
        "geometry_metrics",
    }
)
_COLUMN_MAPPING_KEYS = frozenset(
    {"predecessor_column_index", "successor_column_index"}
)
_GEOMETRY_METRIC_KEYS = frozenset(
    {
        "predecessor_bottom_gap_ppm",
        "successor_top_gap_ppm",
        "outer_x_interval_iou_ppm",
        "max_column_edge_drift_ppm",
        "min_column_interval_iou_ppm",
    }
)


class PdfPrimaryTableContinuationError(ValueError):
    """Raised when an A4.3b continuation proposal is unsafe."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfPrimaryTableContinuationError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys) or keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise PdfPrimaryTableContinuationError(
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
        raise PdfPrimaryTableContinuationError(
            f"{name} must be a bounded non-empty trimmed string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PdfPrimaryTableContinuationError(
            f"{name} contains invalid Unicode"
        ) from error
    if len(encoded) > maximum_bytes:
        raise PdfPrimaryTableContinuationError(
            f"{name} must be a bounded non-empty trimmed string"
        )
    return value


def _sha(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA for character in value)
    ):
        raise PdfPrimaryTableContinuationError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return value


def _positive(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PdfPrimaryTableContinuationError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _positive_or_zero_ppm(name: str, value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= PPM_SCALE
    ):
        raise PdfPrimaryTableContinuationError(
            f"{name} must be an integer from 0 to {PPM_SCALE}"
        )
    return value


def _identifier(name: str, value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PdfPrimaryTableContinuationError(f"{name} is not a canonical ID")
    return value


def _assert_json_limits(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise PdfPrimaryTableContinuationError(
                "table continuation exceeds the JSON node cap"
            )
        if depth > MAX_JSON_DEPTH:
            raise PdfPrimaryTableContinuationError(
                "table continuation exceeds the JSON depth cap"
            )
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise PdfPrimaryTableContinuationError(
                    "table continuation contains a non-finite number"
                )
            continue
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise PdfPrimaryTableContinuationError(
                    "table continuation contains a surrogate code point"
                )
            try:
                encoded = current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PdfPrimaryTableContinuationError(
                    "table continuation contains invalid Unicode"
                ) from error
            if len(encoded) > MAX_STRING_BYTES:
                raise PdfPrimaryTableContinuationError(
                    "table continuation contains an oversized string"
                )
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise PdfPrimaryTableContinuationError(
                        "table continuation object keys must be strings"
                    )
                stack.append((key, depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
            continue
        raise PdfPrimaryTableContinuationError(
            "table continuation contains unsupported type "
            f"{type(current).__name__}"
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
        raise PdfPrimaryTableContinuationError(
            "table continuation is not canonical UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryTableContinuationError(
            "table continuation exceeds the artifact byte cap"
        )
    return encoded


def _continuation_id(
    *,
    source_pdf_sha256: str,
    input_artifacts: Mapping[str, Any],
    relation_kind: str,
    predecessor_table_grid_id: str,
    predecessor_page: int,
    successor_table_grid_id: str,
    successor_page: int,
    column_mapping: Sequence[Mapping[str, int]],
    geometry_metrics: Mapping[str, int],
) -> str:
    payload = {
        "column_mapping": list(column_mapping),
        "geometry_metrics": geometry_metrics,
        "input_artifacts": input_artifacts,
        "policy_version": POLICY_VERSION,
        "predecessor_page": predecessor_page,
        "predecessor_table_grid_id": predecessor_table_grid_id,
        "relation_kind": relation_kind,
        "source_pdf_sha256": source_pdf_sha256,
        "successor_page": successor_page,
        "successor_table_grid_id": successor_table_grid_id,
    }
    return "table-continuation-" + sha256(_canonical_bytes(payload)).hexdigest()


def _validate_input_artifacts(value: object) -> dict[str, str]:
    inputs = _exact_keys(value, _INPUT_KEYS, name="input_artifacts")
    expected_versions = {
        "primary_table_grid_schema_version": PRIMARY_TABLE_GRID_SCHEMA_VERSION,
        "native_capture_schema_version": NATIVE_CAPTURE_SCHEMA_VERSION,
        "render_manifest_schema_version": RENDER_MANIFEST_SCHEMA_VERSION,
        "structure_candidates_schema_version": STRUCTURE_CANDIDATES_SCHEMA_VERSION,
        "reconstruction_plan_schema_version": RECONSTRUCTION_PLAN_SCHEMA_VERSION,
        "surya_layout_artifact_schema_version": SURYA_LAYOUT_ARTIFACT_SCHEMA_VERSION,
    }
    checked: dict[str, str] = {}
    for name, expected in expected_versions.items():
        if inputs[name] != expected:
            raise PdfPrimaryTableContinuationError(f"{name} is invalid")
        checked[name] = expected
        digest_name = name.removesuffix("_schema_version") + "_sha256"
        checked[digest_name] = _sha(
            f"input_artifacts.{digest_name}", inputs[digest_name]
        )
    return checked


def validate_pdf_primary_table_continuation(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate standalone consistency and return a detached canonical copy."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="primary table continuation")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfPrimaryTableContinuationError("schema_version is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfPrimaryTableContinuationError(
            "table continuation must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PdfPrimaryTableContinuationError(
            "standalone validation scope is invalid"
        )
    policy = _exact_keys(root["policy"], _POLICY_KEYS, name="policy")
    if policy["policy_version"] != POLICY_VERSION:
        raise PdfPrimaryTableContinuationError("policy.policy_version is invalid")
    notice_id = _string("notice_id", root["notice_id"])
    source_pdf_sha256 = _sha("source_pdf_sha256", root["source_pdf_sha256"])

    raw_pages = root["page_scope"]
    if not isinstance(raw_pages, list) or not raw_pages or len(raw_pages) > MAX_PAGES:
        raise PdfPrimaryTableContinuationError(
            "page_scope must be a bounded non-empty array"
        )
    pages = [
        _positive(f"page_scope[{index}]", page, maximum=MAX_PAGES)
        for index, page in enumerate(raw_pages)
    ]
    if pages != sorted(set(pages)):
        raise PdfPrimaryTableContinuationError(
            "page_scope must be sorted and unique"
        )
    page_set = set(pages)
    inputs = _validate_input_artifacts(root["input_artifacts"])

    raw_relations = root["continuations"]
    if not isinstance(raw_relations, list) or len(raw_relations) > MAX_CONTINUATIONS:
        raise PdfPrimaryTableContinuationError(
            "continuations must be a bounded array"
        )
    relations: list[dict[str, Any]] = []
    seen_relation_ids: set[str] = set()
    predecessor_successors: dict[str, str] = {}
    successor_predecessors: dict[str, str] = {}
    table_pages: dict[str, int] = {}
    graph: dict[str, str] = {}
    previous_key: tuple[int, str, int, str, str] | None = None
    mapping_work = 0
    for relation_index, raw_relation in enumerate(raw_relations):
        name = f"continuations[{relation_index}]"
        relation = _exact_keys(raw_relation, _CONTINUATION_KEYS, name=name)
        continuation_id = _identifier(
            f"{name}.continuation_id",
            relation["continuation_id"],
            _CONTINUATION_ID,
        )
        if continuation_id in seen_relation_ids:
            raise PdfPrimaryTableContinuationError(
                "continuation IDs must be unique"
            )
        kind = relation["relation_kind"]
        if kind != "between_rows":
            raise PdfPrimaryTableContinuationError(
                f"{name}.relation_kind must be between_rows in v1"
            )
        predecessor_id = _identifier(
            f"{name}.predecessor_table_grid_id",
            relation["predecessor_table_grid_id"],
            _TABLE_GRID_ID,
        )
        successor_id = _identifier(
            f"{name}.successor_table_grid_id",
            relation["successor_table_grid_id"],
            _TABLE_GRID_ID,
        )
        predecessor_page = _positive(
            f"{name}.predecessor_page",
            relation["predecessor_page"],
            maximum=MAX_PAGES,
        )
        successor_page = _positive(
            f"{name}.successor_page",
            relation["successor_page"],
            maximum=MAX_PAGES,
        )
        if (
            predecessor_page not in page_set
            or successor_page not in page_set
            or successor_page != predecessor_page + 1
            or predecessor_id == successor_id
        ):
            raise PdfPrimaryTableContinuationError(
                f"{name} endpoints must be distinct tables on adjacent scoped pages"
            )

        raw_mapping = relation["column_mapping"]
        if (
            not isinstance(raw_mapping, list)
            or not raw_mapping
            or len(raw_mapping) > MAX_COLUMNS
        ):
            raise PdfPrimaryTableContinuationError(
                f"{name}.column_mapping must be a bounded non-empty array"
            )
        mapping: list[dict[str, int]] = []
        predecessor_columns: list[int] = []
        successor_columns: list[int] = []
        for mapping_index, raw_item in enumerate(raw_mapping):
            item_name = f"{name}.column_mapping[{mapping_index}]"
            item = _exact_keys(raw_item, _COLUMN_MAPPING_KEYS, name=item_name)
            predecessor_column = _positive(
                f"{item_name}.predecessor_column_index",
                item["predecessor_column_index"],
                maximum=MAX_COLUMNS,
            )
            successor_column = _positive(
                f"{item_name}.successor_column_index",
                item["successor_column_index"],
                maximum=MAX_COLUMNS,
            )
            predecessor_columns.append(predecessor_column)
            successor_columns.append(successor_column)
            mapping.append(
                {
                    "predecessor_column_index": predecessor_column,
                    "successor_column_index": successor_column,
                }
            )
        mapping_work += len(mapping)
        if mapping_work > MAX_MAPPING_WORK:
            raise PdfPrimaryTableContinuationError(
                "table continuation exceeds the mapping work cap"
            )
        if (
            predecessor_columns != list(range(1, len(mapping) + 1))
            or sorted(successor_columns) != list(range(1, len(mapping) + 1))
        ):
            raise PdfPrimaryTableContinuationError(
                f"{name}.column_mapping must be a canonical complete bijection"
            )

        raw_metrics = _exact_keys(
            relation["geometry_metrics"],
            _GEOMETRY_METRIC_KEYS,
            name=f"{name}.geometry_metrics",
        )
        metrics = {
            metric_name: _positive_or_zero_ppm(
                f"{name}.geometry_metrics.{metric_name}",
                raw_metrics[metric_name],
            )
            for metric_name in sorted(_GEOMETRY_METRIC_KEYS)
        }
        if (
            metrics["predecessor_bottom_gap_ppm"]
            > _ppm(MAX_PREDECESSOR_BOTTOM_GAP_FRACTION)
            or metrics["successor_top_gap_ppm"]
            > _ppm(MAX_SUCCESSOR_TOP_GAP_FRACTION)
            or metrics["outer_x_interval_iou_ppm"]
            < _ppm(MIN_OUTER_INTERVAL_IOU)
            or metrics["max_column_edge_drift_ppm"]
            > _ppm(MAX_HORIZONTAL_EDGE_DELTA_FRACTION)
            or metrics["min_column_interval_iou_ppm"]
            < _ppm(MIN_COLUMN_INTERVAL_IOU)
        ):
            raise PdfPrimaryTableContinuationError(
                f"{name}.geometry_metrics do not satisfy the v1 policy gates"
            )

        canonical_id = _continuation_id(
            source_pdf_sha256=source_pdf_sha256,
            input_artifacts=inputs,
            relation_kind=kind,
            predecessor_table_grid_id=predecessor_id,
            predecessor_page=predecessor_page,
            successor_table_grid_id=successor_id,
            successor_page=successor_page,
            column_mapping=mapping,
            geometry_metrics=metrics,
        )
        if continuation_id != canonical_id:
            raise PdfPrimaryTableContinuationError(
                f"{name}.continuation_id does not bind canonical content"
            )
        relation_key = (
            predecessor_page,
            predecessor_id,
            successor_page,
            successor_id,
            kind,
        )
        if previous_key is not None and relation_key <= previous_key:
            raise PdfPrimaryTableContinuationError(
                "continuations must be in canonical endpoint order"
            )
        previous_key = relation_key
        if predecessor_id in predecessor_successors:
            raise PdfPrimaryTableContinuationError(
                "a predecessor table cannot fan out"
            )
        if successor_id in successor_predecessors:
            raise PdfPrimaryTableContinuationError(
                "a successor table cannot fan in"
            )
        for table_id, page in (
            (predecessor_id, predecessor_page),
            (successor_id, successor_page),
        ):
            prior_page = table_pages.setdefault(table_id, page)
            if prior_page != page:
                raise PdfPrimaryTableContinuationError(
                    "one table grid ID cannot refer to multiple pages"
                )
        predecessor_successors[predecessor_id] = successor_id
        successor_predecessors[successor_id] = predecessor_id
        graph[predecessor_id] = successor_id
        seen_relation_ids.add(continuation_id)
        relations.append(
            {
                "continuation_id": continuation_id,
                "relation_kind": kind,
                "predecessor_table_grid_id": predecessor_id,
                "predecessor_page": predecessor_page,
                "successor_table_grid_id": successor_id,
                "successor_page": successor_page,
                "column_mapping": mapping,
                "geometry_metrics": metrics,
            }
        )

    # Page monotonicity already makes cycles impossible, but retain an
    # explicit bounded graph check so later policy versions cannot weaken it.
    processed: set[str] = set()
    for start in graph:
        if start in processed:
            continue
        path: set[str] = set()
        current = start
        while current in graph and current not in processed:
            if current in path:
                raise PdfPrimaryTableContinuationError(
                    "table continuation graph contains a cycle"
                )
            path.add(current)
            current = graph[current]
        processed.update(path)

    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "policy": {"policy_version": POLICY_VERSION},
        "notice_id": notice_id,
        "source_pdf_sha256": source_pdf_sha256,
        "page_scope": pages,
        "input_artifacts": inputs,
        "continuations": relations,
    }
    _canonical_bytes(result)
    return result


def canonical_pdf_primary_table_continuation_json(
    value: Mapping[str, Any],
) -> bytes:
    return _canonical_bytes(validate_pdf_primary_table_continuation(value))


@dataclass(frozen=True, slots=True)
class _TableGeometry:
    table_grid_id: str
    page: int
    column_count: int
    outer_bbox: tuple[float, float, float, float]
    page_width: float
    page_height: float
    column_intervals: tuple[tuple[float, float], ...]


def _finite_bbox(name: str, value: object) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
    ):
        raise PdfPrimaryTableContinuationError(f"{name} must be a finite bbox")
    bbox = tuple(float(item) for item in value)
    if (
        any(not math.isfinite(item) for item in bbox)
        or bbox[0] >= bbox[2]
        or bbox[1] >= bbox[3]
    ):
        raise PdfPrimaryTableContinuationError(f"{name} must be a finite bbox")
    return bbox


def _table_geometry(
    table: Mapping[str, Any],
    *,
    units_by_candidate: Mapping[str, Mapping[str, Any]],
    page_dimensions: Mapping[int, tuple[float, float]],
) -> _TableGeometry:
    table_id = table["table_grid_id"]
    page = table["page"]
    table_unit = units_by_candidate.get(table["source_table_candidate_id"])
    if (
        table_unit is None
        or table_unit["page"] != page
        or table_unit["bbox_pdf_user_space"] is None
    ):
        raise PdfPrimaryTableContinuationError(
            "table grid is orphaned from source-bound table geometry"
        )
    outer = _finite_bbox(
        f"table {table_id} outer bbox", table_unit["bbox_pdf_user_space"]
    )
    dimensions = page_dimensions.get(page)
    if dimensions is None:
        raise PdfPrimaryTableContinuationError(
            "table page is absent from the trusted render manifest"
        )
    page_width, page_height = dimensions
    if (
        outer[0] < -GEOMETRY_TOLERANCE_PT
        or outer[1] < -GEOMETRY_TOLERANCE_PT
        or outer[2] > page_width + GEOMETRY_TOLERANCE_PT
        or outer[3] > page_height + GEOMETRY_TOLERANCE_PT
    ):
        raise PdfPrimaryTableContinuationError(
            "table bbox is outside the canonical page"
        )

    columns: dict[int, list[tuple[float, float]]] = {
        index: [] for index in range(1, table["column_count"] + 1)
    }
    for row in table["rows"]:
        for cell in row["cells"]:
            unit = units_by_candidate.get(cell["source_cell_candidate_id"])
            if unit is None or unit["page"] != page or unit["bbox_pdf_user_space"] is None:
                raise PdfPrimaryTableContinuationError(
                    "table cell is orphaned from source-bound geometry"
                )
            bbox = _finite_bbox(
                f"cell {cell['cell_id']} bbox", unit["bbox_pdf_user_space"]
            )
            if (
                bbox[0] < outer[0] - GEOMETRY_TOLERANCE_PT
                or bbox[1] < outer[1] - GEOMETRY_TOLERANCE_PT
                or bbox[2] > outer[2] + GEOMETRY_TOLERANCE_PT
                or bbox[3] > outer[3] + GEOMETRY_TOLERANCE_PT
            ):
                raise PdfPrimaryTableContinuationError(
                    "table cell bounds escape the table"
                )
            columns[cell["column_index"]].append((bbox[0], bbox[2]))
    intervals: list[tuple[float, float]] = []
    for column_index in range(1, table["column_count"] + 1):
        members = columns[column_index]
        if not members:
            raise PdfPrimaryTableContinuationError(
                "table column has no source-bound cell geometry"
            )
        interval = (min(item[0] for item in members), max(item[1] for item in members))
        if intervals and interval[0] < intervals[-1][1] - GEOMETRY_TOLERANCE_PT:
            raise PdfPrimaryTableContinuationError(
                "table columns overlap or are not in physical order"
            )
        intervals.append(interval)
    return _TableGeometry(
        table_grid_id=table_id,
        page=page,
        column_count=table["column_count"],
        outer_bbox=outer,
        page_width=page_width,
        page_height=page_height,
        column_intervals=tuple(intervals),
    )


def _normalized_interval(
    interval: tuple[float, float], width: float
) -> tuple[float, float]:
    return interval[0] / width, interval[1] / width


def _interval_iou(
    left: tuple[float, float], right: tuple[float, float]
) -> float:
    intersection = max(0.0, min(left[1], right[1]) - max(left[0], right[0]))
    union = max(left[1], right[1]) - min(left[0], right[0])
    return 0.0 if union <= 0.0 else intersection / union


def _column_mapping(
    predecessor: _TableGeometry,
    successor: _TableGeometry,
) -> tuple[list[dict[str, int]], float, float, float] | None:
    if predecessor.column_count != successor.column_count:
        return None
    predecessor_outer = _normalized_interval(
        (predecessor.outer_bbox[0], predecessor.outer_bbox[2]),
        predecessor.page_width,
    )
    successor_outer = _normalized_interval(
        (successor.outer_bbox[0], successor.outer_bbox[2]),
        successor.page_width,
    )
    outer_iou = _interval_iou(predecessor_outer, successor_outer)
    if (
        abs(predecessor_outer[0] - successor_outer[0])
        > MAX_HORIZONTAL_EDGE_DELTA_FRACTION
        or abs(predecessor_outer[1] - successor_outer[1])
        > MAX_HORIZONTAL_EDGE_DELTA_FRACTION
        or outer_iou < MIN_OUTER_INTERVAL_IOU
    ):
        return None
    mapping: list[dict[str, int]] = []
    edge_drifts: list[float] = []
    interval_ious: list[float] = []
    for index, (predecessor_interval, successor_interval) in enumerate(
        zip(predecessor.column_intervals, successor.column_intervals, strict=True),
        start=1,
    ):
        left = _normalized_interval(predecessor_interval, predecessor.page_width)
        right = _normalized_interval(successor_interval, successor.page_width)
        edge_drift = max(abs(left[0] - right[0]), abs(left[1] - right[1]))
        interval_iou = _interval_iou(left, right)
        if (
            edge_drift > MAX_HORIZONTAL_EDGE_DELTA_FRACTION
            or interval_iou < MIN_COLUMN_INTERVAL_IOU
        ):
            return None
        edge_drifts.append(edge_drift)
        interval_ious.append(interval_iou)
        mapping.append(
            {
                "predecessor_column_index": index,
                "successor_column_index": index,
            }
        )
    return mapping, outer_iou, max(edge_drifts), min(interval_ious)


def _ppm(value: float) -> int:
    return min(PPM_SCALE, max(0, int(round(value * PPM_SCALE))))


def _unique_boundary_table(
    tables: Sequence[_TableGeometry], *, boundary: str
) -> _TableGeometry | None:
    if not tables:
        return None
    if boundary == "bottom":
        eligible = [
            item
            for item in tables
            if item.outer_bbox[1] / item.page_height
            <= MAX_PREDECESSOR_BOTTOM_GAP_FRACTION
            and item.outer_bbox[1]
            <= item.outer_bbox[3] - item.outer_bbox[1] + GEOMETRY_TOLERANCE_PT
        ]
        if len(eligible) != 1:
            return None
        return eligible[0]
    if boundary == "top":
        eligible = [
            item
            for item in tables
            if (item.page_height - item.outer_bbox[3]) / item.page_height
            <= MAX_SUCCESSOR_TOP_GAP_FRACTION
            and item.page_height - item.outer_bbox[3]
            <= item.outer_bbox[3] - item.outer_bbox[1] + GEOMETRY_TOLERANCE_PT
        ]
        if len(eligible) != 1:
            return None
        return eligible[0]
    raise PdfPrimaryTableContinuationError("unknown page boundary")


def build_pdf_primary_table_continuation(
    *,
    primary_table_grid: Mapping[str, Any] | PrimaryTableGridFixture,
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
    """Replay A4.3a and raw sources, then propose adjacent-page continuations."""

    try:
        fixture = (
            primary_table_grid
            if type(primary_table_grid) is PrimaryTableGridFixture
            else PrimaryTableGridFixture.from_dict(primary_table_grid)
        )
    except PdfPrimaryTableGridError as error:
        raise PdfPrimaryTableContinuationError(
            f"A4.3a candidate is invalid: {error}"
        ) from error
    grid_kwargs = {
        "source_pdf": source_pdf,
        "native_capture": native_capture,
        "structure_candidates": structure_candidates,
        "render_manifest": render_manifest,
        "render_artifact_root": render_artifact_root,
        "calibration_proof": calibration_proof,
        "expected_calibration_proof_sha256": expected_calibration_proof_sha256,
        "reconstruction_plan": reconstruction_plan,
        "surya_layout_artifact": surya_layout_artifact,
        "expected_surya_producer": expected_surya_producer,
        "expected_surya_logical_compute_key": expected_surya_logical_compute_key,
        "expected_surya_pages": expected_surya_pages,
    }
    try:
        replayed_grid = build_pdf_primary_table_grid(**grid_kwargs)
        if fixture.canonical_json() != canonical_pdf_primary_table_grid_json(
            replayed_grid
        ):
            raise PdfPrimaryTableContinuationError(
                "A4.3a grid does not match deterministic raw-input replay"
            )
        manifest = validate_render_manifest_files(
            render_manifest, artifact_root=render_artifact_root
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
    except PdfPrimaryTableContinuationError:
        raise
    except (
        PdfPrimaryTableGridError,
        PdfRenderManifestError,
        PdfReconstructionPlanError,
    ) as error:
        raise PdfPrimaryTableContinuationError(
            f"A4.3b source replay failed: {error}"
        ) from error

    grid = fixture.to_dict()
    if (
        grid["notice_id"] != plan["notice_id"]
        or grid["source_pdf_sha256"] != plan["source_pdf_sha256"]
        or grid["page_scope"] != plan["page_scope"]
    ):
        raise PdfPrimaryTableContinuationError(
            "A4.3a grid and reconstruction plan source scopes disagree"
        )
    input_artifacts = {
        "primary_table_grid_schema_version": PRIMARY_TABLE_GRID_SCHEMA_VERSION,
        "primary_table_grid_sha256": fixture.canonical_sha256,
        **grid["input_artifacts"],
    }
    page_dimensions = {
        page.page: (
            page.coordinate_manifest.canonical_width_pt,
            page.coordinate_manifest.canonical_height_pt,
        )
        for page in manifest.pages
    }
    units_by_candidate = {
        unit["source_candidate_id"]: unit
        for unit in plan["units"]
        if unit["origin"] == "opendataloader"
    }
    geometries = [
        _table_geometry(
            table,
            units_by_candidate=units_by_candidate,
            page_dimensions=page_dimensions,
        )
        for table in grid["tables"]
    ]
    tables_by_page: dict[int, list[_TableGeometry]] = {
        page: [] for page in grid["page_scope"]
    }
    for geometry in geometries:
        tables_by_page[geometry.page].append(geometry)

    continuations: list[dict[str, Any]] = []
    mapping_work = 0
    for predecessor_page in grid["page_scope"]:
        successor_page = predecessor_page + 1
        if successor_page not in tables_by_page:
            continue
        predecessor = _unique_boundary_table(
            tables_by_page[predecessor_page], boundary="bottom"
        )
        successor = _unique_boundary_table(
            tables_by_page[successor_page], boundary="top"
        )
        if predecessor is None or successor is None:
            continue
        matched_geometry = _column_mapping(predecessor, successor)
        if matched_geometry is None:
            continue
        mapping, outer_iou, max_edge_drift, min_column_iou = matched_geometry
        mapping_work += len(mapping)
        if mapping_work > MAX_MAPPING_WORK:
            raise PdfPrimaryTableContinuationError(
                "table continuation exceeds the mapping work cap"
            )
        relation = {
            "relation_kind": "between_rows",
            "predecessor_table_grid_id": predecessor.table_grid_id,
            "predecessor_page": predecessor.page,
            "successor_table_grid_id": successor.table_grid_id,
            "successor_page": successor.page,
            "column_mapping": mapping,
            "geometry_metrics": {
                "predecessor_bottom_gap_ppm": _ppm(
                    predecessor.outer_bbox[1] / predecessor.page_height
                ),
                "successor_top_gap_ppm": _ppm(
                    (successor.page_height - successor.outer_bbox[3])
                    / successor.page_height
                ),
                "outer_x_interval_iou_ppm": _ppm(outer_iou),
                "max_column_edge_drift_ppm": _ppm(max_edge_drift),
                "min_column_interval_iou_ppm": _ppm(min_column_iou),
            },
        }
        relation["continuation_id"] = _continuation_id(
            source_pdf_sha256=grid["source_pdf_sha256"],
            input_artifacts=input_artifacts,
            **relation,
        )
        continuations.append(relation)
        if len(continuations) > MAX_CONTINUATIONS:
            raise PdfPrimaryTableContinuationError(
                "continuation count exceeds the safety cap"
            )
    continuations.sort(
        key=lambda item: (
            item["predecessor_page"],
            item["predecessor_table_grid_id"],
            item["successor_page"],
            item["successor_table_grid_id"],
            item["relation_kind"],
        )
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "policy": {"policy_version": POLICY_VERSION},
        "notice_id": grid["notice_id"],
        "source_pdf_sha256": grid["source_pdf_sha256"],
        "page_scope": list(grid["page_scope"]),
        "input_artifacts": input_artifacts,
        "continuations": continuations,
    }
    return validate_pdf_primary_table_continuation(result)


def validate_pdf_primary_table_continuation_against_inputs(
    value: Mapping[str, Any] | "PrimaryTableContinuationFixture",
    **kwargs: Any,
) -> dict[str, Any]:
    """Replay A4.3a and every raw input, requiring exact candidate identity."""

    fixture = (
        value
        if type(value) is PrimaryTableContinuationFixture
        else PrimaryTableContinuationFixture.from_dict(value)
    )
    expected = build_pdf_primary_table_continuation(**kwargs)
    if fixture.canonical_json() != canonical_pdf_primary_table_continuation_json(
        expected
    ):
        raise PdfPrimaryTableContinuationError(
            "table continuation does not match deterministic raw-input replay"
        )
    return fixture.to_dict()


@dataclass(frozen=True, slots=True)
class PrimaryTableContinuationFixture:
    """Immutable standalone sidecar; never evidence of source replay."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise PdfPrimaryTableContinuationError(
                "primary table continuation root must be an object"
            )
        canonical = validate_pdf_primary_table_continuation(self.payload)
        object.__setattr__(self, "payload", _freeze(canonical))

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "PrimaryTableContinuationFixture":
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
            raise PdfPrimaryTableContinuationError(
                f"table continuation contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PdfPrimaryTableContinuationError(
        f"table continuation contains non-finite JSON number {value!r}"
    )


def _bounded_integer(value: str) -> int:
    if len(value.lstrip("-")) > 7:
        raise PdfPrimaryTableContinuationError(
            "table continuation contains an oversized integer"
        )
    return int(value)


def parse_pdf_primary_table_continuation_bytes(
    raw: bytes,
) -> PrimaryTableContinuationFixture:
    """Parse bounded, duplicate-free, exact canonical UTF-8 JSON."""

    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryTableContinuationError(
            "table continuation exceeds the artifact byte cap"
        )
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PdfPrimaryTableContinuationError(
                "table continuation must not contain a UTF-8 BOM"
            )
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
            parse_int=_bounded_integer,
            parse_float=lambda _: (_ for _ in ()).throw(
                PdfPrimaryTableContinuationError(
                    "table continuation must not contain floating-point numbers"
                )
            ),
        )
    except PdfPrimaryTableContinuationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PdfPrimaryTableContinuationError(
            "table continuation is not valid UTF-8 JSON"
        ) from error
    _assert_json_limits(value)
    if not isinstance(value, Mapping):
        raise PdfPrimaryTableContinuationError(
            "primary table continuation root must be an object"
        )
    fixture = PrimaryTableContinuationFixture.from_dict(value)
    if raw != fixture.canonical_json():
        raise PdfPrimaryTableContinuationError(
            "table continuation bytes are not canonical UTF-8 JSON"
        )
    return fixture


def load_pdf_primary_table_continuation_file(
    path: str | Path,
) -> PrimaryTableContinuationFixture:
    """Safely load one regular, non-symlink continuation sidecar."""

    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PdfPrimaryTableContinuationError(
                "table continuation input must be a regular non-symlink file"
            )
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise PdfPrimaryTableContinuationError(
                "table continuation exceeds the artifact byte cap"
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
                raise PdfPrimaryTableContinuationError(
                    "table continuation changed during safe open"
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
                raise PdfPrimaryTableContinuationError(
                    "table continuation changed while being read"
                )
    except PdfPrimaryTableContinuationError:
        raise
    except OSError as error:
        raise PdfPrimaryTableContinuationError(
            "table continuation could not be opened safely"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_pdf_primary_table_continuation_bytes(raw)
