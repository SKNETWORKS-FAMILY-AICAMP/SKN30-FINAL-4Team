"""Partial-Gold evaluation for page-local PDF table-grid proposals.

The public quality gate replays both the deterministic candidate and the
textless Gold artifact from the same raw inputs on every call.  Candidate,
ODL, and Surya identifiers are diagnostics only: scopes are matched by page
and reviewed native-occurrence overlap, then compared by logical grid slot
and occurrence ownership.  Cross-page continuation is intentionally outside
this A4.3a contract.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .primary_table_grid import (
    SCHEMA_VERSION as CANDIDATE_SCHEMA_VERSION,
    PdfPrimaryTableGridError,
    PrimaryTableGridFixture,
    canonical_pdf_primary_table_grid_json,
    validate_pdf_primary_table_grid_against_inputs,
)
from .primary_table_grid_gold import (
    SCHEMA_VERSION as GOLD_SCHEMA_VERSION,
    PrimaryTableGridGoldError,
    PrimaryTableGridGoldFixture,
    ReplayedPrimaryTableGridGold,
    validate_primary_table_grid_gold_against_inputs,
)
from .render_manifest import PdfRenderManifest
from .surya_layout_artifact import SuryaProducerIdentity


SCHEMA_VERSION = "pdf_primary_table_grid_evaluation/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 250_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_SCOPES = 256
MAX_CELLS_PER_SCOPE = 16_384
MAX_OCCURRENCES_PER_SCOPE = 4_096
MAX_TABLES = 10_000
MAX_EXPANDED_GRID_SLOTS = 1_000_000
# Candidate replay is capped at 4,000,000 JSON nodes and Gold can contribute
# at most 256 * 4,096 reviewed occurrences. Membership lookup must visit each
# bounded occurrence at most once, never a table-by-scope Cartesian product.
MAX_SCOPE_MEMBERSHIP_WORK = 4_000_000 + (MAX_SCOPES * MAX_OCCURRENCES_PER_SCOPE)

_SHA = frozenset("0123456789abcdef")
_NOTICE_ID = re.compile(r"^PBLN_[0-9]{15}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_OCCURRENCE = re.compile(
    r"^occ:inspector:p(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6]):"
    r"t(?:0|[1-9][0-9]{0,4}|[1-4][0-9]{5})$"
)

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "notice_id",
        "source",
        "candidate",
        "gold",
        "verdict",
        "blocking_reason_codes",
        "scope_results",
        "unscored_tables",
        "metrics",
    }
)
_SOURCE_KEYS = frozenset({"source_pdf_sha256", "native_capture_sha256"})
_ARTIFACT_REF_KEYS = frozenset({"schema_version", "canonical_sha256"})
_GOLD_REF_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "quality_status"}
)
_SCOPE_KEYS = frozenset(
    {
        "scope_id",
        "physical_page",
        "segment_id",
        "verdict",
        "failure_codes",
        "expected_row_count",
        "expected_column_count",
        "topology_status",
        "observed_tables",
        "shared_candidate_table_ids",
        "cell_results",
        "missing_attachment_occurrence_ids",
        "wrong_partition_occurrence_ids",
        "duplicate_attachment_occurrence_ids",
        "orphan_attachment_occurrence_ids",
        "negative_anchor_attachment_occurrence_ids",
        "unscored_attachment_occurrence_ids",
    }
)
_OBSERVED_TABLE_KEYS = frozenset(
    {"table_grid_id", "row_count", "column_count"}
)
_CELL_KEYS = frozenset(
    {
        "cell_id",
        "row_ids",
        "column_ids",
        "status",
        "failure_codes",
        "expected_occurrence_ids",
        "observed_reviewed_occurrence_ids",
        "observed_candidate_cell_ids",
    }
)
_UNSCORED_TABLE_KEYS = frozenset(
    {"table_grid_id", "physical_page", "occurrence_ids"}
)
_METRIC_KEYS = frozenset(
    {
        "reviewed_scope_count",
        "evaluated_scope_count",
        "passed_scope_count",
        "failed_scope_count",
        "expected_cell_count",
        "matched_cell_count",
        "failed_cell_count",
        "missing_attachment_count",
        "wrong_partition_attachment_count",
        "duplicate_attachment_count",
        "orphan_attachment_count",
        "negative_anchor_attachment_count",
        "unscored_attachment_count",
        "unscored_table_count",
    }
)

_VERDICTS = frozenset(
    {
        "passed",
        "failed",
        "not_evaluable_gold_pending",
        "not_evaluable_gold_untrusted",
        "blocked_scope_mismatch",
    }
)
_QUALITY_STATUSES = frozenset(
    {
        "evaluable",
        "not_evaluable_gold_pending",
        "not_evaluable_untrusted_confirmation",
    }
)
_BLOCKING_REASON_ORDER = (
    "notice_id_mismatch",
    "source_pdf_mismatch",
    "native_capture_mismatch",
    "gold_page_scope_not_covered",
)
_BLOCKING_REASONS = frozenset(_BLOCKING_REASON_ORDER)
_BLOCKING_RANK = {
    reason: index for index, reason in enumerate(_BLOCKING_REASON_ORDER)
}
_SCOPE_FAILURE_ORDER = (
    "segment_missing",
    "topology_mismatch",
    "shared_candidate_table",
    "missing_attachment",
    "wrong_partition",
    "duplicate_attachment",
    "orphan_attachment",
    "negative_anchor_attachment",
)
_SCOPE_FAILURES = frozenset(_SCOPE_FAILURE_ORDER)
_SCOPE_FAILURE_RANK = {
    reason: index for index, reason in enumerate(_SCOPE_FAILURE_ORDER)
}
_CELL_FAILURE_ORDER = (
    "missing_attachment",
    "wrong_partition",
    "duplicate_attachment",
    "orphan_attachment",
)
_CELL_FAILURES = frozenset(_CELL_FAILURE_ORDER)
_CELL_FAILURE_RANK = {
    reason: index for index, reason in enumerate(_CELL_FAILURE_ORDER)
}
_TOPOLOGY_STATUSES = frozenset({"matched", "missing", "mismatched"})


class PdfPrimaryTableGridEvaluationError(ValueError):
    """Raised when an A4.3a evaluation is unsafe or inconsistent."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfPrimaryTableGridEvaluationError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PdfPrimaryTableGridEvaluationError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        if extra:
            detail.append("unexpected keys: " + ", ".join(extra))
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} keys are invalid ({'; '.join(detail)})"
        )
    return value


