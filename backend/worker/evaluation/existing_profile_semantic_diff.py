"""Offline, ID/order-insensitive semantic comparison for Existing Profiles.

This evaluator compares three artifacts for the same notice:

``B`` unreviewed automatic baseline, ``G`` human-reviewed frozen Gold, and
``C`` a newly generated candidate.  It is intentionally not an LLM quality
judge.  It proves only whether the candidate preserves the reviewed semantic
delta already represented by the profile graph:

* ``G - B``: approved additions and removals are reported as diagnostics;
* ``G & B``: retained semantics are reported as diagnostics;
* ``C == G``: the only pass/fail rule is exact semantic multigraph equality.

The graph is a multiset of opaque, SHA-256-addressed atoms.  Reports therefore
contain no source text or prompt material.  Generated fact/component IDs,
array order, model summaries, and processing metadata are deliberately
excluded; exact fact values, roles, source evidence, graph
relationships, components, and support-scale measures are retained.

There is no network, database, object-storage, application-runtime, or LLM
I/O in this module.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence
from zipfile import BadZipFile, ZipFile

from worker import vendor as _vendor  # noqa: F401 - installs vendored contract paths

from semantic_structuring.common_ir_v1 import prepare_common_ir_v1

from scripts.verify_existing_gold100 import (
    COMMON_IR_SCHEMA,
    GoldVerificationError,
    PROFILE_SCHEMA,
    verify_gold_root,
    verify_profile_artifact_triple,
)

from worker.evaluation.existing_profile_diff import (
    DEFAULT_EXPECTED_PROFILE_COUNT,
    ExistingProfileComparisonError,
    _JsonNumber,
    _canonical_json,
    _json_object,
    _assert_snapshot_is_current,
    _read_regular_json_snapshot,
    _safe_zip_member,
    load_automatic_baseline,
    load_reviewed_gold,
    write_comparison_report,
)


SEMANTIC_COMPARISON_SCHEMA_VERSION = "existing_profile_semantic_bgc/v1"
SEMANTIC_REPORT_FILE_NAME = "existing-profile-semantic-bgc.v1.json"
MAX_TOTAL_COMPANION_BYTES = 256 * 1024 * 1024

_NOTICE_ID_PATTERN = re.compile(r"PBLN_[0-9]{15}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_FACT_FIELDS = frozenset({
    "purpose_goal", "applicant_eligibility", "support_target",
    "eligibility_conditions", "beneficiary", "applicable_entity",
    "exclusions", "duplicate_support_conditions", "support_methods",
    "support_activities", "support_items", "support_content",
    "support_scale", "program_period", "support_period", "total_budget",
    "cost_sharing", "payment_terms", "participation_requirements",
    "delivery_roles",
})
_FACT_STATUSES = frozenset({
    "identified", "partially_identified", "mentioned_not_specific",
    "unresolved", "needs_review",
})
_FACT_SCOPES = frozenset({"notice", "component"})
_COMPONENT_KINDS = frozenset({"support_package", "participation_type", "stage_support"})
_EVIDENCE_KEYS = frozenset({
    "source_block_id", "source_occurrence_ids", "common_ir_document_id",
    "common_ir_block_id", "common_ir_cell_id", "common_ir_occurrence_ids",
    "section_id", "text", "source_spans",
})
_EVIDENCE_REQUIRED_KEYS = frozenset({
    "source_block_id", "source_occurrence_ids", "common_ir_document_id",
    "common_ir_block_id", "common_ir_occurrence_ids", "section_id",
})
_VALUE_SOURCE_KEYS = frozenset({"source_block_id", "start_char", "end_char", "text_basis"})
_IDENTITY_KEYS = frozenset({
    "title_raw", "title_source_block_ids", "notice_date_raw",
    "notice_date_source_block_ids", "source_url",
})
_SOURCE_DOCUMENT_KEYS = frozenset({
    "document_name", "format", "source_url", "notice_detail_url", "common_ir",
})
_COMMON_IR_LINEAGE_KEYS = frozenset({
    "document_id", "schema_version", "source_kind", "source_sha256",
    "source_location", "artifact_role",
})
_UNRESOLVED_OBSERVATION_KEYS = frozenset({"kind", "status", "value_raw", "reason"})
_MEASURE_KEYS = frozenset({
    "measure_type", "measure_role", "lower_value", "upper_value", "unit",
    "comparator", "source_fact_id", "source_numeric_candidate_id",
    "applies_per", "calculation_basis", "frequency", "aggregation_scope",
})
_NUMERIC_CANDIDATE_KEYS = frozenset({
    "numeric_candidate_id", "source_block_id", "anchor_text", "start_char", "end_char",
})
_GROUPED_DECIMAL_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_NUMERIC_TOKEN_PREFIX = r"(?<![\d.])(?<!\d,)(?<!\d，)(?<![,，][,，])"
_NUMERIC_TOKEN_SUFFIX = r"(?!\d|[,，]\d|\.\d)"
_NUMERIC_CANDIDATE_PATTERN = re.compile(
    rf"{_NUMERIC_TOKEN_PREFIX}(?:{_GROUPED_DECIMAL_NUMBER}\s*(?:천|만|억)?\s*원|"
    rf"\d+(?:\.\d+)?\s*%|\d+\s*(?:개사|개팀|개 과제|개과제|명|팀|사)){_NUMERIC_TOKEN_SUFFIX}"
)
_AMOUNT_TOKEN = re.compile(
    rf"(?P<number>{_GROUPED_DECIMAL_NUMBER})\s*"
    r"(?P<suffix>천|만|억)?\s*원"
)
_RATE_TOKEN = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*%")
_COUNT_TOKEN = re.compile(r"(?P<number>\d+)\s*(?P<unit>개사|개팀|개 과제|개과제|명|팀|사)")
_NATIVE_LINE = re.compile(r"[^\r\n]+")
_NATIVE_LINE_KINDS = frozenset({None, "paragraph", "text", "list_item", "body", "heading_body"})
_CONTINUATION_KINDS = _NATIVE_LINE_KINDS
_TERMINAL = re.compile(r"[.!?。！？]|(?:다|함|됨|있음|없음|바람|가능|불가)[.)]?\s*$")
_BARE_NUMBER = re.compile(r"^\s*(?:\d+[.)]|[①-⑳]|[가-하][.)])\s*$")
_LEADING_LAYOUT_MARKER = re.compile(r"^\s*(?:[-·◦□○●▪•]\s*)")
TRUSTED_CANDIDATE_TRANSFORM = (
    "common-ir-v1-source-universe/v1:"
    "pdf-native-table-occurrences+native-lines+native-continuations"
)
SOURCE_ADMISSION_SCOPE = "deterministic_common_ir_source_universe_not_runpod_router_parity"
SOURCE_ADMISSION_LIMITATIONS = (
    "does not reproduce section-scope or block-router eligibility decisions",
    "does not admit rewritten or table-relation-derived text without a versioned transform contract",
)


class ExistingProfileSemanticError(ExistingProfileComparisonError):
    """A semantic graph cannot be safely formed or compared."""


@dataclass(frozen=True, slots=True)
class SemanticGraph:
    """Opaque multiset graph, suitable for set algebra without source text."""

    atoms: Counter[str]
    atom_kinds: Mapping[str, str]
    digest: str


@dataclass(frozen=True, slots=True)
class SemanticArtifact:
    """Profile plus the two companion artifacts required for provenance."""

    notice_id: str
    profile: Mapping[str, Any]
    source_selection: Mapping[str, Any]
    common_ir: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LoadedSemanticArtifacts:
    """Safety-checked companion artifacts, keyed by logical notice id."""

    artifacts: Mapping[str, SemanticArtifact]
    identity: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _NativeSourceSpanContract:
    """One server-derived constituent of a trusted composite block."""

    source_block_id: str
    exact_text: str
    start_char: int
    end_char: int
    separator_after: str
    source_order: int
    section_id: str
    common_ir_block_id: str
    common_ir_cell_id: str | None
    common_ir_occurrence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _NativeSourceContract:
    """One deterministic CandidatePack block regenerated from Common IR v1."""

    text: str
    block_kind: str | None
    source_order: int | None
    common_ir_block_id: str
    common_ir_cell_id: str | None
    common_ir_occurrence_ids: tuple[str, ...]
    source_occurrence_ids: tuple[str, ...]
    section_id: str | None
    native_parent_block_id: str | None = None
    native_start_char: int | None = None
    native_end_char: int | None = None
    source_spans: tuple[_NativeSourceSpanContract, ...] = ()


@dataclass(frozen=True, slots=True)
class _NumericLocator:
    """A validated numeric token and its CandidatePack-local coordinates."""

    source_block_id: str
    start_char: int
    end_char: int
    anchor_text: str


def _native_span_payload(span: _NativeSourceSpanContract) -> dict[str, Any]:
    """Return the exact JSON wire contract for one trusted composite span."""

    return {
        "source_block_id": span.source_block_id,
        "exact_text": span.exact_text,
        "start_char": span.start_char,
        "end_char": span.end_char,
        "separator_after": span.separator_after,
        "source_order": span.source_order,
        "section_id": span.section_id,
        "common_ir_block_id": span.common_ir_block_id,
        "common_ir_occurrence_ids": list(span.common_ir_occurrence_ids),
        "common_ir_cell_id": span.common_ir_cell_id,
    }


_FACT_IGNORED_KEYS = frozenset({
    "fact_id",
    "model_summary",
    "support_component_id",
    "applicability_component_ids",
    "modifies_fact_ids",
    "recipient_fact_ids",
    "basis_fact_ids",
})
_FACT_SEMANTIC_KEYS = frozenset({
    "field_name",
    "value_raw",
    "status",
    "scope",
    "subject_role",
    "semantic_role",
    "organization_names",
    "role_raw",
    "role_source_block_id",
    "canonical_role",
    "role_source",
    "value_source",
    "evidence",
    "context_evidence",
})
_COMPONENT_KEYS = frozenset({
    "support_component_id",
    "component_kind",
    "name_raw",
    "name_source_block_id",
    "name_status",
    "source_block_ids",
    "table_block_ids",
    "facts",
})
_RELATION_FIELDS = (
    "applicability_component_ids",
    "modifies_fact_ids",
    "recipient_fact_ids",
    "basis_fact_ids",
)
_FACT_OPTIONAL_KEYS = frozenset({
    "organization_names", "role_raw", "role_source_block_id",
    "canonical_role", "role_source",
})
_FACT_REQUIRED_KEYS = (
    _FACT_IGNORED_KEYS
    | _FACT_SEMANTIC_KEYS
) - _FACT_OPTIONAL_KEYS


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExistingProfileSemanticError(message)


def _plain_json(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Convert lossless comparator numbers to ordinary JSON validation values."""

    try:
        loaded = json.loads(_canonical_json(value))
    except (ExistingProfileComparisonError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ExistingProfileSemanticError(f"artifact is not valid canonical JSON: {error}") from error
    _require(isinstance(loaded, Mapping), "artifact JSON root must be an object")
    return loaded


def _validate_artifact_triple(artifact: SemanticArtifact, *, label: str) -> None:
    """Apply the shared exact-span/provenance contract to an artifact triple."""

    _require(
        isinstance(artifact.notice_id, str)
        and _NOTICE_ID_PATTERN.fullmatch(artifact.notice_id) is not None,
        f"{label} has an invalid logical notice id",
    )
    try:
        verify_profile_artifact_triple(
            _plain_json(artifact.profile),
            _plain_json(artifact.source_selection),
            _plain_json(artifact.common_ir),
            pblanc_id=artifact.notice_id,
            allow_unnamespaced_notice_id=True,
        )
    except (GoldVerificationError, ValueError, TypeError) as error:
        raise ExistingProfileSemanticError(f"{label} artifact triple is invalid: {error}") from error


def _read_zip_json(archive: ZipFile, name: str, *, label: str) -> Mapping[str, Any]:
    """Bound a companion artifact before parsing it as duplicate-key-free JSON."""

    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise ExistingProfileSemanticError(f"{label} is missing from archive") from error
    try:
        _safe_zip_member(info)
        _require(0 <= info.file_size <= 16 * 1024 * 1024, f"{label} exceeds the 16 MiB cap")
        with archive.open(info, "r") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        _require(len(raw) == info.file_size and len(raw) <= 16 * 1024 * 1024, f"{label} byte count is invalid")
        return _json_object(raw, label=label)
    except ExistingProfileSemanticError:
        raise
    except (ExistingProfileComparisonError, BadZipFile, OSError, RuntimeError) as error:
        raise ExistingProfileSemanticError(f"cannot read {label}: {error}") from error


def load_automatic_semantic_artifacts(
    archive_path: Path,
    *,
    expected_profile_count: int = DEFAULT_EXPECTED_PROFILE_COUNT,
    role: str = "unreviewed_automatic_baseline",
) -> LoadedSemanticArtifacts:
    """Load baseline/candidate Profile+selection+Common-IR triples from one ZIP.

    The existing strict Profile loader first validates archive shape, physical
    size, symlink status, duplicate names, and an archive SHA-256.  A second
    descriptor is then held while companion files are read, and its digest is
    rechecked against that identity to fail closed on replacement races.
    """

    _require(role in {"unreviewed_automatic_baseline", "generated_candidate"}, "semantic archive role is invalid")
    try:
        profiles = load_automatic_baseline(
            archive_path,
            expected_profile_count=expected_profile_count,
        )
    except ExistingProfileComparisonError as error:
        raise ExistingProfileSemanticError(
            f"cannot load {role} profile archive: {error}"
        ) from error
    expected_digest = profiles.identity.get("archive_sha256")
    supplied = archive_path.expanduser()
    descriptor: int | None = None
    try:
        descriptor = os.open(supplied, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), "companion archive must be a regular file")
            digest = sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            _require(digest.hexdigest() == expected_digest, "archive changed before companion artifacts were read")
            stream.seek(0)
            with ZipFile(stream) as archive:
                info_by_name = {
                    _safe_zip_member(info): info
                    for info in archive.infolist()
                }
                names = set(info_by_name)
                companion_infos = []
                for notice_id in profiles.profiles:
                    selection_name = f"{notice_id}/pipeline/source_selection.json"
                    common_prefix = f"{notice_id}/pipeline/common_ir_v1/"
                    companion_infos.extend(
                        info
                        for name, info in info_by_name.items()
                        if name == selection_name
                        or (name.startswith(common_prefix) and name.endswith(".json"))
                    )
                declared_companion_bytes = sum(info.file_size for info in companion_infos)
                _require(
                    declared_companion_bytes <= MAX_TOTAL_COMPANION_BYTES,
                    "semantic companion artifacts exceed the total uncompressed-byte cap",
                )
                artifacts: dict[str, SemanticArtifact] = {}
                for notice_id, profile in profiles.profiles.items():
                    selection_name = f"{notice_id}/pipeline/source_selection.json"
                    _require(selection_name in names, f"{role} source selection missing for {notice_id}")
                    common_prefix = f"{notice_id}/pipeline/common_ir_v1/"
                    common_names = sorted(name for name in names if name.startswith(common_prefix) and name.endswith(".json"))
                    _require(len(common_names) == 1, f"{role} Common IR must be exactly one JSON for {notice_id}")
                    artifact = SemanticArtifact(
                        notice_id=notice_id,
                        profile=profile,
                        source_selection=_read_zip_json(archive, selection_name, label=f"{role} source selection {notice_id}"),
                        common_ir=_read_zip_json(archive, common_names[0], label=f"{role} Common IR {notice_id}"),
                    )
                    _validate_artifact_triple(artifact, label=f"{role} {notice_id}")
                    artifacts[notice_id] = artifact
            after = os.fstat(stream.fileno())
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), "archive changed while companion artifacts were read")
    except ExistingProfileSemanticError:
        raise
    except (OSError, BadZipFile) as error:
        raise ExistingProfileSemanticError(f"cannot load semantic companion artifacts: {error}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    identity = dict(profiles.identity)
    identity["role"] = role
    return LoadedSemanticArtifacts(artifacts=artifacts, identity=identity)


def load_reviewed_gold_semantic_artifacts(
    gold_root: Path,
    *,
    expected_profile_count: int = DEFAULT_EXPECTED_PROFILE_COUNT,
) -> LoadedSemanticArtifacts:
    """Load Gold triples only after the frozen-Gold verifier has accepted them."""

    try:
        profiles = load_reviewed_gold(
            gold_root,
            expected_profile_count=expected_profile_count,
        )
    except ExistingProfileComparisonError as error:
        raise ExistingProfileSemanticError(
            f"cannot load reviewed Gold profile corpus: {error}"
        ) from error
    root = gold_root.expanduser().resolve(strict=True)
    artifacts: dict[str, SemanticArtifact] = {}
    snapshots: dict[Path, Any] = {}
    for notice_id, profile in profiles.profiles.items():
        notice_root = root / "notices" / notice_id
        selection_path = notice_root / "source_selection.v0.2.json"
        common_ir_path = notice_root / "common_ir_v1.json"
        try:
            selection_snapshot = _read_regular_json_snapshot(
                selection_path, label=f"Gold source selection {notice_id}"
            )
            common_ir_snapshot = _read_regular_json_snapshot(
                common_ir_path, label=f"Gold Common IR {notice_id}"
            )
        except ExistingProfileComparisonError as error:
            raise ExistingProfileSemanticError(
                f"cannot load reviewed Gold companions for {notice_id}: {error}"
            ) from error
        artifact = SemanticArtifact(
            notice_id=notice_id,
            profile=profile,
            source_selection=selection_snapshot.value,
            common_ir=common_ir_snapshot.value,
        )
        _validate_artifact_triple(artifact, label=f"Gold {notice_id}")
        artifacts[notice_id] = artifact
        snapshots[selection_path] = selection_snapshot
        snapshots[common_ir_path] = common_ir_snapshot
    try:
        verification = verify_gold_root(root, expected_notice_count=expected_profile_count)
    except (GoldVerificationError, ValueError, OSError) as error:
        raise ExistingProfileSemanticError(f"Gold verification failed after companion reads: {error}") from error
    _require(verification.status == "valid", "Gold verifier did not return valid status")
    for path, snapshot in snapshots.items():
        try:
            _assert_snapshot_is_current(
                path,
                snapshot,
                label=f"Gold companion {path.parent.name}/{path.name}",
            )
        except ExistingProfileComparisonError as error:
            raise ExistingProfileSemanticError(
                f"reviewed Gold companion changed during verification: {error}"
            ) from error
    return LoadedSemanticArtifacts(artifacts=artifacts, identity=dict(profiles.identity))


def _canonical_value(value: Any) -> Any:
    """Canonicalize object keys while preserving JSON-array order.

    Set- and multiset-like Profile collections are normalized explicitly at
    their schema-aware call sites.  A global sort would corrupt ordered tuples
    such as PDF bounding boxes and character ranges.
    """

    if isinstance(value, Mapping):
        _require(all(isinstance(key, str) for key in value), "semantic object keys must be strings")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float, _JsonNumber)):
        return value
    raise ExistingProfileSemanticError(f"unsupported semantic JSON type: {type(value).__name__}")


