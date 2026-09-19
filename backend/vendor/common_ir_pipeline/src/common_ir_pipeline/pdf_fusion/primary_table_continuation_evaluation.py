"""Partial-Gold evaluation for cross-page table continuations.

Candidate table IDs are diagnostics only.  The comparison projects each
candidate endpoint into a reviewed A4.3a Gold scope by page-local native
occurrence overlap, then compares the reviewed edge, ``between_rows`` kind,
and complete column mapping.  Relations wholly outside the reviewed universe
are unscored; any other non-matching relation is a false positive.

The public quality gate is connected at the bottom of this module.  It replays
the continuation candidate, continuation Gold, and the A4.3a prerequisite from
the same raw inputs before this comparison is allowed to issue a quality
verdict.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .primary_table_continuation import (
    SCHEMA_VERSION as _CANDIDATE_SCHEMA_VERSION,
    PdfPrimaryTableContinuationError,
    PrimaryTableContinuationFixture,
    canonical_pdf_primary_table_continuation_json,
    validate_pdf_primary_table_continuation_against_inputs,
)
from .primary_table_continuation_gold import (
    SCHEMA_VERSION as _GOLD_SCHEMA_VERSION,
    PrimaryTableContinuationGoldError,
    PrimaryTableContinuationGoldFixture,
    ReplayedPrimaryTableContinuationGold,
    validate_primary_table_continuation_gold_against_inputs,
)
from .primary_table_grid import (
    SCHEMA_VERSION as _TABLE_GRID_SCHEMA_VERSION,
    PdfPrimaryTableGridError,
    PrimaryTableGridFixture,
    canonical_pdf_primary_table_grid_json,
    validate_pdf_primary_table_grid_against_inputs,
)
from .primary_table_grid_evaluation import (
    SCHEMA_VERSION as _TABLE_GRID_EVALUATION_SCHEMA_VERSION,
    PdfPrimaryTableGridEvaluationError,
    canonical_primary_table_grid_evaluation_json,
    evaluate_pdf_primary_table_grid,
)
from .primary_table_grid_gold import (
    SCHEMA_VERSION as _TABLE_GRID_GOLD_SCHEMA_VERSION,
    PrimaryTableGridGoldError,
    PrimaryTableGridGoldFixture,
    ReplayedPrimaryTableGridGold,
    validate_primary_table_grid_gold_against_inputs,
)
from .render_manifest import PdfRenderManifest
from .surya_layout_artifact import SuryaProducerIdentity


SCHEMA_VERSION = "pdf_primary_table_continuation_evaluation/v1"
CANDIDATE_SCHEMA_VERSION = _CANDIDATE_SCHEMA_VERSION
GOLD_SCHEMA_VERSION = _GOLD_SCHEMA_VERSION
TABLE_GRID_SCHEMA_VERSION = _TABLE_GRID_SCHEMA_VERSION
TABLE_GRID_GOLD_SCHEMA_VERSION = _TABLE_GRID_GOLD_SCHEMA_VERSION
TABLE_GRID_EVALUATION_SCHEMA_VERSION = _TABLE_GRID_EVALUATION_SCHEMA_VERSION
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 250_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_REVIEWED_CONTINUATIONS = 1_024
MAX_CANDIDATE_CONTINUATIONS = 10_000
MAX_CANDIDATE_COLUMNS = 1_000
MAX_GOLD_COLUMNS = 1_024
MAX_ENDPOINT_MEMBERSHIP_WORK = 5_048_576

_SHA = frozenset("0123456789abcdef")
_NOTICE_ID = re.compile(r"^PBLN_[0-9]{15}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_CONTINUATION_ID = re.compile(r"^table-continuation-[0-9a-f]{64}$")
_TABLE_GRID_ID = re.compile(r"^table-grid-[0-9a-f]{64}$")
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
        "page_local_candidate",
        "page_local_gold",
        "page_local_prerequisite",
        "verdict",
        "blocking_reason_codes",
        "continuation_results",
        "false_positive_relations",
        "unscored_relations",
        "metrics",
    }
)
_SOURCE_KEYS = frozenset({"source_pdf_sha256", "native_capture_sha256"})
_REF_KEYS = frozenset({"schema_version", "canonical_sha256"})
_GOLD_REF_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "quality_status"}
)
_PREREQUISITE_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "verdict"}
)
_RESULT_KEYS = frozenset(
    {
        "gold_continuation_id",
        "predecessor_scope_id",
        "predecessor_page",
        "successor_scope_id",
        "successor_page",
        "expected_relation_kind",
        "expected_column_mapping",
        "status",
        "failure_codes",
        "observed_relations",
    }
)
_OBSERVED_KEYS = frozenset(
    {
        "continuation_id",
        "predecessor_table_grid_id",
        "predecessor_page",
        "successor_table_grid_id",
        "successor_page",
        "relation_kind",
        "column_mapping",
        "mapped_predecessor_scope_id",
        "mapped_successor_scope_id",
        "normalized_column_mapping",
    }
)
_INDEX_MAPPING_KEYS = frozenset(
    {"predecessor_column_index", "successor_column_index"}
)
_IDENTIFIER_MAPPING_KEYS = frozenset(
    {"predecessor_column_id", "successor_column_id"}
)
_FALSE_POSITIVE_KEYS = frozenset({"relation", "failure_codes"})
_METRIC_KEYS = frozenset(
    {
        "reviewed_continuation_count",
        "evaluated_continuation_count",
        "matched_continuation_count",
        "failed_continuation_count",
        "missing_continuation_count",
        "false_positive_relation_count",
        "unscored_relation_count",
    }
)

_VERDICTS = frozenset(
    {
        "passed",
        "failed",
        "not_evaluable_gold_pending",
        "not_evaluable_gold_untrusted",
        "blocked_page_local_prerequisite",
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
_PREREQUISITE_VERDICTS = frozenset(
    {
        "passed",
        "failed",
        "not_evaluable_gold_pending",
        "not_evaluable_gold_untrusted",
        "blocked_scope_mismatch",
    }
)
_BLOCKING_ORDER = (
    "notice_id_mismatch",
    "source_pdf_mismatch",
    "native_capture_mismatch",
    "candidate_table_grid_mismatch",
    "gold_table_grid_mismatch",
    "gold_page_scope_not_covered",
    "page_local_prerequisite_not_passed",
)
_BLOCKING = frozenset(_BLOCKING_ORDER)
_BLOCKING_RANK = {item: index for index, item in enumerate(_BLOCKING_ORDER)}
_RESULT_FAILURE_ORDER = (
    "missing_relation",
    "multiple_matching_relations",
    "shared_predecessor",
    "shared_successor",
)
_RESULT_FAILURES = frozenset(_RESULT_FAILURE_ORDER)
_RESULT_FAILURE_RANK = {
    item: index for index, item in enumerate(_RESULT_FAILURE_ORDER)
}
_FALSE_POSITIVE_ORDER = (
    "unexpected_edge",
    "wrong_relation_kind",
    "wrong_column_mapping",
    "shared_predecessor",
    "shared_successor",
)
_FALSE_POSITIVE_FAILURES = frozenset(_FALSE_POSITIVE_ORDER)
_FALSE_POSITIVE_RANK = {
    item: index for index, item in enumerate(_FALSE_POSITIVE_ORDER)
}


class PdfPrimaryTableContinuationEvaluationError(ValueError):
    """Raised when an A4.3b evaluation is unsafe or inconsistent."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must be an object"
        )
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} keys must be strings"
        )
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} keys are invalid ({'; '.join(details)})"
        )
    return value