def _string(name: str, value: object, *, maximum: int = MAX_STRING_BYTES) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} must be a non-empty trimmed string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} contains invalid Unicode"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} exceeds its string safety cap"
        )
    return value


def _sha(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if len(checked) != 64 or any(character not in _SHA for character in checked):
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return checked


def _identifier(name: str, value: object) -> str:
    checked = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(checked) is None:
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} is not a bounded identifier"
        )
    return checked


def _occurrence(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if _OCCURRENCE.fullmatch(checked) is None:
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} is not a native occurrence ID"
        )
    return checked


def _occurrence_sort_key(value: str) -> tuple[int, int]:
    _, _, page_part, source_part = value.split(":")
    return int(page_part[1:]), int(source_part[1:])


def _positive(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _nonnegative(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} must be a non-negative integer"
        )
    return value


def _assert_json_limits(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise PdfPrimaryTableGridEvaluationError(
                "evaluation exceeds the JSON node cap"
            )
        if depth > MAX_JSON_DEPTH:
            raise PdfPrimaryTableGridEvaluationError(
                "evaluation exceeds the JSON depth cap"
            )
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise PdfPrimaryTableGridEvaluationError(
                    "evaluation contains a non-finite number"
                )
            continue
        if isinstance(current, str):
            try:
                encoded = current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PdfPrimaryTableGridEvaluationError(
                    "evaluation contains invalid Unicode"
                ) from error
            if len(encoded) > MAX_STRING_BYTES or any(
                0xD800 <= ord(character) <= 0xDFFF for character in current
            ):
                raise PdfPrimaryTableGridEvaluationError(
                    "evaluation contains an oversized or invalid string"
                )
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise PdfPrimaryTableGridEvaluationError(
                        "evaluation object keys must be strings"
                    )
                stack.append((key, depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
            continue
        raise PdfPrimaryTableGridEvaluationError(
            f"evaluation contains unsupported type {type(current).__name__}"
        )


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
        raise PdfPrimaryTableGridEvaluationError(
            "evaluation is not canonical UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryTableGridEvaluationError(
            "evaluation exceeds the artifact byte cap"
        )
    return encoded


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain(nested) for nested in value]
    return value


def _unique_identifiers(
    name: str,
    value: object,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value) > maximum
    ):
        raise PdfPrimaryTableGridEvaluationError(f"{name} must be a bounded array")
    result = [
        _identifier(f"{name}[{index}]", item) for index, item in enumerate(value)
    ]
    if len(result) != len(set(result)):
        raise PdfPrimaryTableGridEvaluationError(f"{name} must be unique")
    return result


def _occurrence_list(name: str, value: object, *, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PdfPrimaryTableGridEvaluationError(f"{name} must be a bounded array")
    result = [
        _occurrence(f"{name}[{index}]", item) for index, item in enumerate(value)
    ]
    if len(result) != len(set(result)) or result != sorted(
        result, key=_occurrence_sort_key
    ):
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} must be unique and in canonical native order"
        )
    return result


def _ranked_codes(
    name: str,
    value: object,
    *,
    allowed: frozenset[str],
    rank: Mapping[str, int],
) -> list[str]:
    if not isinstance(value, list) or len(value) > len(allowed):
        raise PdfPrimaryTableGridEvaluationError(f"{name} must be a bounded array")
    result = [_string(f"{name}[{index}]", item) for index, item in enumerate(value)]
    if (
        len(result) != len(set(result))
        or any(item not in allowed for item in result)
        or result != sorted(result, key=rank.__getitem__)
    ):
        raise PdfPrimaryTableGridEvaluationError(
            f"{name} must contain unique values in canonical rank order"
        )
    return result


def _artifact_ref(value: object, *, name: str) -> dict[str, str]:
    ref = _exact_keys(value, _ARTIFACT_REF_KEYS, name=name)
    if ref["schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise PdfPrimaryTableGridEvaluationError(
            f"{name}.schema_version is invalid"
        )
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "canonical_sha256": _sha(
            f"{name}.canonical_sha256", ref["canonical_sha256"]
        ),
    }


def _gold_ref(value: object) -> dict[str, str]:
    ref = _exact_keys(value, _GOLD_REF_KEYS, name="gold")
    if ref["schema_version"] != GOLD_SCHEMA_VERSION:
        raise PdfPrimaryTableGridEvaluationError("gold.schema_version is invalid")
    quality = _string("gold.quality_status", ref["quality_status"])
    if quality not in _QUALITY_STATUSES:
        raise PdfPrimaryTableGridEvaluationError("gold.quality_status is invalid")
    return {
        "schema_version": GOLD_SCHEMA_VERSION,
        "canonical_sha256": _sha(
            "gold.canonical_sha256", ref["canonical_sha256"]
        ),
        "quality_status": quality,
    }


def _validate_observed_table(value: object, *, name: str) -> dict[str, Any]:
    item = _exact_keys(value, _OBSERVED_TABLE_KEYS, name=name)
    return {
        "table_grid_id": _identifier(
            f"{name}.table_grid_id", item["table_grid_id"]
        ),
        "row_count": _positive(
            f"{name}.row_count", item["row_count"], maximum=1_024
        ),
        "column_count": _positive(
            f"{name}.column_count", item["column_count"], maximum=1_024
        ),
    }


