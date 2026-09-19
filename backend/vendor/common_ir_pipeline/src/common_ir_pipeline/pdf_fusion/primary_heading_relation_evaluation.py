"""Partial-Gold evaluation for replayed PDF heading-to-body relations.

The public evaluator replays the A4.2 sidecar, A4.1 primary view, both Gold
artifacts, and the independent A4.1 semantic evaluation from the same raw
inputs on every call.  A4.1 remains a separately serialized prerequisite;
its metrics are never folded into heading-relation metrics.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .primary_document_view import (
    SCHEMA_VERSION as PRIMARY_VIEW_SCHEMA_VERSION,
    PdfPrimaryDocumentViewError,
    PrimaryDocumentViewFixture,
    ReplayedPrimaryDocumentView,
    validate_pdf_primary_document_view_against_inputs,
)
from .primary_heading_relation_gold import (
    SCHEMA_VERSION as HEADING_GOLD_SCHEMA_VERSION,
    PrimaryHeadingRelationGoldError,
    PrimaryHeadingRelationGoldFixture,
    ReplayedPrimaryHeadingRelationGold,
    validate_primary_heading_relation_gold_against_inputs,
)
from .primary_heading_relations import (
    SCHEMA_VERSION as CANDIDATE_SCHEMA_VERSION,
    PdfPrimaryHeadingRelationsError,
    PrimaryHeadingRelationsFixture,
    canonical_pdf_primary_heading_relations_json,
    validate_pdf_primary_heading_relations_against_inputs,
)
from .primary_structure_gold import (
    SCHEMA_VERSION as BASE_GOLD_SCHEMA_VERSION,
    PrimaryStructureGoldError,
    PrimaryStructureGoldFixture,
    ReplayedPrimaryStructureGold,
    validate_primary_structure_gold_against_inputs,
)
from .primary_view_evaluation import (
    SCHEMA_VERSION as PRIMARY_EVALUATION_SCHEMA_VERSION,
    PdfPrimaryViewEvaluationError,
    canonical_primary_view_evaluation_json,
    evaluate_pdf_primary_document_view,
)
from .render_manifest import PdfRenderManifest
from .surya_layout_artifact import SuryaProducerIdentity


SCHEMA_VERSION = "pdf_primary_heading_relation_evaluation/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 250_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_SCOPES = 256
MAX_RELATIONS_PER_SCOPE = 1_024
MAX_UNSCORED_RELATIONS = 10_000

_SHA = frozenset("0123456789abcdef")
_NOTICE_ID = re.compile(r"^PBLN_[0-9]{15}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_OCCURRENCE = re.compile(
    r"^occ:inspector:p(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6]):"
    r"t(?:0|[1-9][0-9]{0,4}|[1-4][0-9]{5})$"
)
_LEAF_ID = re.compile(r"^leaf-[0-9a-f]{64}$")
_RELATION_ID = re.compile(r"^heading-relation-[0-9a-f]{64}$")

_VERDICTS = frozenset(
    {
        "passed",
        "failed",
        "not_evaluable_gold_pending",
        "not_evaluable_gold_untrusted",
        "blocked_primary_view_prerequisite",
        "blocked_scope_mismatch",
    }
)
_BLOCKING_REASONS = frozenset(
    {
        "notice_id_mismatch",
        "source_pdf_mismatch",
        "native_capture_mismatch",
        "candidate_primary_view_mismatch",
        "gold_page_scope_not_covered",
        "gold_occurrence_not_covered",
        "primary_view_prerequisite_not_passed",
    }
)
_BLOCKING_REASON_ORDER = (
    "notice_id_mismatch",
    "source_pdf_mismatch",
    "native_capture_mismatch",
    "candidate_primary_view_mismatch",
    "gold_page_scope_not_covered",
    "gold_occurrence_not_covered",
    "primary_view_prerequisite_not_passed",
)
_BLOCKING_REASON_RANK = {
    reason: index for index, reason in enumerate(_BLOCKING_REASON_ORDER)
}
_SCOPE_FAILURE_CODES = frozenset(
    {"expected_relation_failed", "false_positive_relation"}
)
_SCOPE_FAILURE_ORDER = (
    "expected_relation_failed",
    "false_positive_relation",
)
_SCOPE_FAILURE_RANK = {
    reason: index for index, reason in enumerate(_SCOPE_FAILURE_ORDER)
}
_RELATION_STATUSES = frozenset({"matched", "missing", "wrong_target"})
_PRIMARY_PREREQUISITE_VERDICTS = frozenset(
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

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "notice_id",
        "source",
        "candidate",
        "primary_document_view",
        "base_gold",
        "heading_gold",
        "primary_view_prerequisite",
        "verdict",
        "blocking_reason_codes",
        "scope_results",
        "unscored_relations",
        "metrics",
    }
)
_SOURCE_KEYS = frozenset({"source_pdf_sha256", "native_capture_sha256"})
_ARTIFACT_REF_KEYS = frozenset({"schema_version", "canonical_sha256"})
_GOLD_REF_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "quality_status"}
)
_PREREQUISITE_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "verdict"}
)
_SCOPE_KEYS = frozenset(
    {
        "scope_id",
        "physical_page",
        "base_structure_scope_id",
        "verdict",
        "failure_codes",
        "relation_results",
        "false_positive_relations",
    }
)
_RELATION_RESULT_KEYS = frozenset(
    {
        "heading_occurrence_id",
        "body_group_id",
        "expected_heading_leaf_id",
        "expected_body_leaf_id",
        "status",
        "observed_relation_id",
        "observed_body_leaf_id",
    }
)
_OBSERVED_RELATION_KEYS = frozenset(
    {
        "relation_id",
        "heading_occurrence_id",
        "heading_leaf_id",
        "body_leaf_id",
    }
)
_METRIC_KEYS = frozenset(
    {
        "reviewed_scope_count",
        "evaluated_scope_count",
        "passed_scope_count",
        "failed_scope_count",
        "expected_relation_count",
        "matched_relation_count",
        "missing_relation_count",
        "wrong_target_relation_count",
        "false_positive_relation_count",
        "unscored_relation_count",
    }
)


class PdfPrimaryHeadingRelationEvaluationError(ValueError):
    """Raised when heading-relation evaluation is unsafe or inconsistent."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfPrimaryHeadingRelationEvaluationError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} keys must be strings"
        )
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} keys are invalid ({'; '.join(details)})"
        )
    return value