def _unordered(values: Sequence[Any]) -> list[Any]:
    """Return one schema-declared unordered collection in canonical order."""

    normalized = [_canonical_value(value) for value in values]
    return sorted(normalized, key=_canonical_bytes)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return _canonical_json(value)
    except ExistingProfileComparisonError as error:
        raise ExistingProfileSemanticError(str(error)) from error


def _atom(kind: str, payload: Mapping[str, Any]) -> tuple[str, str]:
    _require(kind and isinstance(kind, str), "semantic atom kind must be a non-empty string")
    encoded = _canonical_bytes({"kind": kind, "payload": _canonical_value(payload)})
    return sha256(encoded).hexdigest(), kind


def _opaque_counter(counter: Counter[str], kinds: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {"atom_sha256": atom, "kind": kinds[atom], "count": count}
        for atom, count in sorted(counter.items())
        if count > 0
    ]


def _json_integer(value: Any, *, label: str) -> int:
    if isinstance(value, _JsonNumber) and re.fullmatch(r"0|[1-9][0-9]*", value.lexeme):
        return int(value.lexeme)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ExistingProfileSemanticError(f"{label} must be a non-negative JSON integer")


def _native_source_contracts(
    common_ir: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, _NativeSourceContract]:
    """Build the deterministic Common-IR source universe admitted by this gate.

    The transform order follows the reviewed 0.1.4 lexical projections, but
    this function deliberately does not reproduce section-scope or block-router
    eligibility.  It validates source reproducibility, not RunPod CandidatePack
    parity.  Candidate JSON is never the authority for a derived block.
    """

    try:
        prepared, _projection = prepare_common_ir_v1(dict(_plain_json(common_ir)))
    except Exception as error:  # noqa: BLE001 - normalize the vendored boundary
        raise ExistingProfileSemanticError(
            f"{label}.common_ir cannot be projected to the production CandidatePack contract: "
            f"{type(error).__name__}"
        ) from error
    source_blocks = [
        *prepared.fact_candidate_blocks,
        *prepared.table_candidate_blocks,
        *prepared.table_cell_candidate_blocks,
        *prepared.search_only_blocks,
        *prepared.excluded_blocks,
    ]
    contracts: dict[str, _NativeSourceContract] = {}
    for block in source_blocks:
        _require(block.block_id not in contracts, f"{label} has duplicate projected source block ids")
        _require(
            isinstance(block.common_ir_block_id, str) and block.common_ir_block_id,
            f"{label} projected source block lacks Common IR provenance",
        )
        contracts[block.block_id] = _NativeSourceContract(
            text=block.text,
            block_kind=block.block_kind,
            source_order=block.source_order,
            common_ir_block_id=block.common_ir_block_id,
            common_ir_cell_id=block.common_ir_cell_id,
            common_ir_occurrence_ids=tuple(block.common_ir_occurrence_ids),
            source_occurrence_ids=tuple(block.source_occurrence_ids),
            section_id=block.section_id,
        )

    # The reviewed 0.1.4 Common IR projection exposes uniquely identified
    # native pdf-inspector children of a structural ``table_candidate`` as
    # lexical atoms.  The vendored base projector predates that addition, so
    # rebuild the same deterministic atoms here instead of trusting C JSON.
    raw_blocks = common_ir.get("blocks")
    _require(isinstance(raw_blocks, list), f"{label}.common_ir.blocks must be an array")
    occurrence_rows: dict[str, list[Mapping[str, Any]]] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, Mapping):
            continue
        raw_occurrences = raw_block.get("occurrences", [])
        if not isinstance(raw_occurrences, list):
            continue
        for occurrence in raw_occurrences:
            if not isinstance(occurrence, Mapping):
                continue
            occurrence_id = occurrence.get("occurrence_id")
            if isinstance(occurrence_id, str) and occurrence_id:
                occurrence_rows.setdefault(occurrence_id, []).append(occurrence)
    native_pdf_occurrences = {
        occurrence_id: rows[0]
        for occurrence_id, rows in occurrence_rows.items()
        if len(rows) == 1
        and isinstance(rows[0].get("text"), str)
        and rows[0]["text"].strip()
        and isinstance(rows[0].get("provenance"), Mapping)
        and rows[0]["provenance"].get("method") == "pdf_inspector"
    }
    for raw_block in raw_blocks:
        if not isinstance(raw_block, Mapping) or raw_block.get("kind") != "table_candidate":
            continue
        raw_block_id = raw_block.get("block_id")
        raw_order = raw_block.get("reading_order")
        declared = raw_block.get("text_occurrence_ids")
        _require(
            isinstance(raw_block_id, str)
            and raw_block_id
            and isinstance(declared, list),
            f"{label} has an invalid table_candidate block",
        )
        source_order = _json_integer(
            raw_order, label=f"{label} table_candidate.reading_order"
        )
        lexical_occurrences = [
            occurrence_id
            for occurrence_id in declared
            if isinstance(occurrence_id, str)
            and occurrence_id in occurrence_rows
            and any(
                isinstance(row.get("text"), str) and row["text"].strip()
                for row in occurrence_rows[occurrence_id]
            )
        ]
        parent = contracts.get(raw_block_id)
        section_id = parent.section_id if parent is not None else "main_notice"
        for occurrence_index, occurrence_id in enumerate(lexical_occurrences):
            occurrence = native_pdf_occurrences.get(occurrence_id)
            if occurrence is None:
                continue
            block_id = f"{raw_block_id}#native:{occurrence_index:04d}"
            _require(block_id not in contracts, f"{label} has duplicate native table-occurrence candidate ids")
            contracts[block_id] = _NativeSourceContract(
                text=str(occurrence["text"]).strip(),
                block_kind="native_table_occurrence",
                source_order=source_order,
                common_ir_block_id=raw_block_id,
                common_ir_cell_id=None,
                common_ir_occurrence_ids=(occurrence_id,),
                source_occurrence_ids=(occurrence_id,),
                section_id=section_id,
            )

    # Native line atoms expose reversible, trimmed lines from multi-line
    # blocks.  Their ids and offsets match semantic_structuring.native_line_atoms.
    atomic_contracts = list(contracts.items())
    for parent_id, parent in atomic_contracts:
        if (
            parent.block_kind not in _NATIVE_LINE_KINDS
            or ("\n" not in parent.text and "\r" not in parent.text)
        ):
            continue
        for match in _NATIVE_LINE.finditer(parent.text):
            raw = match.group(0)
            left = len(raw) - len(raw.lstrip())
            right = len(raw.rstrip())
            start, end = match.start() + left, match.start() + right
            if end <= start:
                continue
            identity = f"{parent_id}\0{start}\0{end}".encode()
            block_id = f"line:{sha256(identity).hexdigest()[:20]}"
            _require(block_id not in contracts, f"{label} has duplicate native-line candidate ids")
            contracts[block_id] = _NativeSourceContract(
                text=parent.text[start:end],
                block_kind="native_line_atom",
                source_order=parent.source_order,
                common_ir_block_id=parent.common_ir_block_id,
                common_ir_cell_id=parent.common_ir_cell_id,
                common_ir_occurrence_ids=parent.common_ir_occurrence_ids,
                source_occurrence_ids=parent.source_occurrence_ids,
                section_id=parent.section_id,
                native_parent_block_id=parent_id,
                native_start_char=start,
                native_end_char=end,
            )

    source_kind = common_ir.get("document", {}).get("source_kind")
    if source_kind in {"pdf", "hwp", "hwpx"}:
        ordered = sorted(
            contracts.items(),
            key=lambda item: (
                item[1].source_order if item[1].source_order is not None else 10**12,
                item[0],
            ),
        )
        composites: dict[str, _NativeSourceContract] = {}
        for start_index in range(len(ordered) - 1):
            group = [ordered[start_index]]
            while len(group) < 3 and start_index + len(group) < len(ordered):
                following = ordered[start_index + len(group)]
                if not _native_continuation_needed(group[-1][1], following[1]):
                    break
                if (
                    following[1].block_kind == "table_cell"
                    or group[-1][1].block_kind == "table_cell"
                ):
                    break
                group.append(following)
                spans: list[_NativeSourceSpanContract] = []
                for group_index, (source_id, source) in enumerate(group):
                    first = group_index == 0
                    span_start = 0
                    if first:
                        marker = _LEADING_LAYOUT_MARKER.match(source.text)
                        if marker is not None:
                            span_start = marker.end()
                    span_end = len(source.text.rstrip())
                    separator = ""
                    if group_index + 1 < len(group):
                        right_text = group[group_index + 1][1].text.lstrip()
                        left_text = source.text.rstrip()
                        if (
                            left_text
                            and right_text
                            and not left_text[-1].isspace()
                            and not right_text[0].isspace()
                        ):
                            separator = " "
                    spans.append(
                        _NativeSourceSpanContract(
                            source_block_id=source_id,
                            exact_text=source.text[span_start:span_end],
                            start_char=span_start,
                            end_char=span_end,
                            separator_after=separator,
                            source_order=source.source_order or 0,
                            section_id=source.section_id or "main_notice",
                            common_ir_block_id=source.common_ir_block_id,
                            common_ir_cell_id=source.common_ir_cell_id,
                            common_ir_occurrence_ids=source.common_ir_occurrence_ids,
                        )
                    )
                identity = "\0".join(span.source_block_id for span in spans).encode()
                block_id = f"composite:{sha256(identity).hexdigest()[:20]}"
                occurrence_ids = tuple(
                    occurrence_id
                    for span in spans
                    for occurrence_id in span.common_ir_occurrence_ids
                )
                composites[block_id] = _NativeSourceContract(
                    text="".join(span.exact_text + span.separator_after for span in spans),
                    block_kind="native_composite",
                    source_order=spans[0].source_order,
                    common_ir_block_id=spans[0].common_ir_block_id,
                    common_ir_cell_id=None,
                    common_ir_occurrence_ids=occurrence_ids,
                    source_occurrence_ids=occurrence_ids,
                    section_id=spans[0].section_id,
                    source_spans=tuple(spans),
                )
        for block_id, contract in sorted(composites.items()):
            _require(block_id not in contracts, f"{label} has duplicate native-composite candidate ids")
            contracts[block_id] = contract
    _require(contracts, f"{label}.common_ir produced no trusted CandidatePack blocks")
    return contracts