def _validate_cell_result(value: object, *, name: str) -> dict[str, Any]:
    item = _exact_keys(value, _CELL_KEYS, name=name)
    status = _string(f"{name}.status", item["status"])
    if status not in {"passed", "failed"}:
        raise PdfPrimaryTableGridEvaluationError(f"{name}.status is invalid")
    failures = _ranked_codes(
        f"{name}.failure_codes",
        item["failure_codes"],
        allowed=_CELL_FAILURES,
        rank=_CELL_FAILURE_RANK,
    )
    if (status == "failed") != bool(failures):
        raise PdfPrimaryTableGridEvaluationError(
            f"{name}.status disagrees with failure_codes"
        )
    expected = _occurrence_list(
        f"{name}.expected_occurrence_ids",
        item["expected_occurrence_ids"],
        maximum=MAX_OCCURRENCES_PER_SCOPE,
    )
    observed = _occurrence_list(
        f"{name}.observed_reviewed_occurrence_ids",
        item["observed_reviewed_occurrence_ids"],
        maximum=MAX_OCCURRENCES_PER_SCOPE,
    )
    candidate_cells = _unique_identifiers(
        f"{name}.observed_candidate_cell_ids",
        item["observed_candidate_cell_ids"],
        maximum=MAX_CELLS_PER_SCOPE,
        allow_empty=True,
    )
    if candidate_cells != sorted(candidate_cells):
        raise PdfPrimaryTableGridEvaluationError(
            f"{name}.observed_candidate_cell_ids must use canonical order"
        )
    return {
        "cell_id": _identifier(f"{name}.cell_id", item["cell_id"]),
        "row_ids": _unique_identifiers(
            f"{name}.row_ids", item["row_ids"], maximum=1_024
        ),
        "column_ids": _unique_identifiers(
            f"{name}.column_ids", item["column_ids"], maximum=1_024
        ),
        "status": status,
        "failure_codes": failures,
        "expected_occurrence_ids": expected,
        "observed_reviewed_occurrence_ids": observed,
        "observed_candidate_cell_ids": candidate_cells,
    }


