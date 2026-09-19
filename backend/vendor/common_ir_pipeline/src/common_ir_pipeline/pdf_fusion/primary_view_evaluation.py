"""Offline evaluation for a replayed PDF primary document view.

This is the only production module in the primary-view slice that may read
both an automatic candidate and human-reviewed primary-structure Gold.  Its
public quality-gate API replays both artifacts from the same source, native,
render, parser, and calibration inputs on every call.  Replay wrapper objects
are convenience receipts inside a cooperative process, not security tokens.

The Gold contract is partial.  Occurrences attached to a candidate leaf but
outside one reviewed scope are reported as unscored diagnostics; their mere
presence cannot fail the scope.  Explicit ``forbidden_same_leaf`` assertions
remain failures, including when the forbidden occurrence is otherwise not a
positive Gold group member.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .primary_document_view import (
    PdfPrimaryDocumentViewError,
    PrimaryDocumentViewFixture,
    ReplayedPrimaryDocumentView,
    SCHEMA_VERSION as CANDIDATE_SCHEMA_VERSION,
    validate_pdf_primary_document_view_against_inputs,
)
from .primary_structure_gold import (
    PrimaryStructureGoldError,
    PrimaryStructureGoldFixture,
    ReplayedPrimaryStructureGold,
    SCHEMA_VERSION as GOLD_SCHEMA_VERSION,
    validate_primary_structure_gold_against_inputs,
)
from .render_manifest import PdfRenderManifest


SCHEMA_VERSION = "pdf_primary_view_evaluation/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 250_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_SCOPES = 256
MAX_GROUPS_PER_SCOPE = 1_024
MAX_OCCURRENCES_PER_GROUP = 4_096
MAX_HARD_NEGATIVES_PER_SCOPE = 4_096
MAX_DIAGNOSTIC_OCCURRENCES_PER_GROUP = 16_384

_SHA = frozenset("0123456789abcdef")
_NOTICE_ID = re.compile(r"^PBLN_[0-9]{15}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_OCCURRENCE = re.compile(
    r"^occ:inspector:p(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6]):"
    r"t(?:0|[1-9][0-9]{0,4}|[1-4][0-9]{5})$"
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
_BLOCKING_REASONS = frozenset(
    {
        "notice_id_mismatch",
        "source_pdf_mismatch",
        "native_capture_mismatch",
        "gold_page_scope_not_covered",
        "gold_occurrence_not_covered",
    }
)
_GROUP_FAILURE_CODES = frozenset(
    {
        "missing_occurrence",
        "under_merged",
        "reviewed_scope_over_merge",
        "kind_mismatch",
        "occurrence_order_mismatch",
        "boundary_mismatch",
    }
)
_SCOPE_FAILURE_CODES = frozenset(
    {"ordered_group_failed", "hard_negative_violated"}
)
_BOUNDARY_STATUSES = frozenset(
    {"matched", "mismatched", "missing", "unscored_scope_outside"}
)
_JOIN_CLASSES = frozenset({"intra_word_wrap", "inter_token_space"})

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
        "metrics",
    }
)
_SOURCE_KEYS = frozenset({"source_pdf_sha256", "native_capture_sha256"})
_ARTIFACT_REF_KEYS = frozenset({"schema_version", "canonical_sha256"})
_GOLD_REF_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "quality_status"}
)
_SCOPE_RESULT_KEYS = frozenset(
    {
        "scope_id",
        "physical_page",
        "verdict",
        "failure_codes",
        "group_results",
        "hard_negative_results",
    }
)
_GROUP_RESULT_KEYS = frozenset(
    {
        "group_id",
        "expected_kind",
        "status",
        "failure_codes",
        "candidate_leaf_ids",
        "expected_occurrence_ids",
        "observed_reviewed_occurrence_ids",
        "boundary_results",
        "unscored_attached_occurrence_ids",
    }
)
_BOUNDARY_RESULT_KEYS = frozenset(
    {
        "left_occurrence_id",
        "right_occurrence_id",
        "expected_join_class",
        "observed_join_class",
        "status",
    }
)
_HARD_NEGATIVE_RESULT_KEYS = frozenset(
    {
        "kind",
        "occurrence_id",
        "target_group_id",
        "status",
        "violating_leaf_id",
    }
)
_METRIC_KEYS = frozenset(
    {
        "reviewed_scope_count",
        "evaluated_scope_count",
        "passed_scope_count",
        "failed_scope_count",
        "ordered_group_count",
        "matched_group_count",
        "failed_group_count",
        "hard_negative_count",
        "hard_negative_violation_count",
        "unscored_attachment_count",
    }
)


class PdfPrimaryViewEvaluationError(ValueError):
    """Raised when an evaluation input or result is unsafe or inconsistent."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfPrimaryViewEvaluationError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PdfPrimaryViewEvaluationError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        if extra:
            detail.append("unexpected keys: " + ", ".join(extra))
        raise PdfPrimaryViewEvaluationError(
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
        raise PdfPrimaryViewEvaluationError(f"{name} must be a string")
    if (not allow_empty and not value) or value != value.strip():
        raise PdfPrimaryViewEvaluationError(f"{name} must be a trimmed string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PdfPrimaryViewEvaluationError(
            f"{name} must not contain surrogate code points"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PdfPrimaryViewEvaluationError(f"{name} exceeds its string safety cap")
    return value


def _identifier(name: str, value: object) -> str:
    result = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(result) is None:
        raise PdfPrimaryViewEvaluationError(f"{name} is not a bounded identifier")
    return result


def _sha256(name: str, value: object) -> str:
    result = _string(name, value, maximum=64)
    if len(result) != 64 or any(character not in _SHA for character in result):
        raise PdfPrimaryViewEvaluationError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return result


def _positive_int(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PdfPrimaryViewEvaluationError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PdfPrimaryViewEvaluationError(f"{name} must be a non-negative integer")
    return value


def _occurrence(name: str, value: object) -> str:
    result = _string(name, value, maximum=64)
    if _OCCURRENCE.fullmatch(result) is None:
        raise PdfPrimaryViewEvaluationError(f"{name} is not a bounded occurrence ID")
    return result


def _unique_strings(
    name: str,
    value: object,
    *,
    maximum: int,
    allowed: frozenset[str] | None = None,
    occurrence_ids: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PdfPrimaryViewEvaluationError(f"{name} must be a bounded array")
    result: list[str] = []
    for index, item in enumerate(value):
        checked = (
            _occurrence(f"{name}[{index}]", item)
            if occurrence_ids
            else _string(f"{name}[{index}]", item, maximum=256)
        )
        if allowed is not None and checked not in allowed:
            raise PdfPrimaryViewEvaluationError(f"{name} contains an unsupported value")
        result.append(checked)
    if len(result) != len(set(result)):
        raise PdfPrimaryViewEvaluationError(f"{name} must not contain duplicates")
    return result


def _assert_json_limits(
    value: object, *, depth: int = 1, counter: list[int] | None = None
) -> None:
    counter = [0] if counter is None else counter
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise PdfPrimaryViewEvaluationError("JSON exceeds the node safety cap")
    if depth > MAX_JSON_DEPTH:
        raise PdfPrimaryViewEvaluationError("JSON exceeds the nesting depth cap")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PdfPrimaryViewEvaluationError("JSON contains a non-finite number")
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
    raise PdfPrimaryViewEvaluationError(
        f"JSON contains unsupported type {type(value).__name__}"
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
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
        raise PdfPrimaryViewEvaluationError(
            "evaluation is not canonically serializable UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryViewEvaluationError("evaluation exceeds the artifact byte cap")
    return encoded


def _validate_boundary_result(
    value: object, *, name: str
) -> dict[str, Any]:
    item = _exact_keys(value, _BOUNDARY_RESULT_KEYS, name=name)
    left = _occurrence(f"{name}.left_occurrence_id", item["left_occurrence_id"])
    right = _occurrence(f"{name}.right_occurrence_id", item["right_occurrence_id"])
    expected = _string(f"{name}.expected_join_class", item["expected_join_class"])
    if expected not in _JOIN_CLASSES:
        raise PdfPrimaryViewEvaluationError(f"{name} expected join class is invalid")
    observed = item["observed_join_class"]
    if observed is not None:
        observed = _string(f"{name}.observed_join_class", observed)
        if observed not in _JOIN_CLASSES:
            raise PdfPrimaryViewEvaluationError(f"{name} observed join class is invalid")
    status = _string(f"{name}.status", item["status"])
    if status not in _BOUNDARY_STATUSES:
        raise PdfPrimaryViewEvaluationError(f"{name} status is invalid")
    if status == "matched" and observed != expected:
        raise PdfPrimaryViewEvaluationError(f"{name} matched status disagrees with join classes")
    if status == "mismatched" and (observed is None or observed == expected):
        raise PdfPrimaryViewEvaluationError(f"{name} mismatched status disagrees with join classes")
    if status in {"missing", "unscored_scope_outside"} and observed is not None:
        raise PdfPrimaryViewEvaluationError(f"{name} non-observed status must use null observed join class")
    return {
        "left_occurrence_id": left,
        "right_occurrence_id": right,
        "expected_join_class": expected,
        "observed_join_class": observed,
        "status": status,
    }


def _validate_group_result(value: object, *, name: str) -> dict[str, Any]:
    item = _exact_keys(value, _GROUP_RESULT_KEYS, name=name)
    group_id = _identifier(f"{name}.group_id", item["group_id"])
    if item["expected_kind"] != "paragraph":
        raise PdfPrimaryViewEvaluationError(f"{name}.expected_kind must be paragraph")
    status = _string(f"{name}.status", item["status"])
    if status not in {"passed", "failed"}:
        raise PdfPrimaryViewEvaluationError(f"{name}.status is invalid")
    failures = _unique_strings(
        f"{name}.failure_codes",
        item["failure_codes"],
        maximum=len(_GROUP_FAILURE_CODES),
        allowed=_GROUP_FAILURE_CODES,
    )
    leaf_ids = _unique_strings(
        f"{name}.candidate_leaf_ids",
        item["candidate_leaf_ids"],
        maximum=MAX_OCCURRENCES_PER_GROUP,
    )
    expected = _unique_strings(
        f"{name}.expected_occurrence_ids",
        item["expected_occurrence_ids"],
        maximum=MAX_OCCURRENCES_PER_GROUP,
        occurrence_ids=True,
    )
    observed = _unique_strings(
        f"{name}.observed_reviewed_occurrence_ids",
        item["observed_reviewed_occurrence_ids"],
        maximum=MAX_OCCURRENCES_PER_GROUP,
        occurrence_ids=True,
    )
    unscored = _unique_strings(
        f"{name}.unscored_attached_occurrence_ids",
        item["unscored_attached_occurrence_ids"],
        maximum=MAX_DIAGNOSTIC_OCCURRENCES_PER_GROUP,
        occurrence_ids=True,
    )
    boundaries_raw = item["boundary_results"]
    if not isinstance(boundaries_raw, list) or len(boundaries_raw) > max(0, len(expected) - 1):
        raise PdfPrimaryViewEvaluationError(f"{name}.boundary_results is invalid")
    boundaries = [
        _validate_boundary_result(boundary, name=f"{name}.boundary_results[{index}]")
        for index, boundary in enumerate(boundaries_raw)
    ]
    if len(expected) < 1 or len(boundaries) != len(expected) - 1:
        raise PdfPrimaryViewEvaluationError(f"{name} expected group shape is invalid")
    for index, boundary in enumerate(boundaries):
        if (
            boundary["left_occurrence_id"] != expected[index]
            or boundary["right_occurrence_id"] != expected[index + 1]
        ):
            raise PdfPrimaryViewEvaluationError(f"{name} boundary order is invalid")
    expected_failed = bool(failures)
    if (status == "failed") != expected_failed:
        raise PdfPrimaryViewEvaluationError(f"{name} status disagrees with failure codes")
    if any(boundary["status"] in {"missing", "mismatched"} for boundary in boundaries):
        if "boundary_mismatch" not in failures:
            raise PdfPrimaryViewEvaluationError(f"{name} boundary failure is not reflected")
    elif "boundary_mismatch" in failures:
        raise PdfPrimaryViewEvaluationError(f"{name} has a spurious boundary failure")
    return {
        "group_id": group_id,
        "expected_kind": "paragraph",
        "status": status,
        "failure_codes": failures,
        "candidate_leaf_ids": leaf_ids,
        "expected_occurrence_ids": expected,
        "observed_reviewed_occurrence_ids": observed,
        "boundary_results": boundaries,
        "unscored_attached_occurrence_ids": unscored,
    }


def _validate_hard_negative_result(value: object, *, name: str) -> dict[str, Any]:
    item = _exact_keys(value, _HARD_NEGATIVE_RESULT_KEYS, name=name)
    if item["kind"] != "forbidden_same_leaf":
        raise PdfPrimaryViewEvaluationError(f"{name}.kind is invalid")
    occurrence_id = _occurrence(f"{name}.occurrence_id", item["occurrence_id"])
    target_group_id = _identifier(f"{name}.target_group_id", item["target_group_id"])
    status = _string(f"{name}.status", item["status"])
    if status not in {"satisfied", "violated"}:
        raise PdfPrimaryViewEvaluationError(f"{name}.status is invalid")
    violating_leaf_id = item["violating_leaf_id"]
    if status == "violated":
        violating_leaf_id = _string(
            f"{name}.violating_leaf_id", violating_leaf_id, maximum=256
        )
    elif violating_leaf_id is not None:
        raise PdfPrimaryViewEvaluationError(
            f"{name}.violating_leaf_id must be null when satisfied"
        )
    return {
        "kind": "forbidden_same_leaf",
        "occurrence_id": occurrence_id,
        "target_group_id": target_group_id,
        "status": status,
        "violating_leaf_id": violating_leaf_id,
    }


def validate_primary_view_evaluation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate internal evaluation consistency and return a detached copy."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="evaluation")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfPrimaryViewEvaluationError("evaluation schema_version is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfPrimaryViewEvaluationError(
            "primary-view evaluation must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PdfPrimaryViewEvaluationError("standalone validation scope is invalid")
    notice_id = _string("notice_id", root["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PdfPrimaryViewEvaluationError("notice_id is invalid")

    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    source_copy = {
        "source_pdf_sha256": _sha256("source.source_pdf_sha256", source["source_pdf_sha256"]),
        "native_capture_sha256": _sha256(
            "source.native_capture_sha256", source["native_capture_sha256"]
        ),
    }
    candidate = _exact_keys(root["candidate"], _ARTIFACT_REF_KEYS, name="candidate")
    if candidate["schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise PdfPrimaryViewEvaluationError("candidate schema version is invalid")
    candidate_copy = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "canonical_sha256": _sha256("candidate.canonical_sha256", candidate["canonical_sha256"]),
    }
    gold = _exact_keys(root["gold"], _GOLD_REF_KEYS, name="gold")
    if gold["schema_version"] != GOLD_SCHEMA_VERSION:
        raise PdfPrimaryViewEvaluationError("Gold schema version is invalid")
    gold_quality = _string("gold.quality_status", gold["quality_status"])
    if gold_quality not in {
        "evaluable",
        "not_evaluable_gold_pending",
        "not_evaluable_untrusted_confirmation",
    }:
        raise PdfPrimaryViewEvaluationError("Gold quality status is invalid")
    gold_copy = {
        "schema_version": GOLD_SCHEMA_VERSION,
        "canonical_sha256": _sha256("gold.canonical_sha256", gold["canonical_sha256"]),
        "quality_status": gold_quality,
    }

    verdict = _string("verdict", root["verdict"])
    if verdict not in _VERDICTS:
        raise PdfPrimaryViewEvaluationError("verdict is invalid")
    blocking_reasons = _unique_strings(
        "blocking_reason_codes",
        root["blocking_reason_codes"],
        maximum=len(_BLOCKING_REASONS),
        allowed=_BLOCKING_REASONS,
    )
    scopes_raw = root["scope_results"]
    if not isinstance(scopes_raw, list) or len(scopes_raw) > MAX_SCOPES:
        raise PdfPrimaryViewEvaluationError("scope_results must be a bounded array")
    scopes: list[dict[str, Any]] = []
    seen_scopes: set[str] = set()
    for scope_index, raw_scope in enumerate(scopes_raw):
        name = f"scope_results[{scope_index}]"
        scope = _exact_keys(raw_scope, _SCOPE_RESULT_KEYS, name=name)
        scope_id = _identifier(f"{name}.scope_id", scope["scope_id"])
        if scope_id in seen_scopes:
            raise PdfPrimaryViewEvaluationError("scope result IDs must be unique")
        seen_scopes.add(scope_id)
        physical_page = _positive_int(
            f"{name}.physical_page", scope["physical_page"], maximum=MAX_PAGES
        )
        scope_verdict = _string(f"{name}.verdict", scope["verdict"])
        if scope_verdict not in {"passed", "failed"}:
            raise PdfPrimaryViewEvaluationError(f"{name}.verdict is invalid")
        failures = _unique_strings(
            f"{name}.failure_codes",
            scope["failure_codes"],
            maximum=len(_SCOPE_FAILURE_CODES),
            allowed=_SCOPE_FAILURE_CODES,
        )
        groups_raw = scope["group_results"]
        if not isinstance(groups_raw, list) or not groups_raw or len(groups_raw) > MAX_GROUPS_PER_SCOPE:
            raise PdfPrimaryViewEvaluationError(f"{name}.group_results is invalid")
        groups = [
            _validate_group_result(group, name=f"{name}.group_results[{index}]")
            for index, group in enumerate(groups_raw)
        ]
        if len({group["group_id"] for group in groups}) != len(groups):
            raise PdfPrimaryViewEvaluationError(f"{name} group IDs must be unique")
        hard_raw = scope["hard_negative_results"]
        if not isinstance(hard_raw, list) or len(hard_raw) > MAX_HARD_NEGATIVES_PER_SCOPE:
            raise PdfPrimaryViewEvaluationError(f"{name}.hard_negative_results is invalid")
        hard = [
            _validate_hard_negative_result(item, name=f"{name}.hard_negative_results[{index}]")
            for index, item in enumerate(hard_raw)
        ]
        group_ids = {group["group_id"] for group in groups}
        if any(item["target_group_id"] not in group_ids for item in hard):
            raise PdfPrimaryViewEvaluationError(
                f"{name} hard negative references an absent target group"
            )
        hard_identities = [
            (item["occurrence_id"], item["target_group_id"])
            for item in hard
        ]
        if len(hard_identities) != len(set(hard_identities)):
            raise PdfPrimaryViewEvaluationError(
                f"{name} hard-negative assertions must be unique"
            )
        expected_scope_failures: list[str] = []
        if any(group["status"] == "failed" for group in groups):
            expected_scope_failures.append("ordered_group_failed")
        if any(item["status"] == "violated" for item in hard):
            expected_scope_failures.append("hard_negative_violated")
        if failures != expected_scope_failures:
            raise PdfPrimaryViewEvaluationError(
                f"{name} failure codes disagree with detailed results"
            )
        if (scope_verdict == "failed") != bool(failures):
            raise PdfPrimaryViewEvaluationError(
                f"{name} verdict disagrees with failure codes"
            )
        scopes.append(
            {
                "scope_id": scope_id,
                "physical_page": physical_page,
                "verdict": scope_verdict,
                "failure_codes": failures,
                "group_results": groups,
                "hard_negative_results": hard,
            }
        )

    metrics = _exact_keys(root["metrics"], _METRIC_KEYS, name="metrics")
    metric_copy = {name: _nonnegative_int(f"metrics.{name}", metrics[name]) for name in _METRIC_KEYS}
    if metric_copy["reviewed_scope_count"] > MAX_SCOPES:
        raise PdfPrimaryViewEvaluationError(
            "metrics.reviewed_scope_count exceeds the reviewed-scope cap"
        )
    expected_metrics = {
        "reviewed_scope_count": metric_copy["reviewed_scope_count"],
        "evaluated_scope_count": len(scopes),
        "passed_scope_count": sum(scope["verdict"] == "passed" for scope in scopes),
        "failed_scope_count": sum(scope["verdict"] == "failed" for scope in scopes),
        "ordered_group_count": sum(len(scope["group_results"]) for scope in scopes),
        "matched_group_count": sum(
            group["status"] == "passed"
            for scope in scopes
            for group in scope["group_results"]
        ),
        "failed_group_count": sum(
            group["status"] == "failed"
            for scope in scopes
            for group in scope["group_results"]
        ),
        "hard_negative_count": sum(len(scope["hard_negative_results"]) for scope in scopes),
        "hard_negative_violation_count": sum(
            item["status"] == "violated"
            for scope in scopes
            for item in scope["hard_negative_results"]
        ),
        "unscored_attachment_count": sum(
            len(group["unscored_attached_occurrence_ids"])
            for scope in scopes
            for group in scope["group_results"]
        ),
    }
    for name, expected in expected_metrics.items():
        if name == "reviewed_scope_count":
            continue
        if metric_copy[name] != expected:
            raise PdfPrimaryViewEvaluationError(f"metrics.{name} disagrees with scope results")

    if verdict in {"passed", "failed"}:
        if blocking_reasons or not scopes:
            raise PdfPrimaryViewEvaluationError("quality verdict requires evaluated, unblocked scopes")
        if metric_copy["reviewed_scope_count"] != len(scopes):
            raise PdfPrimaryViewEvaluationError(
                "quality verdict must evaluate every reviewed scope"
            )
        expected_verdict = "failed" if expected_metrics["failed_scope_count"] else "passed"
        if verdict != expected_verdict or gold_quality != "evaluable":
            raise PdfPrimaryViewEvaluationError("quality verdict is inconsistent")
    elif verdict == "blocked_scope_mismatch":
        if not blocking_reasons or scopes:
            raise PdfPrimaryViewEvaluationError("blocked verdict shape is invalid")
    else:
        if blocking_reasons or scopes:
            raise PdfPrimaryViewEvaluationError("not-evaluable verdict must not contain comparisons")
        expected = (
            "not_evaluable_gold_pending"
            if gold_quality == "not_evaluable_gold_pending"
            else "not_evaluable_gold_untrusted"
        )
        if verdict != expected:
            raise PdfPrimaryViewEvaluationError("not-evaluable verdict disagrees with Gold status")
    if metric_copy["evaluated_scope_count"] == 0:
        for name in _METRIC_KEYS - {"reviewed_scope_count"}:
            if metric_copy[name] != 0:
                raise PdfPrimaryViewEvaluationError("non-evaluated result metrics must be zero")
    if metric_copy["reviewed_scope_count"] < metric_copy["evaluated_scope_count"]:
        raise PdfPrimaryViewEvaluationError("reviewed scope metric is smaller than evaluated scope count")

    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": notice_id,
        "source": source_copy,
        "candidate": candidate_copy,
        "gold": gold_copy,
        "verdict": verdict,
        "blocking_reason_codes": blocking_reasons,
        "scope_results": scopes,
        "metrics": metric_copy,
    }
    _canonical_bytes(result)
    return result


def canonical_primary_view_evaluation_json(value: Mapping[str, Any]) -> bytes:
    """Return strict canonical UTF-8 JSON for an evaluation result."""

    return _canonical_bytes(validate_primary_view_evaluation(value))


def _deduplicated(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _empty_metrics(*, reviewed_scope_count: int) -> dict[str, int]:
    return {
        "reviewed_scope_count": reviewed_scope_count,
        "evaluated_scope_count": 0,
        "passed_scope_count": 0,
        "failed_scope_count": 0,
        "ordered_group_count": 0,
        "matched_group_count": 0,
        "failed_group_count": 0,
        "hard_negative_count": 0,
        "hard_negative_violation_count": 0,
        "unscored_attachment_count": 0,
    }


def _compare_replayed_primary_document_view(
    candidate: ReplayedPrimaryDocumentView,
    gold: ReplayedPrimaryStructureGold,
) -> dict[str, Any]:
    """Compare inputs replayed by the public evaluator in this call stack.

    A pending or untrusted replayed Gold artifact produces a non-evaluable
    result without reading its assertions.  Only trusted, evaluable Gold can
    produce a quality verdict.
    """

    if type(candidate) is not ReplayedPrimaryDocumentView:
        raise PdfPrimaryViewEvaluationError(
            "candidate must be produced by strict primary-view artifact replay"
        )
    if type(gold) is not ReplayedPrimaryStructureGold:
        raise PdfPrimaryViewEvaluationError(
            "Gold must be produced by strict primary-structure artifact replay"
        )
    candidate_payload = candidate.payload
    gold_payload = gold.payload
    if not isinstance(candidate_payload, Mapping) or not isinstance(gold_payload, Mapping):
        raise PdfPrimaryViewEvaluationError("replayed input payloads must be objects")
    candidate_canonical = candidate.canonical_json()
    gold_canonical = gold.canonical_json()
    if type(candidate_canonical) is not bytes or type(gold_canonical) is not bytes:
        raise PdfPrimaryViewEvaluationError(
            "replayed inputs must expose canonical byte strings"
        )
    candidate_digest = sha256(candidate_canonical).hexdigest()
    gold_digest = sha256(gold_canonical).hexdigest()
    if candidate_digest != candidate.canonical_sha256:
        raise PdfPrimaryViewEvaluationError(
            "candidate replay digest disagrees with its canonical bytes"
        )
    if gold_digest != gold.canonical_sha256:
        raise PdfPrimaryViewEvaluationError(
            "Gold replay digest disagrees with its canonical bytes"
        )

    candidate_source = candidate_payload["source"]
    gold_source = gold_payload["source"]
    source = {
        "source_pdf_sha256": candidate_source["source_pdf_sha256"],
        "native_capture_sha256": candidate_source["native_capture"]["canonical_sha256"],
    }
    common = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": candidate_payload["notice_id"],
        "source": source,
        "candidate": {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "canonical_sha256": candidate_digest,
        },
        "gold": {
            "schema_version": GOLD_SCHEMA_VERSION,
            "canonical_sha256": gold_digest,
            "quality_status": (
                "evaluable"
                if gold.has_trusted_confirmation
                else gold.fixture.quality_gate_status
            ),
        },
    }
    reviewed_scope_count = len(gold_payload["reviewed_scopes"])
    if not gold.has_trusted_confirmation:
        quality_status = gold.fixture.quality_gate_status
        verdict = (
            "not_evaluable_gold_pending"
            if quality_status == "not_evaluable_gold_pending"
            else "not_evaluable_gold_untrusted"
        )
        return validate_primary_view_evaluation(
            {
                **common,
                "verdict": verdict,
                "blocking_reason_codes": [],
                "scope_results": [],
                "metrics": _empty_metrics(reviewed_scope_count=reviewed_scope_count),
            }
        )

    blocking: list[str] = []
    if candidate_payload["notice_id"] != gold_payload["notice_id"]:
        blocking.append("notice_id_mismatch")
    if candidate_source["source_pdf_sha256"] != gold_source["source_pdf_sha256"]:
        blocking.append("source_pdf_mismatch")
    if (
        candidate_source["native_capture"]["canonical_sha256"]
        != gold_source["native_capture"]["canonical_sha256"]
    ):
        blocking.append("native_capture_mismatch")
    candidate_pages = set(candidate_payload["page_scope"])
    if not set(gold_payload["page_scope"]).issubset(candidate_pages):
        blocking.append("gold_page_scope_not_covered")

    leaves = candidate_payload["leaves"]
    leaf_by_occurrence: dict[str, Mapping[str, Any]] = {}
    for leaf in leaves:
        for occurrence_id in leaf["occurrence_ids"]:
            leaf_by_occurrence[occurrence_id] = leaf
    gold_occurrences = {
        occurrence_id
        for scope in gold_payload["reviewed_scopes"]
        for occurrence_id in scope["occurrence_ids"]
    }
    if not gold_occurrences.issubset(leaf_by_occurrence):
        blocking.append("gold_occurrence_not_covered")
    blocking = _deduplicated(blocking)
    if blocking:
        return validate_primary_view_evaluation(
            {
                **common,
                "verdict": "blocked_scope_mismatch",
                "blocking_reason_codes": blocking,
                "scope_results": [],
                "metrics": _empty_metrics(reviewed_scope_count=reviewed_scope_count),
            }
        )

    scope_results: list[dict[str, Any]] = []
    for scope in gold_payload["reviewed_scopes"]:
        reviewed = set(scope["occurrence_ids"])
        group_results: list[dict[str, Any]] = []
        group_by_id = {group["group_id"]: group for group in scope["ordered_groups"]}
        target_leaf_ids_by_group = {
            group_id: {
                leaf_by_occurrence[occurrence_id]["leaf_id"]
                for occurrence_id in group["occurrence_ids"]
            }
            for group_id, group in group_by_id.items()
        }
        for group in scope["ordered_groups"]:
            expected_ids = list(group["occurrence_ids"])
            expected_id_set = set(expected_ids)
            member_leaves = [leaf_by_occurrence[occurrence_id] for occurrence_id in expected_ids]
            candidate_leaf_ids = _deduplicated(
                [leaf["leaf_id"] for leaf in member_leaves]
            )
            same_leaf = len(candidate_leaf_ids) == 1
            observed_reviewed: list[str] = []
            unscored: list[str] = []
            candidate_boundaries: dict[tuple[str, str], str] = {}
            if same_leaf:
                leaf = member_leaves[0]
                observed_reviewed = [
                    occurrence_id
                    for occurrence_id in leaf["occurrence_ids"]
                    if occurrence_id in reviewed
                ]
                unscored = [
                    occurrence_id
                    for occurrence_id in leaf["occurrence_ids"]
                    if occurrence_id not in reviewed
                ]
                candidate_boundaries = {
                    (boundary["left_occurrence_id"], boundary["right_occurrence_id"]): boundary["join_class"]
                    for boundary in leaf["boundaries"]
                }
            else:
                for leaf in member_leaves:
                    observed_reviewed.extend(
                        occurrence_id
                        for occurrence_id in leaf["occurrence_ids"]
                        if occurrence_id in reviewed
                    )
                    unscored.extend(
                        occurrence_id
                        for occurrence_id in leaf["occurrence_ids"]
                        if occurrence_id not in reviewed
                    )
                observed_reviewed = _deduplicated(observed_reviewed)
                unscored = _deduplicated(unscored)

            failures: list[str] = []
            if not same_leaf:
                failures.append("under_merged")
            elif member_leaves[0]["kind"] != group["kind"]:
                failures.append("kind_mismatch")
            if same_leaf and set(observed_reviewed) != expected_id_set:
                failures.append("reviewed_scope_over_merge")
            projected_order = [
                occurrence_id
                for occurrence_id in observed_reviewed
                if occurrence_id in expected_id_set
            ]
            if same_leaf and projected_order != expected_ids:
                failures.append("occurrence_order_mismatch")

            boundary_results: list[dict[str, Any]] = []
            for boundary in group["boundaries"]:
                left = boundary["left_occurrence_id"]
                right = boundary["right_occurrence_id"]
                expected_join = boundary["join_class"]
                observed_join: str | None = None
                if not same_leaf:
                    boundary_status = "missing"
                else:
                    leaf_occurrences = member_leaves[0]["occurrence_ids"]
                    left_index = leaf_occurrences.index(left)
                    right_index = leaf_occurrences.index(right)
                    if right_index != left_index + 1:
                        # Partial Gold cannot adjudicate intervening occurrences
                        # that were never included in the reviewed scope.
                        intervening = leaf_occurrences[left_index + 1 : right_index]
                        if intervening and all(item not in reviewed for item in intervening):
                            boundary_status = "unscored_scope_outside"
                        else:
                            boundary_status = "missing"
                    else:
                        observed_join = candidate_boundaries.get((left, right))
                        if observed_join is None:
                            boundary_status = "missing"
                        elif observed_join == expected_join:
                            boundary_status = "matched"
                        else:
                            boundary_status = "mismatched"
                if boundary_status in {"missing", "mismatched"} and "boundary_mismatch" not in failures:
                    failures.append("boundary_mismatch")
                boundary_results.append(
                    {
                        "left_occurrence_id": left,
                        "right_occurrence_id": right,
                        "expected_join_class": expected_join,
                        "observed_join_class": observed_join,
                        "status": boundary_status,
                    }
                )
            group_results.append(
                {
                    "group_id": group["group_id"],
                    "expected_kind": group["kind"],
                    "status": "failed" if failures else "passed",
                    "failure_codes": failures,
                    "candidate_leaf_ids": candidate_leaf_ids,
                    "expected_occurrence_ids": expected_ids,
                    "observed_reviewed_occurrence_ids": observed_reviewed,
                    "boundary_results": boundary_results,
                    "unscored_attached_occurrence_ids": unscored,
                }
            )

        hard_results: list[dict[str, Any]] = []
        for hard_negative in scope["hard_negatives"]:
            forbidden_leaf = leaf_by_occurrence[hard_negative["occurrence_id"]]
            target_leaf_ids = target_leaf_ids_by_group[
                hard_negative["target_group_id"]
            ]
            violated = forbidden_leaf["leaf_id"] in target_leaf_ids
            hard_results.append(
                {
                    "kind": hard_negative["kind"],
                    "occurrence_id": hard_negative["occurrence_id"],
                    "target_group_id": hard_negative["target_group_id"],
                    "status": "violated" if violated else "satisfied",
                    "violating_leaf_id": forbidden_leaf["leaf_id"] if violated else None,
                }
            )
        scope_failures: list[str] = []
        if any(group["status"] == "failed" for group in group_results):
            scope_failures.append("ordered_group_failed")
        if any(item["status"] == "violated" for item in hard_results):
            scope_failures.append("hard_negative_violated")
        scope_results.append(
            {
                "scope_id": scope["scope_id"],
                "physical_page": scope["physical_page"],
                "verdict": "failed" if scope_failures else "passed",
                "failure_codes": scope_failures,
                "group_results": group_results,
                "hard_negative_results": hard_results,
            }
        )

    metrics = {
        "reviewed_scope_count": reviewed_scope_count,
        "evaluated_scope_count": len(scope_results),
        "passed_scope_count": sum(scope["verdict"] == "passed" for scope in scope_results),
        "failed_scope_count": sum(scope["verdict"] == "failed" for scope in scope_results),
        "ordered_group_count": sum(len(scope["group_results"]) for scope in scope_results),
        "matched_group_count": sum(
            group["status"] == "passed"
            for scope in scope_results
            for group in scope["group_results"]
        ),
        "failed_group_count": sum(
            group["status"] == "failed"
            for scope in scope_results
            for group in scope["group_results"]
        ),
        "hard_negative_count": sum(len(scope["hard_negative_results"]) for scope in scope_results),
        "hard_negative_violation_count": sum(
            item["status"] == "violated"
            for scope in scope_results
            for item in scope["hard_negative_results"]
        ),
        "unscored_attachment_count": sum(
            len(group["unscored_attached_occurrence_ids"])
            for scope in scope_results
            for group in scope["group_results"]
        ),
    }
    result = {
        **common,
        "verdict": "failed" if metrics["failed_scope_count"] else "passed",
        "blocking_reason_codes": [],
        "scope_results": scope_results,
        "metrics": metrics,
    }
    return validate_primary_view_evaluation(result)


def evaluate_pdf_primary_document_view(
    candidate: Mapping[str, Any] | PrimaryDocumentViewFixture,
    gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    structure_candidates: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    render_artifact_root: str | Path,
    calibration_proof: Mapping[str, Any],
    expected_calibration_proof_sha256: str,
    reconstruction_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay both inputs from source artifacts before producing a verdict.

    Python objects cannot be treated as unforgeable capabilities inside a
    shared process.  This public quality-gate boundary therefore never trusts
    a caller-supplied replay receipt: it replays candidate and Gold lineage
    itself, using the same source/native/render objects for both contracts.
    """

    try:
        replayed_candidate = validate_pdf_primary_document_view_against_inputs(
            candidate,
            source_pdf=source_pdf,
            native_capture=native_capture,
            structure_candidates=structure_candidates,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
            calibration_proof=calibration_proof,
            expected_calibration_proof_sha256=expected_calibration_proof_sha256,
            reconstruction_plan=reconstruction_plan,
        )
    except PdfPrimaryDocumentViewError as error:
        raise PdfPrimaryViewEvaluationError(
            "candidate did not pass deterministic source replay"
        ) from error
    try:
        replayed_gold = validate_primary_structure_gold_against_inputs(
            gold,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
        )
    except PrimaryStructureGoldError as error:
        raise PdfPrimaryViewEvaluationError(
            "Gold did not pass deterministic source replay"
        ) from error
    return _compare_replayed_primary_document_view(
        replayed_candidate,
        replayed_gold,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "PdfPrimaryViewEvaluationError",
    "SCHEMA_VERSION",
    "canonical_primary_view_evaluation_json",
    "evaluate_pdf_primary_document_view",
    "validate_primary_view_evaluation",
]