def _native_continuation_needed(
    block: _NativeSourceContract,
    following: _NativeSourceContract,
) -> bool:
    """Mirror the reviewed native continuation admission rule."""

    text = block.text.rstrip()
    bare_number = bool(_BARE_NUMBER.fullmatch(text))
    if not text or (_TERMINAL.search(text) and not bare_number):
        return False
    heading_pair = block.block_kind == following.block_kind == "heading"
    if not heading_pair and (
        block.block_kind not in _CONTINUATION_KINDS
        or following.block_kind not in _CONTINUATION_KINDS
    ):
        return False
    if block.section_id != following.section_id:
        return False
    if heading_pair and not text.endswith((",", ":", ";", "/", "(", "[")):
        return False
    if block.source_order is None or following.source_order is None:
        return False
    if not 0 < following.source_order - block.source_order <= 2:
        return False
    return bool(
        bare_number
        or text.endswith(("및", "또는", "위해", "따라", "대하여", "체납", "지원", "제공", "지"))
        or following.text.lstrip().startswith(("*", "※", "중인", "원하여", "하는", "및 "))
        or not _TERMINAL.search(text)
    )


class _ProvenanceResolver:
    """Resolve generated block/occurrence IDs to immutable Common IR evidence."""

    def __init__(self, source_selection: Mapping[str, Any], common_ir: Mapping[str, Any], *, label: str) -> None:
        self.label = label
        _require(isinstance(source_selection, Mapping), f"{label}.source_selection must be an object")
        _require(isinstance(common_ir, Mapping), f"{label}.common_ir must be an object")
        _require(common_ir.get("schema_version") == COMMON_IR_SCHEMA, f"{label} has an unsupported Common IR schema")
        document = common_ir.get("document")
        blocks = common_ir.get("blocks")
        _require(isinstance(document, Mapping), f"{label}.common_ir.document must be an object")
        _require(isinstance(blocks, list), f"{label}.common_ir.blocks must be an array")
        provenance = document.get("provenance")
        _require(isinstance(provenance, Mapping), f"{label}.common_ir.document.provenance must be an object")
        document_id = document.get("document_id")
        source_sha256 = provenance.get("source_sha256")
        source_kind = document.get("source_kind")
        _require(isinstance(document_id, str) and document_id, f"{label} has invalid Common IR document id")
        _require(isinstance(source_sha256, str) and _SHA256_PATTERN.fullmatch(source_sha256) is not None, f"{label} has invalid Common IR source SHA-256")
        _require(isinstance(source_kind, str) and source_kind, f"{label} has invalid Common IR source kind")
        self.document_id = document_id
        self.source_sha256 = source_sha256
        self.document = {
            "schema_version": COMMON_IR_SCHEMA,
            "source_kind": source_kind,
            "source_sha256": source_sha256,
        }
        identity = source_selection.get("common_ir_identity")
        _require(isinstance(identity, Mapping), f"{label}.source_selection.common_ir_identity must be an object")
        _require(identity.get("source_sha256") == source_sha256, f"{label} selection/Common IR SHA-256 mismatch")
        _require(identity.get("source_kind") == source_kind, f"{label} selection/Common IR source-kind mismatch")
        source_texts = source_selection.get("source_block_texts")
        _require(isinstance(source_texts, Mapping), f"{label}.source_selection.source_block_texts must be an object")
        _require(
            all(isinstance(key, str) and isinstance(text, str) for key, text in source_texts.items()),
            f"{label}.source_selection.source_block_texts must map strings to strings",
        )
        self.source_texts = dict(source_texts)
        self.native_sources = _native_source_contracts(common_ir, label=label)
        self.blocks: dict[str, frozenset[str]] = {}
        self.occurrences: set[str] = set()
        self.occurrence_blocks: dict[str, set[str]] = {}
        self.occurrence_records: dict[str, Mapping[str, Any]] = {}
        self.cells: dict[tuple[str, str], frozenset[str]] = {}
        for index, raw_block in enumerate(blocks):
            block_label = f"{label}.common_ir.blocks[{index}]"
            _require(isinstance(raw_block, Mapping), f"{block_label} must be an object")
            block_id = raw_block.get("block_id")
            text = raw_block.get("text")
            occurrences = raw_block.get("occurrences")
            _require(isinstance(block_id, str) and block_id, f"{block_label}.block_id must be a string")
            _require(isinstance(text, str), f"{block_label}.text must be a string")
            _require(isinstance(occurrences, list), f"{block_label}.occurrences must be an array")
            _require(block_id not in self.blocks, f"{label} has duplicate Common IR block id")
            declared_occurrences = raw_block.get("text_occurrence_ids")
            _require(isinstance(declared_occurrences, list), f"{block_label}.text_occurrence_ids must be an array")
            _require(
                all(isinstance(item, str) and item for item in declared_occurrences),
                f"{block_label}.text_occurrence_ids must contain strings",
            )
            _require(len(declared_occurrences) == len(set(declared_occurrences)), f"{block_label} has duplicate text occurrence ids")
            block_occurrences = set(declared_occurrences)
            for occurrence_index, occurrence in enumerate(occurrences):
                occurrence_label = f"{block_label}.occurrences[{occurrence_index}]"
                _require(isinstance(occurrence, Mapping), f"{occurrence_label} must be an object")
                occurrence_id = occurrence.get("occurrence_id")
                _require(isinstance(occurrence_id, str) and occurrence_id, f"{occurrence_label}.occurrence_id must be a string")
                _require(
                    occurrence_id not in self.occurrence_records,
                    f"{label} has duplicate Common IR occurrence object ids",
                )
                self.occurrence_records[occurrence_id] = occurrence
                block_occurrences.add(occurrence_id)
                self.occurrences.add(occurrence_id)
                self.occurrence_blocks.setdefault(occurrence_id, set()).add(block_id)
            self.occurrences.update(declared_occurrences)
            for occurrence_id in declared_occurrences:
                self.occurrence_blocks.setdefault(occurrence_id, set()).add(block_id)
            cells = raw_block.get("cells", [])
            _require(isinstance(cells, list), f"{block_label}.cells must be an array when present")
            for cell_index, raw_cell in enumerate(cells):
                cell_label = f"{block_label}.cells[{cell_index}]"
                _require(isinstance(raw_cell, Mapping), f"{cell_label} must be an object")
                cell_id = raw_cell.get("cell_id")
                cell_occurrences = raw_cell.get("text_occurrence_ids")
                _require(isinstance(cell_id, str) and cell_id, f"{cell_label}.cell_id must be a string")
                _require(isinstance(cell_occurrences, list), f"{cell_label}.text_occurrence_ids must be an array")
                _require(
                    all(isinstance(item, str) and item in block_occurrences for item in cell_occurrences),
                    f"{cell_label} has unknown occurrence provenance",
                )
                key = (block_id, cell_id)
                _require(key not in self.cells, f"{label} has duplicate Common IR cell ids in one block")
                self.cells[key] = frozenset(cell_occurrences)
            self.blocks[block_id] = frozenset(block_occurrences)
        dangling_occurrences = self.occurrences - set(self.occurrence_records)
        _require(
            not dangling_occurrences,
            f"{label} has Common IR occurrence ids without occurrence objects",
        )

    def block(self, block_id: Any, *, label: str) -> frozenset[str]:
        _require(isinstance(block_id, str) and block_id in self.blocks, f"{label} references an unknown Common IR block")
        return self.blocks[block_id]

    def source_anchor(self, source_id: Any, *, label: str) -> Mapping[str, Any]:
        """Resolve a generated locator to immutable source provenance."""

        _require(isinstance(source_id, str), f"{label} source anchor must be a string")
        _require(
            source_id in self.blocks or source_id in self.occurrences or source_id in self.source_texts,
            f"{label} references an unknown source anchor",
        )
        native = self.native_sources.get(source_id)
        if native is not None:
            return {
                "source_sha256": self.source_sha256,
                "common_ir_block_id": native.common_ir_block_id,
                "common_ir_cell_id": native.common_ir_cell_id,
                "occurrence_ids": sorted(native.common_ir_occurrence_ids),
            }
        if source_id in self.blocks:
            return {
                "source_sha256": self.source_sha256,
                "common_ir_block_id": source_id,
                "common_ir_cell_id": None,
                "occurrence_ids": sorted(self.blocks[source_id]),
            }
        if source_id in self.occurrence_blocks:
            return {
                "source_sha256": self.source_sha256,
                "occurrence_owners": [
                    {"common_ir_block_id": block_id, "occurrence_id": source_id}
                    for block_id in sorted(self.occurrence_blocks[source_id])
                ],
            }
        # Manually adjudicated legacy CandidatePack blocks are permitted for
        # B/G.  C may use them only when its id+text is bound to B separately.
        return {
            "source_sha256": self.source_sha256,
            "candidate_text_sha256": sha256(self.source_texts[source_id].encode("utf-8")).hexdigest(),
        }

    def validate_source_anchor(self, source_id: Any, *, label: str) -> None:
        """Compatibility wrapper for validation-only callers."""

        self.source_anchor(source_id, label=label)

    def evidence(self, evidence: Any, *, label: str) -> Mapping[str, Any]:
        _require(isinstance(evidence, Mapping), f"{label} must be an object")
        unknown = set(evidence) - _EVIDENCE_KEYS
        _require(not unknown, f"{label} has unsupported fields: {sorted(unknown)}")
        missing = _EVIDENCE_REQUIRED_KEYS - set(evidence)
        _require(not missing, f"{label} misses required fields: {sorted(missing)}")
        block_id = evidence.get("common_ir_block_id")
        _require(isinstance(block_id, str) and block_id in self.blocks, f"{label} references an unknown Common IR block")
        _require(evidence.get("common_ir_document_id") == self.document_id, f"{label} references another Common IR document")
        source_block_id = evidence.get("source_block_id")
        _require(isinstance(source_block_id, str) and source_block_id in self.source_texts, f"{label} references an unknown CandidatePack block")
        occurrence_ids = evidence.get("common_ir_occurrence_ids")
        _require(isinstance(occurrence_ids, list), f"{label} occurrence ids must be an array")
        _require(bool(occurrence_ids), f"{label} occurrence ids must not be empty")
        _require(len(occurrence_ids) == len(set(occurrence_ids)), f"{label} occurrence ids must be unique")
        _require(
            all(
                isinstance(occurrence_id, str)
                and occurrence_id in self.occurrence_records
                for occurrence_id in occurrence_ids
            ),
            f"{label} references an unknown Common IR occurrence",
        )
        source_occurrence_ids = evidence.get("source_occurrence_ids", occurrence_ids)
        _require(isinstance(source_occurrence_ids, list), f"{label}.source_occurrence_ids must be an array")
        _require(len(source_occurrence_ids) == len(set(source_occurrence_ids)), f"{label}.source_occurrence_ids must be unique")
        _require(set(source_occurrence_ids) == set(occurrence_ids), f"{label} source/Common-IR occurrence provenance differs")
        section_id = evidence.get("section_id")
        _require(section_id is None or isinstance(section_id, str), f"{label}.section_id must be a string or null")
        native = self.native_sources.get(source_block_id)
        if native is not None:
            _require(block_id == native.common_ir_block_id, f"{label} CandidatePack/Common-IR block provenance differs")
            expected_source_spans = [
                _native_span_payload(span) for span in native.source_spans
            ]
            if expected_source_spans:
                supplied_source_spans = evidence.get("source_spans")
                _require(
                    isinstance(supplied_source_spans, list)
                    and _canonical_bytes(supplied_source_spans)
                    == _canonical_bytes(expected_source_spans),
                    f"{label}.source_spans do not match the trusted composite",
                )
            else:
                _require(
                    "source_spans" not in evidence,
                    f"{label}.source_spans are not valid for an atomic CandidatePack block",
                )
            allowed_occurrences = (
                set(native.common_ir_occurrence_ids)
                if native.source_spans
                else set(self.blocks[block_id])
            )
            _require(
                set(occurrence_ids).issubset(allowed_occurrences),
                f"{label} occurrence provenance differs from the trusted CandidatePack block",
            )
            if native.common_ir_cell_id is not None:
                if "common_ir_cell_id" in evidence:
                    _require(
                        evidence.get("common_ir_cell_id") == native.common_ir_cell_id,
                        f"{label}.common_ir_cell_id does not match the Common IR projection",
                    )
                else:
                    # Two frozen pre-v0.1.4 rows materialize one exact table-cell
                    # value while retaining the whole parent-table occurrence
                    # set and omitting the optional cell locator.  Keep that
                    # narrowly proven legacy shape readable: the source id and
                    # text are still bound to the regenerated cell candidate,
                    # and its native occurrence must be present.  A supplied
                    # locator remains subject to the stricter cell-membership
                    # checks below.
                    _require(
                        set(native.common_ir_occurrence_ids).issubset(occurrence_ids),
                        f"{label} legacy table-cell evidence omits its native occurrence",
                    )
        else:
            _require(
                set(occurrence_ids).issubset(self.blocks[block_id]),
                f"{label} occurrence does not belong to its Common IR block",
            )
        if "common_ir_cell_id" in evidence:
            cell_id = evidence.get("common_ir_cell_id")
            _require(isinstance(cell_id, str) and cell_id, f"{label}.common_ir_cell_id must be a string")
            cell_occurrences = self.cells.get((block_id, cell_id))
            _require(cell_occurrences is not None, f"{label}.common_ir_cell_id is absent from its Common IR block")
            _require(set(occurrence_ids).issubset(cell_occurrences), f"{label} occurrences do not belong to its Common IR cell")
        if "text" in evidence:
            _require(
                isinstance(evidence["text"], str)
                and evidence["text"] == self.source_texts[source_block_id],
                f"{label}.text does not match its trusted CandidatePack block",
            )
        return {
            "source_sha256": self.source_sha256,
            "occurrence_ids": sorted(occurrence_ids),
        }

    def validate_value_source(self, value_source: Any, value_raw: Any, *, label: str) -> tuple[str, int, int]:
        _require(isinstance(value_source, Mapping), f"{label} must be an object")
        _require(set(value_source) == _VALUE_SOURCE_KEYS, f"{label} must use the exact v0.2 span contract")
        _require(value_source.get("text_basis") == "common_ir_v1_candidate_pack", f"{label} has an unsupported text basis")
        block_id = value_source.get("source_block_id")
        _require(isinstance(block_id, str) and block_id in self.source_texts, f"{label} must reference a CandidatePack source block")
        start = _json_integer(value_source.get("start_char"), label=f"{label}.start_char")
        end = _json_integer(value_source.get("end_char"), label=f"{label}.end_char")
        _require(0 <= start < end <= len(self.source_texts[block_id]), f"{label} span is outside CandidatePack source text")
        _require(isinstance(value_raw, str) and self.source_texts[block_id][start:end] == value_raw, f"{label} does not exactly materialize value_raw")
        return block_id, start, end