def _string(name: str, value: object, *, maximum: int = MAX_STRING_BYTES) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must be a non-empty trimmed string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} contains invalid Unicode"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} exceeds its string safety cap"
        )
    return value


def _sha(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if len(checked) != 64 or any(character not in _SHA for character in checked):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return checked


def _identifier(name: str, value: object) -> str:
    checked = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(checked) is None:
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} is not a bounded identifier"
        )
    return checked


def _positive(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _nonnegative(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PdfPrimaryTableContinuationEvaluationError(
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
            raise PdfPrimaryTableContinuationEvaluationError(
                "evaluation exceeds the JSON node cap"
            )
        if depth > MAX_JSON_DEPTH:
            raise PdfPrimaryTableContinuationEvaluationError(
                "evaluation exceeds the JSON depth cap"
            )
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise PdfPrimaryTableContinuationEvaluationError(
                    "evaluation contains a non-finite number"
                )
            continue
        if isinstance(current, str):
            _string("evaluation string", current)
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise PdfPrimaryTableContinuationEvaluationError(
                        "evaluation object keys must be strings"
                    )
                stack.append((key, depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
            continue
        raise PdfPrimaryTableContinuationEvaluationError(
            f"evaluation contains unsupported type {type(current).__name__}"
        )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(nested) for nested in value]
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
        raise PdfPrimaryTableContinuationEvaluationError(
            "evaluation is not canonical UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryTableContinuationEvaluationError(
            "evaluation exceeds the artifact byte cap"
        )
    return encoded


def _ranked_codes(
    name: str,
    value: object,
    *,
    allowed: frozenset[str],
    rank: Mapping[str, int],
) -> list[str]:
    if not isinstance(value, list) or len(value) > len(allowed):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must be a bounded array"
        )
    result = [_string(f"{name}[{index}]", item) for index, item in enumerate(value)]
    if (
        len(result) != len(set(result))
        or any(item not in allowed for item in result)
        or result != sorted(result, key=rank.__getitem__)
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must contain canonical unique failure codes"
        )
    return result


def _artifact_ref(
    value: object, *, name: str, schema_version: str
) -> dict[str, str]:
    ref = _exact_keys(value, _REF_KEYS, name=name)
    if ref["schema_version"] != schema_version:
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name}.schema_version is invalid"
        )
    return {
        "schema_version": schema_version,
        "canonical_sha256": _sha(
            f"{name}.canonical_sha256", ref["canonical_sha256"]
        ),
    }


def _column_mapping(
    value: object,
    *,
    name: str,
    identifier_mapping: bool,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value)
        > (MAX_GOLD_COLUMNS if identifier_mapping else MAX_CANDIDATE_COLUMNS)
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must be a bounded array"
        )
    expected_keys = (
        _IDENTIFIER_MAPPING_KEYS if identifier_mapping else _INDEX_MAPPING_KEYS
    )
    result: list[dict[str, Any]] = []
    for index, raw_item in enumerate(value):
        item_name = f"{name}[{index}]"
        item = _exact_keys(raw_item, expected_keys, name=item_name)
        if identifier_mapping:
            normalized = {
                "predecessor_column_id": _identifier(
                    f"{item_name}.predecessor_column_id",
                    item["predecessor_column_id"],
                ),
                "successor_column_id": _identifier(
                    f"{item_name}.successor_column_id",
                    item["successor_column_id"],
                ),
            }
        else:
            normalized = {
                "predecessor_column_index": _positive(
                    f"{item_name}.predecessor_column_index",
                    item["predecessor_column_index"],
                    maximum=MAX_CANDIDATE_COLUMNS,
                ),
                "successor_column_index": _positive(
                    f"{item_name}.successor_column_index",
                    item["successor_column_index"],
                    maximum=MAX_CANDIDATE_COLUMNS,
                ),
            }
        result.append(normalized)
    predecessor_key = (
        "predecessor_column_id"
        if identifier_mapping
        else "predecessor_column_index"
    )
    successor_key = (
        "successor_column_id" if identifier_mapping else "successor_column_index"
    )
    pairs = [(item[predecessor_key], item[successor_key]) for item in result]
    if (
        (not identifier_mapping and pairs != sorted(pairs))
        or len({item[0] for item in pairs}) != len(pairs)
        or len({item[1] for item in pairs}) != len(pairs)
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must be a canonical one-to-one mapping"
        )
    return result


def _nullable_identifier(name: str, value: object) -> str | None:
    return None if value is None else _identifier(name, value)