def validate_primary_table_grid_evaluation(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate standalone result shape and internal metric consistency."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="evaluation")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfPrimaryTableGridEvaluationError("schema_version is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfPrimaryTableGridEvaluationError(
            "evaluation must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PdfPrimaryTableGridEvaluationError(
            "standalone validation scope is invalid"
        )
    notice_id = _string("notice_id", root["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PdfPrimaryTableGridEvaluationError("notice_id is invalid")
    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    source_copy = {
        "source_pdf_sha256": _sha(
            "source.source_pdf_sha256", source["source_pdf_sha256"]
        ),
        "native_capture_sha256": _sha(
            "source.native_capture_sha256", source["native_capture_sha256"]
        ),
    }
    candidate_ref = _artifact_ref(root["candidate"], name="candidate")
    gold_ref = _gold_ref(root["gold"])
    verdict = _string("verdict", root["verdict"])
    if verdict not in _VERDICTS:
        raise PdfPrimaryTableGridEvaluationError("verdict is invalid")
    blocking = _ranked_codes(
        "blocking_reason_codes",
        root["blocking_reason_codes"],
        allowed=_BLOCKING_REASONS,
        rank=_BLOCKING_RANK,
    )

    scopes_raw = root["scope_results"]
    if not isinstance(scopes_raw, list) or len(scopes_raw) > MAX_SCOPES:
        raise PdfPrimaryTableGridEvaluationError(
            "scope_results must be a bounded array"
        )
    scopes: list[dict[str, Any]] = []
    prior_scope_key: tuple[int, str] | None = None
    seen_scope_ids: set[str] = set()
    expanded_slot_work = 0
    for scope_index, raw_scope in enumerate(scopes_raw):
        name = f"scope_results[{scope_index}]"
        scope = _exact_keys(raw_scope, _SCOPE_KEYS, name=name)
        scope_id = _identifier(f"{name}.scope_id", scope["scope_id"])
        page = _positive(
            f"{name}.physical_page", scope["physical_page"], maximum=MAX_PAGES
        )
        scope_key = (page, scope_id)
        if scope_id in seen_scope_ids or (
            prior_scope_key is not None and scope_key <= prior_scope_key
        ):
            raise PdfPrimaryTableGridEvaluationError(
                "scope_results must use unique canonical page/scope order"
            )
        seen_scope_ids.add(scope_id)
        prior_scope_key = scope_key
        scope_verdict = _string(f"{name}.verdict", scope["verdict"])
        if scope_verdict not in {"passed", "failed"}:
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.verdict is invalid"
            )
        failures = _ranked_codes(
            f"{name}.failure_codes",
            scope["failure_codes"],
            allowed=_SCOPE_FAILURES,
            rank=_SCOPE_FAILURE_RANK,
        )
        if (scope_verdict == "failed") != bool(failures):
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.verdict disagrees with failure_codes"
            )
        row_count = _positive(
            f"{name}.expected_row_count",
            scope["expected_row_count"],
            maximum=1_024,
        )
        column_count = _positive(
            f"{name}.expected_column_count",
            scope["expected_column_count"],
            maximum=1_024,
        )
        topology = _string(f"{name}.topology_status", scope["topology_status"])
        if topology not in _TOPOLOGY_STATUSES:
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.topology_status is invalid"
            )
        observed_raw = scope["observed_tables"]
        if not isinstance(observed_raw, list) or len(observed_raw) > MAX_TABLES:
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.observed_tables must be bounded"
            )
        observed_tables = [
            _validate_observed_table(
                item, name=f"{name}.observed_tables[{index}]"
            )
            for index, item in enumerate(observed_raw)
        ]
        observed_ids = [item["table_grid_id"] for item in observed_tables]
        if len(observed_ids) != len(set(observed_ids)) or observed_ids != sorted(
            observed_ids
        ):
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.observed_tables must use unique canonical ID order"
            )
        shared_table_ids = _unique_identifiers(
            f"{name}.shared_candidate_table_ids",
            scope["shared_candidate_table_ids"],
            maximum=MAX_TABLES,
            allow_empty=True,
        )
        if shared_table_ids != sorted(shared_table_ids) or not set(
            shared_table_ids
        ).issubset(observed_ids):
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.shared_candidate_table_ids must be a canonical subset of observed tables"
            )
        dimension_topology = (
            "missing"
            if not observed_tables
            else (
                "matched"
                if len(observed_tables) == 1
                and observed_tables[0]["row_count"] == row_count
                and observed_tables[0]["column_count"] == column_count
                else "mismatched"
            )
        )
        if dimension_topology in {"missing", "mismatched"} and topology != dimension_topology:
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.topology_status disagrees with observed tables"
            )
        if dimension_topology == "matched" and topology == "missing":
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.topology_status cannot be missing when an observed "
                "table matches the expected dimensions"
            )

        cells_raw = scope["cell_results"]
        if (
            not isinstance(cells_raw, list)
            or not cells_raw
            or len(cells_raw) > MAX_CELLS_PER_SCOPE
        ):
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.cell_results must be bounded and non-empty"
            )
        cells = [
            _validate_cell_result(item, name=f"{name}.cell_results[{index}]")
            for index, item in enumerate(cells_raw)
        ]
        cell_ids = [item["cell_id"] for item in cells]
        if len(cell_ids) != len(set(cell_ids)) or cell_ids != sorted(cell_ids):
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.cell_results must use unique canonical cell-ID order"
            )
        scope_slot_work = row_count * column_count
        for cell in cells:
            scope_slot_work += len(cell["row_ids"]) * len(cell["column_ids"])
        expanded_slot_work += scope_slot_work
        if expanded_slot_work > MAX_EXPANDED_GRID_SLOTS:
            raise PdfPrimaryTableGridEvaluationError(
                "evaluation exceeds the aggregate expanded-grid-slot work cap"
            )

        occurrence_arrays: dict[str, list[str]] = {}
        for key in (
            "missing_attachment_occurrence_ids",
            "wrong_partition_occurrence_ids",
            "duplicate_attachment_occurrence_ids",
            "orphan_attachment_occurrence_ids",
            "negative_anchor_attachment_occurrence_ids",
            "unscored_attachment_occurrence_ids",
        ):
            occurrence_arrays[key] = _occurrence_list(
                f"{name}.{key}",
                scope[key],
                maximum=MAX_OCCURRENCES_PER_SCOPE,
            )
        expected_owner: dict[str, str] = {}
        for cell in cells:
            for occurrence_id in cell["expected_occurrence_ids"]:
                if occurrence_id in expected_owner:
                    raise PdfPrimaryTableGridEvaluationError(
                        f"{name} expected occurrences must belong to one cell"
                    )
                expected_owner[occurrence_id] = cell["cell_id"]
        expected_occurrences = set(expected_owner)
        missing_set = set(
            occurrence_arrays["missing_attachment_occurrence_ids"]
        )
        for category in (
            "missing_attachment_occurrence_ids",
            "wrong_partition_occurrence_ids",
            "duplicate_attachment_occurrence_ids",
            "orphan_attachment_occurrence_ids",
        ):
            if not set(occurrence_arrays[category]).issubset(expected_occurrences):
                raise PdfPrimaryTableGridEvaluationError(
                    f"{name}.{category} must refer to expected cell occurrences"
                )
        negative_set = set(
            occurrence_arrays["negative_anchor_attachment_occurrence_ids"]
        )
        unscored_set = set(
            occurrence_arrays["unscored_attachment_occurrence_ids"]
        )
        if (
            expected_occurrences.intersection(negative_set | unscored_set)
            or negative_set.intersection(unscored_set)
        ):
            raise PdfPrimaryTableGridEvaluationError(
                f"{name} expected, negative-anchor, and unscored occurrences must be disjoint"
            )
        for cell in cells:
            expected_set = set(cell["expected_occurrence_ids"])
            if set(cell["observed_reviewed_occurrence_ids"]) != (
                expected_set - missing_set
            ):
                raise PdfPrimaryTableGridEvaluationError(
                    f"{name} observed cell occurrences disagree with missing attachments"
                )
            expected_cell_failures = [
                code
                for code, category in (
                    ("missing_attachment", "missing_attachment_occurrence_ids"),
                    ("wrong_partition", "wrong_partition_occurrence_ids"),
                    ("duplicate_attachment", "duplicate_attachment_occurrence_ids"),
                    ("orphan_attachment", "orphan_attachment_occurrence_ids"),
                )
                if expected_set.intersection(occurrence_arrays[category])
            ]
            if cell["failure_codes"] != expected_cell_failures:
                raise PdfPrimaryTableGridEvaluationError(
                    f"{name} cell failure codes disagree with attachment results"
                )
        has_cell_partition_failure = any(
            "wrong_partition" in cell["failure_codes"] for cell in cells
        )
        if (
            topology == "mismatched"
            and dimension_topology == "matched"
            and not has_cell_partition_failure
        ):
            raise PdfPrimaryTableGridEvaluationError(
                f"{name} matched dimensions need a cell partition failure "
                "to mark topology mismatched"
            )
        if topology == "missing" and missing_set != expected_occurrences:
            raise PdfPrimaryTableGridEvaluationError(
                f"{name} missing segment must miss every positive occurrence"
            )
        if topology == "missing" and any(
            cell["observed_candidate_cell_ids"] for cell in cells
        ):
            raise PdfPrimaryTableGridEvaluationError(
                f"{name} missing segment cannot identify observed candidate cells"
            )
        expected_failures: list[str] = []
        if topology == "missing":
            expected_failures.append("segment_missing")
        elif topology == "mismatched":
            expected_failures.append("topology_mismatch")
        if shared_table_ids:
            expected_failures.append("shared_candidate_table")
        for code, key in (
            ("missing_attachment", "missing_attachment_occurrence_ids"),
            ("wrong_partition", "wrong_partition_occurrence_ids"),
            ("duplicate_attachment", "duplicate_attachment_occurrence_ids"),
            ("orphan_attachment", "orphan_attachment_occurrence_ids"),
            (
                "negative_anchor_attachment",
                "negative_anchor_attachment_occurrence_ids",
            ),
        ):
            if occurrence_arrays[key] or (
                code == "wrong_partition" and has_cell_partition_failure
            ):
                expected_failures.append(code)
        expected_failures.sort(key=_SCOPE_FAILURE_RANK.__getitem__)
        if failures != expected_failures:
            raise PdfPrimaryTableGridEvaluationError(
                f"{name}.failure_codes disagree with detailed results"
            )
        scopes.append(
            {
                "scope_id": scope_id,
                "physical_page": page,
                "segment_id": _identifier(
                    f"{name}.segment_id", scope["segment_id"]
                ),
                "verdict": scope_verdict,
                "failure_codes": failures,
                "expected_row_count": row_count,
                "expected_column_count": column_count,
                "topology_status": topology,
                "observed_tables": observed_tables,
                "shared_candidate_table_ids": shared_table_ids,
                "cell_results": cells,
                **occurrence_arrays,
            }
        )

    unscored_raw = root["unscored_tables"]
    if not isinstance(unscored_raw, list) or len(unscored_raw) > MAX_TABLES:
        raise PdfPrimaryTableGridEvaluationError(
            "unscored_tables must be a bounded array"
        )
    unscored: list[dict[str, Any]] = []
    prior_unscored_key: tuple[int, str] | None = None
    for index, raw_item in enumerate(unscored_raw):
        name = f"unscored_tables[{index}]"
        item = _exact_keys(raw_item, _UNSCORED_TABLE_KEYS, name=name)
        table_id = _identifier(f"{name}.table_grid_id", item["table_grid_id"])
        page = _positive(
            f"{name}.physical_page", item["physical_page"], maximum=MAX_PAGES
        )
        key = (page, table_id)
        if prior_unscored_key is not None and key <= prior_unscored_key:
            raise PdfPrimaryTableGridEvaluationError(
                "unscored_tables must use unique canonical page/ID order"
            )
        prior_unscored_key = key
        unscored.append(
            {
                "table_grid_id": table_id,
                "physical_page": page,
                "occurrence_ids": _occurrence_list(
                    f"{name}.occurrence_ids",
                    item["occurrence_ids"],
                    maximum=MAX_OCCURRENCES_PER_SCOPE,
                ),
            }
        )
    observed_table_ids = {
        table["table_grid_id"]
        for scope in scopes
        for table in scope["observed_tables"]
    }
    unscored_ids = {item["table_grid_id"] for item in unscored}
    if observed_table_ids.intersection(unscored_ids):
        raise PdfPrimaryTableGridEvaluationError(
            "an observed candidate table cannot also be unscored"
        )

    metrics = _exact_keys(root["metrics"], _METRIC_KEYS, name="metrics")
    metric_copy = {
        key: _nonnegative(f"metrics.{key}", metrics[key])
        for key in _METRIC_KEYS
    }
    expected_metrics = {
        "reviewed_scope_count": metric_copy["reviewed_scope_count"],
        "evaluated_scope_count": len(scopes),
        "passed_scope_count": sum(item["verdict"] == "passed" for item in scopes),
        "failed_scope_count": sum(item["verdict"] == "failed" for item in scopes),
        "expected_cell_count": sum(len(item["cell_results"]) for item in scopes),
        "matched_cell_count": sum(
            cell["status"] == "passed"
            for item in scopes
            for cell in item["cell_results"]
        ),
        "failed_cell_count": sum(
            cell["status"] == "failed"
            for item in scopes
            for cell in item["cell_results"]
        ),
        "missing_attachment_count": sum(
            len(item["missing_attachment_occurrence_ids"]) for item in scopes
        ),
        "wrong_partition_attachment_count": sum(
            len(item["wrong_partition_occurrence_ids"]) for item in scopes
        ),
        "duplicate_attachment_count": sum(
            len(item["duplicate_attachment_occurrence_ids"]) for item in scopes
        ),
        "orphan_attachment_count": sum(
            len(item["orphan_attachment_occurrence_ids"]) for item in scopes
        ),
        "negative_anchor_attachment_count": sum(
            len(item["negative_anchor_attachment_occurrence_ids"])
            for item in scopes
        ),
        "unscored_attachment_count": sum(
            len(item["unscored_attachment_occurrence_ids"]) for item in scopes
        ),
        "unscored_table_count": len(unscored),
    }
    if metric_copy["reviewed_scope_count"] > MAX_SCOPES:
        raise PdfPrimaryTableGridEvaluationError(
            "reviewed_scope_count exceeds the cap"
        )
    for key, expected in expected_metrics.items():
        if key != "reviewed_scope_count" and metric_copy[key] != expected:
            raise PdfPrimaryTableGridEvaluationError(
                f"metrics.{key} disagrees with detailed results"
            )

    if verdict in {"passed", "failed"}:
        if (
            gold_ref["quality_status"] != "evaluable"
            or blocking
            or not scopes
            or metric_copy["reviewed_scope_count"] != len(scopes)
        ):
            raise PdfPrimaryTableGridEvaluationError(
                "quality verdict requires trusted Gold and every reviewed scope"
            )
        expected_verdict = (
            "failed" if metric_copy["failed_scope_count"] else "passed"
        )
        if verdict != expected_verdict:
            raise PdfPrimaryTableGridEvaluationError(
                "quality verdict disagrees with scope results"
            )
    elif verdict == "not_evaluable_gold_pending":
        if (
            gold_ref["quality_status"] != "not_evaluable_gold_pending"
            or blocking
            or scopes
            or unscored
        ):
            raise PdfPrimaryTableGridEvaluationError(
                "pending-Gold verdict shape is invalid"
            )
    elif verdict == "not_evaluable_gold_untrusted":
        if (
            gold_ref["quality_status"] != "not_evaluable_untrusted_confirmation"
            or blocking
            or scopes
            or unscored
        ):
            raise PdfPrimaryTableGridEvaluationError(
                "untrusted-Gold verdict shape is invalid"
            )
    else:
        if (
            gold_ref["quality_status"] != "evaluable"
            or not blocking
            or scopes
            or unscored
        ):
            raise PdfPrimaryTableGridEvaluationError(
                "scope-mismatch verdict shape is invalid"
            )
    if not scopes:
        for key in _METRIC_KEYS - {"reviewed_scope_count"}:
            if metric_copy[key] != 0:
                raise PdfPrimaryTableGridEvaluationError(
                    "non-evaluated metrics must be zero"
                )

    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": notice_id,
        "source": source_copy,
        "candidate": candidate_ref,
        "gold": gold_ref,
        "verdict": verdict,
        "blocking_reason_codes": blocking,
        "scope_results": scopes,
        "unscored_tables": unscored,
        "metrics": metric_copy,
    }
    _canonical_bytes(result)
    return result