def _validate_candidate_materialized_source(
    raw_source: Any,
    *,
    trusted: Mapping[str, _NativeSourceContract],
    document_id: str,
    expected_text: str,
    label: str,
) -> None:
    """Check one C materialization against a server-regenerated source block."""

    _require(isinstance(raw_source, Mapping), f"{label} must be an object")
    allowed = {
        "source_block_id", "text", "section_id", "source_occurrence_ids",
        "common_ir_document_id", "common_ir_block_id", "common_ir_cell_id",
        "common_ir_occurrence_ids", "source_spans", "native_parent_span",
    }
    unknown = set(raw_source) - allowed
    _require(not unknown, f"{label} has unsupported materialized source fields: {sorted(unknown)}")
    source_block_id = raw_source.get("source_block_id")
    _require(
        isinstance(source_block_id, str) and source_block_id in trusted,
        f"{label} references an untrusted CandidatePack block",
    )
    contract = trusted[source_block_id]
    _require(raw_source.get("text") == expected_text, f"{label}.text is not the server-resolved span")
    _require(raw_source.get("section_id") == contract.section_id, f"{label}.section_id differs from the trusted source")
    _require(raw_source.get("common_ir_document_id") == document_id, f"{label} references another Common IR document")
    _require(raw_source.get("common_ir_block_id") == contract.common_ir_block_id, f"{label}.common_ir_block_id differs from the trusted source")
    _require(
        raw_source.get("common_ir_cell_id") == contract.common_ir_cell_id,
        f"{label}.common_ir_cell_id differs from the trusted source",
    )
    _require(
        _canonical_bytes(raw_source.get("source_occurrence_ids"))
        == _canonical_bytes(list(contract.source_occurrence_ids)),
        f"{label}.source_occurrence_ids differ from the trusted source",
    )
    _require(
        _canonical_bytes(raw_source.get("common_ir_occurrence_ids"))
        == _canonical_bytes(list(contract.common_ir_occurrence_ids)),
        f"{label}.common_ir_occurrence_ids differ from the trusted source",
    )

    expected_spans = [_native_span_payload(span) for span in contract.source_spans]
    if expected_spans:
        _require(
            _canonical_bytes(raw_source.get("source_spans"))
            == _canonical_bytes(expected_spans),
            f"{label}.source_spans differ from the trusted composite",
        )
    else:
        _require("source_spans" not in raw_source, f"{label}.source_spans are invalid for an atomic source")

    if contract.native_parent_block_id is not None:
        expected_parent_span = {
            "source_block_id": contract.native_parent_block_id,
            "start_char": contract.native_start_char,
            "end_char": contract.native_end_char,
            "exact_text": contract.text,
        }
        _require(
            _canonical_bytes(raw_source.get("native_parent_span"))
            == _canonical_bytes(expected_parent_span),
            f"{label}.native_parent_span differs from the trusted line atom",
        )
    else:
        _require(
            "native_parent_span" not in raw_source,
            f"{label}.native_parent_span is invalid for this source block",
        )


