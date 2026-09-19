"""Strict, textless partial Gold for PDF heading-to-body relations.

``pdf_primary_heading_relation_gold/v1`` is an additive review artifact.  It
does not classify candidate leaves and never contains candidate leaf IDs.
Each expected relation identifies one native heading occurrence and one
paragraph group from a bound ``pdf_primary_structure_gold/v1`` artifact.

Standalone parsing requires that base Gold so the cross-artifact references
are checked immediately.  Crossing a trust boundary additionally requires a
full replay of the base Gold against the source PDF, native capture, and
canonical page renders.
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

from .primary_structure_gold import (
    SCHEMA_VERSION as PRIMARY_STRUCTURE_GOLD_SCHEMA_VERSION,
    PrimaryStructureGoldError,
    PrimaryStructureGoldFixture,
    validate_primary_structure_gold_against_inputs,
)
from .render_manifest import PdfRenderManifest


SCHEMA_VERSION = "pdf_primary_heading_relation_gold/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 50_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_TEXT_ITEM_INDEX = 499_999
MAX_REVIEWED_SCOPES = 256
MAX_RELATIONS_PER_SCOPE = 1_024
TRUSTED_CONFIRMED_HEADING_RELATION_GOLD_SHA256S: frozenset[str] = frozenset(
    {"6bc14a5bc1802fc4e5752795f875dd8b61d95fc04cca4e663923791fd8dd83d5"}
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
    {
        "source_pdf_sha256",
        "canonical_page_renders",
        "native_capture",
        "primary_structure_gold",
    }
)
_PAGE_RENDER_KEYS = frozenset({"physical_page", "canonical_render_sha256"})
_NATIVE_CAPTURE_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "extractor_version"}
)
_BASE_GOLD_KEYS = frozenset({"schema_version", "canonical_sha256"})
_SCOPE_KEYS = frozenset(
    {
        "scope_id",
        "physical_page",
        "base_structure_scope_id",
        "review_status",
        "reviewer_ref",
        "confirmed_at",
        "expected_relations",
    }
)
_RELATION_KEYS = frozenset({"heading_occurrence_id", "body_group_id"})
_REVIEW_STATUSES = frozenset(
    {"pending_human_confirmation", "human_confirmed"}
)


class PrimaryHeadingRelationGoldError(ValueError):
    """Raised when heading-relation Gold is unsafe or ambiguous."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrimaryHeadingRelationGoldError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PrimaryHeadingRelationGoldError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        if extra:
            detail.append("unexpected keys: " + ", ".join(extra))
        raise PrimaryHeadingRelationGoldError(
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
        raise PrimaryHeadingRelationGoldError(f"{name} must be a string")
    if (not allow_empty and not value) or value != value.strip():
        raise PrimaryHeadingRelationGoldError(f"{name} must be a trimmed string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PrimaryHeadingRelationGoldError(
            f"{name} must not contain surrogate code points"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PrimaryHeadingRelationGoldError(
            f"{name} exceeds its string safety cap"
        )
    return value


def _identifier(name: str, value: object) -> str:
    checked = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(checked) is None:
        raise PrimaryHeadingRelationGoldError(
            f"{name} is not a bounded identifier"
        )
    return checked


def _sha256(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if len(checked) != 64 or any(character not in _SHA for character in checked):
        raise PrimaryHeadingRelationGoldError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return checked


def _positive_int(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PrimaryHeadingRelationGoldError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _occurrence_parts(name: str, value: object) -> tuple[str, int, int]:
    occurrence_id = _string(name, value, maximum=64)
    if _OCCURRENCE.fullmatch(occurrence_id) is None:
        raise PrimaryHeadingRelationGoldError(
            f"{name} is not a bounded occurrence ID"
        )
    page_and_index = occurrence_id.removeprefix("occ:inspector:p").split(
        ":t", 1
    )
    page, item_index = int(page_and_index[0]), int(page_and_index[1])
    if page > MAX_PAGES or item_index > MAX_TEXT_ITEM_INDEX:
        raise PrimaryHeadingRelationGoldError(f"{name} exceeds occurrence bounds")
    return occurrence_id, page, item_index


def _strictly_increasing(values: Sequence[int]) -> bool:
    return all(left < right for left, right in zip(values, values[1:]))


def _assert_json_limits(
    value: object, *, depth: int = 1, counter: list[int] | None = None
) -> None:
    counter = [0] if counter is None else counter
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise PrimaryHeadingRelationGoldError("JSON exceeds the node safety cap")
    if depth > MAX_JSON_DEPTH:
        raise PrimaryHeadingRelationGoldError("JSON exceeds the nesting depth cap")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PrimaryHeadingRelationGoldError(
                "JSON contains a non-finite number"
            )
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
    raise PrimaryHeadingRelationGoldError(
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
        raise PrimaryHeadingRelationGoldError(
            "heading-relation Gold is not canonically serializable UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PrimaryHeadingRelationGoldError(
            "heading-relation Gold exceeds the artifact byte cap"
        )
    return encoded


def _review_metadata(scope: Mapping[str, Any], *, name: str) -> None:
    status = scope["review_status"]
    if status not in _REVIEW_STATUSES:
        raise PrimaryHeadingRelationGoldError(
            f"{name}.review_status is unsupported"
        )
    reviewer_ref = scope["reviewer_ref"]
    confirmed_at = scope["confirmed_at"]
    if status == "pending_human_confirmation":
        if reviewer_ref is not None or confirmed_at is not None:
            raise PrimaryHeadingRelationGoldError(
                f"{name} pending review metadata must be null"
            )
        return
    if (
        not isinstance(reviewer_ref, str)
        or _REVIEWER_REF.fullmatch(reviewer_ref) is None
    ):
        raise PrimaryHeadingRelationGoldError(
            f"{name}.reviewer_ref must be a non-identifying reviewer reference"
        )
    timestamp = _string(f"{name}.confirmed_at", confirmed_at, maximum=20)
    if _UTC_TIMESTAMP.fullmatch(timestamp) is None:
        raise PrimaryHeadingRelationGoldError(
            f"{name}.confirmed_at must be second-precision RFC3339 UTC"
        )
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise PrimaryHeadingRelationGoldError(
            f"{name}.confirmed_at is not a real UTC timestamp"
        ) from error


def _base_fixture(
    value: Mapping[str, Any] | PrimaryStructureGoldFixture,
) -> PrimaryStructureGoldFixture:
    try:
        return (
            value
            if type(value) is PrimaryStructureGoldFixture
            else PrimaryStructureGoldFixture.from_dict(value)
        )
    except PrimaryStructureGoldError as error:
        raise PrimaryHeadingRelationGoldError(
            "base primary-structure Gold is invalid"
        ) from error


def validate_primary_heading_relation_gold(
    value: Mapping[str, Any],
    *,
    primary_structure_gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
) -> dict[str, Any]:
    """Validate v1 shape and every reference into the bound base Gold."""

    base_fixture = _base_fixture(primary_structure_gold)
    base = base_fixture.to_dict()
    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="heading-relation Gold")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PrimaryHeadingRelationGoldError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PrimaryHeadingRelationGoldError(
            "heading-relation Gold must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PrimaryHeadingRelationGoldError(
            "standalone validation must disclose internal-consistency-only scope"
        )
    notice_id = _string("notice_id", root["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PrimaryHeadingRelationGoldError(
            "notice_id must use the canonical PBLN_ plus 15 digits form"
        )
    if notice_id != base["notice_id"]:
        raise PrimaryHeadingRelationGoldError("notice_id does not match base Gold")

    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    source_pdf_sha256 = _sha256(
        "source.source_pdf_sha256", source["source_pdf_sha256"]
    )
    if source_pdf_sha256 != base["source"]["source_pdf_sha256"]:
        raise PrimaryHeadingRelationGoldError(
            "source PDF binding does not match base Gold"
        )
    capture = _exact_keys(
        source["native_capture"],
        _NATIVE_CAPTURE_KEYS,
        name="source.native_capture",
    )
    checked_capture = {
        "schema_version": _string(
            "source.native_capture.schema_version",
            capture["schema_version"],
            maximum=64,
        ),
        "canonical_sha256": _sha256(
            "source.native_capture.canonical_sha256",
            capture["canonical_sha256"],
        ),
        "extractor_version": _string(
            "source.native_capture.extractor_version",
            capture["extractor_version"],
            maximum=64,
        ),
    }
    if checked_capture != base["source"]["native_capture"]:
        raise PrimaryHeadingRelationGoldError(
            "native capture binding does not match base Gold"
        )
    base_reference = _exact_keys(
        source["primary_structure_gold"],
        _BASE_GOLD_KEYS,
        name="source.primary_structure_gold",
    )
    if base_reference["schema_version"] != PRIMARY_STRUCTURE_GOLD_SCHEMA_VERSION:
        raise PrimaryHeadingRelationGoldError(
            "base Gold schema binding is unsupported"
        )
    base_sha256 = _sha256(
        "source.primary_structure_gold.canonical_sha256",
        base_reference["canonical_sha256"],
    )
    if base_sha256 != base_fixture.canonical_sha256:
        raise PrimaryHeadingRelationGoldError(
            "base Gold canonical hash binding mismatch"
        )

    page_scope_raw = root["page_scope"]
    if (
        not isinstance(page_scope_raw, list)
        or not page_scope_raw
        or len(page_scope_raw) > MAX_PAGES
    ):
        raise PrimaryHeadingRelationGoldError(
            "page_scope must be a bounded non-empty list"
        )
    page_scope = [
        _positive_int(f"page_scope[{index}]", page, maximum=MAX_PAGES)
        for index, page in enumerate(page_scope_raw)
    ]
    if not _strictly_increasing(page_scope):
        raise PrimaryHeadingRelationGoldError(
            "page_scope must be unique and ascending"
        )
    if not set(page_scope).issubset(base["page_scope"]):
        raise PrimaryHeadingRelationGoldError(
            "page_scope must be covered by base Gold"
        )

    renders_raw = source["canonical_page_renders"]
    if not isinstance(renders_raw, list) or len(renders_raw) != len(page_scope):
        raise PrimaryHeadingRelationGoldError(
            "canonical_page_renders must bind every scoped page exactly once"
        )
    base_renders = {
        render["physical_page"]: render["canonical_render_sha256"]
        for render in base["source"]["canonical_page_renders"]
    }
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
        canonical_render_sha256 = _sha256(
            f"canonical_page_renders[{index}].canonical_render_sha256",
            render["canonical_render_sha256"],
        )
        if base_renders.get(physical_page) != canonical_render_sha256:
            raise PrimaryHeadingRelationGoldError(
                "canonical page-render binding does not match base Gold"
            )
        renders.append(
            {
                "physical_page": physical_page,
                "canonical_render_sha256": canonical_render_sha256,
            }
        )
    if [render["physical_page"] for render in renders] != page_scope:
        raise PrimaryHeadingRelationGoldError(
            "canonical_page_renders must use page_scope order without gaps"
        )

    base_scopes = {scope["scope_id"]: scope for scope in base["reviewed_scopes"]}
    scopes_raw = root["reviewed_scopes"]
    if (
        not isinstance(scopes_raw, list)
        or not scopes_raw
        or len(scopes_raw) > MAX_REVIEWED_SCOPES
    ):
        raise PrimaryHeadingRelationGoldError(
            "reviewed_scopes must be a bounded non-empty list"
        )
    scopes: list[dict[str, Any]] = []
    seen_scope_ids: set[str] = set()
    seen_base_scope_ids: set[str] = set()
    seen_heading_occurrences: set[str] = set()
    prior_scope_key: tuple[int, str] | None = None
    seen_scope_pages: set[int] = set()
    for scope_index, raw_scope in enumerate(scopes_raw):
        name = f"reviewed_scopes[{scope_index}]"
        scope = _exact_keys(raw_scope, _SCOPE_KEYS, name=name)
        scope_id = _identifier(f"{name}.scope_id", scope["scope_id"])
        physical_page = _positive_int(
            f"{name}.physical_page", scope["physical_page"], maximum=MAX_PAGES
        )
        if physical_page not in page_scope:
            raise PrimaryHeadingRelationGoldError(f"{name} is outside page_scope")
        scope_key = (physical_page, scope_id)
        if scope_id in seen_scope_ids or (
            prior_scope_key is not None and scope_key <= prior_scope_key
        ):
            raise PrimaryHeadingRelationGoldError(
                "reviewed_scopes must have unique IDs and canonical page/ID order"
            )
        seen_scope_ids.add(scope_id)
        prior_scope_key = scope_key
        seen_scope_pages.add(physical_page)

        base_scope_id = _identifier(
            f"{name}.base_structure_scope_id",
            scope["base_structure_scope_id"],
        )
        base_scope = base_scopes.get(base_scope_id)
        if base_scope is None or base_scope["physical_page"] != physical_page:
            raise PrimaryHeadingRelationGoldError(
                f"{name} must reference a same-page base reviewed scope"
            )
        if base_scope_id in seen_base_scope_ids:
            raise PrimaryHeadingRelationGoldError(
                "a base reviewed scope may be adjudicated only once"
            )
        seen_base_scope_ids.add(base_scope_id)
        _review_metadata(scope, name=name)
        if (
            scope["review_status"] == "human_confirmed"
            and base_scope["review_status"] != "human_confirmed"
        ):
            raise PrimaryHeadingRelationGoldError(
                "confirmed heading Gold requires confirmed base Gold"
            )

        groups = {
            group["group_id"]: frozenset(group["occurrence_ids"])
            for group in base_scope["ordered_groups"]
        }
        hard_negatives = {
            (negative["occurrence_id"], negative["target_group_id"])
            for negative in base_scope["hard_negatives"]
            if negative["kind"] == "forbidden_same_leaf"
        }
        relations_raw = scope["expected_relations"]
        if (
            not isinstance(relations_raw, list)
            or not relations_raw
            or len(relations_raw) > MAX_RELATIONS_PER_SCOPE
        ):
            raise PrimaryHeadingRelationGoldError(
                f"{name}.expected_relations must be bounded and non-empty"
            )
        relations: list[dict[str, str]] = []
        seen_body_groups: set[str] = set()
        prior_relation_key: tuple[int, str] | None = None
        for relation_index, raw_relation in enumerate(relations_raw):
            relation_name = f"{name}.expected_relations[{relation_index}]"
            relation = _exact_keys(
                raw_relation, _RELATION_KEYS, name=relation_name
            )
            heading, page, source_index = _occurrence_parts(
                f"{relation_name}.heading_occurrence_id",
                relation["heading_occurrence_id"],
            )
            body_group_id = _identifier(
                f"{relation_name}.body_group_id", relation["body_group_id"]
            )
            body_members = groups.get(body_group_id)
            if page != physical_page or heading not in base_scope["occurrence_ids"]:
                raise PrimaryHeadingRelationGoldError(
                    f"{relation_name} heading must be in the base reviewed scope"
                )
            if body_members is None:
                raise PrimaryHeadingRelationGoldError(
                    f"{relation_name} body group is absent from the base scope"
                )
            if heading in body_members:
                raise PrimaryHeadingRelationGoldError(
                    f"{relation_name} heading must remain outside its body group"
                )
            if (heading, body_group_id) not in hard_negatives:
                raise PrimaryHeadingRelationGoldError(
                    f"{relation_name} requires a forbidden_same_leaf base hard negative"
                )
            relation_key = (source_index, body_group_id)
            if prior_relation_key is not None and relation_key <= prior_relation_key:
                raise PrimaryHeadingRelationGoldError(
                    f"{name}.expected_relations must use canonical source/group order"
                )
            if heading in seen_heading_occurrences:
                raise PrimaryHeadingRelationGoldError(
                    "a heading occurrence may be adjudicated only once"
                )
            if body_group_id in seen_body_groups:
                raise PrimaryHeadingRelationGoldError(
                    f"{name} body group may have only one heading in v1"
                )
            prior_relation_key = relation_key
            seen_heading_occurrences.add(heading)
            seen_body_groups.add(body_group_id)
            relations.append(
                {
                    "heading_occurrence_id": heading,
                    "body_group_id": body_group_id,
                }
            )

        scopes.append(
            {
                "scope_id": scope_id,
                "physical_page": physical_page,
                "base_structure_scope_id": base_scope_id,
                "review_status": scope["review_status"],
                "reviewer_ref": scope["reviewer_ref"],
                "confirmed_at": scope["confirmed_at"],
                "expected_relations": relations,
            }
        )

    if seen_scope_pages != set(page_scope):
        raise PrimaryHeadingRelationGoldError(
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
            "native_capture": checked_capture,
            "primary_structure_gold": {
                "schema_version": PRIMARY_STRUCTURE_GOLD_SCHEMA_VERSION,
                "canonical_sha256": base_sha256,
            },
        },
        "page_scope": page_scope,
        "reviewed_scopes": scopes,
    }
    _canonical_bytes(canonical)
    return canonical


def canonical_primary_heading_relation_gold_json(
    value: Mapping[str, Any],
    *,
    primary_structure_gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
) -> bytes:
    """Return canonical UTF-8 JSON after strict cross-artifact validation."""

    return _canonical_bytes(
        validate_primary_heading_relation_gold(
            value, primary_structure_gold=primary_structure_gold
        )
    )


@dataclass(frozen=True, slots=True)
class PrimaryHeadingRelationGoldFixture:
    """Immutable heading-relation Gold bound to immutable base Gold."""

    payload: Mapping[str, Any]
    primary_structure_gold: PrimaryStructureGoldFixture

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise PrimaryHeadingRelationGoldError("Gold root must be an object")
        base = _base_fixture(self.primary_structure_gold)
        canonical = validate_primary_heading_relation_gold(
            self.payload, primary_structure_gold=base
        )
        object.__setattr__(self, "payload", _freeze(canonical))
        object.__setattr__(self, "primary_structure_gold", base)

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        primary_structure_gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
    ) -> "PrimaryHeadingRelationGoldFixture":
        return cls(value, _base_fixture(primary_structure_gold))

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
            and self.primary_structure_gold.has_trusted_confirmation
            and self.canonical_sha256
            in TRUSTED_CONFIRMED_HEADING_RELATION_GOLD_SHA256S
        )

    @property
    def is_evaluable(self) -> bool:
        """A standalone fixture is never evaluable before artifact replay."""

        return False

    @property
    def quality_gate_status(self) -> str:
        if any(
            scope["review_status"] == "pending_human_confirmation"
            for scope in self.payload["reviewed_scopes"]
        ) or any(
            scope["review_status"] == "pending_human_confirmation"
            for scope in self.primary_structure_gold.payload["reviewed_scopes"]
        ):
            return "not_evaluable_gold_pending"
        if not self.has_trusted_confirmation:
            return "not_evaluable_untrusted_confirmation"
        return "not_evaluable_input_replay_required"


@dataclass(frozen=True, slots=True, init=False)
class ReplayedPrimaryHeadingRelationGold:
    """Cooperative receipt returned after complete base-Gold source replay."""

    fixture: PrimaryHeadingRelationGoldFixture
    _replayed_canonical_sha256: str

    def __init__(
        self,
        fixture: PrimaryHeadingRelationGoldFixture,
        *,
        _construction_token: object,
        replayed_canonical_sha256: str,
    ) -> None:
        if (
            _construction_token is not _REPLAY_CONSTRUCTION_TOKEN
            or type(fixture) is not PrimaryHeadingRelationGoldFixture
            or replayed_canonical_sha256 != fixture.canonical_sha256
        ):
            raise PrimaryHeadingRelationGoldError(
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
            raise PrimaryHeadingRelationGoldError(
                f"Gold contains duplicate JSON key {key!r}"
            )
        value[key] = nested
    return value


def _reject_constant(value: str) -> None:
    raise PrimaryHeadingRelationGoldError(
        f"Gold contains non-finite JSON number {value!r}"
    )


def parse_primary_heading_relation_gold_bytes(
    raw: bytes,
    *,
    primary_structure_gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
) -> PrimaryHeadingRelationGoldFixture:
    """Parse exact canonical UTF-8 JSON with bounded fail-closed semantics."""

    if not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES:
        raise PrimaryHeadingRelationGoldError("Gold exceeds the artifact byte cap")
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PrimaryHeadingRelationGoldError(
                "Gold must not include a UTF-8 BOM"
            )
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
        )
    except PrimaryHeadingRelationGoldError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise PrimaryHeadingRelationGoldError(
            "Gold is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, Mapping):
        raise PrimaryHeadingRelationGoldError("Gold root must be an object")
    fixture = PrimaryHeadingRelationGoldFixture.from_dict(
        value, primary_structure_gold=primary_structure_gold
    )
    if raw != fixture.canonical_json():
        raise PrimaryHeadingRelationGoldError(
            "Gold bytes are not canonical UTF-8 JSON"
        )
    return fixture


def load_primary_heading_relation_gold_file(
    path: str | Path,
    *,
    primary_structure_gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
) -> PrimaryHeadingRelationGoldFixture:
    """Read one regular canonical Gold file without following symlinks."""

    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PrimaryHeadingRelationGoldError(
                "Gold input must be a regular non-symlink file"
            )
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise PrimaryHeadingRelationGoldError(
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
                raise PrimaryHeadingRelationGoldError("Gold changed while opening")
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
            raise PrimaryHeadingRelationGoldError("Gold changed while reading")
    except PrimaryHeadingRelationGoldError:
        raise
    except OSError as error:
        raise PrimaryHeadingRelationGoldError(
            "Gold input cannot be safely opened"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_primary_heading_relation_gold_bytes(
        raw, primary_structure_gold=primary_structure_gold
    )


def validate_primary_heading_relation_gold_against_inputs(
    gold: Mapping[str, Any] | PrimaryHeadingRelationGoldFixture,
    *,
    primary_structure_gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    render_artifact_root: str | Path,
) -> ReplayedPrimaryHeadingRelationGold:
    """Replay base Gold from source inputs, then recheck every relation."""

    base_fixture = _base_fixture(primary_structure_gold)
    fixture = (
        gold
        if type(gold) is PrimaryHeadingRelationGoldFixture
        else PrimaryHeadingRelationGoldFixture.from_dict(
            gold, primary_structure_gold=base_fixture
        )
    )
    if (
        type(fixture) is not PrimaryHeadingRelationGoldFixture
        or fixture.primary_structure_gold.canonical_sha256
        != base_fixture.canonical_sha256
    ):
        raise PrimaryHeadingRelationGoldError(
            "heading-relation Gold fixture is bound to a different base Gold"
        )
    try:
        replayed_base = validate_primary_structure_gold_against_inputs(
            base_fixture,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=render_artifact_root,
        )
    except PrimaryStructureGoldError as error:
        raise PrimaryHeadingRelationGoldError(
            "base Gold did not pass complete source replay"
        ) from error
    replayed_fixture = PrimaryHeadingRelationGoldFixture.from_dict(
        fixture.to_dict(), primary_structure_gold=replayed_base.fixture
    )
    if replayed_fixture.canonical_json() != fixture.canonical_json():
        raise PrimaryHeadingRelationGoldError(
            "heading-relation Gold changed during source replay"
        )
    return ReplayedPrimaryHeadingRelationGold(
        replayed_fixture,
        _construction_token=_REPLAY_CONSTRUCTION_TOKEN,
        replayed_canonical_sha256=replayed_fixture.canonical_sha256,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "PrimaryHeadingRelationGoldError",
    "PrimaryHeadingRelationGoldFixture",
    "ReplayedPrimaryHeadingRelationGold",
    "SCHEMA_VERSION",
    "TRUSTED_CONFIRMED_HEADING_RELATION_GOLD_SHA256S",
    "canonical_primary_heading_relation_gold_json",
    "load_primary_heading_relation_gold_file",
    "parse_primary_heading_relation_gold_bytes",
    "validate_primary_heading_relation_gold",
    "validate_primary_heading_relation_gold_against_inputs",
]