def _string(
    name: str,
    value: object,
    *,
    maximum: int = MAX_STRING_BYTES,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} must be a non-empty trimmed string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} contains invalid Unicode"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} exceeds its string safety cap"
        )
    return value


def _sha(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if len(checked) != 64 or any(character not in _SHA for character in checked):
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return checked


def _identifier(name: str, value: object) -> str:
    checked = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(checked) is None:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} is not a bounded identifier"
        )
    return checked


def _occurrence(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if _OCCURRENCE.fullmatch(checked) is None:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} is not an occurrence ID"
        )
    return checked


def _leaf_id(name: str, value: object) -> str:
    checked = _string(name, value, maximum=69)
    if _LEAF_ID.fullmatch(checked) is None:
        raise PdfPrimaryHeadingRelationEvaluationError(f"{name} is not a leaf ID")
    return checked


def _relation_id(name: str, value: object) -> str:
    checked = _string(name, value, maximum=81)
    if _RELATION_ID.fullmatch(checked) is None:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} is not a relation ID"
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
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _nonnegative(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PdfPrimaryHeadingRelationEvaluationError(
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
            raise PdfPrimaryHeadingRelationEvaluationError(
                "evaluation exceeds the JSON node cap"
            )
        if depth > MAX_JSON_DEPTH:
            raise PdfPrimaryHeadingRelationEvaluationError(
                "evaluation exceeds the JSON depth cap"
            )
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise PdfPrimaryHeadingRelationEvaluationError(
                    "evaluation contains a non-finite number"
                )
            continue
        if isinstance(current, str):
            _string("JSON string", current, maximum=MAX_STRING_BYTES)
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                _string("JSON object key", key, maximum=MAX_STRING_BYTES)
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
            continue
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"evaluation contains unsupported type {type(current).__name__}"
        )


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    _assert_json_limits(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "evaluation is not canonical UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "evaluation exceeds the artifact byte cap"
        )
    return encoded


def _unique_strings(
    name: str,
    value: object,
    *,
    maximum: int,
    allowed: frozenset[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} must be a bounded array"
        )
    checked = [_string(f"{name}[{index}]", item) for index, item in enumerate(value)]
    if checked != list(dict.fromkeys(checked)):
        raise PdfPrimaryHeadingRelationEvaluationError(f"{name} must be unique")
    if allowed is not None and any(item not in allowed for item in checked):
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} contains an unsupported value"
        )
    return checked


def _artifact_ref(
    value: object, *, name: str, schema_version: str
) -> dict[str, str]:
    ref = _exact_keys(value, _ARTIFACT_REF_KEYS, name=name)
    if ref["schema_version"] != schema_version:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name}.schema_version is invalid"
        )
    return {
        "schema_version": schema_version,
        "canonical_sha256": _sha(
            f"{name}.canonical_sha256", ref["canonical_sha256"]
        ),
    }


def _gold_ref(
    value: object, *, name: str, schema_version: str
) -> dict[str, str]:
    ref = _exact_keys(value, _GOLD_REF_KEYS, name=name)
    if ref["schema_version"] != schema_version:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name}.schema_version is invalid"
        )
    quality = _string(f"{name}.quality_status", ref["quality_status"])
    if quality not in _QUALITY_STATUSES:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name}.quality_status is invalid"
        )
    return {
        "schema_version": schema_version,
        "canonical_sha256": _sha(
            f"{name}.canonical_sha256", ref["canonical_sha256"]
        ),
        "quality_status": quality,
    }


def _optional_leaf(name: str, value: object) -> str | None:
    return None if value is None else _leaf_id(name, value)


def _optional_relation(name: str, value: object) -> str | None:
    return None if value is None else _relation_id(name, value)