def canonical_primary_table_grid_evaluation_json(
    value: Mapping[str, Any],
) -> bytes:
    return _canonical_bytes(validate_primary_table_grid_evaluation(value))


def _empty_metrics(*, reviewed_scope_count: int) -> dict[str, int]:
    return {
        "reviewed_scope_count": reviewed_scope_count,
        "evaluated_scope_count": 0,
        "passed_scope_count": 0,
        "failed_scope_count": 0,
        "expected_cell_count": 0,
        "matched_cell_count": 0,
        "failed_cell_count": 0,
        "missing_attachment_count": 0,
        "wrong_partition_attachment_count": 0,
        "duplicate_attachment_count": 0,
        "orphan_attachment_count": 0,
        "negative_anchor_attachment_count": 0,
        "unscored_attachment_count": 0,
        "unscored_table_count": 0,
    }


def _candidate_table_occurrences(table: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            occurrence_id
            for row in table["rows"]
            for cell in row["cells"]
            for occurrence_id in cell["occurrence_ids"]
        },
        key=_occurrence_sort_key,
    )


def _candidate_cells(table: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in table["rows"]:
        for cell in row["cells"]:
            result.append(
                {
                    "table_grid_id": table["table_grid_id"],
                    "cell_id": cell["cell_id"],
                    "row_index": cell["row_index"],
                    "column_index": cell["column_index"],
                    "row_span": cell["row_span"],
                    "column_span": cell["column_span"],
                    "occurrence_ids": list(cell["occurrence_ids"]),
                }
            )
    return result


def _gold_quality_status(gold: ReplayedPrimaryTableGridGold) -> str:
    return (
        "evaluable"
        if gold.has_trusted_confirmation
        else gold.fixture.quality_gate_status
    )


def _compare_replayed_table_grid(
    candidate: Mapping[str, Any],
    gold: ReplayedPrimaryTableGridGold,
) -> dict[str, Any]:
    """Compare a candidate replayed by the public gate with replayed Gold."""

    if type(gold) is not ReplayedPrimaryTableGridGold:
        raise PdfPrimaryTableGridEvaluationError(
            "table-grid Gold must come from strict source replay"
        )
    candidate_digest = sha256(
        canonical_pdf_primary_table_grid_json(candidate)
    ).hexdigest()
    gold_canonical = gold.canonical_json()
    if type(gold_canonical) is not bytes:
        raise PdfPrimaryTableGridEvaluationError(
            "Gold replay must expose canonical byte strings"
        )
    gold_digest = sha256(gold_canonical).hexdigest()
    if gold_digest != gold.canonical_sha256:
        raise PdfPrimaryTableGridEvaluationError(
            "Gold replay digest disagrees with its canonical bytes"
        )
    gold_payload = gold.payload
    quality_status = _gold_quality_status(gold)
    common = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": candidate["notice_id"],
        "source": {
            "source_pdf_sha256": candidate["source_pdf_sha256"],
            "native_capture_sha256": gold_payload["source"]["native_capture"][
                "canonical_sha256"
            ],
        },
        "candidate": {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "canonical_sha256": candidate_digest,
        },
        "gold": {
            "schema_version": GOLD_SCHEMA_VERSION,
            "canonical_sha256": gold_digest,
            "quality_status": quality_status,
        },
    }
    reviewed_scope_count = len(gold_payload["reviewed_scopes"])
    if quality_status == "not_evaluable_gold_pending":
        return validate_primary_table_grid_evaluation(
            {
                **common,
                "verdict": "not_evaluable_gold_pending",
                "blocking_reason_codes": [],
                "scope_results": [],
                "unscored_tables": [],
                "metrics": _empty_metrics(
                    reviewed_scope_count=reviewed_scope_count
                ),
            }
        )
    if quality_status != "evaluable":
        return validate_primary_table_grid_evaluation(
            {
                **common,
                "verdict": "not_evaluable_gold_untrusted",
                "blocking_reason_codes": [],
                "scope_results": [],
                "unscored_tables": [],
                "metrics": _empty_metrics(
                    reviewed_scope_count=reviewed_scope_count
                ),
            }
        )

    blocking: list[str] = []
    if candidate["notice_id"] != gold_payload["notice_id"]:
        blocking.append("notice_id_mismatch")
    if candidate["source_pdf_sha256"] != gold_payload["source"]["source_pdf_sha256"]:
        blocking.append("source_pdf_mismatch")
    if (
        candidate["input_artifacts"]["native_capture_sha256"]
        != gold_payload["source"]["native_capture"]["canonical_sha256"]
    ):
        blocking.append("native_capture_mismatch")
    if not set(gold_payload["page_scope"]).issubset(candidate["page_scope"]):
        blocking.append("gold_page_scope_not_covered")
    blocking = sorted(set(blocking), key=_BLOCKING_RANK.__getitem__)
    if blocking:
        return validate_primary_table_grid_evaluation(
            {
                **common,
                "verdict": "blocked_scope_mismatch",
                "blocking_reason_codes": blocking,
                "scope_results": [],
                "unscored_tables": [],
                "metrics": _empty_metrics(
                    reviewed_scope_count=reviewed_scope_count
                ),
            }
        )

    tables = list(candidate["tables"])
    table_occurrences = {
        table["table_grid_id"]: _candidate_table_occurrences(table)
        for table in tables
    }
    positive_occurrences_by_scope = {
        scope["scope_id"]: {
            occurrence_id
            for cell in scope["cells"]
            for occurrence_id in cell["occurrence_ids"]
        }
        for scope in gold_payload["reviewed_scopes"]
    }
    all_gold_reviewed_occurrences = {
        occurrence_id
        for scope in gold_payload["reviewed_scopes"]
        for occurrence_id in scope["occurrence_ids"]
    }
    scope_ids_by_positive_occurrence: dict[str, set[str]] = {}
    membership_work = 0
    for scope_id, positive_occurrences in positive_occurrences_by_scope.items():
        for occurrence_id in positive_occurrences:
            membership_work += 1
            if membership_work > MAX_SCOPE_MEMBERSHIP_WORK:
                raise PdfPrimaryTableGridEvaluationError(
                    "comparison exceeds the scope-membership work cap"
                )
            scope_ids_by_positive_occurrence.setdefault(occurrence_id, set()).add(
                scope_id
            )

    matching_scope_ids_by_table: dict[str, set[str]] = {}
    tables_by_matching_scope: dict[str, list[Mapping[str, Any]]] = {
        scope_id: [] for scope_id in positive_occurrences_by_scope
    }
    for table in tables:
        table_id = table["table_grid_id"]
        matching_scope_ids: set[str] = set()
        for occurrence_id in table_occurrences[table_id]:
            membership_work += 1
            if membership_work > MAX_SCOPE_MEMBERSHIP_WORK:
                raise PdfPrimaryTableGridEvaluationError(
                    "comparison exceeds the scope-membership work cap"
                )
            matching_scope_ids.update(
                scope_ids_by_positive_occurrence.get(occurrence_id, ())
            )
        matching_scope_ids_by_table[table_id] = matching_scope_ids
        for scope_id in matching_scope_ids:
            tables_by_matching_scope[scope_id].append(table)
    for matching_tables in tables_by_matching_scope.values():
        matching_tables.sort(key=lambda item: item["table_grid_id"])
    scored_table_ids: set[str] = set()
    scope_results: list[dict[str, Any]] = []
    expanded_slot_work = 0
    for scope in gold_payload["reviewed_scopes"]:
        gold_cells = list(scope["cells"])
        positive_to_cell = {
            occurrence_id: cell["cell_id"]
            for cell in gold_cells
            for occurrence_id in cell["occurrence_ids"]
        }
        negatives = {
            item["occurrence_id"] for item in scope["hard_negatives"]
        }
        selected_tables = [
            table
            for table in tables_by_matching_scope[scope["scope_id"]]
            if table["page"] == scope["physical_page"]
        ]
        scored_table_ids.update(table["table_grid_id"] for table in selected_tables)
        observed_tables = [
            {
                "table_grid_id": table["table_grid_id"],
                "row_count": table["row_count"],
                "column_count": table["column_count"],
            }
            for table in selected_tables
        ]
        shared_candidate_table_ids = sorted(
            table["table_grid_id"]
            for table in selected_tables
            if len(matching_scope_ids_by_table[table["table_grid_id"]]) > 1
        )
        expected_rows = list(scope["row_ids"])
        expected_columns = list(scope["column_ids"])
        dimension_topology_status = (
            "missing"
            if not selected_tables
            else (
                "matched"
                if len(selected_tables) == 1
                and selected_tables[0]["row_count"] == len(expected_rows)
                and selected_tables[0]["column_count"] == len(expected_columns)
                else "mismatched"
            )
        )

        row_position = {
            row_id: index for index, row_id in enumerate(expected_rows, start=1)
        }
        column_position = {
            column_id: index
            for index, column_id in enumerate(expected_columns, start=1)
        }
        gold_slot_to_cell: dict[tuple[int, int], str] = {}
        gold_cell_slots: dict[str, set[tuple[int, int]]] = {}
        for cell in gold_cells:
            slots = {
                (row_position[row_id], column_position[column_id])
                for row_id in cell["row_ids"]
                for column_id in cell["column_ids"]
            }
            expanded_slot_work += len(slots)
            if expanded_slot_work > MAX_EXPANDED_GRID_SLOTS:
                raise PdfPrimaryTableGridEvaluationError(
                    "comparison exceeds the aggregate expanded-grid-slot work cap"
                )
            gold_cell_slots[cell["cell_id"]] = slots
            for slot in slots:
                gold_slot_to_cell[slot] = cell["cell_id"]

        attachments: dict[str, list[dict[str, Any]]] = {}
        candidate_cells: list[dict[str, Any]] = []
        for table in selected_tables:
            candidate_cells.extend(_candidate_cells(table))
        expanded_candidate_cells: list[dict[str, Any]] = []
        for cell in candidate_cells:
            slot_count = cell["row_span"] * cell["column_span"]
            expanded_slot_work += slot_count
            if expanded_slot_work > MAX_EXPANDED_GRID_SLOTS:
                raise PdfPrimaryTableGridEvaluationError(
                    "comparison exceeds the aggregate expanded-grid-slot work cap"
                )
            slots = {
                (row_index, column_index)
                for row_index in range(
                    cell["row_index"], cell["row_index"] + cell["row_span"]
                )
                for column_index in range(
                    cell["column_index"],
                    cell["column_index"] + cell["column_span"],
                )
            }
            attachment = {**cell, "slots": slots}
            expanded_candidate_cells.append(attachment)
            for occurrence_id in cell["occurrence_ids"]:
                attachments.setdefault(occurrence_id, []).append(attachment)

        partition_mismatch_cells: set[str] = set()
        candidate_cells_by_gold_cell: dict[str, list[dict[str, Any]]] = {}
        for gold_cell_id, expected_slots in gold_cell_slots.items():
            overlapping = [
                cell
                for cell in expanded_candidate_cells
                if cell["slots"].intersection(expected_slots)
            ]
            candidate_cells_by_gold_cell[gold_cell_id] = overlapping
            if len(overlapping) != 1 or overlapping[0]["slots"] != expected_slots:
                partition_mismatch_cells.add(gold_cell_id)
        topology_status = (
            "mismatched"
            if dimension_topology_status == "matched" and partition_mismatch_cells
            else dimension_topology_status
        )

        missing: set[str] = set()
        wrong: set[str] = set()
        duplicate: set[str] = set()
        orphan: set[str] = set()
        for occurrence_id, expected_cell_id in positive_to_cell.items():
            observed = attachments.get(occurrence_id, [])
            if not observed:
                missing.add(occurrence_id)
                continue
            if len(observed) > 1:
                duplicate.add(occurrence_id)
            expected_slots = gold_cell_slots[expected_cell_id]
            observed_slot_owners = {
                gold_slot_to_cell.get(slot)
                for attachment in observed
                for slot in attachment["slots"]
            }
            if None in observed_slot_owners:
                orphan.add(occurrence_id)
            if observed_slot_owners - {None, expected_cell_id}:
                wrong.add(occurrence_id)
            if expected_cell_id not in observed_slot_owners and None not in observed_slot_owners:
                wrong.add(occurrence_id)
        for cell in gold_cells:
            if cell["cell_id"] in partition_mismatch_cells:
                wrong.update(cell["occurrence_ids"])

        negative_attached = {
            occurrence_id
            for occurrence_id in negatives
            if attachments.get(occurrence_id)
        }
        unscored_attached = set(attachments) - all_gold_reviewed_occurrences
        cell_results: list[dict[str, Any]] = []
        for cell in gold_cells:
            expected_ids = list(cell["occurrence_ids"])
            expected_set = set(expected_ids)
            cell_failures: list[str] = []
            if expected_set & missing:
                cell_failures.append("missing_attachment")
            if expected_set & wrong:
                cell_failures.append("wrong_partition")
            elif cell["cell_id"] in partition_mismatch_cells:
                cell_failures.append("wrong_partition")
            if expected_set & duplicate:
                cell_failures.append("duplicate_attachment")
            if expected_set & orphan:
                cell_failures.append("orphan_attachment")
            observed_ids = sorted(
                {
                    occurrence_id
                    for occurrence_id in expected_ids
                    if attachments.get(occurrence_id)
                },
                key=_occurrence_sort_key,
            )
            observed_cell_ids = sorted(
                {
                    candidate_cell["cell_id"]
                    for candidate_cell in candidate_cells_by_gold_cell[
                        cell["cell_id"]
                    ]
                }
            )
            cell_results.append(
                {
                    "cell_id": cell["cell_id"],
                    "row_ids": list(cell["row_ids"]),
                    "column_ids": list(cell["column_ids"]),
                    "status": "failed" if cell_failures else "passed",
                    "failure_codes": cell_failures,
                    "expected_occurrence_ids": expected_ids,
                    "observed_reviewed_occurrence_ids": observed_ids,
                    "observed_candidate_cell_ids": observed_cell_ids,
                }
            )
        cell_results.sort(key=lambda item: item["cell_id"])

        failures: list[str] = []
        if topology_status == "missing":
            failures.append("segment_missing")
        elif topology_status == "mismatched":
            failures.append("topology_mismatch")
        if shared_candidate_table_ids:
            failures.append("shared_candidate_table")
        for code, values in (
            ("missing_attachment", missing),
            ("wrong_partition", wrong),
            ("duplicate_attachment", duplicate),
            ("orphan_attachment", orphan),
            ("negative_anchor_attachment", negative_attached),
        ):
            if values:
                failures.append(code)
        scope_results.append(
            {
                "scope_id": scope["scope_id"],
                "physical_page": scope["physical_page"],
                "segment_id": scope["segment_id"],
                "verdict": "failed" if failures else "passed",
                "failure_codes": failures,
                "expected_row_count": len(expected_rows),
                "expected_column_count": len(expected_columns),
                "topology_status": topology_status,
                "observed_tables": observed_tables,
                "shared_candidate_table_ids": shared_candidate_table_ids,
                "cell_results": cell_results,
                "missing_attachment_occurrence_ids": sorted(
                    missing, key=_occurrence_sort_key
                ),
                "wrong_partition_occurrence_ids": sorted(
                    wrong, key=_occurrence_sort_key
                ),
                "duplicate_attachment_occurrence_ids": sorted(
                    duplicate, key=_occurrence_sort_key
                ),
                "orphan_attachment_occurrence_ids": sorted(
                    orphan, key=_occurrence_sort_key
                ),
                "negative_anchor_attachment_occurrence_ids": sorted(
                    negative_attached, key=_occurrence_sort_key
                ),
                "unscored_attachment_occurrence_ids": sorted(
                    unscored_attached, key=_occurrence_sort_key
                ),
            }
        )

    scope_results.sort(key=lambda item: (item["physical_page"], item["scope_id"]))
    unscored_tables = [
        {
            "table_grid_id": table["table_grid_id"],
            "physical_page": table["page"],
            "occurrence_ids": table_occurrences[table["table_grid_id"]],
        }
        for table in tables
        if table["table_grid_id"] not in scored_table_ids
    ]
    unscored_tables.sort(
        key=lambda item: (item["physical_page"], item["table_grid_id"])
    )
    metrics = {
        "reviewed_scope_count": reviewed_scope_count,
        "evaluated_scope_count": len(scope_results),
        "passed_scope_count": sum(item["verdict"] == "passed" for item in scope_results),
        "failed_scope_count": sum(item["verdict"] == "failed" for item in scope_results),
        "expected_cell_count": sum(len(item["cell_results"]) for item in scope_results),
        "matched_cell_count": sum(
            cell["status"] == "passed"
            for item in scope_results
            for cell in item["cell_results"]
        ),
        "failed_cell_count": sum(
            cell["status"] == "failed"
            for item in scope_results
            for cell in item["cell_results"]
        ),
        "missing_attachment_count": sum(
            len(item["missing_attachment_occurrence_ids"]) for item in scope_results
        ),
        "wrong_partition_attachment_count": sum(
            len(item["wrong_partition_occurrence_ids"]) for item in scope_results
        ),
        "duplicate_attachment_count": sum(
            len(item["duplicate_attachment_occurrence_ids"]) for item in scope_results
        ),
        "orphan_attachment_count": sum(
            len(item["orphan_attachment_occurrence_ids"]) for item in scope_results
        ),
        "negative_anchor_attachment_count": sum(
            len(item["negative_anchor_attachment_occurrence_ids"])
            for item in scope_results
        ),
        "unscored_attachment_count": sum(
            len(item["unscored_attachment_occurrence_ids"])
            for item in scope_results
        ),
        "unscored_table_count": len(unscored_tables),
    }
    return validate_primary_table_grid_evaluation(
        {
            **common,
            "verdict": "failed" if metrics["failed_scope_count"] else "passed",
            "blocking_reason_codes": [],
            "scope_results": scope_results,
            "unscored_tables": unscored_tables,
            "metrics": metrics,
        }
    )


