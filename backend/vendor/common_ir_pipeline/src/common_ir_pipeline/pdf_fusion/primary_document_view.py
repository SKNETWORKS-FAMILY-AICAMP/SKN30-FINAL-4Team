"""Gold-blind, native-first primary document-view candidate for PDF A4.1.

The persisted artifact is textless and evaluation-only.  Its primary leaves
partition the replayed reconstruction-plan ledger exactly once; parser/layout
sidecars may veto a native continuity boundary but never enumerate primary
evidence.  Standalone validation proves internal consistency only.  Crossing
a trust boundary requires deterministic replay against every source artifact.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping

from .native_capture import (
    CAPTURE_SCHEMA_VERSION,
    NativeCaptureError,
    PINNED_PDF_INSPECTOR_VERSION,
    canonical_json_bytes as canonical_native_json_bytes,
    validate_native_capture,
)
from .primary_paragraph_policy import (
    POLICY_VERSION,
    BoundaryDecision,
    PrimaryParagraphPolicyError,
    classify_native_continuity_boundary,
)
from .reconstruction_plan import (
    MAX_NATIVE_OCCURRENCES,
    SCHEMA_VERSION as RECONSTRUCTION_PLAN_SCHEMA_VERSION,
    PdfReconstructionPlanError,
    canonical_reconstruction_plan_json,
    validate_reconstruction_plan_against_inputs,
)
from .render_manifest import (
    MAX_PNG_FILE_BYTES,
    PdfRenderManifest,
    PdfRenderManifestError,
    validate_render_manifest_files,
)


SCHEMA_VERSION = "pdf_primary_document_view/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"

MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 4_000_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256
MAX_OCCURRENCES_PER_LEAF = 2
MAX_REASON_CODES = 64
MAX_RENDER_MANIFEST_TOTAL_BYTES = 512 * 1024 * 1024

_REPLAY_CONSTRUCTION_TOKEN = object()
_SHA = frozenset("0123456789abcdef")
_OCCURRENCE = re.compile(
    r"^occ:inspector:p(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6]):"
    r"t(?:0|[1-9][0-9]{0,4}|[1-4][0-9]{5})$"
)
_LEAF_ID = re.compile(r"^leaf-[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "notice_id",
        "source",
        "page_scope",
        "policy",
        "leaves",
        "relations",
        "metrics",
    }
)
_SOURCE_KEYS = frozenset(
    {"source_pdf_sha256", "native_capture", "reconstruction_plan"}
)
_NATIVE_SOURCE_KEYS = frozenset(
    {"schema_version", "canonical_sha256", "extractor_version"}
)
_PLAN_SOURCE_KEYS = frozenset({"schema_version", "canonical_sha256"})
_POLICY_KEYS = frozenset({"policy_version"})
_LEAF_KEYS = frozenset(
    {
        "leaf_id",
        "kind",
        "page",
        "occurrence_ids",
        "boundaries",
        "placement_method",
        "reason_codes",
        "structural_evidence_occurrence_ids",
    }
)
_BOUNDARY_KEYS = frozenset(
    {"left_occurrence_id", "right_occurrence_id", "join_class"}
)
_METRIC_KEYS = frozenset(
    {
        "authoritative_occurrence_count",
        "placed_occurrence_count",
        "leaf_count",
        "atomic_fallback_leaf_count",
        "paragraph_leaf_count",
        "merged_boundary_count",
        "missing_occurrence_count",
        "duplicate_occurrence_count",
        "ownership_gate_status",
    }
)
_TABLE_CONTEXT_KINDS = frozenset({"table", "table_row", "table_cell"})


class PdfPrimaryDocumentViewError(ValueError):
    """Raised when a primary document view is unsafe or ambiguous."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfPrimaryDocumentViewError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PdfPrimaryDocumentViewError(f"{name} keys must be strings")
    if keys != expected:
        missing, extra = sorted(expected - keys), sorted(keys - expected)
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise PdfPrimaryDocumentViewError(
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
        raise PdfPrimaryDocumentViewError(
            f"{name} must be a bounded non-empty trimmed string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PdfPrimaryDocumentViewError(
            f"{name} must not contain surrogate code points"
        ) from error
    if len(encoded) > maximum_bytes:
        raise PdfPrimaryDocumentViewError(
            f"{name} must be a bounded non-empty trimmed string"
        )
    return value


def _sha(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA for character in value)
    ):
        raise PdfPrimaryDocumentViewError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return value