def _validate_observed_relation(value: object, *, name: str) -> dict[str, Any]:
    relation = _exact_keys(value, _OBSERVED_KEYS, name=name)
    continuation_id = _string(
        f"{name}.continuation_id", relation["continuation_id"], maximum=83
    )
    if _CONTINUATION_ID.fullmatch(continuation_id) is None:
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name}.continuation_id is invalid"
        )
    predecessor_table = _string(
        f"{name}.predecessor_table_grid_id",
        relation["predecessor_table_grid_id"],
        maximum=75,
    )
    successor_table = _string(
        f"{name}.successor_table_grid_id",
        relation["successor_table_grid_id"],
        maximum=75,
    )
    if (
        _TABLE_GRID_ID.fullmatch(predecessor_table) is None
        or _TABLE_GRID_ID.fullmatch(successor_table) is None
        or predecessor_table == successor_table
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} table-grid endpoints are invalid"
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
    if successor_page != predecessor_page + 1:
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} endpoints must be on adjacent increasing pages"
        )
    relation_kind = _string(f"{name}.relation_kind", relation["relation_kind"])
    if relation_kind not in {"between_rows", "same_row"}:
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name}.relation_kind is invalid"
        )
    predecessor_scope = _nullable_identifier(
        f"{name}.mapped_predecessor_scope_id",
        relation["mapped_predecessor_scope_id"],
    )
    successor_scope = _nullable_identifier(
        f"{name}.mapped_successor_scope_id",
        relation["mapped_successor_scope_id"],
    )
    raw_mapping = _column_mapping(
        relation["column_mapping"],
        name=f"{name}.column_mapping",
        identifier_mapping=False,
    )
    normalized_mapping = _column_mapping(
        relation["normalized_column_mapping"],
        name=f"{name}.normalized_column_mapping",
        identifier_mapping=True,
        allow_empty=True,
    )
    if (predecessor_scope is None or successor_scope is None) and normalized_mapping:
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} cannot normalize columns outside reviewed scopes"
        )
    if (
        predecessor_scope is not None
        and successor_scope is not None
        and not normalized_mapping
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} reviewed endpoints require a normalized column mapping"
        )
    if normalized_mapping and len(normalized_mapping) != len(raw_mapping):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} normalized mapping must preserve every candidate column pair"
        )
    return {
        "continuation_id": continuation_id,
        "predecessor_table_grid_id": predecessor_table,
        "predecessor_page": predecessor_page,
        "successor_table_grid_id": successor_table,
        "successor_page": successor_page,
        "relation_kind": relation_kind,
        "column_mapping": raw_mapping,
        "mapped_predecessor_scope_id": predecessor_scope,
        "mapped_successor_scope_id": successor_scope,
        "normalized_column_mapping": normalized_mapping,
    }


def _observed_sort_key(value: Mapping[str, Any]) -> str:
    return str(value["continuation_id"])


def _validate_observed_array(
    value: object, *, name: str, allow_empty: bool = True
) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value) > MAX_CANDIDATE_CONTINUATIONS
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must be a bounded array"
        )
    result = [
        _validate_observed_relation(item, name=f"{name}[{index}]")
        for index, item in enumerate(value)
    ]
    ids = [item["continuation_id"] for item in result]
    if len(ids) != len(set(ids)) or result != sorted(result, key=_observed_sort_key):
        raise PdfPrimaryTableContinuationEvaluationError(
            f"{name} must use unique canonical continuation-ID order"
        )
    return result


def _empty_metrics(*, reviewed_continuation_count: int) -> dict[str, int]:
    return {
        "reviewed_continuation_count": reviewed_continuation_count,
        "evaluated_continuation_count": 0,
        "matched_continuation_count": 0,
        "failed_continuation_count": 0,
        "missing_continuation_count": 0,
        "false_positive_relation_count": 0,
        "unscored_relation_count": 0,
    }