def evaluate_pdf_primary_table_grid(
    candidate: Mapping[str, Any] | PrimaryTableGridFixture,
    gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
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
    """Replay candidate and Gold from the same raw inputs, then compare."""

    candidate_inputs = {
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
        replayed_candidate = validate_pdf_primary_table_grid_against_inputs(
            candidate, **candidate_inputs
        )
    except PdfPrimaryTableGridError as error:
        raise PdfPrimaryTableGridEvaluationError(
            "table-grid candidate did not pass deterministic source replay"
        ) from error
    if isinstance(replayed_candidate, PrimaryTableGridFixture):
        candidate_payload = replayed_candidate.to_dict()
    elif isinstance(replayed_candidate, Mapping):
        candidate_payload = dict(replayed_candidate)
    else:
        raise PdfPrimaryTableGridEvaluationError(
            "candidate replay returned an unsupported receipt"
        )
    try:
        replayed_gold = validate_primary_table_grid_gold_against_inputs(
            gold,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
        )
    except PrimaryTableGridGoldError as error:
        raise PdfPrimaryTableGridEvaluationError(
            "table-grid Gold did not pass deterministic source replay"
        ) from error
    return _compare_replayed_table_grid(candidate_payload, replayed_gold)


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "PdfPrimaryTableGridEvaluationError",
    "SCHEMA_VERSION",
    "canonical_primary_table_grid_evaluation_json",
    "evaluate_pdf_primary_table_grid",
    "validate_primary_table_grid_evaluation",
]