def _validate_candidate_source_basis(
    candidate: SemanticArtifact,
    *,
    label: str,
) -> None:
    """Bind every C CandidatePack block to a server-regenerated contract.

    Native routing may legitimately select a different subset than B, so C is
    not required to reproduce B's whole source-block map.  It may not reuse a
    baseline-only or Gold adjudication block: every id and byte of text must be
    reproducible from the pinned Common IR and the fixed transform above.
    """

    candidate_texts = candidate.source_selection.get("source_block_texts")
    _require(isinstance(candidate_texts, Mapping), f"{label} candidate source_block_texts must be an object")
    trusted = _native_source_contracts(candidate.common_ir, label=label)
    for source_block_id, text in candidate_texts.items():
        _require(
            isinstance(source_block_id, str) and source_block_id and isinstance(text, str) and text,
            f"{label} candidate source_block_texts must contain non-empty string pairs",
        )
        trusted_contract = trusted.get(source_block_id)
        _require(
            trusted_contract is not None and text == trusted_contract.text,
            f"{label} candidate source block is not exactly reproducible from pinned Common IR",
        )

    numeric_candidates = candidate.source_selection.get("numeric_candidates")
    _require(isinstance(numeric_candidates, list), f"{label}.numeric_candidates must be an array")
    numeric_spans_by_block: dict[str, frozenset[tuple[int, int, str]]] = {}
    for numeric_index, raw_numeric in enumerate(numeric_candidates):
        numeric_label = f"{label}.numeric_candidates[{numeric_index}]"
        _require(isinstance(raw_numeric, Mapping), f"{numeric_label} must be an object")
        source_block_id = raw_numeric.get("source_block_id")
        anchor_text = raw_numeric.get("anchor_text")
        _require(
            isinstance(source_block_id, str)
            and source_block_id in candidate_texts
            and source_block_id in trusted,
            f"{numeric_label} is outside the admitted CandidatePack",
        )
        _require(isinstance(anchor_text, str) and anchor_text, f"{numeric_label}.anchor_text must be non-empty")
        start = _json_integer(raw_numeric.get("start_char"), label=f"{numeric_label}.start_char")
        end = _json_integer(raw_numeric.get("end_char"), label=f"{numeric_label}.end_char")
        enumerated_spans = numeric_spans_by_block.get(source_block_id)
        if enumerated_spans is None:
            enumerated_spans = _server_enumerated_numeric_spans(
                candidate_texts[source_block_id]
            )
            numeric_spans_by_block[source_block_id] = enumerated_spans
        _require(
            (start, end, anchor_text) in enumerated_spans,
            f"{numeric_label} is not an independently enumerated complete numeric token",
        )

    components = candidate.profile.get("support_components")
    _require(isinstance(components, list), f"{label}.profile.support_components must be an array")
    for component_index, raw_component in enumerate(components):
        component_label = f"{label}.profile.support_components[{component_index}]"
        _require(isinstance(raw_component, Mapping), f"{component_label} must be an object")
        for source_key in ("source_block_ids", "table_block_ids"):
            source_ids = raw_component.get(source_key)
            _require(isinstance(source_ids, list), f"{component_label}.{source_key} must be an array")
            for source_index, source_block_id in enumerate(source_ids):
                _require(
                    isinstance(source_block_id, str)
                    and source_block_id in candidate_texts
                    and source_block_id in trusted,
                    f"{component_label}.{source_key}[{source_index}] is outside the admitted CandidatePack",
                )
        name_raw = raw_component.get("name_raw")
        name_source_block_id = raw_component.get("name_source_block_id")
        if name_raw is not None:
            _require(
                isinstance(name_raw, str)
                and name_raw
                and isinstance(name_source_block_id, str)
                and name_source_block_id in candidate_texts
                and name_source_block_id in trusted,
                f"{component_label} name source is outside the admitted CandidatePack",
            )
            _require(
                candidate_texts[name_source_block_id].count(name_raw) == 1,
                f"{component_label}.name_raw must occur exactly once in its admitted source block",
            )

    document = candidate.common_ir.get("document")
    _require(isinstance(document, Mapping), f"{label}.common_ir.document must be an object")
    document_id = document.get("document_id")
    _require(isinstance(document_id, str) and document_id, f"{label} has no Common IR document id")
    materialized = candidate.source_selection.get("materialized_evidence")
    _require(isinstance(materialized, list), f"{label}.materialized_evidence must be an array")
    for row_index, raw_row in enumerate(materialized):
        row_label = f"{label}.materialized_evidence[{row_index}]"
        _require(isinstance(raw_row, Mapping), f"{row_label} must be an object")
        value_source = raw_row.get("value_source")
        _require(isinstance(value_source, Mapping), f"{row_label}.value_source must be an object")
        value_block_id = value_source.get("source_block_id")
        _require(
            isinstance(value_block_id, str) and value_block_id in trusted,
            f"{row_label}.value_source references an untrusted CandidatePack block",
        )
        value_contract = trusted[value_block_id]
        start = _json_integer(value_source.get("start_char"), label=f"{row_label}.value_source.start_char")
        end = _json_integer(value_source.get("end_char"), label=f"{row_label}.value_source.end_char")
        _require(0 <= start < end <= len(value_contract.text), f"{row_label}.value_source span is outside the trusted source")
        source_blocks = raw_row.get("source_blocks")
        _require(
            isinstance(source_blocks, list) and len(source_blocks) == 1,
            f"{row_label}.source_blocks must contain exactly one source",
        )
        _validate_candidate_materialized_source(
            source_blocks[0],
            trusted=trusted,
            document_id=document_id,
            expected_text=value_contract.text[start:end],
            label=f"{row_label}.source_blocks[0]",
        )
        context_blocks = raw_row.get("context_blocks")
        _require(isinstance(context_blocks, list), f"{row_label}.context_blocks must be an array")
        for context_index, raw_context in enumerate(context_blocks):
            _require(isinstance(raw_context, Mapping), f"{row_label}.context_blocks[{context_index}] must be an object")
            context_id = raw_context.get("source_block_id")
            _require(
                isinstance(context_id, str) and context_id in trusted,
                f"{row_label}.context_blocks[{context_index}] references an untrusted CandidatePack block",
            )
            _validate_candidate_materialized_source(
                raw_context,
                trusted=trusted,
                document_id=document_id,
                expected_text=trusted[context_id].text,
                label=f"{row_label}.context_blocks[{context_index}]",
            )


def _fact_base(
    fact: Mapping[str, Any],
    *,
    resolver: _ProvenanceResolver,
    label: str,
) -> Mapping[str, Any]:
    keys = set(fact)
    unknown = keys - _FACT_IGNORED_KEYS - _FACT_SEMANTIC_KEYS
    _require(not unknown, f"{label} has unsupported fact fields: {sorted(unknown)}")
    missing = _FACT_REQUIRED_KEYS - keys
    _require(not missing, f"{label} misses required fact fields: {sorted(missing)}")
    for key in ("field_name", "value_raw", "status", "scope"):
        _require(isinstance(fact[key], str), f"{label}.{key} must be a string")
    _require(fact["field_name"] in _FACT_FIELDS, f"{label}.field_name is unsupported")
    _require(fact["status"] in _FACT_STATUSES, f"{label}.status is unsupported")
    _require(fact["scope"] in _FACT_SCOPES, f"{label}.scope is unsupported")
    _require(fact.get("model_summary") is None or isinstance(fact.get("model_summary"), str), f"{label}.model_summary must be a string or null")
    for key in ("subject_role", "semantic_role"):
        _require(fact.get(key) is None or isinstance(fact.get(key), str), f"{label}.{key} must be a string or null")
    resolver.validate_value_source(
        fact["value_source"], fact["value_raw"], label=f"{label}.value_source"
    )
    result = {
        key: fact[key]
        for key in ("field_name", "value_raw", "status", "scope", "subject_role", "semantic_role")
    }
    for evidence_key in ("evidence", "context_evidence"):
        raw_evidence = fact[evidence_key]
        _require(isinstance(raw_evidence, list), f"{label}.{evidence_key} must be an array")
        if evidence_key == "evidence":
            _require(bool(raw_evidence), f"{label}.evidence must not be empty")
        normalized_evidence = [
            resolver.evidence(item, label=f"{label}.{evidence_key}[{index}]")
            for index, item in enumerate(raw_evidence)
        ]
        occurrence_ids = [
            occurrence_id
            for item in normalized_evidence
            for occurrence_id in item["occurrence_ids"]
        ]
        _require(
            len(occurrence_ids) == len(set(occurrence_ids)),
            f"{label}.{evidence_key} repeats Common IR occurrence provenance",
        )
        result[evidence_key] = _unordered(normalized_evidence)
    if "organization_names" in fact:
        organizations = fact["organization_names"]
        _require(isinstance(organizations, list) and organizations, f"{label}.organization_names must be a non-empty array")
        normalized_organizations: list[Mapping[str, Any]] = []
        for index, organization in enumerate(organizations):
            organization_label = f"{label}.organization_names[{index}]"
            _require(isinstance(organization, Mapping), f"{organization_label} must be an object")
            _require(set(organization) == {"value_raw", "value_source"}, f"{organization_label} has invalid fields")
            value_raw = organization.get("value_raw")
            _require(isinstance(value_raw, str) and value_raw, f"{organization_label}.value_raw must be non-empty")
            resolver.validate_value_source(
                organization["value_source"], value_raw, label=f"{organization_label}.value_source"
            )
            normalized_organizations.append({
                "value_raw": value_raw,
            })
        result["organization_names"] = _unordered(normalized_organizations)
    if "role_raw" in fact:
        _require(isinstance(fact["role_raw"], str) and fact["role_raw"], f"{label}.role_raw must be non-empty")
        result["role_raw"] = fact["role_raw"]
    if "canonical_role" in fact:
        _require(fact["canonical_role"] is None or isinstance(fact["canonical_role"], str), f"{label}.canonical_role must be a string or null")
        result["canonical_role"] = fact["canonical_role"]
    if "role_source" in fact:
        _require("role_raw" in fact, f"{label}.role_source requires role_raw")
        resolver.validate_value_source(
            fact["role_source"], fact["role_raw"], label=f"{label}.role_source"
        )
    if "role_source_block_id" in fact and fact["role_source_block_id"] is not None:
        resolver.validate_source_anchor(
            fact["role_source_block_id"], label=f"{label}.role_source_block_id"
        )
    return result


def _component_base(component: Mapping[str, Any], *, resolver: _ProvenanceResolver, label: str) -> Mapping[str, Any]:
    unknown = set(component) - _COMPONENT_KEYS
    _require(not unknown, f"{label} has unsupported component fields: {sorted(unknown)}")
    required = _COMPONENT_KEYS
    missing = required - set(component)
    _require(not missing, f"{label} misses required component fields: {sorted(missing)}")
    _require(isinstance(component["support_component_id"], str), f"{label}.support_component_id must be a string")
    _require(component["component_kind"] in _COMPONENT_KINDS, f"{label}.component_kind is unsupported")
    _require(isinstance(component["facts"], list), f"{label}.facts must be an array")
    result = {
        key: component[key]
        for key in ("component_kind", "name_raw", "name_status")
    }
    name_raw = component["name_raw"]
    name_source_block_id = component.get("name_source_block_id")
    _require(name_raw is None or isinstance(name_raw, str), f"{label}.name_raw must be a string or null")
    _require(isinstance(component["name_status"], str) and component["name_status"], f"{label}.name_status must be non-empty")
    component_source_ids: set[str] = set()
    for source_key in ("source_block_ids", "table_block_ids"):
        raw_ids = component[source_key]
        _require(isinstance(raw_ids, list), f"{label}.{source_key} must be an array")
        _require(len(raw_ids) == len(set(raw_ids)), f"{label}.{source_key} must not contain duplicates")
        for source_index, source_id in enumerate(raw_ids):
            resolver.validate_source_anchor(
                source_id, label=f"{label}.{source_key}[{source_index}]"
            )
            component_source_ids.add(source_id)
    if name_raw is None:
        _require(name_source_block_id is None, f"{label} empty name must not have source provenance")
    else:
        _require(bool(name_raw), f"{label}.name_raw must be non-empty when identified")
        _require(
            isinstance(name_source_block_id, str)
            and name_source_block_id in component_source_ids,
            f"{label}.name_source_block_id must reference a component source block",
        )
        _require(
            name_source_block_id in resolver.source_texts,
            f"{label}.name_source_block_id must reference an admitted CandidatePack block",
        )
        _require(
            resolver.source_texts[name_source_block_id].count(name_raw) == 1,
            f"{label}.name_raw must occur exactly once in its source block",
        )
    return result


def _optional_json_integer(value: Any, *, label: str) -> int | None:
    if value is None:
        return None
    return _json_integer(value, label=label)


def _exact_decimal_integer(value: str, *, factor: int, label: str) -> int:
    try:
        normalized = Decimal(value.replace(",", "")) * factor
    except InvalidOperation as error:
        raise ExistingProfileSemanticError(f"{label} contains an invalid numeric token") from error
    _require(normalized == normalized.to_integral_value(), f"{label} cannot be represented as an integer")
    return int(normalized)


def _normalized_numeric_token(anchor_text: str, *, label: str) -> tuple[str, int, str]:
    """Normalize one complete server-enumerated amount/rate/count token."""

    amount = _AMOUNT_TOKEN.fullmatch(anchor_text)
    if amount is not None:
        factor = {None: 1, "천": 1_000, "만": 10_000, "억": 100_000_000}[
            amount.group("suffix")
        ]
        return (
            "amount",
            _exact_decimal_integer(amount.group("number"), factor=factor, label=label),
            "KRW",
        )
    rate = _RATE_TOKEN.fullmatch(anchor_text)
    if rate is not None:
        return (
            "rate",
            _exact_decimal_integer(rate.group("number"), factor=100, label=label),
            "BPS",
        )
    count = _COUNT_TOKEN.fullmatch(anchor_text)
    if count is not None:
        return "count", int(count.group("number")), count.group("unit")
    raise ExistingProfileSemanticError(
        f"{label} is not a complete supported amount, rate, or count token"
    )


def _server_enumerated_numeric_spans(
    source_text: str,
) -> frozenset[tuple[int, int, str]]:
    """Enumerate production complete-token spans once for one source block."""

    return frozenset(
        (match.start(), match.end(), match.group(0))
        for match in _NUMERIC_CANDIDATE_PATTERN.finditer(source_text)
    )