def _validate_relation_result(value: object, *, name: str) -> dict[str, Any]:
    item = _exact_keys(value, _RELATION_RESULT_KEYS, name=name)
    status = _string(f"{name}.status", item["status"])
    if status not in _RELATION_STATUSES:
        raise PdfPrimaryHeadingRelationEvaluationError(f"{name}.status is invalid")
    observed_relation = _optional_relation(
        f"{name}.observed_relation_id", item["observed_relation_id"]
    )
    observed_body = _optional_leaf(
        f"{name}.observed_body_leaf_id", item["observed_body_leaf_id"]
    )
    if status == "missing" and (
        observed_relation is not None or observed_body is not None
    ):
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} missing status must not carry an observed relation"
        )
    if status in {"matched", "wrong_target"} and (
        observed_relation is None or observed_body is None
    ):
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} observed status requires relation and body IDs"
        )
    expected_body = _leaf_id(
        f"{name}.expected_body_leaf_id", item["expected_body_leaf_id"]
    )
    if status == "matched" and observed_body != expected_body:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} matched status disagrees with body leaf"
        )
    if status == "wrong_target" and observed_body == expected_body:
        raise PdfPrimaryHeadingRelationEvaluationError(
            f"{name} wrong-target status matches the expected body"
        )
    return {
        "heading_occurrence_id": _occurrence(
            f"{name}.heading_occurrence_id", item["heading_occurrence_id"]
        ),
        "body_group_id": _identifier(
            f"{name}.body_group_id", item["body_group_id"]
        ),
        "expected_heading_leaf_id": _leaf_id(
            f"{name}.expected_heading_leaf_id", item["expected_heading_leaf_id"]
        ),
        "expected_body_leaf_id": expected_body,
        "status": status,
        "observed_relation_id": observed_relation,
        "observed_body_leaf_id": observed_body,
    }


def _validate_observed_relation(value: object, *, name: str) -> dict[str, str]:
    item = _exact_keys(value, _OBSERVED_RELATION_KEYS, name=name)
    return {
        "relation_id": _relation_id(f"{name}.relation_id", item["relation_id"]),
        "heading_occurrence_id": _occurrence(
            f"{name}.heading_occurrence_id", item["heading_occurrence_id"]
        ),
        "heading_leaf_id": _leaf_id(
            f"{name}.heading_leaf_id", item["heading_leaf_id"]
        ),
        "body_leaf_id": _leaf_id(f"{name}.body_leaf_id", item["body_leaf_id"]),
    }


