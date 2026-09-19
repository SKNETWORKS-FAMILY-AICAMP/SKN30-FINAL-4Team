"""Strict, textless Gold for page-local table continuation relations.

``pdf_primary_table_continuation_gold/v1`` records reviewer-approved
``between_rows`` links between page-local scopes from a bound
``pdf_primary_table_grid_gold/v1`` artifact.  It contains no parser, OCR,
candidate-grid, geometry, text, or occurrence payload.

Standalone validation checks every endpoint and column reference against the
bound base Gold.  A trust boundary must additionally call
``validate_primary_table_continuation_gold_against_inputs`` so the base Gold,
source PDF, native capture, and canonical renders are replayed together.
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

from .primary_table_grid_gold import (
    SCHEMA_VERSION as BASE_TABLE_GRID_GOLD_SCHEMA_VERSION,
    PrimaryTableGridGoldError,
    PrimaryTableGridGoldFixture,
    validate_primary_table_grid_gold_against_inputs,
)
from .render_manifest import PdfRenderManifest


SCHEMA_VERSION = "pdf_primary_table_continuation_gold/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 18
MAX_JSON_NODES = 50_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_CONTINUATIONS = 256
MAX_COLUMNS_PER_CONTINUATION = 1_024

# A human-confirmed claim becomes trusted only through a separately reviewed
# digest change.  The base A4.3a Gold must independently be trusted as well.
TRUSTED_CONFIRMED_TABLE_CONTINUATION_GOLD_SHA256S: frozenset[str] = frozenset(
    {
        "36d58cf6c7fb3e85e429937a927fb4a1c5542e487736359aa760b2191c4b98eb",
    }
)
_REPLAY_CONSTRUCTION_TOKEN = object()

_SHA = frozenset("0123456789abcdef")
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
        "reviewed_continuations",
    }
)
_SOURCE_KEYS = frozenset(
    {
        "source_pdf_sha256",
        "canonical_page_renders",
        "native_capture",
        "base_table_grid_gold",
    }
)
_PAGE_RENDER_KEYS = frozenset({"physical_page", "canonical_render_sha256"})
_NATIVE_CAPTURE_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "extractor_version"}
)
_BASE_GOLD_KEYS = frozenset({"schema_version", "canonical_sha256"})
_CONTINUATION_KEYS = frozenset(
    {
        "continuation_id",
        "predecessor_scope_id",
        "successor_scope_id",
        "relation_kind",
        "column_mapping",
        "review_status",
        "reviewer_ref",
        "confirmed_at",
    }
)
_COLUMN_MAPPING_KEYS = frozenset(
    {"predecessor_column_id", "successor_column_id"}
)
_REVIEW_STATUSES = frozenset(
    {"pending_human_confirmation", "human_confirmed"}
)


class PrimaryTableContinuationGoldError(ValueError):
    """Raised when table-continuation Gold is unsafe or ambiguous."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrimaryTableContinuationGoldError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PrimaryTableContinuationGoldError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        if extra:
            detail.append("unexpected keys: " + ", ".join(extra))
        raise PrimaryTableContinuationGoldError(
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
        raise PrimaryTableContinuationGoldError(f"{name} must be a string")
    if (not allow_empty and not value) or value != value.strip():
        raise PrimaryTableContinuationGoldError(f"{name} must be a trimmed string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PrimaryTableContinuationGoldError(
            f"{name} contains invalid Unicode"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PrimaryTableContinuationGoldError(
            f"{name} exceeds its string safety cap"
        )
    return value


def _identifier(name: str, value: object) -> str:
    checked = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(checked) is None:
        raise PrimaryTableContinuationGoldError(
            f"{name} is not a bounded identifier"
        )
    return checked


def _sha256(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if len(checked) != 64 or any(character not in _SHA for character in checked):
        raise PrimaryTableContinuationGoldError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return checked


def _positive_int(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PrimaryTableContinuationGoldError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _strictly_increasing(values: Sequence[int]) -> bool:
    return all(left < right for left, right in zip(values, values[1:]))


def _assert_json_limits(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise PrimaryTableContinuationGoldError(
                "Gold exceeds the JSON node safety cap"
            )
        if depth > MAX_JSON_DEPTH:
            raise PrimaryTableContinuationGoldError(
                "Gold exceeds the JSON depth safety cap"
            )
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise PrimaryTableContinuationGoldError(
                    "Gold contains a non-finite number"
                )
            continue
        if isinstance(current, str):
            _string("Gold string", current, allow_empty=True)
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                _string("Gold object key", key)
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
            continue
        raise PrimaryTableContinuationGoldError(
            f"Gold contains unsupported type {type(current).__name__}"
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
        raise PrimaryTableContinuationGoldError(
            "Gold is not canonical UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PrimaryTableContinuationGoldError(
            "Gold exceeds the artifact byte cap"
        )
    return encoded


def _review_metadata(item: Mapping[str, Any], *, name: str) -> None:
    status = item["review_status"]
    if status not in _REVIEW_STATUSES:
        raise PrimaryTableContinuationGoldError(
            f"{name}.review_status is unsupported"
        )
    reviewer_ref = item["reviewer_ref"]
    confirmed_at = item["confirmed_at"]
    if status == "pending_human_confirmation":
        if reviewer_ref is not None or confirmed_at is not None:
            raise PrimaryTableContinuationGoldError(
                f"{name} pending review metadata must be null"
            )
        return
    if (
        not isinstance(reviewer_ref, str)
        or _REVIEWER_REF.fullmatch(reviewer_ref) is None
    ):
        raise PrimaryTableContinuationGoldError(
            f"{name}.reviewer_ref must be a non-identifying reviewer reference"
        )
    timestamp = _string(f"{name}.confirmed_at", confirmed_at, maximum=20)
    if _UTC_TIMESTAMP.fullmatch(timestamp) is None:
        raise PrimaryTableContinuationGoldError(
            f"{name}.confirmed_at must be second-precision RFC3339 UTC"
        )
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise PrimaryTableContinuationGoldError(
            f"{name}.confirmed_at is not a real UTC timestamp"
        ) from error


def _base_fixture(
    value: Mapping[str, Any] | PrimaryTableGridGoldFixture,
) -> PrimaryTableGridGoldFixture:
    try:
        return (
            value
            if type(value) is PrimaryTableGridGoldFixture
            else PrimaryTableGridGoldFixture.from_dict(value)
        )
    except PrimaryTableGridGoldError as error:
        raise PrimaryTableContinuationGoldError(
            "base table-grid Gold is invalid"
        ) from error


def _has_cycle(edges: Mapping[str, str]) -> bool:
    """Return True for a directed cycle without recursive traversal."""

    complete: set[str] = set()
    for start in edges:
        if start in complete:
            continue
        path: set[str] = set()
        current = start
        while current in edges and current not in complete:
            if current in path:
                return True
            path.add(current)
            current = edges[current]
        complete.update(path)
    return False


def validate_primary_table_continuation_gold(
    value: Mapping[str, Any],
    *,
    base_table_grid_gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
) -> dict[str, Any]:
    """Validate shape and every endpoint against one bound A4.3a Gold."""

    base_fixture = _base_fixture(base_table_grid_gold)
    base = base_fixture.to_dict()
    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="table-continuation Gold")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PrimaryTableContinuationGoldError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PrimaryTableContinuationGoldError(
            "Gold must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PrimaryTableContinuationGoldError(
            "standalone validation must disclose internal-consistency-only scope"
        )
    notice_id = _string("notice_id", root["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PrimaryTableContinuationGoldError(
            "notice_id must use PBLN_ plus 15 digits"
        )
    if notice_id != base["notice_id"]:
        raise PrimaryTableContinuationGoldError("notice_id does not match base Gold")

    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    source_pdf_sha256 = _sha256(
        "source.source_pdf_sha256", source["source_pdf_sha256"]
    )
    if source_pdf_sha256 != base["source"]["source_pdf_sha256"]:
        raise PrimaryTableContinuationGoldError(
            "source PDF binding does not match base Gold"
        )
    capture = _exact_keys(
        source["native_capture"],
        _NATIVE_CAPTURE_KEYS,
        name="source.native_capture",
    )
    checked_capture = {
        "schema_version": _string(
            "source.native_capture.schema_version", capture["schema_version"], maximum=64
        ),
        "canonical_sha256": _sha256(
            "source.native_capture.canonical_sha256", capture["canonical_sha256"]
        ),
        "extractor_version": _string(
            "source.native_capture.extractor_version",
            capture["extractor_version"],
            maximum=64,
        ),
    }
    if checked_capture != base["source"]["native_capture"]:
        raise PrimaryTableContinuationGoldError(
            "native capture binding does not match base Gold"
        )
    base_reference = _exact_keys(
        source["base_table_grid_gold"],
        _BASE_GOLD_KEYS,
        name="source.base_table_grid_gold",
    )
    if base_reference["schema_version"] != BASE_TABLE_GRID_GOLD_SCHEMA_VERSION:
        raise PrimaryTableContinuationGoldError(
            "base table-grid Gold schema binding is unsupported"
        )
    base_sha256 = _sha256(
        "source.base_table_grid_gold.canonical_sha256",
        base_reference["canonical_sha256"],
    )
    if base_sha256 != base_fixture.canonical_sha256:
        raise PrimaryTableContinuationGoldError(
            "base table-grid Gold canonical hash binding mismatch"
        )

    page_scope_raw = root["page_scope"]
    if (
        not isinstance(page_scope_raw, list)
        or len(page_scope_raw) < 2
        or len(page_scope_raw) > MAX_PAGES
    ):
        raise PrimaryTableContinuationGoldError(
            "page_scope must contain at least two bounded pages"
        )
    page_scope = [
        _positive_int(f"page_scope[{index}]", page, maximum=MAX_PAGES)
        for index, page in enumerate(page_scope_raw)
    ]
    if not _strictly_increasing(page_scope):
        raise PrimaryTableContinuationGoldError(
            "page_scope must be unique and ascending"
        )
    if not set(page_scope).issubset(base["page_scope"]):
        raise PrimaryTableContinuationGoldError(
            "page_scope must be covered by base Gold"
        )

    renders_raw = source["canonical_page_renders"]
    if not isinstance(renders_raw, list) or len(renders_raw) != len(page_scope):
        raise PrimaryTableContinuationGoldError(
            "canonical_page_renders must bind every scoped page exactly once"
        )
    base_renders = {
        item["physical_page"]: item["canonical_render_sha256"]
        for item in base["source"]["canonical_page_renders"]
    }
    renders: list[dict[str, Any]] = []
    for index, raw_render in enumerate(renders_raw):
        name = f"source.canonical_page_renders[{index}]"
        render = _exact_keys(raw_render, _PAGE_RENDER_KEYS, name=name)
        page = _positive_int(
            f"{name}.physical_page", render["physical_page"], maximum=MAX_PAGES
        )
        digest = _sha256(
            f"{name}.canonical_render_sha256", render["canonical_render_sha256"]
        )
        if base_renders.get(page) != digest:
            raise PrimaryTableContinuationGoldError(
                "canonical render binding does not match base Gold"
            )
        renders.append(
            {"physical_page": page, "canonical_render_sha256": digest}
        )
    if [item["physical_page"] for item in renders] != page_scope:
        raise PrimaryTableContinuationGoldError(
            "canonical_page_renders must use page_scope order"
        )

    base_scopes = {scope["scope_id"]: scope for scope in base["reviewed_scopes"]}
    continuations_raw = root["reviewed_continuations"]
    if (
        not isinstance(continuations_raw, list)
        or not continuations_raw
        or len(continuations_raw) > MAX_CONTINUATIONS
    ):
        raise PrimaryTableContinuationGoldError(
            "reviewed_continuations must be bounded and non-empty"
        )
    continuations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    outgoing: dict[str, str] = {}
    incoming: dict[str, str] = {}
    endpoint_pages: set[int] = set()
    prior_key: tuple[int, str, str, str] | None = None
    for index, raw_item in enumerate(continuations_raw):
        name = f"reviewed_continuations[{index}]"
        item = _exact_keys(raw_item, _CONTINUATION_KEYS, name=name)
        continuation_id = _identifier(
            f"{name}.continuation_id", item["continuation_id"]
        )
        predecessor_id = _identifier(
            f"{name}.predecessor_scope_id", item["predecessor_scope_id"]
        )
        successor_id = _identifier(
            f"{name}.successor_scope_id", item["successor_scope_id"]
        )
        predecessor = base_scopes.get(predecessor_id)
        successor = base_scopes.get(successor_id)
        if predecessor is None or successor is None:
            raise PrimaryTableContinuationGoldError(
                f"{name} endpoint scope is absent from base Gold"
            )
        predecessor_page = predecessor["physical_page"]
        successor_page = successor["physical_page"]
        if successor_page != predecessor_page + 1:
            raise PrimaryTableContinuationGoldError(
                f"{name} endpoints must be on adjacent increasing pages"
            )
        if predecessor_page not in page_scope or successor_page not in page_scope:
            raise PrimaryTableContinuationGoldError(
                f"{name} endpoints are outside page_scope"
            )
        relation_kind = item["relation_kind"]
        if relation_kind != "between_rows":
            raise PrimaryTableContinuationGoldError(
                f"{name}.relation_kind must be between_rows"
            )
        canonical_key = (
            predecessor_page,
            predecessor_id,
            successor_id,
            continuation_id,
        )
        if continuation_id in seen_ids or (
            prior_key is not None and canonical_key <= prior_key
        ):
            raise PrimaryTableContinuationGoldError(
                "reviewed_continuations must have unique IDs in canonical order"
            )
        if predecessor_id in outgoing:
            raise PrimaryTableContinuationGoldError(
                "a predecessor scope may have only one continuation"
            )
        if successor_id in incoming:
            raise PrimaryTableContinuationGoldError(
                "a successor scope may have only one continuation"
            )
        _review_metadata(item, name=name)
        if item["review_status"] == "human_confirmed" and (
            predecessor["review_status"] != "human_confirmed"
            or successor["review_status"] != "human_confirmed"
        ):
            raise PrimaryTableContinuationGoldError(
                "confirmed continuation requires confirmed endpoint scopes"
            )

        predecessor_columns = list(predecessor["column_ids"])
        successor_columns = list(successor["column_ids"])
        mappings_raw = item["column_mapping"]
        if (
            not isinstance(mappings_raw, list)
            or not mappings_raw
            or len(mappings_raw) > MAX_COLUMNS_PER_CONTINUATION
        ):
            raise PrimaryTableContinuationGoldError(
                f"{name}.column_mapping must be bounded and non-empty"
            )
        mappings: list[dict[str, str]] = []
        seen_predecessor_columns: set[str] = set()
        seen_successor_columns: set[str] = set()
        for mapping_index, raw_mapping in enumerate(mappings_raw):
            mapping_name = f"{name}.column_mapping[{mapping_index}]"
            mapping = _exact_keys(
                raw_mapping, _COLUMN_MAPPING_KEYS, name=mapping_name
            )
            predecessor_column = _identifier(
                f"{mapping_name}.predecessor_column_id",
                mapping["predecessor_column_id"],
            )
            successor_column = _identifier(
                f"{mapping_name}.successor_column_id",
                mapping["successor_column_id"],
            )
            if (
                predecessor_column in seen_predecessor_columns
                or successor_column in seen_successor_columns
            ):
                raise PrimaryTableContinuationGoldError(
                    f"{name}.column_mapping must be bijective"
                )
            seen_predecessor_columns.add(predecessor_column)
            seen_successor_columns.add(successor_column)
            mappings.append(
                {
                    "predecessor_column_id": predecessor_column,
                    "successor_column_id": successor_column,
                }
            )
        if (
            [mapping["predecessor_column_id"] for mapping in mappings]
            != predecessor_columns
            or seen_successor_columns != set(successor_columns)
            or len(mappings) != len(successor_columns)
        ):
            raise PrimaryTableContinuationGoldError(
                f"{name}.column_mapping must be a total canonical bijection"
            )

        seen_ids.add(continuation_id)
        outgoing[predecessor_id] = successor_id
        incoming[successor_id] = predecessor_id
        endpoint_pages.update((predecessor_page, successor_page))
        prior_key = canonical_key
        continuations.append(
            {
                "continuation_id": continuation_id,
                "predecessor_scope_id": predecessor_id,
                "successor_scope_id": successor_id,
                "relation_kind": "between_rows",
                "column_mapping": mappings,
                "review_status": item["review_status"],
                "reviewer_ref": item["reviewer_ref"],
                "confirmed_at": item["confirmed_at"],
            }
        )

    if endpoint_pages != set(page_scope):
        raise PrimaryTableContinuationGoldError(
            "every page_scope page must be used by a continuation endpoint"
        )
    if _has_cycle(outgoing):
        raise PrimaryTableContinuationGoldError(
            "table continuations must not contain a directed cycle"
        )

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": notice_id,
        "source": {
            "source_pdf_sha256": source_pdf_sha256,
            "canonical_page_renders": renders,
            "native_capture": checked_capture,
            "base_table_grid_gold": {
                "schema_version": BASE_TABLE_GRID_GOLD_SCHEMA_VERSION,
                "canonical_sha256": base_sha256,
            },
        },
        "page_scope": page_scope,
        "reviewed_continuations": continuations,
    }
    _canonical_bytes(canonical)
    return canonical


def canonical_primary_table_continuation_gold_json(
    value: Mapping[str, Any],
    *,
    base_table_grid_gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
) -> bytes:
    """Return canonical UTF-8 JSON after strict cross-artifact validation."""

    return _canonical_bytes(
        validate_primary_table_continuation_gold(
            value, base_table_grid_gold=base_table_grid_gold
        )
    )


@dataclass(frozen=True, slots=True)
class PrimaryTableContinuationGoldFixture:
    """Immutable continuation Gold bound to immutable A4.3a Gold."""

    payload: Mapping[str, Any]
    base_table_grid_gold: PrimaryTableGridGoldFixture

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise PrimaryTableContinuationGoldError("Gold root must be an object")
        base = _base_fixture(self.base_table_grid_gold)
        canonical = validate_primary_table_continuation_gold(
            self.payload, base_table_grid_gold=base
        )
        object.__setattr__(self, "payload", _freeze(canonical))
        object.__setattr__(self, "base_table_grid_gold", base)

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        base_table_grid_gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
    ) -> "PrimaryTableContinuationGoldFixture":
        return cls(value, _base_fixture(base_table_grid_gold))

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
                item["review_status"] == "human_confirmed"
                for item in self.payload["reviewed_continuations"]
            )
            and self.base_table_grid_gold.has_trusted_confirmation
            and self.canonical_sha256
            in TRUSTED_CONFIRMED_TABLE_CONTINUATION_GOLD_SHA256S
        )

    @property
    def is_evaluable(self) -> bool:
        return False

    @property
    def quality_gate_status(self) -> str:
        if any(
            item["review_status"] == "pending_human_confirmation"
            for item in self.payload["reviewed_continuations"]
        ) or any(
            scope["review_status"] == "pending_human_confirmation"
            for scope in self.base_table_grid_gold.payload["reviewed_scopes"]
        ):
            return "not_evaluable_gold_pending"
        if not self.has_trusted_confirmation:
            return "not_evaluable_untrusted_confirmation"
        return "not_evaluable_input_replay_required"


@dataclass(frozen=True, slots=True, init=False)
class ReplayedPrimaryTableContinuationGold:
    """Cooperative receipt returned after complete base-Gold source replay."""

    fixture: PrimaryTableContinuationGoldFixture
    _replayed_canonical_sha256: str

    def __init__(
        self,
        fixture: PrimaryTableContinuationGoldFixture,
        *,
        _construction_token: object,
        replayed_canonical_sha256: str,
    ) -> None:
        if (
            _construction_token is not _REPLAY_CONSTRUCTION_TOKEN
            or type(fixture) is not PrimaryTableContinuationGoldFixture
            or replayed_canonical_sha256 != fixture.canonical_sha256
        ):
            raise PrimaryTableContinuationGoldError(
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
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrimaryTableContinuationGoldError(
                f"Gold contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PrimaryTableContinuationGoldError(
        f"Gold contains non-finite JSON number {value!r}"
    )


def _bounded_integer(value: str) -> int:
    if len(value.lstrip("-")) > 7:
        raise PrimaryTableContinuationGoldError(
            "Gold contains an oversized integer"
        )
    return int(value)


def _reject_float(_: str) -> float:
    raise PrimaryTableContinuationGoldError(
        "Gold must not contain floating-point numbers"
    )


def parse_primary_table_continuation_gold_bytes(
    raw: bytes,
    *,
    base_table_grid_gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
) -> PrimaryTableContinuationGoldFixture:
    """Parse exact canonical UTF-8 JSON with bounded fail-closed semantics."""

    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise PrimaryTableContinuationGoldError(
            "Gold exceeds the artifact byte cap"
        )
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PrimaryTableContinuationGoldError(
                "Gold must not include a UTF-8 BOM"
            )
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
            parse_int=_bounded_integer,
            parse_float=_reject_float,
        )
    except PrimaryTableContinuationGoldError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PrimaryTableContinuationGoldError(
            "Gold is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, Mapping):
        raise PrimaryTableContinuationGoldError("Gold root must be an object")
    fixture = PrimaryTableContinuationGoldFixture.from_dict(
        value, base_table_grid_gold=base_table_grid_gold
    )
    if raw != fixture.canonical_json():
        raise PrimaryTableContinuationGoldError(
            "Gold bytes are not canonical UTF-8 JSON"
        )
    return fixture


def load_primary_table_continuation_gold_file(
    path: str | Path,
    *,
    base_table_grid_gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
) -> PrimaryTableContinuationGoldFixture:
    """Read one regular canonical Gold file without following symlinks."""

    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PrimaryTableContinuationGoldError(
                "Gold input must be a regular non-symlink file"
            )
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise PrimaryTableContinuationGoldError(
                "Gold exceeds the artifact byte cap"
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
                raise PrimaryTableContinuationGoldError(
                    "Gold changed while opening"
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
            raise PrimaryTableContinuationGoldError(
                "Gold changed while being read"
            )
    except PrimaryTableContinuationGoldError:
        raise
    except OSError as error:
        raise PrimaryTableContinuationGoldError(
            "Gold input cannot be safely opened"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_primary_table_continuation_gold_bytes(
        raw, base_table_grid_gold=base_table_grid_gold
    )


def validate_primary_table_continuation_gold_against_inputs(
    gold: Mapping[str, Any] | PrimaryTableContinuationGoldFixture,
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    render_artifact_root: str | Path,
    base_table_grid_gold: Mapping[str, Any] | PrimaryTableGridGoldFixture,
) -> ReplayedPrimaryTableContinuationGold:
    """Replay the bound A4.3a Gold and then recheck every continuation."""

    base_fixture = _base_fixture(base_table_grid_gold)
    fixture = (
        gold
        if type(gold) is PrimaryTableContinuationGoldFixture
        else PrimaryTableContinuationGoldFixture.from_dict(
            gold, base_table_grid_gold=base_fixture
        )
    )
    if (
        type(fixture) is not PrimaryTableContinuationGoldFixture
        or fixture.base_table_grid_gold.canonical_sha256
        != base_fixture.canonical_sha256
    ):
        raise PrimaryTableContinuationGoldError(
            "continuation Gold fixture is bound to a different base Gold"
        )
    try:
        replayed_base = validate_primary_table_grid_gold_against_inputs(
            base_fixture,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
        )
    except PrimaryTableGridGoldError as error:
        raise PrimaryTableContinuationGoldError(
            "base table-grid Gold did not pass complete source replay"
        ) from error
    replayed_fixture = PrimaryTableContinuationGoldFixture.from_dict(
        fixture.to_dict(), base_table_grid_gold=replayed_base.fixture
    )
    if replayed_fixture.canonical_json() != fixture.canonical_json():
        raise PrimaryTableContinuationGoldError(
            "continuation Gold changed during source replay"
        )
    return ReplayedPrimaryTableContinuationGold(
        replayed_fixture,
        _construction_token=_REPLAY_CONSTRUCTION_TOKEN,
        replayed_canonical_sha256=replayed_fixture.canonical_sha256,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "PrimaryTableContinuationGoldError",
    "PrimaryTableContinuationGoldFixture",
    "ReplayedPrimaryTableContinuationGold",
    "SCHEMA_VERSION",
    "TRUSTED_CONFIRMED_TABLE_CONTINUATION_GOLD_SHA256S",
    "canonical_primary_table_continuation_gold_json",
    "load_primary_table_continuation_gold_file",
    "parse_primary_table_continuation_gold_bytes",
    "validate_primary_table_continuation_gold",
    "validate_primary_table_continuation_gold_against_inputs",
]