def _validate_measure_locator_value(
    measure: Mapping[str, Any],
    locator: _NumericLocator,
    *,
    label: str,
) -> None:
    measure_type, numeric_value, unit = _normalized_numeric_token(
        locator.anchor_text, label=f"{label}.source_numeric_candidate"
    )
    _require(measure.get("measure_type") == measure_type, f"{label}.measure_type disagrees with its numeric locator")
    _require(measure.get("unit") == unit, f"{label}.unit disagrees with its numeric locator")
    expected_role = {
        "rate": {"support_rate"},
        "count": {"selection_capacity"},
        "amount": {"support_amount", "support_limit"},
    }[measure_type]
    _require(measure.get("measure_role") in expected_role, f"{label}.measure_role disagrees with its numeric locator")
    lower = _optional_json_integer(measure.get("lower_value"), label=f"{label}.lower_value")
    upper = _optional_json_integer(measure.get("upper_value"), label=f"{label}.upper_value")
    comparator = measure.get("comparator")
    if comparator in {"lt", "lte"}:
        _require(lower is None and upper == numeric_value, f"{label} upper bound disagrees with its numeric locator")
    elif comparator in {"gt", "gte"}:
        _require(lower == numeric_value and upper is None, f"{label} lower bound disagrees with its numeric locator")
    elif comparator in {"eq", "approx"}:
        _require(lower == upper == numeric_value, f"{label} value disagrees with its numeric locator")
    else:
        # v0.2 supplies one numeric locator per measure, so a two-bound range
        # cannot be provenance-complete under the current contract.
        raise ExistingProfileSemanticError(
            f"{label} range comparator is not modeled by semantic gate v1"
        )


def _validate_measure_shape(measure: Mapping[str, Any], *, label: str) -> None:
    """Validate deterministic support-scale measure semantics."""

    measure_type = measure.get("measure_type")
    measure_role = measure.get("measure_role")
    comparator = measure.get("comparator")
    unit = measure.get("unit")
    _require(measure_type in {"amount", "rate", "count"}, f"{label}.measure_type is unsupported")
    _require(
        measure_role in {"selection_capacity", "support_amount", "support_limit", "support_rate"},
        f"{label}.measure_role is unsupported",
    )
    _require(comparator in {"eq", "lt", "lte", "gt", "gte", "range", "approx"}, f"{label}.comparator is unsupported")
    _require(isinstance(unit, str) and unit, f"{label}.unit must be non-empty")
    if measure_type == "amount":
        _require(unit == "KRW", f"{label} amount unit must be KRW")
    elif measure_type == "rate":
        _require(unit == "BPS", f"{label} rate unit must be BPS")
    else:
        _require(unit not in {"KRW", "BPS", "DAY", "MONTH", "YEAR"}, f"{label} count unit is invalid")
    lower = _optional_json_integer(measure.get("lower_value"), label=f"{label}.lower_value")
    upper = _optional_json_integer(measure.get("upper_value"), label=f"{label}.upper_value")
    if comparator in {"eq", "approx"}:
        _require(lower is not None and lower == upper, f"{label} eq/approx bounds must be equal")
    elif comparator in {"lt", "lte"}:
        _require(lower is None and upper is not None, f"{label} upper-bound comparator has invalid bounds")
    elif comparator in {"gt", "gte"}:
        _require(lower is not None and upper is None, f"{label} lower-bound comparator has invalid bounds")
    else:
        _require(lower is not None and upper is not None and lower <= upper, f"{label} range bounds are invalid")
    applies_per = measure.get("applies_per")
    aggregation_scope = measure.get("aggregation_scope")
    _require(applies_per is None or isinstance(applies_per, str), f"{label}.applies_per must be a string or null")
    _require(aggregation_scope in {None, "TOTAL", "PER_UNIT"}, f"{label}.aggregation_scope is unsupported")
    if aggregation_scope == "PER_UNIT":
        _require(isinstance(applies_per, str) and bool(applies_per), f"{label} PER_UNIT requires applies_per")
    if aggregation_scope == "TOTAL":
        _require(applies_per is None, f"{label} TOTAL must not have applies_per")
    _require(
        measure.get("calculation_basis") in {None, "WAGE", "TOTAL_PROJECT_COST", "ELIGIBLE_COST"},
        f"{label}.calculation_basis is unsupported",
    )
    _require(measure.get("frequency") is None or isinstance(measure.get("frequency"), str), f"{label}.frequency must be a string or null")


def _projection_consistency_value(projection: Any, *, label: str) -> Mapping[str, Any]:
    """Normalize only schema-declared set-like projection collections."""

    _require(isinstance(projection, Mapping), f"{label} must be an object")
    normalized = dict(projection)
    for key in (
        "source_fact_ids",
        "positive_source_fact_ids",
        "exclusion_source_fact_ids",
        "activities",
        "methods",
        "items",
    ):
        if key in normalized:
            values = normalized[key]
            _require(isinstance(values, list), f"{label}.{key} must be an array")
            normalized[key] = _unordered(values)
    if "measures" in normalized:
        measures = normalized["measures"]
        _require(isinstance(measures, list), f"{label}.measures must be an array")
        normalized["measures"] = _unordered(measures)
    return _canonical_value(normalized)