def validate_primary_heading_relation_evaluation(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate result shape, status combinations, counts, and verdict."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="evaluation")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfPrimaryHeadingRelationEvaluationError("schema_version is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "evaluation must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "standalone validation scope is invalid"
        )
    notice_id = _string("notice_id", root["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PdfPrimaryHeadingRelationEvaluationError("notice_id is invalid")
    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    source_copy = {
        "source_pdf_sha256": _sha(
            "source.source_pdf_sha256", source["source_pdf_sha256"]
        ),
        "native_capture_sha256": _sha(
            "source.native_capture_sha256", source["native_capture_sha256"]
        ),
    }
    candidate = _artifact_ref(
        root["candidate"], name="candidate", schema_version=CANDIDATE_SCHEMA_VERSION
    )
    primary_view = _artifact_ref(
        root["primary_document_view"],
        name="primary_document_view",
        schema_version=PRIMARY_VIEW_SCHEMA_VERSION,
    )
    base_gold = _gold_ref(
        root["base_gold"], name="base_gold", schema_version=BASE_GOLD_SCHEMA_VERSION
    )
    heading_gold = _gold_ref(
        root["heading_gold"],
        name="heading_gold",
        schema_version=HEADING_GOLD_SCHEMA_VERSION,
    )
    prerequisite = _exact_keys(
        root["primary_view_prerequisite"],
        _PREREQUISITE_KEYS,
        name="primary_view_prerequisite",
    )
    if prerequisite["schema_version"] != PRIMARY_EVALUATION_SCHEMA_VERSION:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "primary-view prerequisite schema is invalid"
        )
    prerequisite_verdict = _string(
        "primary_view_prerequisite.verdict", prerequisite["verdict"]
    )
    if prerequisite_verdict not in _PRIMARY_PREREQUISITE_VERDICTS:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "primary-view prerequisite verdict is invalid"
        )
    prerequisite_copy = {
        "schema_version": PRIMARY_EVALUATION_SCHEMA_VERSION,
        "canonical_sha256": _sha(
            "primary_view_prerequisite.canonical_sha256",
            prerequisite["canonical_sha256"],
        ),
        "verdict": prerequisite_verdict,
    }
    verdict = _string("verdict", root["verdict"])
    if verdict not in _VERDICTS:
        raise PdfPrimaryHeadingRelationEvaluationError("verdict is invalid")
    blocking = _unique_strings(
        "blocking_reason_codes",
        root["blocking_reason_codes"],
        maximum=len(_BLOCKING_REASONS),
        allowed=_BLOCKING_REASONS,
    )
    if blocking != sorted(blocking, key=_BLOCKING_REASON_RANK.__getitem__):
        raise PdfPrimaryHeadingRelationEvaluationError(
            "blocking_reason_codes must use canonical rank order"
        )

    scopes_raw = root["scope_results"]
    if not isinstance(scopes_raw, list) or len(scopes_raw) > MAX_SCOPES:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "scope_results must be a bounded array"
        )
    scopes: list[dict[str, Any]] = []
    seen_scope_ids: set[str] = set()
    previous_scope_key: tuple[int, str] | None = None
    for scope_index, raw_scope in enumerate(scopes_raw):
        name = f"scope_results[{scope_index}]"
        scope = _exact_keys(raw_scope, _SCOPE_KEYS, name=name)
        scope_id = _identifier(f"{name}.scope_id", scope["scope_id"])
        if scope_id in seen_scope_ids:
            raise PdfPrimaryHeadingRelationEvaluationError(
                "scope result IDs must be unique"
            )
        seen_scope_ids.add(scope_id)
        physical_page = _positive(
            f"{name}.physical_page",
            scope["physical_page"],
            maximum=MAX_PAGES,
        )
        scope_key = (physical_page, scope_id)
        if previous_scope_key is not None and scope_key <= previous_scope_key:
            raise PdfPrimaryHeadingRelationEvaluationError(
                "scope_results must use canonical page/scope order"
            )
        previous_scope_key = scope_key
        scope_verdict = _string(f"{name}.verdict", scope["verdict"])
        if scope_verdict not in {"passed", "failed"}:
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name}.verdict is invalid"
            )
        failures = _unique_strings(
            f"{name}.failure_codes",
            scope["failure_codes"],
            maximum=len(_SCOPE_FAILURE_CODES),
            allowed=_SCOPE_FAILURE_CODES,
        )
        if failures != sorted(failures, key=_SCOPE_FAILURE_RANK.__getitem__):
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name}.failure_codes must use canonical rank order"
            )
        relations_raw = scope["relation_results"]
        if (
            not isinstance(relations_raw, list)
            or not relations_raw
            or len(relations_raw) > MAX_RELATIONS_PER_SCOPE
        ):
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name}.relation_results must be bounded and non-empty"
            )
        relations = [
            _validate_relation_result(
                item, name=f"{name}.relation_results[{index}]"
            )
            for index, item in enumerate(relations_raw)
        ]
        relation_keys = [
            (item["heading_occurrence_id"], item["body_group_id"])
            for item in relations
        ]
        if len(relation_keys) != len(set(relation_keys)):
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name} expected relation results must be unique"
            )
        canonical_relation_keys = [
            (_occurrence_sort_key(occurrence_id), group_id)
            for occurrence_id, group_id in relation_keys
        ]
        if canonical_relation_keys != sorted(canonical_relation_keys):
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name}.relation_results must use canonical occurrence/group order"
            )
        false_raw = scope["false_positive_relations"]
        if not isinstance(false_raw, list) or len(false_raw) > MAX_RELATIONS_PER_SCOPE:
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name}.false_positive_relations must be bounded"
            )
        false_positive = [
            _validate_observed_relation(
                item, name=f"{name}.false_positive_relations[{index}]"
            )
            for index, item in enumerate(false_raw)
        ]
        false_ids = [item["relation_id"] for item in false_positive]
        if len(false_ids) != len(set(false_ids)):
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name} false-positive relations must be unique"
            )
        false_keys = [
            (_occurrence_sort_key(item["heading_occurrence_id"]), item["relation_id"])
            for item in false_positive
        ]
        if false_keys != sorted(false_keys):
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name}.false_positive_relations must use canonical order"
            )
        expected_failures: list[str] = []
        if any(item["status"] != "matched" for item in relations):
            expected_failures.append("expected_relation_failed")
        if false_positive:
            expected_failures.append("false_positive_relation")
        if failures != expected_failures:
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name} failure codes disagree with relation results"
            )
        if (scope_verdict == "failed") != bool(failures):
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name} verdict disagrees with failures"
            )
        scopes.append(
            {
                "scope_id": scope_id,
                "physical_page": physical_page,
                "base_structure_scope_id": _identifier(
                    f"{name}.base_structure_scope_id",
                    scope["base_structure_scope_id"],
                ),
                "verdict": scope_verdict,
                "failure_codes": failures,
                "relation_results": relations,
                "false_positive_relations": false_positive,
            }
        )

    unscored_raw = root["unscored_relations"]
    if (
        not isinstance(unscored_raw, list)
        or len(unscored_raw) > MAX_UNSCORED_RELATIONS
    ):
        raise PdfPrimaryHeadingRelationEvaluationError(
            "unscored_relations must be a bounded array"
        )
    unscored = [
        _validate_observed_relation(item, name=f"unscored_relations[{index}]")
        for index, item in enumerate(unscored_raw)
    ]
    unscored_ids = [item["relation_id"] for item in unscored]
    if len(unscored_ids) != len(set(unscored_ids)):
        raise PdfPrimaryHeadingRelationEvaluationError(
            "unscored relation IDs must be unique"
        )
    unscored_keys = [
        (_occurrence_sort_key(item["heading_occurrence_id"]), item["relation_id"])
        for item in unscored
    ]
    if unscored_keys != sorted(unscored_keys):
        raise PdfPrimaryHeadingRelationEvaluationError(
            "unscored_relations must use canonical order"
        )

    metrics = _exact_keys(root["metrics"], _METRIC_KEYS, name="metrics")
    metric_copy = {
        name: _nonnegative(f"metrics.{name}", metrics[name])
        for name in _METRIC_KEYS
    }
    expected_metrics = {
        "reviewed_scope_count": metric_copy["reviewed_scope_count"],
        "evaluated_scope_count": len(scopes),
        "passed_scope_count": sum(scope["verdict"] == "passed" for scope in scopes),
        "failed_scope_count": sum(scope["verdict"] == "failed" for scope in scopes),
        "expected_relation_count": sum(
            len(scope["relation_results"]) for scope in scopes
        ),
        "matched_relation_count": sum(
            item["status"] == "matched"
            for scope in scopes
            for item in scope["relation_results"]
        ),
        "missing_relation_count": sum(
            item["status"] == "missing"
            for scope in scopes
            for item in scope["relation_results"]
        ),
        "wrong_target_relation_count": sum(
            item["status"] == "wrong_target"
            for scope in scopes
            for item in scope["relation_results"]
        ),
        "false_positive_relation_count": sum(
            len(scope["false_positive_relations"]) for scope in scopes
        ),
        "unscored_relation_count": len(unscored),
    }
    if metric_copy["reviewed_scope_count"] > MAX_SCOPES:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "reviewed_scope_count exceeds the cap"
        )
    for name, expected in expected_metrics.items():
        if name != "reviewed_scope_count" and metric_copy[name] != expected:
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"metrics.{name} disagrees with detailed results"
            )

    quality_statuses = {
        base_gold["quality_status"], heading_gold["quality_status"]
    }
    combined_quality = (
        "not_evaluable_gold_pending"
        if "not_evaluable_gold_pending" in quality_statuses
        else (
            None
            if quality_statuses == {"evaluable"}
            else "not_evaluable_gold_untrusted"
        )
    )
    if verdict in {"passed", "failed"}:
        if blocking or not scopes or prerequisite_copy["verdict"] != "passed":
            raise PdfPrimaryHeadingRelationEvaluationError(
                "quality verdict requires passed prerequisite and evaluated scopes"
            )
        if metric_copy["reviewed_scope_count"] != len(scopes):
            raise PdfPrimaryHeadingRelationEvaluationError(
                "quality verdict must evaluate every reviewed scope"
            )
        expected_verdict = "failed" if expected_metrics["failed_scope_count"] else "passed"
        if verdict != expected_verdict or combined_quality is not None:
            raise PdfPrimaryHeadingRelationEvaluationError(
                "quality verdict is inconsistent"
            )
    elif verdict in {
        "not_evaluable_gold_pending",
        "not_evaluable_gold_untrusted",
    }:
        if blocking or scopes or unscored or verdict != combined_quality:
            raise PdfPrimaryHeadingRelationEvaluationError(
                "not-evaluable verdict shape is inconsistent"
            )
    elif verdict == "blocked_primary_view_prerequisite":
        if (
            combined_quality is not None
            or blocking != ["primary_view_prerequisite_not_passed"]
            or scopes
            or unscored
            or prerequisite_copy["verdict"] == "passed"
        ):
            raise PdfPrimaryHeadingRelationEvaluationError(
                "prerequisite-blocked verdict shape is invalid"
            )
    else:
        if (
            combined_quality is not None
            or prerequisite_copy["verdict"] != "passed"
            or not blocking
            or "primary_view_prerequisite_not_passed" in blocking
            or scopes
            or unscored
        ):
            raise PdfPrimaryHeadingRelationEvaluationError(
                "scope-mismatch verdict shape is invalid"
            )
    if not scopes:
        for name in _METRIC_KEYS - {"reviewed_scope_count"}:
            if metric_copy[name] != 0:
                raise PdfPrimaryHeadingRelationEvaluationError(
                    "non-evaluated metrics must be zero"
                )

    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": notice_id,
        "source": source_copy,
        "candidate": candidate,
        "primary_document_view": primary_view,
        "base_gold": base_gold,
        "heading_gold": heading_gold,
        "primary_view_prerequisite": prerequisite_copy,
        "verdict": verdict,
        "blocking_reason_codes": blocking,
        "scope_results": scopes,
        "unscored_relations": unscored,
        "metrics": metric_copy,
    }
    _canonical_bytes(result)
    return result