def validate_primary_table_continuation_evaluation(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate result shape and all standalone internal invariants."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="evaluation")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfPrimaryTableContinuationEvaluationError(
            "schema_version is invalid"
        )
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfPrimaryTableContinuationEvaluationError(
            "evaluation must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PdfPrimaryTableContinuationEvaluationError(
            "standalone validation scope is invalid"
        )
    notice_id = _string("notice_id", root["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PdfPrimaryTableContinuationEvaluationError("notice_id is invalid")
    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    source_copy = {
        "source_pdf_sha256": _sha(
            "source.source_pdf_sha256", source["source_pdf_sha256"]
        ),
        "native_capture_sha256": _sha(
            "source.native_capture_sha256", source["native_capture_sha256"]
        ),
    }
    candidate_ref = _artifact_ref(
        root["candidate"], name="candidate", schema_version=CANDIDATE_SCHEMA_VERSION
    )
    gold_raw = _exact_keys(root["gold"], _GOLD_REF_KEYS, name="gold")
    if gold_raw["schema_version"] != GOLD_SCHEMA_VERSION:
        raise PdfPrimaryTableContinuationEvaluationError(
            "gold.schema_version is invalid"
        )
    quality_status = _string("gold.quality_status", gold_raw["quality_status"])
    if quality_status not in _QUALITY_STATUSES:
        raise PdfPrimaryTableContinuationEvaluationError(
            "gold.quality_status is invalid"
        )
    gold_ref = {
        "schema_version": GOLD_SCHEMA_VERSION,
        "canonical_sha256": _sha(
            "gold.canonical_sha256", gold_raw["canonical_sha256"]
        ),
        "quality_status": quality_status,
    }
    page_local_candidate = _artifact_ref(
        root["page_local_candidate"],
        name="page_local_candidate",
        schema_version=TABLE_GRID_SCHEMA_VERSION,
    )
    page_local_gold = _artifact_ref(
        root["page_local_gold"],
        name="page_local_gold",
        schema_version=TABLE_GRID_GOLD_SCHEMA_VERSION,
    )
    prerequisite_raw = _exact_keys(
        root["page_local_prerequisite"],
        _PREREQUISITE_KEYS,
        name="page_local_prerequisite",
    )
    if prerequisite_raw["schema_version"] != TABLE_GRID_EVALUATION_SCHEMA_VERSION:
        raise PdfPrimaryTableContinuationEvaluationError(
            "page_local_prerequisite.schema_version is invalid"
        )
    prerequisite_verdict = _string(
        "page_local_prerequisite.verdict", prerequisite_raw["verdict"]
    )
    if prerequisite_verdict not in _PREREQUISITE_VERDICTS:
        raise PdfPrimaryTableContinuationEvaluationError(
            "page_local_prerequisite.verdict is invalid"
        )
    prerequisite = {
        "schema_version": TABLE_GRID_EVALUATION_SCHEMA_VERSION,
        "canonical_sha256": _sha(
            "page_local_prerequisite.canonical_sha256",
            prerequisite_raw["canonical_sha256"],
        ),
        "verdict": prerequisite_verdict,
    }
    verdict = _string("verdict", root["verdict"])
    if verdict not in _VERDICTS:
        raise PdfPrimaryTableContinuationEvaluationError("verdict is invalid")
    blocking = _ranked_codes(
        "blocking_reason_codes",
        root["blocking_reason_codes"],
        allowed=_BLOCKING,
        rank=_BLOCKING_RANK,
    )

    results_raw = root["continuation_results"]
    if (
        not isinstance(results_raw, list)
        or len(results_raw) > MAX_REVIEWED_CONTINUATIONS
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            "continuation_results must be a bounded array"
        )
    results: list[dict[str, Any]] = []
    prior_result_id: str | None = None
    seen_gold_ids: set[str] = set()
    seen_expected_predecessors: set[str] = set()
    seen_expected_successors: set[str] = set()
    for index, raw_result in enumerate(results_raw):
        name = f"continuation_results[{index}]"
        item = _exact_keys(raw_result, _RESULT_KEYS, name=name)
        gold_id = _identifier(
            f"{name}.gold_continuation_id", item["gold_continuation_id"]
        )
        if gold_id in seen_gold_ids or (
            prior_result_id is not None and gold_id <= prior_result_id
        ):
            raise PdfPrimaryTableContinuationEvaluationError(
                "continuation_results must use canonical unique Gold-ID order"
            )
        seen_gold_ids.add(gold_id)
        prior_result_id = gold_id
        predecessor_scope = _identifier(
            f"{name}.predecessor_scope_id", item["predecessor_scope_id"]
        )
        successor_scope = _identifier(
            f"{name}.successor_scope_id", item["successor_scope_id"]
        )
        if (
            predecessor_scope == successor_scope
            or predecessor_scope in seen_expected_predecessors
            or successor_scope in seen_expected_successors
        ):
            raise PdfPrimaryTableContinuationEvaluationError(
                "reviewed continuations must have one predecessor and successor"
            )
        seen_expected_predecessors.add(predecessor_scope)
        seen_expected_successors.add(successor_scope)
        predecessor_page = _positive(
            f"{name}.predecessor_page",
            item["predecessor_page"],
            maximum=MAX_PAGES,
        )
        successor_page = _positive(
            f"{name}.successor_page",
            item["successor_page"],
            maximum=MAX_PAGES,
        )
        if successor_page != predecessor_page + 1:
            raise PdfPrimaryTableContinuationEvaluationError(
                f"{name} expected endpoints must be adjacent increasing pages"
            )
        if item["expected_relation_kind"] != "between_rows":
            raise PdfPrimaryTableContinuationEvaluationError(
                f"{name}.expected_relation_kind is invalid"
            )
        expected_mapping = _column_mapping(
            item["expected_column_mapping"],
            name=f"{name}.expected_column_mapping",
            identifier_mapping=True,
        )
        status = _string(f"{name}.status", item["status"])
        if status not in {"matched", "failed"}:
            raise PdfPrimaryTableContinuationEvaluationError(
                f"{name}.status is invalid"
            )
        failures = _ranked_codes(
            f"{name}.failure_codes",
            item["failure_codes"],
            allowed=_RESULT_FAILURES,
            rank=_RESULT_FAILURE_RANK,
        )
        observed = _validate_observed_array(
            item["observed_relations"], name=f"{name}.observed_relations"
        )
        for relation in observed:
            if (
                relation["mapped_predecessor_scope_id"] != predecessor_scope
                or relation["mapped_successor_scope_id"] != successor_scope
                or relation["relation_kind"] != "between_rows"
                or relation["normalized_column_mapping"] != expected_mapping
            ):
                raise PdfPrimaryTableContinuationEvaluationError(
                    f"{name}.observed_relations must exactly match the reviewed edge"
                )
        derived_base: list[str] = []
        if not observed:
            derived_base.append("missing_relation")
        elif len(observed) > 1:
            derived_base.append("multiple_matching_relations")
        for code in derived_base:
            if code not in failures:
                raise PdfPrimaryTableContinuationEvaluationError(
                    f"{name}.failure_codes omit {code}"
                )
        if any(
            code in failures
            for code in ("missing_relation", "multiple_matching_relations")
        ) and set(failures) - set(derived_base) - {
            "shared_predecessor",
            "shared_successor",
        }:
            raise PdfPrimaryTableContinuationEvaluationError(
                f"{name}.failure_codes disagree with observed relations"
            )
        if (status == "failed") != bool(failures):
            raise PdfPrimaryTableContinuationEvaluationError(
                f"{name}.status disagrees with failure_codes"
            )
        if status == "matched" and len(observed) != 1:
            raise PdfPrimaryTableContinuationEvaluationError(
                f"{name} matched status requires exactly one relation"
            )
        results.append(
            {
                "gold_continuation_id": gold_id,
                "predecessor_scope_id": predecessor_scope,
                "predecessor_page": predecessor_page,
                "successor_scope_id": successor_scope,
                "successor_page": successor_page,
                "expected_relation_kind": "between_rows",
                "expected_column_mapping": expected_mapping,
                "status": status,
                "failure_codes": failures,
                "observed_relations": observed,
            }
        )

    false_raw = root["false_positive_relations"]
    if not isinstance(false_raw, list) or len(false_raw) > MAX_CANDIDATE_CONTINUATIONS:
        raise PdfPrimaryTableContinuationEvaluationError(
            "false_positive_relations must be a bounded array"
        )
    false_positives: list[dict[str, Any]] = []
    prior_false_id: str | None = None
    for index, raw_false in enumerate(false_raw):
        name = f"false_positive_relations[{index}]"
        item = _exact_keys(raw_false, _FALSE_POSITIVE_KEYS, name=name)
        relation = _validate_observed_relation(
            item["relation"], name=f"{name}.relation"
        )
        failures = _ranked_codes(
            f"{name}.failure_codes",
            item["failure_codes"],
            allowed=_FALSE_POSITIVE_FAILURES,
            rank=_FALSE_POSITIVE_RANK,
        )
        if not failures:
            raise PdfPrimaryTableContinuationEvaluationError(
                f"{name}.failure_codes must be non-empty"
            )
        relation_id = relation["continuation_id"]
        if prior_false_id is not None and relation_id <= prior_false_id:
            raise PdfPrimaryTableContinuationEvaluationError(
                "false_positive_relations must use canonical unique ID order"
            )
        prior_false_id = relation_id
        false_positives.append({"relation": relation, "failure_codes": failures})

    unscored = _validate_observed_array(
        root["unscored_relations"], name="unscored_relations"
    )
    if any(
        item["mapped_predecessor_scope_id"] is not None
        or item["mapped_successor_scope_id"] is not None
        for item in unscored
    ):
        raise PdfPrimaryTableContinuationEvaluationError(
            "unscored relations must remain outside every reviewed endpoint scope"
        )

    all_observed = [
        relation
        for result in results
        for relation in result["observed_relations"]
    ] + [item["relation"] for item in false_positives] + unscored
    all_ids = [item["continuation_id"] for item in all_observed]
    if len(all_ids) != len(set(all_ids)):
        raise PdfPrimaryTableContinuationEvaluationError(
            "each candidate relation must have exactly one evaluation disposition"
        )
    predecessor_counts = Counter(
        item["predecessor_table_grid_id"] for item in all_observed
    )
    successor_counts = Counter(item["successor_table_grid_id"] for item in all_observed)
    shared_predecessor_ids = {
        relation_id
        for item in all_observed
        if predecessor_counts[item["predecessor_table_grid_id"]] > 1
        for relation_id in [item["continuation_id"]]
    }
    shared_successor_ids = {
        relation_id
        for item in all_observed
        if successor_counts[item["successor_table_grid_id"]] > 1
        for relation_id in [item["continuation_id"]]
    }
    expected_by_scope_pair = {
        (result["predecessor_scope_id"], result["successor_scope_id"]): result
        for result in results
    }
    for result in results:
        observed_ids = {
            item["continuation_id"] for item in result["observed_relations"]
        }
        expected_shared = {
            code
            for code, ids in (
                ("shared_predecessor", shared_predecessor_ids),
                ("shared_successor", shared_successor_ids),
            )
            if observed_ids.intersection(ids)
        }
        actual_shared = set(result["failure_codes"]).intersection(
            {"shared_predecessor", "shared_successor"}
        )
        if actual_shared != expected_shared:
            raise PdfPrimaryTableContinuationEvaluationError(
                "result shared-endpoint failure codes are inconsistent"
            )
    for item in false_positives:
        relation = item["relation"]
        relation_id = relation["continuation_id"]
        expected = expected_by_scope_pair.get(
            (
                relation["mapped_predecessor_scope_id"],
                relation["mapped_successor_scope_id"],
            )
        )
        expected_base: set[str] = set()
        if expected is None:
            expected_base.add("unexpected_edge")
        else:
            if relation["relation_kind"] != expected["expected_relation_kind"]:
                expected_base.add("wrong_relation_kind")
            if (
                relation["normalized_column_mapping"]
                != expected["expected_column_mapping"]
            ):
                expected_base.add("wrong_column_mapping")
            if not expected_base:
                raise PdfPrimaryTableContinuationEvaluationError(
                    "an exact reviewed relation cannot be a false positive"
                )
        expected_shared = {
            code
            for code, ids in (
                ("shared_predecessor", shared_predecessor_ids),
                ("shared_successor", shared_successor_ids),
            )
            if relation_id in ids
        }
        expected_failures = expected_base | expected_shared
        if set(item["failure_codes"]) != expected_failures:
            raise PdfPrimaryTableContinuationEvaluationError(
                "false-positive failure codes are inconsistent"
            )

    metrics_raw = _exact_keys(root["metrics"], _METRIC_KEYS, name="metrics")
    metrics = {
        key: _nonnegative(f"metrics.{key}", metrics_raw[key])
        for key in _METRIC_KEYS
    }
    expected_metrics = {
        "reviewed_continuation_count": metrics["reviewed_continuation_count"],
        "evaluated_continuation_count": len(results),
        "matched_continuation_count": sum(
            item["status"] == "matched" for item in results
        ),
        "failed_continuation_count": sum(
            item["status"] == "failed" for item in results
        ),
        "missing_continuation_count": sum(
            "missing_relation" in item["failure_codes"] for item in results
        ),
        "false_positive_relation_count": len(false_positives),
        "unscored_relation_count": len(unscored),
    }
    if metrics["reviewed_continuation_count"] > MAX_REVIEWED_CONTINUATIONS:
        raise PdfPrimaryTableContinuationEvaluationError(
            "reviewed_continuation_count exceeds its cap"
        )
    for key, expected in expected_metrics.items():
        if key != "reviewed_continuation_count" and metrics[key] != expected:
            raise PdfPrimaryTableContinuationEvaluationError(
                f"metrics.{key} disagrees with detailed results"
            )

    if verdict in {"passed", "failed"}:
        if (
            quality_status != "evaluable"
            or prerequisite_verdict != "passed"
            or blocking
            or not results
            or metrics["reviewed_continuation_count"] != len(results)
        ):
            raise PdfPrimaryTableContinuationEvaluationError(
                "quality verdict requires trusted Gold and a passed prerequisite"
            )
        expected_verdict = (
            "failed"
            if metrics["failed_continuation_count"]
            or metrics["false_positive_relation_count"]
            else "passed"
        )
        if verdict != expected_verdict:
            raise PdfPrimaryTableContinuationEvaluationError(
                "verdict disagrees with continuation results"
            )
    elif verdict == "not_evaluable_gold_pending":
        if quality_status != "not_evaluable_gold_pending" or blocking:
            raise PdfPrimaryTableContinuationEvaluationError(
                "pending-Gold verdict shape is invalid"
            )
    elif verdict == "not_evaluable_gold_untrusted":
        if quality_status != "not_evaluable_untrusted_confirmation" or blocking:
            raise PdfPrimaryTableContinuationEvaluationError(
                "untrusted-Gold verdict shape is invalid"
            )
    elif verdict == "blocked_page_local_prerequisite":
        if (
            quality_status != "evaluable"
            or prerequisite_verdict == "passed"
            or "page_local_prerequisite_not_passed" not in blocking
        ):
            raise PdfPrimaryTableContinuationEvaluationError(
                "page-local prerequisite verdict shape is invalid"
            )
    elif quality_status != "evaluable" or not blocking:
        raise PdfPrimaryTableContinuationEvaluationError(
            "scope-mismatch verdict shape is invalid"
        )
    if verdict not in {"passed", "failed"}:
        if results or false_positives or unscored:
            raise PdfPrimaryTableContinuationEvaluationError(
                "non-quality verdicts cannot expose comparison results"
            )
        for key in _METRIC_KEYS - {"reviewed_continuation_count"}:
            if metrics[key] != 0:
                raise PdfPrimaryTableContinuationEvaluationError(
                    "non-quality metrics must be zero"
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
        "page_local_candidate": page_local_candidate,
        "page_local_gold": page_local_gold,
        "page_local_prerequisite": prerequisite,
        "verdict": verdict,
        "blocking_reason_codes": blocking,
        "continuation_results": results,
        "false_positive_relations": false_positives,
        "unscored_relations": unscored,
        "metrics": metrics,
    }
    _canonical_bytes(result)
    return result


def canonical_primary_table_continuation_evaluation_json(
    value: Mapping[str, Any],
) -> bytes:
    return _canonical_bytes(validate_primary_table_continuation_evaluation(value))


def _mapping_payload(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return payload
    raise PdfPrimaryTableContinuationEvaluationError(
        "replayed artifact did not expose a mapping payload"
    )


def _canonical_mapping_sha256(value: Mapping[str, Any]) -> str:
    try:
        raw = json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PdfPrimaryTableContinuationEvaluationError(
            "replayed artifact cannot be canonically hashed"
        ) from error
    return sha256(raw).hexdigest()


def _table_occurrences(table: Mapping[str, Any]) -> list[str]:
    occurrences = sorted(
        {
            occurrence_id
            for row in table["rows"]
            for cell in row["cells"]
            for occurrence_id in cell["occurrence_ids"]
        },
        key=lambda value: tuple(
            int(part[1:])
            for part in (value.split(":")[2], value.split(":")[3])
        ),
    )
    if any(_OCCURRENCE.fullmatch(item) is None for item in occurrences):
        raise PdfPrimaryTableContinuationEvaluationError(
            "page-local candidate contains an invalid occurrence ID"
        )
    return occurrences


def _observed_relation(
    relation: Mapping[str, Any],
    *,
    predecessor_scope: str | None,
    successor_scope: str | None,
    scope_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    raw_mapping = [dict(item) for item in relation["column_mapping"]]
    normalized: list[dict[str, str]] = []
    if predecessor_scope is not None and successor_scope is not None:
        predecessor_columns = list(scope_by_id[predecessor_scope]["column_ids"])
        successor_columns = list(scope_by_id[successor_scope]["column_ids"])
        for item in raw_mapping:
            predecessor_index = item["predecessor_column_index"]
            successor_index = item["successor_column_index"]
            if (
                isinstance(predecessor_index, bool)
                or not isinstance(predecessor_index, int)
                or isinstance(successor_index, bool)
                or not isinstance(successor_index, int)
                or not 1 <= predecessor_index <= len(predecessor_columns)
                or not 1 <= successor_index <= len(successor_columns)
            ):
                raise PdfPrimaryTableContinuationEvaluationError(
                    "candidate column mapping is outside its reviewed endpoint grid"
                )
            normalized.append(
                {
                    "predecessor_column_id": predecessor_columns[
                        predecessor_index - 1
                    ],
                    "successor_column_id": successor_columns[successor_index - 1],
                }
            )
    return {
        "continuation_id": relation["continuation_id"],
        "predecessor_table_grid_id": relation["predecessor_table_grid_id"],
        "predecessor_page": relation["predecessor_page"],
        "successor_table_grid_id": relation["successor_table_grid_id"],
        "successor_page": relation["successor_page"],
        "relation_kind": relation["relation_kind"],
        "column_mapping": raw_mapping,
        "mapped_predecessor_scope_id": predecessor_scope,
        "mapped_successor_scope_id": successor_scope,
        "normalized_column_mapping": normalized,
    }


def _compare_replayed_table_continuations(
    candidate: Mapping[str, Any],
    gold_payload: Mapping[str, Any],
    *,
    gold_quality_status: str,
    candidate_canonical_sha256: str,
    gold_canonical_sha256: str,
    page_local_candidate: Mapping[str, Any],
    page_local_candidate_canonical_sha256: str,
    page_local_gold: Mapping[str, Any],
    page_local_gold_canonical_sha256: str,
    page_local_evaluation: Mapping[str, Any],
    page_local_evaluation_canonical_sha256: str,
) -> dict[str, Any]:
    """Compare strictly replayed artifacts without trusting candidate IDs."""

    if gold_quality_status not in _QUALITY_STATUSES:
        raise PdfPrimaryTableContinuationEvaluationError(
            "continuation Gold quality status is invalid"
        )
    candidate_input = candidate["input_artifacts"]
    gold_source = gold_payload["source"]
    native_capture_sha = gold_source["native_capture"]["canonical_sha256"]
    common = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": candidate["notice_id"],
        "source": {
            "source_pdf_sha256": candidate["source_pdf_sha256"],
            "native_capture_sha256": native_capture_sha,
        },
        "candidate": {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "canonical_sha256": candidate_canonical_sha256,
        },
        "gold": {
            "schema_version": GOLD_SCHEMA_VERSION,
            "canonical_sha256": gold_canonical_sha256,
            "quality_status": gold_quality_status,
        },
        "page_local_candidate": {
            "schema_version": TABLE_GRID_SCHEMA_VERSION,
            "canonical_sha256": page_local_candidate_canonical_sha256,
        },
        "page_local_gold": {
            "schema_version": TABLE_GRID_GOLD_SCHEMA_VERSION,
            "canonical_sha256": page_local_gold_canonical_sha256,
        },
        "page_local_prerequisite": {
            "schema_version": TABLE_GRID_EVALUATION_SCHEMA_VERSION,
            "canonical_sha256": page_local_evaluation_canonical_sha256,
            "verdict": page_local_evaluation["verdict"],
        },
    }
    reviewed_count = len(gold_payload["reviewed_continuations"])
    if gold_quality_status == "not_evaluable_gold_pending":
        return validate_primary_table_continuation_evaluation(
            {
                **common,
                "verdict": "not_evaluable_gold_pending",
                "blocking_reason_codes": [],
                "continuation_results": [],
                "false_positive_relations": [],
                "unscored_relations": [],
                "metrics": _empty_metrics(
                    reviewed_continuation_count=reviewed_count
                ),
            }
        )
    if gold_quality_status != "evaluable":
        return validate_primary_table_continuation_evaluation(
            {
                **common,
                "verdict": "not_evaluable_gold_untrusted",
                "blocking_reason_codes": [],
                "continuation_results": [],
                "false_positive_relations": [],
                "unscored_relations": [],
                "metrics": _empty_metrics(
                    reviewed_continuation_count=reviewed_count
                ),
            }
        )

    blocking: list[str] = []
    if candidate["notice_id"] != gold_payload["notice_id"]:
        blocking.append("notice_id_mismatch")
    if candidate["source_pdf_sha256"] != gold_source["source_pdf_sha256"]:
        blocking.append("source_pdf_mismatch")
    if candidate_input["native_capture_sha256"] != native_capture_sha:
        blocking.append("native_capture_mismatch")
    if (
        candidate_input["primary_table_grid_sha256"]
        != page_local_candidate_canonical_sha256
    ):
        blocking.append("candidate_table_grid_mismatch")
    if (
        gold_source["base_table_grid_gold"]["canonical_sha256"]
        != page_local_gold_canonical_sha256
    ):
        blocking.append("gold_table_grid_mismatch")
    if not set(gold_payload["page_scope"]).issubset(candidate["page_scope"]):
        blocking.append("gold_page_scope_not_covered")
    blocking = sorted(set(blocking), key=_BLOCKING_RANK.__getitem__)
    if blocking:
        return validate_primary_table_continuation_evaluation(
            {
                **common,
                "verdict": "blocked_scope_mismatch",
                "blocking_reason_codes": blocking,
                "continuation_results": [],
                "false_positive_relations": [],
                "unscored_relations": [],
                "metrics": _empty_metrics(
                    reviewed_continuation_count=reviewed_count
                ),
            }
        )
    if page_local_evaluation["verdict"] != "passed":
        return validate_primary_table_continuation_evaluation(
            {
                **common,
                "verdict": "blocked_page_local_prerequisite",
                "blocking_reason_codes": [
                    "page_local_prerequisite_not_passed"
                ],
                "continuation_results": [],
                "false_positive_relations": [],
                "unscored_relations": [],
                "metrics": _empty_metrics(
                    reviewed_continuation_count=reviewed_count
                ),
            }
        )

    scope_by_id = {
        scope["scope_id"]: scope for scope in page_local_gold["reviewed_scopes"]
    }
    scope_ids_by_occurrence: dict[str, set[str]] = {}
    membership_work = 0
    for scope_id, scope in scope_by_id.items():
        for cell in scope["cells"]:
            for occurrence_id in cell["occurrence_ids"]:
                membership_work += 1
                if membership_work > MAX_ENDPOINT_MEMBERSHIP_WORK:
                    raise PdfPrimaryTableContinuationEvaluationError(
                        "endpoint matching exceeds its work cap"
                    )
                scope_ids_by_occurrence.setdefault(occurrence_id, set()).add(
                    scope_id
                )
    table_by_id = {
        table["table_grid_id"]: table for table in page_local_candidate["tables"]
    }
    scope_by_table_id: dict[str, str | None] = {}
    for table_id, table in table_by_id.items():
        matched_scope_ids: set[str] = set()
        for occurrence_id in _table_occurrences(table):
            membership_work += 1
            if membership_work > MAX_ENDPOINT_MEMBERSHIP_WORK:
                raise PdfPrimaryTableContinuationEvaluationError(
                    "endpoint matching exceeds its work cap"
                )
            matched_scope_ids.update(scope_ids_by_occurrence.get(occurrence_id, ()))
        if len(matched_scope_ids) > 1:
            raise PdfPrimaryTableContinuationEvaluationError(
                "one page-local candidate table overlaps multiple reviewed scopes"
            )
        scope_by_table_id[table_id] = (
            next(iter(matched_scope_ids)) if matched_scope_ids else None
        )

    observed: list[dict[str, Any]] = []
    for relation in candidate["continuations"]:
        predecessor_id = relation["predecessor_table_grid_id"]
        successor_id = relation["successor_table_grid_id"]
        if predecessor_id not in table_by_id or successor_id not in table_by_id:
            raise PdfPrimaryTableContinuationEvaluationError(
                "continuation endpoint is absent from the replayed page-local grid"
            )
        if (
            table_by_id[predecessor_id]["page"] != relation["predecessor_page"]
            or table_by_id[successor_id]["page"] != relation["successor_page"]
        ):
            raise PdfPrimaryTableContinuationEvaluationError(
                "continuation endpoint pages disagree with the page-local grid"
            )
        observed.append(
            _observed_relation(
                relation,
                predecessor_scope=scope_by_table_id[predecessor_id],
                successor_scope=scope_by_table_id[successor_id],
                scope_by_id=scope_by_id,
            )
        )
    observed.sort(key=_observed_sort_key)
    predecessor_counts = Counter(
        item["predecessor_table_grid_id"] for item in observed
    )
    successor_counts = Counter(item["successor_table_grid_id"] for item in observed)

    expected_by_pair = {
        (item["predecessor_scope_id"], item["successor_scope_id"]): item
        for item in gold_payload["reviewed_continuations"]
    }
    exact_by_gold_id: dict[str, list[dict[str, Any]]] = {
        item["continuation_id"]: []
        for item in gold_payload["reviewed_continuations"]
    }
    false_positives: list[dict[str, Any]] = []
    unscored: list[dict[str, Any]] = []
    for relation in observed:
        pair = (
            relation["mapped_predecessor_scope_id"],
            relation["mapped_successor_scope_id"],
        )
        expected = expected_by_pair.get(pair)
        exact = (
            expected is not None
            and relation["relation_kind"] == expected["relation_kind"]
            and relation["normalized_column_mapping"]
            == expected["column_mapping"]
        )
        if exact:
            exact_by_gold_id[expected["continuation_id"]].append(relation)
            continue
        if pair == (None, None):
            unscored.append(relation)
            continue
        failures: list[str] = []
        if expected is None:
            failures.append("unexpected_edge")
        else:
            if relation["relation_kind"] != expected["relation_kind"]:
                failures.append("wrong_relation_kind")
            if relation["normalized_column_mapping"] != expected["column_mapping"]:
                failures.append("wrong_column_mapping")
        if predecessor_counts[relation["predecessor_table_grid_id"]] > 1:
            failures.append("shared_predecessor")
        if successor_counts[relation["successor_table_grid_id"]] > 1:
            failures.append("shared_successor")
        failures.sort(key=_FALSE_POSITIVE_RANK.__getitem__)
        false_positives.append({"relation": relation, "failure_codes": failures})

    continuation_results: list[dict[str, Any]] = []
    for expected in sorted(
        gold_payload["reviewed_continuations"],
        key=lambda item: item["continuation_id"],
    ):
        matching = sorted(
            exact_by_gold_id[expected["continuation_id"]],
            key=_observed_sort_key,
        )
        failures: list[str] = []
        if not matching:
            failures.append("missing_relation")
        elif len(matching) > 1:
            failures.append("multiple_matching_relations")
        if any(
            predecessor_counts[item["predecessor_table_grid_id"]] > 1
            for item in matching
        ):
            failures.append("shared_predecessor")
        if any(
            successor_counts[item["successor_table_grid_id"]] > 1
            for item in matching
        ):
            failures.append("shared_successor")
        failures.sort(key=_RESULT_FAILURE_RANK.__getitem__)
        predecessor_scope = scope_by_id[expected["predecessor_scope_id"]]
        successor_scope = scope_by_id[expected["successor_scope_id"]]
        continuation_results.append(
            {
                "gold_continuation_id": expected["continuation_id"],
                "predecessor_scope_id": expected["predecessor_scope_id"],
                "predecessor_page": predecessor_scope["physical_page"],
                "successor_scope_id": expected["successor_scope_id"],
                "successor_page": successor_scope["physical_page"],
                "expected_relation_kind": expected["relation_kind"],
                "expected_column_mapping": [
                    dict(item) for item in expected["column_mapping"]
                ],
                "status": "failed" if failures else "matched",
                "failure_codes": failures,
                "observed_relations": matching,
            }
        )
    false_positives.sort(key=lambda item: _observed_sort_key(item["relation"]))
    unscored.sort(key=_observed_sort_key)
    metrics = {
        "reviewed_continuation_count": reviewed_count,
        "evaluated_continuation_count": len(continuation_results),
        "matched_continuation_count": sum(
            item["status"] == "matched" for item in continuation_results
        ),
        "failed_continuation_count": sum(
            item["status"] == "failed" for item in continuation_results
        ),
        "missing_continuation_count": sum(
            "missing_relation" in item["failure_codes"]
            for item in continuation_results
        ),
        "false_positive_relation_count": len(false_positives),
        "unscored_relation_count": len(unscored),
    }
    return validate_primary_table_continuation_evaluation(
        {
            **common,
            "verdict": (
                "failed"
                if metrics["failed_continuation_count"] or false_positives
                else "passed"
            ),
            "blocking_reason_codes": [],
            "continuation_results": continuation_results,
            "false_positive_relations": false_positives,
            "unscored_relations": unscored,
            "metrics": metrics,
        }
    )


def evaluate_pdf_primary_table_continuation(
    candidate: Mapping[str, Any] | PrimaryTableContinuationFixture,
    gold: Mapping[str, Any] | PrimaryTableContinuationGoldFixture,
    *,
    primary_table_grid: Mapping[str, Any] | PrimaryTableGridFixture,
    base_table_grid_gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
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
    """Replay candidate, both Gold layers, and A4.3a before comparison."""

    grid_inputs = {
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
        "expected_surya_logical_compute_key": (
            expected_surya_logical_compute_key
        ),
        "expected_surya_pages": expected_surya_pages,
    }
    try:
        replayed_grid = validate_pdf_primary_table_grid_against_inputs(
            primary_table_grid, **grid_inputs
        )
        replayed_candidate = validate_pdf_primary_table_continuation_against_inputs(
            candidate,
            primary_table_grid=primary_table_grid,
            **grid_inputs,
        )
    except (PdfPrimaryTableGridError, PdfPrimaryTableContinuationError) as error:
        raise PdfPrimaryTableContinuationEvaluationError(
            "continuation candidate did not pass deterministic source replay"
        ) from error
    try:
        replayed_base_gold = validate_primary_table_grid_gold_against_inputs(
            base_table_grid_gold,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
        )
        replayed_gold = validate_primary_table_continuation_gold_against_inputs(
            gold,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
            base_table_grid_gold=base_table_grid_gold,
        )
    except (PrimaryTableGridGoldError, PrimaryTableContinuationGoldError) as error:
        raise PdfPrimaryTableContinuationEvaluationError(
            "continuation Gold did not pass deterministic source replay"
        ) from error
    if type(replayed_gold) is not ReplayedPrimaryTableContinuationGold:
        raise PdfPrimaryTableContinuationEvaluationError(
            "continuation Gold replay returned an unsupported receipt"
        )
    if type(replayed_base_gold) is not ReplayedPrimaryTableGridGold:
        raise PdfPrimaryTableContinuationEvaluationError(
            "page-local Gold replay returned an unsupported receipt"
        )
    try:
        prerequisite = evaluate_pdf_primary_table_grid(
            primary_table_grid,
            base_table_grid_gold,
            **grid_inputs,
        )
    except PdfPrimaryTableGridEvaluationError as error:
        raise PdfPrimaryTableContinuationEvaluationError(
            "page-local table-grid prerequisite could not be replayed"
        ) from error

    candidate_payload = _mapping_payload(replayed_candidate)
    grid_payload = _mapping_payload(replayed_grid)
    base_gold_payload = replayed_base_gold.to_dict()
    gold_payload = replayed_gold.to_dict()
    quality_status = (
        "evaluable"
        if replayed_gold.has_trusted_confirmation
        else replayed_gold.fixture.quality_gate_status
    )
    return _compare_replayed_table_continuations(
        candidate_payload,
        gold_payload,
        gold_quality_status=quality_status,
        candidate_canonical_sha256=sha256(
            canonical_pdf_primary_table_continuation_json(candidate_payload)
        ).hexdigest(),
        gold_canonical_sha256=replayed_gold.canonical_sha256,
        page_local_candidate=grid_payload,
        page_local_candidate_canonical_sha256=sha256(
            canonical_pdf_primary_table_grid_json(grid_payload)
        ).hexdigest(),
        page_local_gold=base_gold_payload,
        page_local_gold_canonical_sha256=replayed_base_gold.canonical_sha256,
        page_local_evaluation=prerequisite,
        page_local_evaluation_canonical_sha256=sha256(
            canonical_primary_table_grid_evaluation_json(prerequisite)
        ).hexdigest(),
    )


# Plural spelling is retained as a convenience for callers that treat the
# artifact as a collection; both names execute the same strict replay gate.
evaluate_pdf_primary_table_continuations = evaluate_pdf_primary_table_continuation


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "PdfPrimaryTableContinuationEvaluationError",
    "SCHEMA_VERSION",
    "canonical_primary_table_continuation_evaluation_json",
    "evaluate_pdf_primary_table_continuation",
    "evaluate_pdf_primary_table_continuations",
    "validate_primary_table_continuation_evaluation",
]