def build_semantic_graph(
    artifact: SemanticArtifact,
    *,
    label: str = "profile",
) -> SemanticGraph:
    """Turn a Profile into an ID/order-insensitive exact semantic multigraph.

    It fails closed on unrecognized fact/component fields or dangling links so
    a schema change cannot silently remove semantic information from a gate.
    """

    _require(isinstance(artifact, SemanticArtifact), "provenance-grade comparison requires Profile+source-selection+Common-IR artifact")
    _validate_artifact_triple(artifact, label=label)
    profile = artifact.profile
    _require(isinstance(profile, Mapping), f"{label} must be an object")
    allowed_root = {
        "schema_version", "notice_id", "source_profile_id", "source_documents",
        "comparison_profile", "support_components", "derived_projections",
        "identity", "table_catalog", "unresolved_observations", "unresolved_relations",
        "processing_metadata",
    }
    _require(set(profile) == allowed_root, f"{label} must use the exact Existing Profile v0.2 root contract")
    _require(profile.get("schema_version") == PROFILE_SCHEMA, f"{label}.schema_version is unsupported")
    _require(
        profile.get("notice_id") in {artifact.notice_id, f"bizinfo:{artifact.notice_id}"},
        f"{label}.notice_id does not match its logical notice",
    )
    resolver = _ProvenanceResolver(artifact.source_selection, artifact.common_ir, label=label)
    _require(profile.get("source_profile_id") == resolver.document_id, f"{label}.source_profile_id does not match Common IR")
    comparison = profile.get("comparison_profile")
    components = profile.get("support_components")
    projections = profile.get("derived_projections")
    _require(isinstance(comparison, Mapping), f"{label}.comparison_profile must be an object")
    _require(isinstance(components, list), f"{label}.support_components must be an array")
    _require(isinstance(projections, list), f"{label}.derived_projections must be an array")
    for projection_type, selection_key in (
        ("support_facets", "support_facets"),
        ("support_scale_measures", "support_scale_measures"),
    ):
        selected_projections = artifact.source_selection.get(selection_key)
        _require(isinstance(selected_projections, list), f"{label}.source_selection.{selection_key} must be an array")
        profile_projections = [
            projection
            for projection in projections
            if isinstance(projection, Mapping) and projection.get("projection_type") == projection_type
        ]
        normalized_selected = [
            _projection_consistency_value(
                projection,
                label=f"{label}.source_selection.{selection_key}[{index}]",
            )
            for index, projection in enumerate(selected_projections)
        ]
        normalized_profile = [
            _projection_consistency_value(
                projection,
                label=f"{label}.derived_projections[{index}]",
            )
            for index, projection in enumerate(profile_projections)
        ]
        _require(
            _canonical_bytes(_unordered(normalized_selected))
            == _canonical_bytes(_unordered(normalized_profile)),
            f"{label} Profile and source-selection {selection_key} projections differ",
        )

    atoms: Counter[str] = Counter()
    kinds: dict[str, str] = {}

    def add(kind: str, payload: Mapping[str, Any]) -> str:
        digest, known_kind = _atom(kind, payload)
        previous = kinds.setdefault(digest, known_kind)
        _require(previous == known_kind, "SHA-256 atom kind collision")
        atoms[digest] += 1
        return digest

    # The source SHA/kind/schema is semantic provenance. Deployment paths and
    # generated document/profile ids are validation-only and deliberately
    # excluded from equality.
    add("profile_provenance", resolver.document)
    source_documents = profile["source_documents"]
    _require(isinstance(source_documents, list) and source_documents, f"{label}.source_documents must be a non-empty array")
    for index, raw_document in enumerate(source_documents):
        document_label = f"{label}.source_documents[{index}]"
        _require(isinstance(raw_document, Mapping), f"{document_label} must be an object")
        _require(set(raw_document) == _SOURCE_DOCUMENT_KEYS, f"{document_label} has unsupported or missing fields")
        lineage = raw_document.get("common_ir")
        _require(isinstance(lineage, Mapping), f"{document_label}.common_ir must be an object")
        _require(set(lineage) == _COMMON_IR_LINEAGE_KEYS, f"{document_label}.common_ir has unsupported or missing fields")
        normalized_document = {
            key: raw_document[key]
            for key in ("document_name", "format", "source_url", "notice_detail_url")
        }
        normalized_document["common_ir"] = {
            key: lineage[key]
            for key in ("schema_version", "source_kind", "source_sha256", "artifact_role")
        }
        add("source_document", normalized_document)

    identity = profile["identity"]
    _require(isinstance(identity, Mapping) and set(identity) == _IDENTITY_KEYS, f"{label}.identity has unsupported or missing fields")
    for source_key in ("title_source_block_ids", "notice_date_source_block_ids"):
        source_ids = identity[source_key]
        _require(isinstance(source_ids, list), f"{label}.identity.{source_key} must be an array")
        _require(len(source_ids) == len(set(source_ids)), f"{label}.identity.{source_key} contains duplicates")
        for source_index, source_id in enumerate(source_ids):
            resolver.validate_source_anchor(source_id, label=f"{label}.identity.{source_key}[{source_index}]")
    add(
        "profile_identity",
        {
            "title_raw": identity["title_raw"],
            "notice_date_raw": identity["notice_date_raw"],
            "source_url": identity["source_url"],
        },
    )
    table_catalog = profile["table_catalog"]
    unresolved_relations = profile["unresolved_relations"]
    _require(isinstance(table_catalog, list) and not table_catalog, f"{label}.table_catalog is not modeled by semantic gate v1")
    _require(isinstance(unresolved_relations, list) and not unresolved_relations, f"{label}.unresolved_relations is not modeled by semantic gate v1")
    unresolved_observations = profile["unresolved_observations"]
    _require(isinstance(unresolved_observations, list), f"{label}.unresolved_observations must be an array")
    for index, observation in enumerate(unresolved_observations):
        observation_label = f"{label}.unresolved_observations[{index}]"
        _require(isinstance(observation, Mapping), f"{observation_label} must be an object")
        _require(set(observation) == _UNRESOLVED_OBSERVATION_KEYS, f"{observation_label} has unsupported or missing fields")
        for key in ("kind", "status", "value_raw"):
            _require(isinstance(observation[key], str) and observation[key], f"{observation_label}.{key} must be non-empty")
        _require(observation["reason"] is None or isinstance(observation["reason"], str), f"{observation_label}.reason must be a string or null")
        add(
            "unresolved_observation",
            {key: observation[key] for key in ("kind", "status", "value_raw", "reason")},
        )

    raw_fact_by_id: dict[str, Mapping[str, Any]] = {}
    fact_base_by_id: dict[str, Mapping[str, Any]] = {}

    def register_fact(raw_fact: Any, *, fact_label: str, expected_field: str | None = None, component_id: str | None = None) -> None:
        _require(isinstance(raw_fact, Mapping), f"{fact_label} must be an object")
        if expected_field is not None:
            _require(raw_fact.get("field_name") == expected_field, f"{fact_label}.field_name must match its bucket")
        fact_id = raw_fact.get("fact_id")
        _require(isinstance(fact_id, str) and fact_id, f"{fact_label}.fact_id must be a non-empty string")
        _require(fact_id not in raw_fact_by_id, f"{label} has duplicate fact id")
        if component_id is not None:
            _require(raw_fact.get("support_component_id") == component_id, f"{fact_label} component pointer disagrees with containment")
            _require(raw_fact.get("scope") == "component", f"{fact_label} contained fact must have component scope")
        else:
            _require(raw_fact.get("scope") == "notice", f"{fact_label} top-level fact must have notice scope")
            _require(raw_fact.get("support_component_id") is None, f"{fact_label} notice-scoped fact cannot own a component")
        raw_fact_by_id[fact_id] = raw_fact
        fact_base_by_id[fact_id] = _fact_base(raw_fact, resolver=resolver, label=fact_label)

    unknown_fields = set(comparison) - _FACT_FIELDS
    _require(not unknown_fields, f"{label}.comparison_profile has unsupported fields: {sorted(unknown_fields)}")
    for field_name in sorted(comparison):
        raw_facts = comparison[field_name]
        _require(isinstance(raw_facts, list), f"{label}.comparison_profile.{field_name} must be an array")
        for index, raw_fact in enumerate(raw_facts):
            register_fact(raw_fact, fact_label=f"{label}.comparison_profile.{field_name}[{index}]", expected_field=field_name)

    component_bases: dict[str, Mapping[str, Any]] = {}
    component_records: list[tuple[str, Mapping[str, Any]]] = []
    for index, raw_component in enumerate(components):
        component_label = f"{label}.support_components[{index}]"
        _require(isinstance(raw_component, Mapping), f"{component_label} must be an object")
        component_id = raw_component.get("support_component_id")
        _require(isinstance(component_id, str) and component_id, f"{component_label} has invalid component id")
        _require(component_id not in component_bases, f"{label} has duplicate component id")
        base = _component_base(raw_component, resolver=resolver, label=component_label)
        component_bases[component_id] = base
        component_records.append((component_id, raw_component))
        for fact_index, raw_fact in enumerate(raw_component["facts"]):
            register_fact(
                raw_fact,
                fact_label=f"{component_label}.facts[{fact_index}]",
                component_id=component_id,
            )

    # Fact nodes retain their component association semantically, but replace
    # the generated component id by the exact component node payload.
    fact_atom_by_id: dict[str, str] = {}
    fact_id_by_atom: dict[str, str] = {}
    for fact_id, raw_fact in raw_fact_by_id.items():
        base = fact_base_by_id[fact_id]
        semantic = dict(base)
        component_id = raw_fact.get("support_component_id")
        if component_id is not None:
            _require(
                isinstance(component_id, str) and component_id in component_bases,
                f"{label} fact references an unknown support component",
            )
            semantic["support_component"] = component_bases[component_id]
        fact_atom, _ = _atom("fact", semantic)
        _require(
            fact_atom not in fact_id_by_atom,
            f"{label} has ambiguous duplicate semantic facts: {fact_id_by_atom.get(fact_atom)!r}, {fact_id!r}",
        )
        fact_id_by_atom[fact_atom] = fact_id
        fact_atom_by_id[fact_id] = add("fact", semantic)

    # Component identity includes its fact-membership multiset. This accepts
    # same-kind/name components with different contents, but ambiguous exact
    # duplicate nodes fail closed instead of falling back to generated ids.
    component_atom_by_id: dict[str, str] = {}
    component_id_by_atom: dict[str, str] = {}
    for component_id, raw_component in component_records:
        member_atoms: list[str] = []
        for raw_fact in raw_component["facts"]:
            _require(isinstance(raw_fact, Mapping), f"{label} component membership must be a fact object")
            fact_id = raw_fact.get("fact_id")
            _require(isinstance(fact_id, str) and fact_id in fact_atom_by_id, f"{label} component membership references unknown fact")
            member_atoms.append(fact_atom_by_id[fact_id])
        component_payload = dict(component_bases[component_id])
        component_payload["member_facts"] = sorted(member_atoms)
        component_atom, _ = _atom("support_component", component_payload)
        _require(
            component_atom not in component_id_by_atom,
            f"{label} has ambiguous duplicate semantic components: {component_id_by_atom.get(component_atom)!r}, {component_id!r}",
        )
        component_id_by_atom[component_atom] = component_id
        component_atom_by_id[component_id] = add("support_component", component_payload)
        for target in member_atoms:
            add("component_membership", {"component": component_atom, "fact": target})

    # Directed fact-to-fact and fact-to-component links are semantic edges.
    for fact_id, raw_fact in raw_fact_by_id.items():
        source = fact_atom_by_id[fact_id]
        for relation in _RELATION_FIELDS:
            references = raw_fact.get(relation, [])
            _require(isinstance(references, list), f"{label}.{relation} must be an array")
            _require(len(references) == len(set(references)), f"{label}.{relation} must not contain duplicate references")
            for reference in references:
                if relation == "applicability_component_ids":
                    _require(
                        isinstance(reference, str) and reference in component_atom_by_id,
                        f"{label}.{relation} references an unknown component id",
                    )
                    target = component_atom_by_id[reference]
                    target_kind = "support_component"
                else:
                    _require(isinstance(reference, str) and reference in fact_atom_by_id, f"{label}.{relation} references an unknown fact id")
                    target = fact_atom_by_id[reference]
                    target_kind = "fact"
                add(
                    "relationship",
                    {
                        "relation": relation,
                        "source_fact": source,
                        "target": target,
                        "target_kind": target_kind,
                    },
                )

    numeric_candidates_raw = artifact.source_selection.get("numeric_candidates")
    _require(isinstance(numeric_candidates_raw, list), f"{label}.source_selection.numeric_candidates must be an array")
    numeric_candidates: dict[str, _NumericLocator] = {}
    for index, raw_candidate in enumerate(numeric_candidates_raw):
        candidate_label = f"{label}.source_selection.numeric_candidates[{index}]"
        _require(isinstance(raw_candidate, Mapping), f"{candidate_label} must be an object")
        _require(set(raw_candidate) == _NUMERIC_CANDIDATE_KEYS, f"{candidate_label} has unsupported or missing fields")
        candidate_id = raw_candidate.get("numeric_candidate_id")
        source_block_id = raw_candidate.get("source_block_id")
        anchor_text = raw_candidate.get("anchor_text")
        _require(isinstance(candidate_id, str) and candidate_id, f"{candidate_label}.numeric_candidate_id must be non-empty")
        _require(candidate_id not in numeric_candidates, f"{label} has duplicate numeric candidate id")
        _require(isinstance(source_block_id, str) and source_block_id in resolver.source_texts, f"{candidate_label} references an unknown source block")
        _require(isinstance(anchor_text, str) and anchor_text, f"{candidate_label}.anchor_text must be non-empty")
        start = _json_integer(raw_candidate.get("start_char"), label=f"{candidate_label}.start_char")
        end = _json_integer(raw_candidate.get("end_char"), label=f"{candidate_label}.end_char")
        _require(0 <= start < end <= len(resolver.source_texts[source_block_id]), f"{candidate_label} span is invalid")
        _require(resolver.source_texts[source_block_id][start:end] == anchor_text, f"{candidate_label} does not exactly match source text")
        numeric_candidates[candidate_id] = _NumericLocator(
            source_block_id=source_block_id,
            start_char=start,
            end_char=end,
            anchor_text=anchor_text,
        )

    # Deterministic projections retain exact business values and source-fact
    # links, while generated numeric-candidate ids remain validation-only.
    for index, projection in enumerate(projections):
        projection_label = f"{label}.derived_projections[{index}]"
        _require(isinstance(projection, Mapping), f"{projection_label} must be an object")
        projection_type = projection.get("projection_type")
        _require(projection_type in {"support_scale_measures", "support_facets"}, f"{projection_label} has unsupported projection_type")
        _require(projection.get("status") in {"identified", "partially_identified"}, f"{projection_label}.status is unsupported")
        allowed_projection = ({"projection_type", "status", "source_fact_ids", "measures"} if projection_type == "support_scale_measures" else {"projection_type", "status", "source_fact_ids", "activities", "methods", "items"})
        _require(set(projection) == allowed_projection, f"{projection_label} has unsupported or missing fields")
        source_fact_ids = projection.get("source_fact_ids")
        _require(isinstance(source_fact_ids, list) and source_fact_ids, f"{projection_label}.source_fact_ids must be a non-empty array")
        _require(len(source_fact_ids) == len(set(source_fact_ids)), f"{projection_label}.source_fact_ids contains duplicates")
        for source_id in source_fact_ids:
            _require(isinstance(source_id, str) and source_id in fact_atom_by_id, f"{projection_label} references unknown source fact")
        normalized_projection: dict[str, Any] = {
            "projection_type": projection_type,
            "status": projection["status"],
            "source_facts": sorted(fact_atom_by_id[source_id] for source_id in source_fact_ids),
        }
        if projection_type == "support_scale_measures":
            measures = projection.get("measures")
            _require(isinstance(measures, list) and measures, f"{projection_label}.measures must be a non-empty array")
            normalized_measures: list[Mapping[str, Any]] = []
            measured_fact_ids: set[str] = set()
            for measure_index, raw_measure in enumerate(measures):
                measure_label = f"{projection_label}.measures[{measure_index}]"
                _require(isinstance(raw_measure, Mapping), f"{measure_label} must be an object")
                _require(set(raw_measure) == _MEASURE_KEYS, f"{measure_label} has unsupported or missing fields")
                source_id = raw_measure.get("source_fact_id")
                _require(isinstance(source_id, str) and source_id in fact_atom_by_id, f"{measure_label} references unknown source fact")
                _require(raw_fact_by_id[source_id].get("field_name") == "support_scale", f"{measure_label} source fact must be support_scale")
                _require(source_id in source_fact_ids, f"{measure_label} source fact is absent from projection source_fact_ids")
                measured_fact_ids.add(source_id)
                numeric_id = raw_measure.get("source_numeric_candidate_id")
                _require(isinstance(numeric_id, str) and numeric_id in numeric_candidates, f"{measure_label} references unknown numeric candidate")
                fact_source = raw_fact_by_id[source_id].get("value_source")
                fact_block, fact_start, fact_end = resolver.validate_value_source(
                    fact_source,
                    raw_fact_by_id[source_id].get("value_raw"),
                    label=f"{measure_label}.source_fact.value_source",
                )
                numeric_locator = numeric_candidates[numeric_id]
                _require(
                    numeric_locator.source_block_id == fact_block
                    and fact_start <= numeric_locator.start_char
                    < numeric_locator.end_char <= fact_end,
                    f"{measure_label} numeric candidate is outside its source fact span",
                )
                _validate_measure_shape(raw_measure, label=measure_label)
                _validate_measure_locator_value(
                    raw_measure,
                    numeric_locator,
                    label=measure_label,
                )
                measure = {
                    key: raw_measure[key]
                    for key in sorted(_MEASURE_KEYS - {"source_fact_id", "source_numeric_candidate_id"})
                }
                measure["source_fact"] = fact_atom_by_id[source_id]
                normalized_measures.append(measure)
            _require(measured_fact_ids == set(source_fact_ids), f"{projection_label} source facts and measure facts differ")
            normalized_projection["measures"] = _unordered(normalized_measures)
        else:
            for facet_key in ("activities", "methods", "items"):
                values = projection.get(facet_key)
                _require(isinstance(values, list), f"{projection_label}.{facet_key} must be an array")
                _require(all(isinstance(value, str) and value for value in values), f"{projection_label}.{facet_key} must contain non-empty strings")
                _require(len(values) == len(set(values)), f"{projection_label}.{facet_key} contains duplicates")
                normalized_projection[facet_key] = sorted(values)
        projection_atom = add(
            str(projection_type), normalized_projection,
        )
        for source_id in source_fact_ids:
            add(
                "projection_source",
                {
                    "projection": projection_atom,
                    "fact": fact_atom_by_id[source_id],
                },
            )

    graph_payload = _opaque_counter(atoms, kinds)
    return SemanticGraph(
        atoms=atoms,
        atom_kinds=kinds,
        digest=sha256(_canonical_bytes(graph_payload)).hexdigest(),
    )