def _nonnegative(name: str, value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
    ):
        raise PdfPrimaryDocumentViewError(
            f"{name} must be an integer from 0 to {maximum}"
        )
    return value


def _positive(name: str, value: object, *, maximum: int) -> int:
    result = _nonnegative(name, value, maximum=maximum)
    if result == 0:
        raise PdfPrimaryDocumentViewError(f"{name} must be positive")
    return result


def _occurrence_parts(name: str, value: object) -> tuple[int, int]:
    if not isinstance(value, str) or _OCCURRENCE.fullmatch(value) is None:
        raise PdfPrimaryDocumentViewError(f"{name} is not a native occurrence id")
    page_and_index = value.removeprefix("occ:inspector:p").split(":t", 1)
    page, source_index = int(page_and_index[0]), int(page_and_index[1])
    if source_index >= MAX_NATIVE_OCCURRENCES:
        raise PdfPrimaryDocumentViewError(
            f"{name} source item index exceeds the native capture cap"
        )
    return page, source_index


def _assert_json_limits(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise PdfPrimaryDocumentViewError(
                "primary document view exceeds the JSON node cap"
            )
        if depth > MAX_JSON_DEPTH:
            raise PdfPrimaryDocumentViewError(
                "primary document view exceeds the JSON depth cap"
            )
        if current is None or isinstance(current, (bool, int, float)):
            continue
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise PdfPrimaryDocumentViewError(
                    "primary document view contains a surrogate code point"
                )
            try:
                encoded = current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PdfPrimaryDocumentViewError(
                    "primary document view contains invalid Unicode"
                ) from error
            if len(encoded) > MAX_STRING_BYTES:
                raise PdfPrimaryDocumentViewError(
                    "primary document view contains an oversized string"
                )
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise PdfPrimaryDocumentViewError(
                        "primary document view object keys must be strings"
                    )
                stack.append((key, depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
            continue
        raise PdfPrimaryDocumentViewError(
            "primary document view contains a non-JSON value"
        )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(nested) for nested in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(nested) for key, nested in value.items()})
    if isinstance(value, (tuple, list)):
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
        raise PdfPrimaryDocumentViewError(
            "primary document view is not canonical JSON"
        ) from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryDocumentViewError(
            "primary document view exceeds the artifact byte cap"
        )
    return encoded


def _leaf_id(source_pdf_sha256: str, leaf: Mapping[str, Any]) -> str:
    identity = {
        "source_pdf_sha256": source_pdf_sha256,
        "policy_version": POLICY_VERSION,
        **leaf,
    }
    return "leaf-" + sha256(_canonical_bytes(identity)).hexdigest()


def _checked_reason_codes(value: object, *, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_REASON_CODES
        or any(
            not isinstance(reason, str) or _REASON_CODE.fullmatch(reason) is None
            for reason in value
        )
        or value != sorted(set(value))
    ):
        raise PdfPrimaryDocumentViewError(
            f"{name} must be a bounded, sorted, unique reason-code array"
        )
    return list(value)


def validate_pdf_primary_document_view(value: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate internal v1 structure without authenticating inputs."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="primary document view")
    if root["schema_version"] != SCHEMA_VERSION:
        raise PdfPrimaryDocumentViewError("schema_version is invalid")
    if root["evaluation_only"] is not True or root["non_promotable"] is not True:
        raise PdfPrimaryDocumentViewError(
            "primary document view must remain evaluation-only and non-promotable"
        )
    if root["standalone_validation_scope"] != STANDALONE_VALIDATION_SCOPE:
        raise PdfPrimaryDocumentViewError(
            "standalone validation scope is invalid"
        )
    notice_id = _string("notice_id", root["notice_id"])

    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    source_pdf_sha256 = _sha(
        "source.source_pdf_sha256", source["source_pdf_sha256"]
    )
    native_source = _exact_keys(
        source["native_capture"], _NATIVE_SOURCE_KEYS, name="source.native_capture"
    )
    if native_source["schema_version"] != CAPTURE_SCHEMA_VERSION:
        raise PdfPrimaryDocumentViewError("native capture schema version is invalid")
    native_sha256 = _sha(
        "source.native_capture.canonical_sha256",
        native_source["canonical_sha256"],
    )
    if native_source["extractor_version"] != PINNED_PDF_INSPECTOR_VERSION:
        raise PdfPrimaryDocumentViewError("native capture extractor version is invalid")
    plan_source = _exact_keys(
        source["reconstruction_plan"],
        _PLAN_SOURCE_KEYS,
        name="source.reconstruction_plan",
    )
    if plan_source["schema_version"] != RECONSTRUCTION_PLAN_SCHEMA_VERSION:
        raise PdfPrimaryDocumentViewError(
            "reconstruction plan schema version is invalid"
        )
    plan_sha256 = _sha(
        "source.reconstruction_plan.canonical_sha256",
        plan_source["canonical_sha256"],
    )

    raw_pages = root["page_scope"]
    if not isinstance(raw_pages, list) or not raw_pages or len(raw_pages) > MAX_PAGES:
        raise PdfPrimaryDocumentViewError("page_scope must be a bounded non-empty array")
    pages = tuple(
        _positive(f"page_scope[{index}]", page, maximum=MAX_PAGES)
        for index, page in enumerate(raw_pages)
    )
    if pages != tuple(sorted(set(pages))):
        raise PdfPrimaryDocumentViewError("page_scope must be sorted and unique")

    policy = _exact_keys(root["policy"], _POLICY_KEYS, name="policy")
    if policy["policy_version"] != POLICY_VERSION:
        raise PdfPrimaryDocumentViewError("paragraph policy version is invalid")

    raw_leaves = root["leaves"]
    if (
        not isinstance(raw_leaves, list)
        or not raw_leaves
        or len(raw_leaves) > MAX_NATIVE_OCCURRENCES
    ):
        raise PdfPrimaryDocumentViewError("leaves must be a bounded non-empty array")

    leaves: list[dict[str, Any]] = []
    seen_leaf_ids: set[str] = set()
    placed_occurrences: list[str] = []
    previous_sort_key: tuple[int, int] | None = None
    atomic_count = 0
    paragraph_count = 0
    merged_boundary_count = 0
    for leaf_index, raw_leaf in enumerate(raw_leaves):
        leaf = _exact_keys(raw_leaf, _LEAF_KEYS, name=f"leaves[{leaf_index}]")
        leaf_id = leaf["leaf_id"]
        if (
            not isinstance(leaf_id, str)
            or _LEAF_ID.fullmatch(leaf_id) is None
            or leaf_id in seen_leaf_ids
        ):
            raise PdfPrimaryDocumentViewError("leaf identity is invalid or duplicated")
        kind = leaf["kind"]
        if kind not in {"unclassified_text", "paragraph"}:
            raise PdfPrimaryDocumentViewError("leaf kind is invalid for A4.1")
        page = _positive(
            f"leaves[{leaf_index}].page", leaf["page"], maximum=MAX_PAGES
        )
        if page not in pages:
            raise PdfPrimaryDocumentViewError("leaf page is outside page_scope")
        occurrences = leaf["occurrence_ids"]
        if (
            not isinstance(occurrences, list)
            or not 1 <= len(occurrences) <= MAX_OCCURRENCES_PER_LEAF
            or any(not isinstance(item, str) for item in occurrences)
            or len(occurrences) != len(set(occurrences))
        ):
            raise PdfPrimaryDocumentViewError("leaf occurrences are invalid")
        occurrence_parts = [
            _occurrence_parts(
                f"leaves[{leaf_index}].occurrence_ids[{index}]", occurrence
            )
            for index, occurrence in enumerate(occurrences)
        ]
        if any(part_page != page for part_page, _ in occurrence_parts):
            raise PdfPrimaryDocumentViewError(
                "leaf occurrences must belong to the leaf page"
            )
        source_indices = [source_index for _, source_index in occurrence_parts]
        if source_indices != sorted(set(source_indices)):
            raise PdfPrimaryDocumentViewError(
                "leaf occurrences must follow strict native source order"
            )
        sort_key = (page, source_indices[0])
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            raise PdfPrimaryDocumentViewError("leaves must be canonically ordered")
        previous_sort_key = sort_key

        boundaries = leaf["boundaries"]
        if not isinstance(boundaries, list) or len(boundaries) != len(occurrences) - 1:
            raise PdfPrimaryDocumentViewError(
                "leaf boundaries must exactly cover adjacent occurrences"
            )
        checked_boundaries: list[dict[str, str]] = []
        for boundary_index, raw_boundary in enumerate(boundaries):
            boundary = _exact_keys(
                raw_boundary,
                _BOUNDARY_KEYS,
                name=f"leaves[{leaf_index}].boundaries[{boundary_index}]",
            )
            if (
                boundary["left_occurrence_id"] != occurrences[boundary_index]
                or boundary["right_occurrence_id"] != occurrences[boundary_index + 1]
                or boundary["join_class"] != "intra_word_wrap"
            ):
                raise PdfPrimaryDocumentViewError(
                    "boundary must bind one adjacent intra-word occurrence pair"
                )
            if source_indices[boundary_index + 1] != source_indices[boundary_index] + 1:
                raise PdfPrimaryDocumentViewError(
                    "paragraph boundary must preserve raw native adjacency"
                )
            checked_boundaries.append(
                {
                    "left_occurrence_id": boundary["left_occurrence_id"],
                    "right_occurrence_id": boundary["right_occurrence_id"],
                    "join_class": "intra_word_wrap",
                }
            )

        reason_codes = _checked_reason_codes(
            leaf["reason_codes"], name=f"leaves[{leaf_index}].reason_codes"
        )
        structural = leaf["structural_evidence_occurrence_ids"]
        if not isinstance(structural, list) or structural:
            raise PdfPrimaryDocumentViewError(
                "A4.1 leaves must not carry structural evidence occurrences"
            )
        if kind == "unclassified_text":
            if (
                len(occurrences) != 1
                or boundaries
                or leaf["placement_method"] != "atomic_fallback"
                or reason_codes != ["authoritative_atomic_fallback"]
            ):
                raise PdfPrimaryDocumentViewError(
                    "unclassified_text must be one canonical atomic fallback"
                )
            atomic_count += 1
        else:
            if (
                len(occurrences) != 2
                or leaf["placement_method"] != "native_continuity"
            ):
                raise PdfPrimaryDocumentViewError(
                    "A4.1 paragraph must be one qualified disjoint pair"
                )
            paragraph_count += 1
            merged_boundary_count += len(boundaries)

        leaf_without_id = {
            "kind": kind,
            "page": page,
            "occurrence_ids": list(occurrences),
            "boundaries": checked_boundaries,
            "placement_method": leaf["placement_method"],
            "reason_codes": reason_codes,
            "structural_evidence_occurrence_ids": [],
        }
        if leaf_id != _leaf_id(source_pdf_sha256, leaf_without_id):
            raise PdfPrimaryDocumentViewError(
                "leaf_id does not bind the canonical leaf content"
            )
        checked_leaf = {"leaf_id": leaf_id, **leaf_without_id}
        leaves.append(checked_leaf)
        seen_leaf_ids.add(leaf_id)
        placed_occurrences.extend(occurrences)

    occurrence_counts = Counter(placed_occurrences)
    duplicates = sum(count - 1 for count in occurrence_counts.values())
    if duplicates:
        raise PdfPrimaryDocumentViewError(
            "primary occurrence may appear in exactly one leaf"
        )
    if not isinstance(root["relations"], list) or root["relations"]:
        raise PdfPrimaryDocumentViewError("A4.1 relations must be an empty array")

    metrics = _exact_keys(root["metrics"], _METRIC_KEYS, name="metrics")
    checked_metrics = {
        name: _nonnegative(
            f"metrics.{name}", item, maximum=MAX_NATIVE_OCCURRENCES
        )
        for name, item in metrics.items()
        if name != "ownership_gate_status"
    }
    if metrics["ownership_gate_status"] != "passed":
        raise PdfPrimaryDocumentViewError("ownership gate status must be passed")
    placed_count = len(placed_occurrences)
    if (
        checked_metrics["authoritative_occurrence_count"] != placed_count
        or checked_metrics["placed_occurrence_count"] != placed_count
        or checked_metrics["leaf_count"] != len(leaves)
        or checked_metrics["atomic_fallback_leaf_count"] != atomic_count
        or checked_metrics["paragraph_leaf_count"] != paragraph_count
        or checked_metrics["merged_boundary_count"] != merged_boundary_count
        or checked_metrics["missing_occurrence_count"] != 0
        or checked_metrics["duplicate_occurrence_count"] != 0
        or len(leaves) != atomic_count + paragraph_count
        or placed_count != atomic_count + 2 * paragraph_count
    ):
        raise PdfPrimaryDocumentViewError(
            "primary document view metrics are inconsistent"
        )

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": notice_id,
        "source": {
            "source_pdf_sha256": source_pdf_sha256,
            "native_capture": {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "canonical_sha256": native_sha256,
                "extractor_version": PINNED_PDF_INSPECTOR_VERSION,
            },
            "reconstruction_plan": {
                "schema_version": RECONSTRUCTION_PLAN_SCHEMA_VERSION,
                "canonical_sha256": plan_sha256,
            },
        },
        "page_scope": list(pages),
        "policy": {"policy_version": POLICY_VERSION},
        "leaves": leaves,
        "relations": [],
        "metrics": {
            **checked_metrics,
            "ownership_gate_status": "passed",
        },
    }
    _canonical_bytes(canonical)
    return canonical


def canonical_pdf_primary_document_view_json(value: Mapping[str, Any]) -> bytes:
    """Validate and serialize canonical UTF-8 JSON."""

    return _canonical_bytes(validate_pdf_primary_document_view(value))


def _manifest_preflight(
    value: PdfRenderManifest | Mapping[str, Any], *, artifact_root: str | Path
) -> PdfRenderManifest:
    try:
        if isinstance(value, Mapping):
            raw_page_count = value.get("page_count")
            raw_pages = value.get("pages")
            if (
                isinstance(raw_page_count, bool)
                or not isinstance(raw_page_count, int)
                or not 1 <= raw_page_count <= MAX_PAGES
                or not isinstance(raw_pages, list)
                or not 1 <= len(raw_pages) <= MAX_PAGES
            ):
                raise PdfRenderManifestError(
                    "render manifest exceeds the page preflight cap"
                )
            manifest = PdfRenderManifest.from_dict(value)
        else:
            manifest = value
        if not isinstance(manifest, PdfRenderManifest) or manifest.page_count > MAX_PAGES:
            raise PdfRenderManifestError("render manifest exceeds the page cap")
        total_size = sum(page.image_size_bytes for page in manifest.pages)
        if (
            total_size > MAX_RENDER_MANIFEST_TOTAL_BYTES
            or any(page.image_size_bytes > MAX_PNG_FILE_BYTES for page in manifest.pages)
        ):
            raise PdfRenderManifestError(
                "render manifest exceeds the PNG byte cap"
            )
        return validate_render_manifest_files(manifest, artifact_root=artifact_root)
    except PdfRenderManifestError as error:
        raise PdfPrimaryDocumentViewError(
            f"render manifest did not pass file replay: {error}"
        ) from error


def build_pdf_primary_document_view(
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
    """Build a deterministic primary view after complete source replay."""

    checked_manifest = _manifest_preflight(
        render_manifest, artifact_root=render_artifact_root
    )
    try:
        plan = validate_reconstruction_plan_against_inputs(
            reconstruction_plan,
            source_pdf=source_pdf,
            native_capture=native_capture,
            structure_candidates=structure_candidates,
            render_manifest=checked_manifest,
            calibration_proof=calibration_proof,
            expected_calibration_proof_sha256=expected_calibration_proof_sha256,
        )
    except PdfReconstructionPlanError as error:
        raise PdfPrimaryDocumentViewError(
            f"reconstruction plan did not pass source replay: {error}"
        ) from error
    try:
        native = validate_native_capture(
            native_capture,
            source_pdf=source_pdf,
            expected_notice_id=plan["notice_id"],
        )
    except NativeCaptureError as error:
        raise PdfPrimaryDocumentViewError(
            f"native capture did not pass source replay: {error}"
        ) from error

    if (
        plan["native_text_status"] != "native_text_available"
        or plan["metrics"]["native_ownership_gate_status"] != "passed"
        or plan["metrics"]["unowned_substantive_occurrence_count"] != 0
        or plan["metrics"]["duplicate_primary_owner_count"] != 0
    ):
        raise PdfPrimaryDocumentViewError(
            "primary view requires a passed native ownership gate"
        )

    authoritative = [
        entry
        for entry in plan["native_occurrence_ledger"]
        if entry["substantive_status"] == "substantive"
        and entry["disposition"] == "owned_atomic"
        and entry["primary_owner_unit_id"] is not None
    ]
    if not authoritative:
        raise PdfPrimaryDocumentViewError(
            "primary view requires authoritative native occurrences"
        )
    authoritative.sort(key=lambda entry: (entry["page"], entry["source_item_index"]))
    if len(authoritative) != plan["metrics"]["atomic_evidence_unit_count"]:
        raise PdfPrimaryDocumentViewError(
            "authoritative ledger count disagrees with atomic ownership"
        )

    native_items = native["text_items"]
    for entry in authoritative:
        source_index = entry["source_item_index"]
        if source_index >= len(native_items) or native_items[source_index]["page"] != entry["page"]:
            raise PdfPrimaryDocumentViewError(
                "ledger source item does not bind the canonical native capture"
            )

    crop_boxes = {
        page.page: page.coordinate_manifest.crop_box for page in checked_manifest.pages
    }
    table_context_occurrences: set[str] = set()
    for unit in plan["units"]:
        if (
            unit["origin"] == "opendataloader"
            and unit["use_policy"] == "context_only"
            and unit["kind"] in _TABLE_CONTEXT_KINDS
        ):
            table_context_occurrences.update(unit["reference_occurrence_ids"])

    qualified_edges: list[tuple[Mapping[str, Any], Mapping[str, Any], BoundaryDecision]] = []
    for left, right in zip(authoritative, authoritative[1:]):
        if left["page"] != right["page"]:
            continue
        try:
            decision = classify_native_continuity_boundary(
                left_ledger=left,
                right_ledger=right,
                left_item=native_items[left["source_item_index"]],
                right_item=native_items[right["source_item_index"]],
                crop_box=crop_boxes[left["page"]],
                table_context_veto=(
                    left["occurrence_id"] in table_context_occurrences
                    or right["occurrence_id"] in table_context_occurrences
                ),
            )
        except PrimaryParagraphPolicyError as error:
            raise PdfPrimaryDocumentViewError(
                f"native paragraph policy rejected its inputs: {error}"
            ) from error
        if type(decision) is not BoundaryDecision:
            raise PdfPrimaryDocumentViewError(
                "native paragraph policy returned an invalid decision type"
            )
        if decision.qualified:
            if decision.join_class != "intra_word_wrap":
                raise PdfPrimaryDocumentViewError(
                    "A4.1 policy may qualify only intra_word_wrap boundaries"
                )
            qualified_edges.append((left, right, decision))

    degrees: Counter[str] = Counter()
    for left, right, _ in qualified_edges:
        degrees[left["occurrence_id"]] += 1
        degrees[right["occurrence_id"]] += 1
    retained_edges = {
        (left["occurrence_id"], right["occurrence_id"]): decision
        for left, right, decision in qualified_edges
        if degrees[left["occurrence_id"]] == 1
        and degrees[right["occurrence_id"]] == 1
    }

    leaves: list[dict[str, Any]] = []
    index = 0
    while index < len(authoritative):
        run = [authoritative[index]]
        decisions: list[BoundaryDecision] = []
        while index + 1 < len(authoritative):
            left, right = authoritative[index], authoritative[index + 1]
            decision = retained_edges.get(
                (left["occurrence_id"], right["occurrence_id"])
            )
            if decision is None:
                break
            run.append(right)
            decisions.append(decision)
            index += 1

        occurrences = [entry["occurrence_id"] for entry in run]
        if len(run) == 1:
            leaf_without_id: dict[str, Any] = {
                "kind": "unclassified_text",
                "page": run[0]["page"],
                "occurrence_ids": occurrences,
                "boundaries": [],
                "placement_method": "atomic_fallback",
                "reason_codes": ["authoritative_atomic_fallback"],
                "structural_evidence_occurrence_ids": [],
            }
        else:
            reason_codes = sorted(
                {
                    reason
                    for decision in decisions
                    for reason in decision.reason_codes
                }
            )
            if not reason_codes:
                raise PdfPrimaryDocumentViewError(
                    "qualified paragraph boundary must carry policy reasons"
                )
            leaf_without_id = {
                "kind": "paragraph",
                "page": run[0]["page"],
                "occurrence_ids": occurrences,
                "boundaries": [
                    {
                        "left_occurrence_id": occurrences[position],
                        "right_occurrence_id": occurrences[position + 1],
                        "join_class": decisions[position].join_class,
                    }
                    for position in range(len(decisions))
                ],
                "placement_method": "native_continuity",
                "reason_codes": reason_codes,
                "structural_evidence_occurrence_ids": [],
            }
        leaves.append(
            {
                "leaf_id": _leaf_id(plan["source_pdf_sha256"], leaf_without_id),
                **leaf_without_id,
            }
        )
        index += 1

    authority_counts = Counter(
        entry["occurrence_id"] for entry in authoritative
    )
    placement_counts = Counter(
        occurrence
        for leaf in leaves
        for occurrence in leaf["occurrence_ids"]
    )
    missing_count = sum((authority_counts - placement_counts).values())
    duplicate_count = sum(
        max(0, count - authority_counts.get(occurrence, 0))
        for occurrence, count in placement_counts.items()
    )
    if placement_counts != authority_counts or missing_count or duplicate_count:
        raise PdfPrimaryDocumentViewError(
            "primary leaves do not partition authoritative occurrences exactly once"
        )

    native_sha256 = sha256(canonical_native_json_bytes(native)).hexdigest()
    plan_sha256 = sha256(canonical_reconstruction_plan_json(plan)).hexdigest()
    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "notice_id": plan["notice_id"],
        "source": {
            "source_pdf_sha256": plan["source_pdf_sha256"],
            "native_capture": {
                "schema_version": native["capture_schema_version"],
                "canonical_sha256": native_sha256,
                "extractor_version": native["version"],
            },
            "reconstruction_plan": {
                "schema_version": plan["schema_version"],
                "canonical_sha256": plan_sha256,
            },
        },
        "page_scope": list(plan["page_scope"]),
        "policy": {"policy_version": POLICY_VERSION},
        "leaves": leaves,
        "relations": [],
        "metrics": {
            "authoritative_occurrence_count": len(authoritative),
            "placed_occurrence_count": sum(len(leaf["occurrence_ids"]) for leaf in leaves),
            "leaf_count": len(leaves),
            "atomic_fallback_leaf_count": sum(
                leaf["kind"] == "unclassified_text" for leaf in leaves
            ),
            "paragraph_leaf_count": sum(leaf["kind"] == "paragraph" for leaf in leaves),
            "merged_boundary_count": sum(len(leaf["boundaries"]) for leaf in leaves),
            "missing_occurrence_count": 0,
            "duplicate_occurrence_count": 0,
            "ownership_gate_status": "passed",
        },
    }
    return validate_pdf_primary_document_view(result)


@dataclass(frozen=True, slots=True)
class PrimaryDocumentViewFixture:
    """Immutable standalone candidate; not evidence of source replay."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise PdfPrimaryDocumentViewError(
                "primary document view root must be an object"
            )
        canonical = validate_pdf_primary_document_view(self.payload)
        object.__setattr__(self, "payload", _freeze(canonical))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PrimaryDocumentViewFixture":
        return cls(value)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self.payload)

    def canonical_json(self) -> bytes:
        return _canonical_bytes(self.payload)

    @property
    def canonical_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()

@dataclass(frozen=True, slots=True, init=False)
class ReplayedPrimaryDocumentView:
    """Immutable receipt returned after one deterministic candidate replay.

    The receipt is an accidental-misuse guard for cooperative code, not an
    unforgeable Python capability.  Public quality gates replay the source
    artifacts again instead of trusting a caller-supplied receipt.
    """

    fixture: PrimaryDocumentViewFixture
    _replayed_canonical_sha256: str

    def __init__(
        self,
        fixture: PrimaryDocumentViewFixture,
        *,
        _construction_token: object,
        replayed_canonical_sha256: str,
    ) -> None:
        if (
            _construction_token is not _REPLAY_CONSTRUCTION_TOKEN
            or type(fixture) is not PrimaryDocumentViewFixture
            or replayed_canonical_sha256 != fixture.canonical_sha256
        ):
            raise PdfPrimaryDocumentViewError(
                "primary document view replay receipt requires matching internal replay inputs"
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
    def is_replay_receipt(self) -> bool:
        """Identify the convenience wrapper without asserting admission trust."""

        return True


def validate_pdf_primary_document_view_against_inputs(
    value: Mapping[str, Any] | PrimaryDocumentViewFixture,
    *,
    source_pdf: str | Path,
    native_capture: Mapping[str, Any],
    structure_candidates: Mapping[str, Any],
    render_manifest: PdfRenderManifest | Mapping[str, Any],
    render_artifact_root: str | Path,
    calibration_proof: Mapping[str, Any],
    expected_calibration_proof_sha256: str,
    reconstruction_plan: Mapping[str, Any],
) -> ReplayedPrimaryDocumentView:
    """Require byte identity with a complete deterministic source replay."""

    fixture = (
        value
        if type(value) is PrimaryDocumentViewFixture
        else PrimaryDocumentViewFixture.from_dict(value)
    )
    expected = build_pdf_primary_document_view(
        source_pdf=source_pdf,
        native_capture=native_capture,
        structure_candidates=structure_candidates,
        render_manifest=render_manifest,
        render_artifact_root=render_artifact_root,
        calibration_proof=calibration_proof,
        expected_calibration_proof_sha256=expected_calibration_proof_sha256,
        reconstruction_plan=reconstruction_plan,
    )
    if fixture.canonical_json() != canonical_pdf_primary_document_view_json(expected):
        raise PdfPrimaryDocumentViewError(
            "primary document view does not match deterministic replay of inputs"
        )
    return ReplayedPrimaryDocumentView(
        fixture,
        _construction_token=_REPLAY_CONSTRUCTION_TOKEN,
        replayed_canonical_sha256=fixture.canonical_sha256,
    )


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PdfPrimaryDocumentViewError(
                f"primary document view contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PdfPrimaryDocumentViewError(
        f"primary document view contains non-finite JSON number {value!r}"
    )


def parse_pdf_primary_document_view_bytes(raw: bytes) -> PrimaryDocumentViewFixture:
    """Parse bounded exact canonical UTF-8 JSON."""

    if not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES:
        raise PdfPrimaryDocumentViewError(
            "primary document view exceeds the artifact byte cap"
        )
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PdfPrimaryDocumentViewError(
                "primary document view must not contain a UTF-8 BOM"
            )
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
        )
    except PdfPrimaryDocumentViewError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PdfPrimaryDocumentViewError(
            "primary document view is not valid UTF-8 JSON"
        ) from error
    _assert_json_limits(value)
    if not isinstance(value, Mapping):
        raise PdfPrimaryDocumentViewError(
            "primary document view root must be an object"
        )
    fixture = PrimaryDocumentViewFixture.from_dict(value)
    if raw != fixture.canonical_json():
        raise PdfPrimaryDocumentViewError(
            "primary document view bytes are not canonical UTF-8 JSON"
        )
    return fixture


def load_pdf_primary_document_view_file(
    path: str | Path,
) -> PrimaryDocumentViewFixture:
    """Safely read one regular, non-symlink candidate file."""

    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PdfPrimaryDocumentViewError(
                "primary document view input must be a regular non-symlink file"
            )
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise PdfPrimaryDocumentViewError(
                "primary document view exceeds the artifact byte cap"
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
                raise PdfPrimaryDocumentViewError(
                    "primary document view changed while opening"
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
            raise PdfPrimaryDocumentViewError(
                "primary document view changed while reading"
            )
    except PdfPrimaryDocumentViewError:
        raise
    except OSError as error:
        raise PdfPrimaryDocumentViewError(
            "primary document view input cannot be safely opened"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return parse_pdf_primary_document_view_bytes(raw)


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "PdfPrimaryDocumentViewError",
    "PrimaryDocumentViewFixture",
    "SCHEMA_VERSION",
    "build_pdf_primary_document_view",
    "canonical_pdf_primary_document_view_json",
    "load_pdf_primary_document_view_file",
    "parse_pdf_primary_document_view_bytes",
    "validate_pdf_primary_document_view",
    "validate_pdf_primary_document_view_against_inputs",
]