def canonical_primary_heading_relation_evaluation_json(
    value: Mapping[str, Any],
) -> bytes:
    return _canonical_bytes(validate_primary_heading_relation_evaluation(value))


def _empty_metrics(*, reviewed_scope_count: int) -> dict[str, int]:
    return {
        "reviewed_scope_count": reviewed_scope_count,
        "evaluated_scope_count": 0,
        "passed_scope_count": 0,
        "failed_scope_count": 0,
        "expected_relation_count": 0,
        "matched_relation_count": 0,
        "missing_relation_count": 0,
        "wrong_target_relation_count": 0,
        "false_positive_relation_count": 0,
        "unscored_relation_count": 0,
    }


def _quality_status(fixture: Any) -> str:
    return "evaluable" if fixture.has_trusted_confirmation else fixture.fixture.quality_gate_status


def _observed_relation(
    relation: Mapping[str, Any], heading_occurrence_id: str
) -> dict[str, str]:
    return {
        "relation_id": relation["relation_id"],
        "heading_occurrence_id": heading_occurrence_id,
        "heading_leaf_id": relation["heading_leaf_id"],
        "body_leaf_id": relation["body_leaf_id"],
    }


def _compare_replayed_heading_relations(
    candidate: Mapping[str, Any],
    primary_view: ReplayedPrimaryDocumentView,
    base_gold: ReplayedPrimaryStructureGold,
    heading_gold: ReplayedPrimaryHeadingRelationGold,
    primary_view_evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare artifacts replayed by the public evaluator in this call."""

    if type(primary_view) is not ReplayedPrimaryDocumentView:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "primary view must come from strict replay"
        )
    if type(base_gold) is not ReplayedPrimaryStructureGold:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "base Gold must come from strict replay"
        )
    if type(heading_gold) is not ReplayedPrimaryHeadingRelationGold:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "heading Gold must come from strict replay"
        )
    candidate_digest = sha256(
        canonical_pdf_primary_heading_relations_json(candidate)
    ).hexdigest()
    replayed_artifacts = (
        ("primary view", primary_view),
        ("base Gold", base_gold),
        ("heading Gold", heading_gold),
    )
    replayed_digests: list[str] = []
    for name, replayed in replayed_artifacts:
        canonical = replayed.canonical_json()
        if type(canonical) is not bytes:
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name} replay must expose canonical byte strings"
            )
        digest = sha256(canonical).hexdigest()
        if digest != replayed.canonical_sha256:
            raise PdfPrimaryHeadingRelationEvaluationError(
                f"{name} replay digest disagrees with its canonical bytes"
            )
        replayed_digests.append(digest)
    primary_digest, base_digest, heading_digest = replayed_digests
    prerequisite = dict(primary_view_evaluation)
    try:
        prerequisite_digest = sha256(
            canonical_primary_view_evaluation_json(prerequisite)
        ).hexdigest()
    except PdfPrimaryViewEvaluationError as error:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "primary-view prerequisite result is invalid"
        ) from error

    view = primary_view.payload
    base = base_gold.payload
    heading = heading_gold.payload
    if (
        prerequisite["notice_id"] != view["notice_id"]
        or prerequisite["source"]["source_pdf_sha256"]
        != view["source"]["source_pdf_sha256"]
        or prerequisite["source"]["native_capture_sha256"]
        != view["source"]["native_capture"]["canonical_sha256"]
        or prerequisite["candidate"]["canonical_sha256"] != primary_digest
        or prerequisite["gold"]["canonical_sha256"] != base_digest
    ):
        raise PdfPrimaryHeadingRelationEvaluationError(
            "primary-view prerequisite does not bind the replayed A4.1 inputs"
        )
    common = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": view["notice_id"],
        "source": {
            "source_pdf_sha256": view["source"]["source_pdf_sha256"],
            "native_capture_sha256": view["source"]["native_capture"][
                "canonical_sha256"
            ],
        },
        "candidate": {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "canonical_sha256": candidate_digest,
        },
        "primary_document_view": {
            "schema_version": PRIMARY_VIEW_SCHEMA_VERSION,
            "canonical_sha256": primary_digest,
        },
        "base_gold": {
            "schema_version": BASE_GOLD_SCHEMA_VERSION,
            "canonical_sha256": base_digest,
            "quality_status": _quality_status(base_gold),
        },
        "heading_gold": {
            "schema_version": HEADING_GOLD_SCHEMA_VERSION,
            "canonical_sha256": heading_digest,
            "quality_status": _quality_status(heading_gold),
        },
        "primary_view_prerequisite": {
            "schema_version": PRIMARY_EVALUATION_SCHEMA_VERSION,
            "canonical_sha256": prerequisite_digest,
            "verdict": prerequisite["verdict"],
        },
    }
    reviewed_scope_count = len(heading["reviewed_scopes"])
    quality_statuses = {
        common["base_gold"]["quality_status"],
        common["heading_gold"]["quality_status"],
    }
    if "not_evaluable_gold_pending" in quality_statuses:
        return validate_primary_heading_relation_evaluation(
            {
                **common,
                "verdict": "not_evaluable_gold_pending",
                "blocking_reason_codes": [],
                "scope_results": [],
                "unscored_relations": [],
                "metrics": _empty_metrics(reviewed_scope_count=reviewed_scope_count),
            }
        )
    if quality_statuses != {"evaluable"}:
        return validate_primary_heading_relation_evaluation(
            {
                **common,
                "verdict": "not_evaluable_gold_untrusted",
                "blocking_reason_codes": [],
                "scope_results": [],
                "unscored_relations": [],
                "metrics": _empty_metrics(reviewed_scope_count=reviewed_scope_count),
            }
        )
    if prerequisite["verdict"] != "passed":
        return validate_primary_heading_relation_evaluation(
            {
                **common,
                "verdict": "blocked_primary_view_prerequisite",
                "blocking_reason_codes": ["primary_view_prerequisite_not_passed"],
                "scope_results": [],
                "unscored_relations": [],
                "metrics": _empty_metrics(reviewed_scope_count=reviewed_scope_count),
            }
        )

    blocking: list[str] = []
    if candidate["notice_id"] != view["notice_id"] or heading["notice_id"] != view["notice_id"]:
        blocking.append("notice_id_mismatch")
    if (
        candidate["source_pdf_sha256"]
        != view["source"]["source_pdf_sha256"]
        or heading["source"]["source_pdf_sha256"]
        != view["source"]["source_pdf_sha256"]
    ):
        blocking.append("source_pdf_mismatch")
    if (
        heading["source"]["native_capture"]["canonical_sha256"]
        != view["source"]["native_capture"]["canonical_sha256"]
    ):
        blocking.append("native_capture_mismatch")
    if candidate["input_artifacts"]["primary_document_view_sha256"] != primary_digest:
        blocking.append("candidate_primary_view_mismatch")
    if not set(heading["page_scope"]).issubset(view["page_scope"]):
        blocking.append("gold_page_scope_not_covered")

    leaves = view["leaves"]
    leaf_by_id = {leaf["leaf_id"]: leaf for leaf in leaves}
    leaf_by_occurrence = {
        occurrence_id: leaf
        for leaf in leaves
        for occurrence_id in leaf["occurrence_ids"]
    }
    reviewed_occurrences = {
        occurrence_id
        for scope in base["reviewed_scopes"]
        for occurrence_id in scope["occurrence_ids"]
    }
    if not reviewed_occurrences.issubset(leaf_by_occurrence):
        blocking.append("gold_occurrence_not_covered")
    if any(
        relation["heading_leaf_id"] not in leaf_by_id
        or relation["body_leaf_id"] not in leaf_by_id
        for relation in candidate["relations"]
    ):
        blocking.append("candidate_primary_view_mismatch")
    blocking = sorted(
        set(blocking), key=_BLOCKING_REASON_RANK.__getitem__
    )
    if blocking:
        return validate_primary_heading_relation_evaluation(
            {
                **common,
                "verdict": "blocked_scope_mismatch",
                "blocking_reason_codes": blocking,
                "scope_results": [],
                "unscored_relations": [],
                "metrics": _empty_metrics(reviewed_scope_count=reviewed_scope_count),
            }
        )

    base_scopes = {scope["scope_id"]: scope for scope in base["reviewed_scopes"]}
    observed_by_heading = {
        relation["heading_leaf_id"]: relation for relation in candidate["relations"]
    }
    scope_results: list[dict[str, Any]] = []
    scored_relation_ids: set[str] = set()
    for gold_scope in heading["reviewed_scopes"]:
        base_scope = base_scopes[gold_scope["base_structure_scope_id"]]
        reviewed = set(base_scope["occurrence_ids"])
        groups = {
            group["group_id"]: group for group in base_scope["ordered_groups"]
        }
        expected_heading_occurrences = {
            relation["heading_occurrence_id"]
            for relation in gold_scope["expected_relations"]
        }
        relation_results: list[dict[str, Any]] = []
        for expected in gold_scope["expected_relations"]:
            heading_leaf = leaf_by_occurrence[expected["heading_occurrence_id"]]
            body_leaf_ids = {
                leaf_by_occurrence[occurrence_id]["leaf_id"]
                for occurrence_id in groups[expected["body_group_id"]][
                    "occurrence_ids"
                ]
            }
            if len(body_leaf_ids) != 1:
                raise PdfPrimaryHeadingRelationEvaluationError(
                    "passed A4.1 prerequisite did not resolve one body leaf"
                )
            body_leaf_id = next(iter(body_leaf_ids))
            observed = observed_by_heading.get(heading_leaf["leaf_id"])
            if observed is None:
                status = "missing"
                observed_relation_id = None
                observed_body_leaf_id = None
            else:
                scored_relation_ids.add(observed["relation_id"])
                observed_relation_id = observed["relation_id"]
                observed_body_leaf_id = observed["body_leaf_id"]
                status = (
                    "matched"
                    if observed_body_leaf_id == body_leaf_id
                    else "wrong_target"
                )
            relation_results.append(
                {
                    "heading_occurrence_id": expected["heading_occurrence_id"],
                    "body_group_id": expected["body_group_id"],
                    "expected_heading_leaf_id": heading_leaf["leaf_id"],
                    "expected_body_leaf_id": body_leaf_id,
                    "status": status,
                    "observed_relation_id": observed_relation_id,
                    "observed_body_leaf_id": observed_body_leaf_id,
                }
            )

        false_positive: list[dict[str, str]] = []
        for relation in candidate["relations"]:
            heading_leaf = leaf_by_id[relation["heading_leaf_id"]]
            unexpected_reviewed_occurrences = [
                occurrence_id
                for occurrence_id in heading_leaf["occurrence_ids"]
                if occurrence_id in reviewed
                and occurrence_id not in expected_heading_occurrences
            ]
            if unexpected_reviewed_occurrences:
                scored_relation_ids.add(relation["relation_id"])
                false_positive.append(
                    _observed_relation(
                        relation, unexpected_reviewed_occurrences[0]
                    )
                )
        failures: list[str] = []
        if any(item["status"] != "matched" for item in relation_results):
            failures.append("expected_relation_failed")
        if false_positive:
            failures.append("false_positive_relation")
        scope_results.append(
            {
                "scope_id": gold_scope["scope_id"],
                "physical_page": gold_scope["physical_page"],
                "base_structure_scope_id": gold_scope[
                    "base_structure_scope_id"
                ],
                "verdict": "failed" if failures else "passed",
                "failure_codes": failures,
                "relation_results": sorted(
                    relation_results,
                    key=lambda item: (
                        _occurrence_sort_key(item["heading_occurrence_id"]),
                        item["body_group_id"],
                    ),
                ),
                "false_positive_relations": sorted(
                    false_positive,
                    key=lambda item: (
                        _occurrence_sort_key(item["heading_occurrence_id"]),
                        item["relation_id"],
                    ),
                ),
            }
        )

    scope_results.sort(
        key=lambda item: (item["physical_page"], item["scope_id"])
    )
    unscored = []
    for relation in candidate["relations"]:
        if relation["relation_id"] in scored_relation_ids:
            continue
        heading_leaf = leaf_by_id[relation["heading_leaf_id"]]
        unscored.append(
            _observed_relation(relation, heading_leaf["occurrence_ids"][0])
        )
    unscored.sort(
        key=lambda item: (
            _occurrence_sort_key(item["heading_occurrence_id"]),
            item["relation_id"],
        )
    )
    metrics = {
        "reviewed_scope_count": reviewed_scope_count,
        "evaluated_scope_count": len(scope_results),
        "passed_scope_count": sum(scope["verdict"] == "passed" for scope in scope_results),
        "failed_scope_count": sum(scope["verdict"] == "failed" for scope in scope_results),
        "expected_relation_count": sum(len(scope["relation_results"]) for scope in scope_results),
        "matched_relation_count": sum(
            item["status"] == "matched"
            for scope in scope_results
            for item in scope["relation_results"]
        ),
        "missing_relation_count": sum(
            item["status"] == "missing"
            for scope in scope_results
            for item in scope["relation_results"]
        ),
        "wrong_target_relation_count": sum(
            item["status"] == "wrong_target"
            for scope in scope_results
            for item in scope["relation_results"]
        ),
        "false_positive_relation_count": sum(
            len(scope["false_positive_relations"])
            for scope in scope_results
        ),
        "unscored_relation_count": len(unscored),
    }
    return validate_primary_heading_relation_evaluation(
        {
            **common,
            "verdict": "failed" if metrics["failed_scope_count"] else "passed",
            "blocking_reason_codes": [],
            "scope_results": scope_results,
            "unscored_relations": unscored,
            "metrics": metrics,
        }
    )


def evaluate_pdf_primary_heading_relations(
    candidate: Mapping[str, Any] | PrimaryHeadingRelationsFixture,
    primary_document_view: Mapping[str, Any] | PrimaryDocumentViewFixture,
    base_gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
    heading_gold: Mapping[str, Any] | PrimaryHeadingRelationGoldFixture,
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
    """Replay every candidate/Gold input before semantic comparison."""

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
    primary_inputs = {
        key: candidate_inputs[key]
        for key in (
            "source_pdf",
            "native_capture",
            "structure_candidates",
            "render_manifest",
            "render_artifact_root",
            "calibration_proof",
            "expected_calibration_proof_sha256",
            "reconstruction_plan",
        )
    }
    try:
        replayed_candidate = validate_pdf_primary_heading_relations_against_inputs(
            candidate, **candidate_inputs
        )
    except PdfPrimaryHeadingRelationsError as error:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "heading candidate did not pass deterministic source replay"
        ) from error
    try:
        replayed_primary = validate_pdf_primary_document_view_against_inputs(
            primary_document_view, **primary_inputs
        )
    except PdfPrimaryDocumentViewError as error:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "primary document view did not pass deterministic source replay"
        ) from error
    try:
        replayed_base = validate_primary_structure_gold_against_inputs(
            base_gold,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
        )
    except PrimaryStructureGoldError as error:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "base Gold did not pass deterministic source replay"
        ) from error
    try:
        replayed_heading = validate_primary_heading_relation_gold_against_inputs(
            heading_gold,
            primary_structure_gold=base_gold,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
        )
    except PrimaryHeadingRelationGoldError as error:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "heading Gold did not pass deterministic source replay"
        ) from error
    try:
        prerequisite = evaluate_pdf_primary_document_view(
            primary_document_view,
            base_gold,
            **primary_inputs,
        )
    except PdfPrimaryViewEvaluationError as error:
        raise PdfPrimaryHeadingRelationEvaluationError(
            "primary-view prerequisite evaluation failed to replay"
        ) from error
    return _compare_replayed_heading_relations(
        replayed_candidate,
        replayed_primary,
        replayed_base,
        replayed_heading,
        prerequisite,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "PdfPrimaryHeadingRelationEvaluationError",
    "SCHEMA_VERSION",
    "canonical_primary_heading_relation_evaluation_json",
    "evaluate_pdf_primary_heading_relations",
    "validate_primary_heading_relation_evaluation",
]