def _common_ir_input_digest(common_ir: Mapping[str, Any]) -> str:
    """Pin candidate input to baseline Common IR, excluding deployment path."""

    normalized = dict(common_ir)
    document = common_ir.get("document")
    _require(isinstance(document, Mapping), "Common IR document must be an object")
    normalized_document = dict(document)
    provenance = document.get("provenance")
    _require(isinstance(provenance, Mapping), "Common IR provenance must be an object")
    normalized_provenance = dict(provenance)
    normalized_provenance.pop("source_location", None)
    normalized_document["provenance"] = normalized_provenance
    normalized["document"] = normalized_document
    return sha256(_canonical_bytes(normalized)).hexdigest()


def _compare_semantic_profiles(
    baseline: SemanticArtifact,
    gold: SemanticArtifact,
    candidate: SemanticArtifact,
    *,
    notice_id: str,
    enforce_candidate_source_basis: bool,
) -> dict[str, Any]:
    """Evaluate one candidate gate or explicit baseline calibration."""

    b = build_semantic_graph(baseline, label=f"baseline {notice_id}")
    g = build_semantic_graph(gold, label=f"Gold {notice_id}")
    c = build_semantic_graph(candidate, label=f"candidate {notice_id}")
    _require(
        baseline.notice_id == gold.notice_id == candidate.notice_id == notice_id,
        f"semantic artifacts do not share logical notice id {notice_id}",
    )
    _require(
        _common_ir_input_digest(candidate.common_ir) == _common_ir_input_digest(baseline.common_ir),
        f"candidate {notice_id} was not generated from the pinned baseline Common IR",
    )
    if enforce_candidate_source_basis:
        _validate_candidate_source_basis(
            candidate,
            label=f"candidate {notice_id}",
        )
    approved = g.atoms - b.atoms
    rejected = b.atoms - g.atoms
    retained = g.atoms & b.atoms
    candidate_additions = c.atoms - b.atoms
    candidate_removals = b.atoms - c.atoms
    candidate_only = c.atoms - g.atoms
    missing_gold = g.atoms - c.atoms
    recovered_approved_additions = approved & candidate_additions
    missing_approved_additions = approved - candidate_additions
    completed_approved_removals = rejected & candidate_removals
    remaining_baseline_surplus = rejected & candidate_only
    missing_retained = retained - c.atoms
    kinds = {**b.atom_kinds, **g.atom_kinds, **c.atom_kinds}
    # With multisets, the four intuitive delta checks can be fooled by count
    # overlap (B=3, G=2, C=2).  Exact C==G is the only sound provenance-grade
    # gate; A/D/R below remain useful recovery diagnostics.
    passed = not (missing_gold or candidate_only)
    return {
        "notice_id": notice_id,
        "semantic_gate_status": "passed" if passed else "failed",
        "graphs": {
            "baseline_sha256": b.digest,
            "gold_sha256": g.digest,
            "candidate_sha256": c.digest,
        },
        "counts": {
            "baseline_atoms": sum(b.atoms.values()),
            "gold_atoms": sum(g.atoms.values()),
            "candidate_atoms": sum(c.atoms.values()),
            "approved_delta": sum(approved.values()),
            "rejected_baseline_delta": sum(rejected.values()),
            "retained": sum(retained.values()),
            "candidate_only": sum(candidate_only.values()),
            "missing_gold": sum(missing_gold.values()),
            "recovered_approved_additions": sum(recovered_approved_additions.values()),
            "missing_approved_additions": sum(missing_approved_additions.values()),
            "completed_approved_removals": sum(completed_approved_removals.values()),
            "remaining_baseline_surplus": sum(remaining_baseline_surplus.values()),
            "missing_retained": sum(missing_retained.values()),
        },
        "differences": {
            "approved_delta": _opaque_counter(approved, kinds),
            "rejected_baseline_delta": _opaque_counter(rejected, kinds),
            "retained": _opaque_counter(retained, kinds),
            "candidate_only": _opaque_counter(candidate_only, kinds),
            "missing_gold": _opaque_counter(missing_gold, kinds),
            "recovered_approved_additions": _opaque_counter(recovered_approved_additions, kinds),
            "missing_approved_additions": _opaque_counter(missing_approved_additions, kinds),
            "completed_approved_removals": _opaque_counter(completed_approved_removals, kinds),
            "remaining_baseline_surplus": _opaque_counter(remaining_baseline_surplus, kinds),
            "missing_retained": _opaque_counter(missing_retained, kinds),
        },
        "gate": {
            "candidate_exactly_equals_gold": passed,
            "approved_additions_recovered": not missing_approved_additions,
            "approved_removals_completed": not remaining_baseline_surplus,
            "retained_is_subset_of_candidate": not missing_retained,
            "candidate_has_no_unreviewed_additions": not candidate_only,
        },
    }


def _opaque_corpus_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Keep corpus pins and counts without copying source-bearing names."""

    allowed = ("archive_sha256", "freeze_manifest_sha256", "profile_count", "role")
    return {key: identity[key] for key in allowed if key in identity}


def compare_semantic_profiles(
    baseline: SemanticArtifact,
    gold: SemanticArtifact,
    candidate: SemanticArtifact,
    *,
    notice_id: str,
) -> dict[str, Any]:
    """Evaluate one real candidate after strict source-universe admission."""

    return _compare_semantic_profiles(
        baseline,
        gold,
        candidate,
        notice_id=notice_id,
        enforce_candidate_source_basis=True,
    )


def calibrate_semantic_profiles(
    baseline: SemanticArtifact,
    gold: SemanticArtifact,
    *,
    notice_id: str,
) -> dict[str, Any]:
    """Measure the reviewed B→G delta without pretending B is a new C."""

    return _compare_semantic_profiles(
        baseline,
        gold,
        baseline,
        notice_id=notice_id,
        enforce_candidate_source_basis=False,
    )


def _compare_loaded_semantic_corpora(
    baseline: LoadedSemanticArtifacts,
    gold: LoadedSemanticArtifacts,
    candidate: LoadedSemanticArtifacts,
    *,
    notice_ids: Sequence[str] | None = None,
    baseline_calibration: bool,
) -> dict[str, Any]:
    """Shared corpus implementation with an explicit evaluation mode."""

    baseline_ids = set(baseline.artifacts)
    gold_ids = set(gold.artifacts)
    candidate_ids = set(candidate.artifacts)
    if notice_ids is None:
        _require(
            baseline_ids == gold_ids == candidate_ids,
            "full semantic comparison requires exact baseline/Gold/candidate notice-id sets",
        )
        selected = sorted(baseline_ids)
    else:
        _require(len(notice_ids) == len(set(notice_ids)), "selected notice ids must not contain duplicates")
        selected = sorted(set(notice_ids))
        shared = baseline_ids & gold_ids & candidate_ids
        missing = [notice_id for notice_id in selected if notice_id not in shared]
        _require(not missing, f"selected notice ids are absent from one or more corpora: {missing}")
    _require(bool(selected), "semantic comparison has no selected shared notices")
    notices = [
        _compare_semantic_profiles(
            baseline.artifacts[notice_id],
            gold.artifacts[notice_id],
            candidate.artifacts[notice_id],
            notice_id=notice_id,
            enforce_candidate_source_basis=not baseline_calibration,
        )
        for notice_id in selected
    ]
    passed = sum(item["semantic_gate_status"] == "passed" for item in notices)
    return {
        "schema_version": SEMANTIC_COMPARISON_SCHEMA_VERSION,
        "evaluation_kind": (
            "baseline_gold_calibration" if baseline_calibration else "candidate_gold_gate"
        ),
        "comparison_mode": "offline_id_and_order_insensitive_exact_semantic_multigraph",
        "normalization": {
            "ignored": [
                "generated fact_id/support_component_id and links are resolved to semantic nodes",
                "array order",
                "model_summary",
                "processing_metadata",
                "generated document/profile ids",
                "deployment source_location",
                "CandidatePack block ids/offsets and Common IR grouping block/cell/section after strict admission",
                "component-local source locators after strict admission",
            ],
            "preserved": [
                "fact values, field/status/scope/roles",
                "evidence/context occurrence provenance and source SHA-256",
                "business identity and source-document metadata",
                "components and component membership",
                "directed fact/component relationships",
                "support_scale_measures and source-fact provenance",
                "unresolved-observation reason",
            ],
            "candidate_input_binding": (
                "not applicable: explicit baseline/Gold calibration"
                if baseline_calibration
                else (
                    "candidate Common IR equals baseline Common IR except source_location; "
                    f"candidate blocks are admitted by {TRUSTED_CANDIDATE_TRANSFORM}"
                )
            ),
            "candidate_source_admission": (
                None
                if baseline_calibration
                else {
                    "scope": SOURCE_ADMISSION_SCOPE,
                    "transform": TRUSTED_CANDIDATE_TRANSFORM,
                    "limitations": list(SOURCE_ADMISSION_LIMITATIONS),
                }
            ),
            "not_a_quality_judge": True,
        },
        "baseline": _opaque_corpus_identity(baseline.identity),
        "gold": _opaque_corpus_identity(gold.identity),
        "candidate": _opaque_corpus_identity(candidate.identity),
        "counts": {"selected": len(selected), "passed": passed, "failed": len(selected) - passed},
        "notices": notices,
    }


def compare_loaded_semantic_corpora(
    baseline: LoadedSemanticArtifacts,
    gold: LoadedSemanticArtifacts,
    candidate: LoadedSemanticArtifacts,
    *,
    notice_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compare an exact full candidate corpus or an explicit subset."""

    return _compare_loaded_semantic_corpora(
        baseline,
        gold,
        candidate,
        notice_ids=notice_ids,
        baseline_calibration=False,
    )


def calibrate_loaded_semantic_corpora(
    baseline: LoadedSemanticArtifacts,
    gold: LoadedSemanticArtifacts,
    *,
    notice_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Explicitly reproduce the reviewed B/G split without C admission."""

    return _compare_loaded_semantic_corpora(
        baseline,
        gold,
        baseline,
        notice_ids=notice_ids,
        baseline_calibration=True,
    )


def compare_semantic_profile_corpora(
    baseline_archive: Path,
    gold_root: Path,
    candidate_archive: Path,
    *,
    expected_reference_count: int = DEFAULT_EXPECTED_PROFILE_COUNT,
    expected_candidate_count: int | None = None,
    notice_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Load three corpus artifacts under the existing strict path/hash gates."""

    return compare_loaded_semantic_corpora(
        load_automatic_semantic_artifacts(baseline_archive, expected_profile_count=expected_reference_count),
        load_reviewed_gold_semantic_artifacts(gold_root, expected_profile_count=expected_reference_count),
        load_automatic_semantic_artifacts(
            candidate_archive,
            expected_profile_count=(expected_reference_count if expected_candidate_count is None else expected_candidate_count),
            role="generated_candidate",
        ),
        notice_ids=notice_ids,
    )


def calibrate_semantic_profile_corpora(
    baseline_archive: Path,
    gold_root: Path,
    *,
    expected_reference_count: int = DEFAULT_EXPECTED_PROFILE_COUNT,
    notice_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Load B/G once and report their reviewed semantic delta explicitly."""

    return calibrate_loaded_semantic_corpora(
        load_automatic_semantic_artifacts(
            baseline_archive,
            expected_profile_count=expected_reference_count,
        ),
        load_reviewed_gold_semantic_artifacts(
            gold_root,
            expected_profile_count=expected_reference_count,
        ),
        notice_ids=notice_ids,
    )


def write_semantic_report(report: Mapping[str, Any], *, output_dir: Path, gold_root: Path) -> Path:
    """Publish an opaque report through the shared hardened report writer."""

    try:
        return write_comparison_report(
            report,
            output_dir=output_dir,
            gold_root=gold_root,
            report_file_name=SEMANTIC_REPORT_FILE_NAME,
        )
    except ExistingProfileComparisonError as error:
        raise ExistingProfileSemanticError(str(error)) from error


__all__ = [
    "SEMANTIC_COMPARISON_SCHEMA_VERSION",
    "SEMANTIC_REPORT_FILE_NAME",
    "ExistingProfileSemanticError",
    "SemanticGraph",
    "SemanticArtifact",
    "LoadedSemanticArtifacts",
    "load_automatic_semantic_artifacts",
    "load_reviewed_gold_semantic_artifacts",
    "build_semantic_graph",
    "compare_semantic_profiles",
    "calibrate_semantic_profiles",
    "compare_loaded_semantic_corpora",
    "calibrate_loaded_semantic_corpora",
    "compare_semantic_profile_corpora",
    "calibrate_semantic_profile_corpora",
    "write_semantic_report",
]
