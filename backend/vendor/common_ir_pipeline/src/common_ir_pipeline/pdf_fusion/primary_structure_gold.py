"""Strict, textless partial Gold for PDF primary-structure evaluation.

``pdf_primary_structure_gold/v1`` is deliberately narrower than a document
model.  It records only a reviewed occurrence scope, ordered paragraph
groups, the join class between adjacent occurrences, and explicit
same-leaf hard negatives.  It never records source text, reconstructed text,
or a raw separator.

The artifact is evaluation-only.  A scope that has not been confirmed by a
human remains useful for canonical replay and diagnostics, but it is never
eligible to pass a quality gate.
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


SCHEMA_VERSION = "pdf_primary_structure_gold/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 50_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_TEXT_ITEM_INDEX = 499_999
MAX_REVIEWED_SCOPES = 256
MAX_OCCURRENCES_PER_SCOPE = 4_096
MAX_GROUPS_PER_SCOPE = 1_024
MAX_OCCURRENCES_PER_GROUP = 4_096
MAX_HARD_NEGATIVES_PER_SCOPE = 4_096
MAX_RENDER_MANIFEST_TOTAL_BYTES = 512 * 1024 * 1024
# Human confirmation recorded inside a Gold artifact is a claim, not a trust
# anchor.  A confirmed artifact becomes quality-gate eligible only after its
# canonical digest is added here by a separately reviewed code change.
TRUSTED_CONFIRMED_GOLD_SHA256S: frozenset[str] = frozenset(
    {"2e80529bb500c3947a1121a592483164ce50365e321f352891fdf660d1f362ea"}
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
_PRINTED_PAGE_LABEL = re.compile(r"^(?:[0-9]{1,4}|[IVXLCDMivxlcdm]{1,16})$")
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
        "printed_page_label_status",
        "printed_page_label",
        "review_status",
        "reviewer_ref",
        "confirmed_at",
        "occurrence_ids",
        "ordered_groups",
        "hard_negatives",
    }
)
_GROUP_KEYS = frozenset({"group_id", "kind", "occurrence_ids", "boundaries"})
_BOUNDARY_KEYS = frozenset(
    {"left_occurrence_id", "right_occurrence_id", "join_class"}
)
_HARD_NEGATIVE_KEYS = frozenset(
    {"kind", "occurrence_id", "target_group_id"}
)

_REVIEW_STATUSES = frozenset(
    {"pending_human_confirmation", "human_confirmed"}
)
_JOIN_CLASSES = frozenset({"intra_word_wrap", "inter_token_space"})
_PRINTED_LABEL_STATUSES = frozenset({"identified", "absent"})


class PrimaryStructureGoldError(ValueError):
    """Raised when primary-structure Gold is unsafe or ambiguous."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrimaryStructureGoldError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PrimaryStructureGoldError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        if extra:
            detail.append("unexpected keys: " + ", ".join(extra))
        raise PrimaryStructureGoldError(
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
        raise PrimaryStructureGoldError(f"{name} must be a string")
    if (not allow_empty and not value) or value != value.strip():
        raise PrimaryStructureGoldError(f"{name} must be a trimmed string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PrimaryStructureGoldError(
            f"{name} must not contain surrogate code points"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PrimaryStructureGoldError(f"{name} exceeds its string safety cap")
    return value


def _identifier(name: str, value: object) -> str:
    checked = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(checked) is None:
        raise PrimaryStructureGoldError(f"{name} is not a bounded identifier")
    return checked


def _sha256(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if len(checked) != 64 or any(character not in _SHA for character in checked):
        raise PrimaryStructureGoldError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return checked


def _positive_int(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PrimaryStructureGoldError(
            f"{name} must be an integer from 1 to {maximum}"
        )
    return value


def _occurrence_parts(name: str, value: object) -> tuple[str, int, int]:
    occurrence_id = _string(name, value, maximum=64)
    if _OCCURRENCE.fullmatch(occurrence_id) is None:
        raise PrimaryStructureGoldError(f"{name} is not a bounded occurrence ID")
    page_and_index = occurrence_id.removeprefix("occ:inspector:p").split(":t", 1)
    page, item_index = int(page_and_index[0]), int(page_and_index[1])
    if page > MAX_PAGES or item_index > MAX_TEXT_ITEM_INDEX:
        raise PrimaryStructureGoldError(f"{name} exceeds occurrence bounds")
    return occurrence_id, page, item_index


def _strictly_increasing(values: Sequence[int]) -> bool:
    return all(left < right for left, right in zip(values, values[1:]))


def _assert_json_limits(
    value: object, *, depth: int = 1, counter: list[int] | None = None
) -> None:
    counter = [0] if counter is None else counter
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise PrimaryStructureGoldError("JSON exceeds the node safety cap")
    if depth > MAX_JSON_DEPTH:
        raise PrimaryStructureGoldError("JSON exceeds the nesting depth cap")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PrimaryStructureGoldError("JSON contains a non-finite number")
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
    raise PrimaryStructureGoldError(
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
        raise PrimaryStructureGoldError(
            "Gold is not canonically serializable UTF-8 JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PrimaryStructureGoldError("Gold exceeds the artifact byte cap")
    return encoded


def _review_metadata(scope: Mapping[str, Any], *, name: str) -> None:
    status = scope["review_status"]
    if status not in _REVIEW_STATUSES:
        raise PrimaryStructureGoldError(f"{name}.review_status is unsupported")
    reviewer_ref = scope["reviewer_ref"]
    confirmed_at = scope["confirmed_at"]
    if status == "pending_human_confirmation":
        if reviewer_ref is not None or confirmed_at is not None:
            raise PrimaryStructureGoldError(
                f"{name} pending review metadata must be null"
            )
        return

    if not isinstance(reviewer_ref, str) or _REVIEWER_REF.fullmatch(reviewer_ref) is None:
        raise PrimaryStructureGoldError(
            f"{name}.reviewer_ref must be a non-identifying reviewer reference"
        )
    timestamp = _string(f"{name}.confirmed_at", confirmed_at, maximum=20)
    if _UTC_TIMESTAMP.fullmatch(timestamp) is None:
        raise PrimaryStructureGoldError(
            f"{name}.confirmed_at must be second-precision RFC3339 UTC"
        )
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise PrimaryStructureGoldError(
            f"{name}.confirmed_at is not a real UTC timestamp"
        ) from error


def validate_primary_structure_gold(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a detached canonical-value copy of v1 Gold.

    This proves only internal consistency.  Use
    :func:`validate_primary_structure_gold_against_inputs` before trusting
    persisted Gold across an artifact boundary.
    """

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="Gold")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PrimaryStructureGoldError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PrimaryStructureGoldError(
            "primary-structure Gold must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PrimaryStructureGoldError(
            "standalone validation must disclose internal-consistency-only scope"
        )
    notice_id = _string("notice_id", root["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PrimaryStructureGoldError(
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
        raise PrimaryStructureGoldError(
            "source.native_capture.schema_version is not the strict native contract"
        )
    native_capture_sha256 = _sha256(
        "source.native_capture.canonical_sha256", capture["canonical_sha256"]
    )
    if capture["extractor_version"] != PINNED_PDF_INSPECTOR_VERSION:
        raise PrimaryStructureGoldError(
            "source.native_capture.extractor_version is not the pinned extractor"
        )

    page_scope_raw = root["page_scope"]
    if (
        not isinstance(page_scope_raw, list)
        or not page_scope_raw
        or len(page_scope_raw) > MAX_PAGES
    ):
        raise PrimaryStructureGoldError("page_scope must be a bounded non-empty list")
    page_scope = [
        _positive_int(f"page_scope[{index}]", page, maximum=MAX_PAGES)
        for index, page in enumerate(page_scope_raw)
    ]
    if not _strictly_increasing(page_scope):
        raise PrimaryStructureGoldError("page_scope must be unique and ascending")

    renders_raw = source["canonical_page_renders"]
    if not isinstance(renders_raw, list) or len(renders_raw) != len(page_scope):
        raise PrimaryStructureGoldError(
            "canonical_page_renders must bind every scoped page exactly once"
        )
    renders: list[dict[str, Any]] = []
    for index, raw_render in enumerate(renders_raw):
        render = _exact_keys(
            raw_render, _PAGE_RENDER_KEYS, name=f"canonical_page_renders[{index}]"
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
    if [item["physical_page"] for item in renders] != page_scope:
        raise PrimaryStructureGoldError(
            "canonical_page_renders must use page_scope order without gaps"
        )

    scopes_raw = root["reviewed_scopes"]
    if (
        not isinstance(scopes_raw, list)
        or not scopes_raw
        or len(scopes_raw) > MAX_REVIEWED_SCOPES
    ):
        raise PrimaryStructureGoldError(
            "reviewed_scopes must be a bounded non-empty list"
        )
    scopes: list[dict[str, Any]] = []
    seen_scope_ids: set[str] = set()
    prior_scope_key: tuple[int, str] | None = None
    seen_scope_pages: set[int] = set()
    reviewed_occurrence_owners: set[str] = set()
    for scope_index, raw_scope in enumerate(scopes_raw):
        name = f"reviewed_scopes[{scope_index}]"
        scope = _exact_keys(raw_scope, _SCOPE_KEYS, name=name)
        scope_id = _identifier(f"{name}.scope_id", scope["scope_id"])
        physical_page = _positive_int(
            f"{name}.physical_page", scope["physical_page"], maximum=MAX_PAGES
        )
        if physical_page not in page_scope:
            raise PrimaryStructureGoldError(f"{name} is outside page_scope")
        scope_key = (physical_page, scope_id)
        if scope_id in seen_scope_ids or (
            prior_scope_key is not None and scope_key <= prior_scope_key
        ):
            raise PrimaryStructureGoldError(
                "reviewed_scopes must have unique IDs and canonical page/ID order"
            )
        prior_scope_key = scope_key
        seen_scope_ids.add(scope_id)
        seen_scope_pages.add(physical_page)

        label_status = scope["printed_page_label_status"]
        printed_label = scope["printed_page_label"]
        if label_status not in _PRINTED_LABEL_STATUSES:
            raise PrimaryStructureGoldError(
                f"{name}.printed_page_label_status is unsupported"
            )
        if label_status == "absent":
            if printed_label is not None:
                raise PrimaryStructureGoldError(
                    f"{name}.printed_page_label must be null when absent"
                )
        else:
            checked_label = _string(
                f"{name}.printed_page_label", printed_label, maximum=16
            )
            if _PRINTED_PAGE_LABEL.fullmatch(checked_label) is None:
                raise PrimaryStructureGoldError(
                    f"{name}.printed_page_label must be a normalized numeric or Roman page label"
                )
            printed_label = checked_label

        _review_metadata(scope, name=name)

        occurrence_values = scope["occurrence_ids"]
        if (
            not isinstance(occurrence_values, list)
            or not occurrence_values
            or len(occurrence_values) > MAX_OCCURRENCES_PER_SCOPE
        ):
            raise PrimaryStructureGoldError(
                f"{name}.occurrence_ids must be a bounded non-empty list"
            )
        occurrences: list[str] = []
        source_indices: list[int] = []
        for occurrence_index, raw_occurrence in enumerate(occurrence_values):
            occurrence_id, page, item_index = _occurrence_parts(
                f"{name}.occurrence_ids[{occurrence_index}]", raw_occurrence
            )
            if page != physical_page:
                raise PrimaryStructureGoldError(
                    f"{name}.occurrence_ids must remain on the scope page"
                )
            occurrences.append(occurrence_id)
            source_indices.append(item_index)
        if not _strictly_increasing(source_indices):
            raise PrimaryStructureGoldError(
                f"{name}.occurrence_ids must be unique and in native source order"
            )
        scope_occurrence_set = set(occurrences)
        if reviewed_occurrence_owners.intersection(scope_occurrence_set):
            raise PrimaryStructureGoldError(
                "an occurrence cannot be adjudicated by two reviewed scopes"
            )
        reviewed_occurrence_owners.update(scope_occurrence_set)

        groups_raw = scope["ordered_groups"]
        if (
            not isinstance(groups_raw, list)
            or not groups_raw
            or len(groups_raw) > MAX_GROUPS_PER_SCOPE
        ):
            raise PrimaryStructureGoldError(
                f"{name}.ordered_groups must be a bounded non-empty list"
            )
        groups: list[dict[str, Any]] = []
        group_ids: set[str] = set()
        group_members_by_id: dict[str, frozenset[str]] = {}
        positively_grouped: set[str] = set()
        prior_group_last_index = -1
        for group_index, raw_group in enumerate(groups_raw):
            group_name = f"{name}.ordered_groups[{group_index}]"
            group = _exact_keys(raw_group, _GROUP_KEYS, name=group_name)
            group_id = _identifier(f"{group_name}.group_id", group["group_id"])
            if group_id in group_ids:
                raise PrimaryStructureGoldError(f"{name} has a duplicate group ID")
            group_ids.add(group_id)
            if group["kind"] != "paragraph":
                raise PrimaryStructureGoldError(
                    f"{group_name}.kind must be paragraph in v1"
                )
            members_raw = group["occurrence_ids"]
            if (
                not isinstance(members_raw, list)
                or not members_raw
                or len(members_raw) > MAX_OCCURRENCES_PER_GROUP
            ):
                raise PrimaryStructureGoldError(
                    f"{group_name}.occurrence_ids must be bounded and non-empty"
                )
            members: list[str] = []
            member_indices: list[int] = []
            for member_index, raw_member in enumerate(members_raw):
                member, page, item_index = _occurrence_parts(
                    f"{group_name}.occurrence_ids[{member_index}]", raw_member
                )
                if page != physical_page or member not in scope_occurrence_set:
                    raise PrimaryStructureGoldError(
                        f"{group_name} references an occurrence outside its scope"
                    )
                members.append(member)
                member_indices.append(item_index)
            if not _strictly_increasing(member_indices):
                raise PrimaryStructureGoldError(
                    f"{group_name} members must be unique and in native source order"
                )
            if positively_grouped.intersection(members):
                raise PrimaryStructureGoldError(
                    f"{name} occurrence cannot belong to two positive groups"
                )
            if member_indices[0] <= prior_group_last_index:
                raise PrimaryStructureGoldError(
                    f"{name}.ordered_groups are not in native source order"
                )
            prior_group_last_index = member_indices[-1]
            positively_grouped.update(members)

            boundaries_raw = group["boundaries"]
            if not isinstance(boundaries_raw, list) or len(boundaries_raw) != max(
                0, len(members) - 1
            ):
                raise PrimaryStructureGoldError(
                    f"{group_name}.boundaries must cover every adjacent member pair"
                )
            boundaries: list[dict[str, Any]] = []
            for boundary_index, raw_boundary in enumerate(boundaries_raw):
                boundary_name = f"{group_name}.boundaries[{boundary_index}]"
                boundary = _exact_keys(
                    raw_boundary, _BOUNDARY_KEYS, name=boundary_name
                )
                left, _, _ = _occurrence_parts(
                    f"{boundary_name}.left_occurrence_id",
                    boundary["left_occurrence_id"],
                )
                right, _, _ = _occurrence_parts(
                    f"{boundary_name}.right_occurrence_id",
                    boundary["right_occurrence_id"],
                )
                if (left, right) != (
                    members[boundary_index],
                    members[boundary_index + 1],
                ):
                    raise PrimaryStructureGoldError(
                        f"{boundary_name} must bind its adjacent ordered members"
                    )
                join_class = boundary["join_class"]
                if join_class not in _JOIN_CLASSES:
                    raise PrimaryStructureGoldError(
                        f"{boundary_name}.join_class is unsupported"
                    )
                boundaries.append(
                    {
                        "left_occurrence_id": left,
                        "right_occurrence_id": right,
                        "join_class": join_class,
                    }
                )
            groups.append(
                {
                    "group_id": group_id,
                    "kind": "paragraph",
                    "occurrence_ids": members,
                    "boundaries": boundaries,
                }
            )
            group_members_by_id[group_id] = frozenset(members)

        negatives_raw = scope["hard_negatives"]
        if (
            not isinstance(negatives_raw, list)
            or len(negatives_raw) > MAX_HARD_NEGATIVES_PER_SCOPE
        ):
            raise PrimaryStructureGoldError(
                f"{name}.hard_negatives exceeds the safety cap"
            )
        negatives: list[dict[str, Any]] = []
        seen_negative_keys: set[tuple[str, int]] = set()
        prior_negative_key: tuple[str, int] | None = None
        negatively_reviewed: set[str] = set()
        for negative_index, raw_negative in enumerate(negatives_raw):
            negative_name = f"{name}.hard_negatives[{negative_index}]"
            negative = _exact_keys(
                raw_negative, _HARD_NEGATIVE_KEYS, name=negative_name
            )
            if negative["kind"] != "forbidden_same_leaf":
                raise PrimaryStructureGoldError(
                    f"{negative_name}.kind must be forbidden_same_leaf"
                )
            occurrence_id, page, item_index = _occurrence_parts(
                f"{negative_name}.occurrence_id", negative["occurrence_id"]
            )
            target_group_id = _identifier(
                f"{negative_name}.target_group_id", negative["target_group_id"]
            )
            if (
                page != physical_page
                or occurrence_id not in scope_occurrence_set
                or target_group_id not in group_ids
            ):
                raise PrimaryStructureGoldError(
                    f"{negative_name} must bind an in-scope occurrence and group"
                )
            target_members = group_members_by_id[target_group_id]
            if occurrence_id in target_members:
                raise PrimaryStructureGoldError(
                    f"{negative_name} occurrence is already in its forbidden group"
                )
            negative_key = (target_group_id, item_index)
            if negative_key in seen_negative_keys:
                raise PrimaryStructureGoldError(
                    f"{name} contains a duplicate hard negative"
                )
            if prior_negative_key is not None and negative_key <= prior_negative_key:
                raise PrimaryStructureGoldError(
                    f"{name}.hard_negatives must use canonical group/source order"
                )
            prior_negative_key = negative_key
            seen_negative_keys.add(negative_key)
            negatively_reviewed.add(occurrence_id)
            negatives.append(
                {
                    "kind": "forbidden_same_leaf",
                    "occurrence_id": occurrence_id,
                    "target_group_id": target_group_id,
                }
            )
        if positively_grouped | negatively_reviewed != scope_occurrence_set:
            raise PrimaryStructureGoldError(
                f"{name}.occurrence_ids must all have a positive or hard-negative review"
            )

        scopes.append(
            {
                "scope_id": scope_id,
                "physical_page": physical_page,
                "printed_page_label_status": label_status,
                "printed_page_label": printed_label,
                "review_status": scope["review_status"],
                "reviewer_ref": scope["reviewer_ref"],
                "confirmed_at": scope["confirmed_at"],
                "occurrence_ids": occurrences,
                "ordered_groups": groups,
                "hard_negatives": negatives,
            }
        )

    if seen_scope_pages != set(page_scope):
        raise PrimaryStructureGoldError(
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


def canonical_primary_structure_gold_json(value: Mapping[str, Any]) -> bytes:
    """Return canonical UTF-8 JSON after strict semantic validation."""

    return _canonical_bytes(validate_primary_structure_gold(value))


@dataclass(frozen=True, slots=True)
class PrimaryStructureGoldFixture:
    """Immutable canonical primary-structure Gold."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise PrimaryStructureGoldError("Gold root must be an object")
        canonical = validate_primary_structure_gold(self.payload)
        object.__setattr__(self, "payload", _freeze(canonical))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PrimaryStructureGoldFixture":
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
            and self.canonical_sha256 in TRUSTED_CONFIRMED_GOLD_SHA256S
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
        ):
            return "not_evaluable_gold_pending"
        if not self.has_trusted_confirmation:
            return "not_evaluable_untrusted_confirmation"
        return "not_evaluable_input_replay_required"


@dataclass(frozen=True, slots=True, init=False)
class ReplayedPrimaryStructureGold:
    """Immutable receipt returned after one successful Gold replay.

    This object prevents accidental mixing of raw and replayed values inside
    cooperative code.  It is not an unforgeable capability in Python; every
    public quality-gate boundary must perform source replay in its own call.
    """

    fixture: PrimaryStructureGoldFixture
    _replayed_canonical_sha256: str

    def __init__(
        self,
        fixture: PrimaryStructureGoldFixture,
        *,
        _construction_token: object,
        replayed_canonical_sha256: str,
    ) -> None:
        if (
            _construction_token is not _REPLAY_CONSTRUCTION_TOKEN
            or type(fixture) is not PrimaryStructureGoldFixture
            or replayed_canonical_sha256 != fixture.canonical_sha256
        ):
            raise PrimaryStructureGoldError(
                "Gold replay receipt requires matching internal replay inputs"
            )
        object.__setattr__(self, "fixture", fixture)
        object.__setattr__(
            self,
            "_replayed_canonical_sha256",
            replayed_canonical_sha256,
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
        """Identify the convenience wrapper without asserting admission trust."""

        return True


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise PrimaryStructureGoldError(
                f"Gold contains duplicate JSON key {key!r}"
            )
        value[key] = nested
    return value


def _reject_constant(value: str) -> None:
    raise PrimaryStructureGoldError(
        f"Gold contains non-finite JSON number {value!r}"
    )


def parse_primary_structure_gold_bytes(raw: bytes) -> PrimaryStructureGoldFixture:
    """Parse exact canonical UTF-8 JSON with bounded, fail-closed semantics."""

    if not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES:
        raise PrimaryStructureGoldError("Gold exceeds the artifact byte cap")
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PrimaryStructureGoldError("Gold must not include a UTF-8 BOM")
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
        )
    except PrimaryStructureGoldError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise PrimaryStructureGoldError("Gold is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise PrimaryStructureGoldError("Gold root must be an object")
    fixture = PrimaryStructureGoldFixture.from_dict(value)
    if raw != fixture.canonical_json():
        raise PrimaryStructureGoldError("Gold bytes are not canonical UTF-8 JSON")
    return fixture


def load_primary_structure_gold_file(
    path: str | Path,
) -> PrimaryStructureGoldFixture:
    """Read one regular canonical Gold file without following symlinks."""

    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PrimaryStructureGoldError(
                "Gold input must be a regular non-symlink file"
            )
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise PrimaryStructureGoldError("Gold exceeds the artifact byte cap")
        descriptor = os.open(
            candidate, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (before.st_dev, before.st_ino):
                raise PrimaryStructureGoldError("Gold changed while opening")
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
            raise PrimaryStructureGoldError("Gold changed while reading")
    except PrimaryStructureGoldError:
        raise
    except OSError as error:
        raise PrimaryStructureGoldError("Gold input cannot be safely opened") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_primary_structure_gold_bytes(raw)


def _native_occurrence_is_visible(
    item: Mapping[str, Any],
    *,
    crop_box: Sequence[float],
    tolerance: float = 1e-5,
) -> bool:
    """Return whether a positive-size native bbox lies in the rendered crop."""

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


def validate_primary_structure_gold_against_inputs(
    gold: Mapping[str, Any] | PrimaryStructureGoldFixture,
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    render_artifact_root: str | Path,
) -> ReplayedPrimaryStructureGold:
    """Replay source, native namespace, and renderer-lineage bindings.

    Reconstruction-plan, ODL, Surya, fragment, and context artifacts are
    intentionally absent: parser output must never become the Gold oracle.
    """

    fixture = (
        gold
        if isinstance(gold, PrimaryStructureGoldFixture)
        else PrimaryStructureGoldFixture.from_dict(gold)
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
        raise PrimaryStructureGoldError(
            "native capture did not pass strict source replay"
        ) from error
    if capture["capture_schema_version"] != source["native_capture"]["schema_version"]:
        raise PrimaryStructureGoldError("native capture schema binding mismatch")
    if capture["version"] != source["native_capture"]["extractor_version"]:
        raise PrimaryStructureGoldError("native capture extractor binding mismatch")
    if capture["source_sha256"] != source["source_pdf_sha256"]:
        raise PrimaryStructureGoldError("source PDF binding mismatch")
    capture_sha256 = sha256(canonical_native_json_bytes(capture)).hexdigest()
    if capture_sha256 != source["native_capture"]["canonical_sha256"]:
        raise PrimaryStructureGoldError("native capture canonical hash mismatch")

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
                or len(raw_pages) < 1
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
        total_render_bytes = sum(page.image_size_bytes for page in manifest.pages)
        if total_render_bytes > MAX_RENDER_MANIFEST_TOTAL_BYTES:
            raise PdfRenderManifestError(
                "render manifest exceeds the aggregate PNG byte cap"
            )
        checked_manifest = validate_render_manifest_files(
            manifest,
            artifact_root=render_artifact_root,
        )
    except PdfRenderManifestError as error:
        raise PrimaryStructureGoldError(
            "render manifest did not pass strict artifact replay"
        ) from error
    if checked_manifest.source_pdf_sha256 != source["source_pdf_sha256"]:
        raise PrimaryStructureGoldError("render manifest source binding mismatch")
    if checked_manifest.page_count != capture["process_result"]["page_count"]:
        raise PrimaryStructureGoldError(
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
        raise PrimaryStructureGoldError("canonical page-render binding mismatch")

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
    missing = reviewed_occurrences - capture_occurrences.keys()
    if missing:
        raise PrimaryStructureGoldError(
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
        raise PrimaryStructureGoldError(
            "paragraph Gold may reference only substantive native text occurrences"
        )
    return ReplayedPrimaryStructureGold(
        fixture,
        _construction_token=_REPLAY_CONSTRUCTION_TOKEN,
        replayed_canonical_sha256=fixture.canonical_sha256,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_RENDER_MANIFEST_TOTAL_BYTES",
    "PrimaryStructureGoldError",
    "PrimaryStructureGoldFixture",
    "SCHEMA_VERSION",
    "TRUSTED_CONFIRMED_GOLD_SHA256S",
    "canonical_primary_structure_gold_json",
    "load_primary_structure_gold_file",
    "parse_primary_structure_gold_bytes",
    "validate_primary_structure_gold",
    "validate_primary_structure_gold_against_inputs",
]
