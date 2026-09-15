#!/usr/bin/env python3
"""Run a tightly pinned, routing-only Existing A canary.

The model phase accepts only Common IR that passes a fail-closed check for
manual-adjudication identifiers and provenance.  Before the run, Frozen Gold
contributes only its pinned freeze metadata.  Full Gold verification and
oracle reads happen after the model phase; no Gold selection, Profile, or
adjudication-only Common IR block may enter an OpenAI request.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
import time
from typing import Any, Callable, Mapping, Protocol
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker import announcement_profiles, vendor  # noqa: E402,F401
from worker.adapters.openai_llm_client import (  # noqa: E402
    OpenAICompletionTelemetry,
    OpenAILLMClient,
)
from worker.ports.llm import LLMClient  # noqa: E402
from worker.profiles import StageError  # noqa: E402

from semantic_structuring.common_ir_v1 import _is_main_notice_section, prepare_common_ir_v1  # noqa: E402
from semantic_structuring.composite_candidates import (  # noqa: E402
    COMPOSITE_CANDIDATE_GENERATOR_VERSION,
    FATAL_COMPOSITE_DIAGNOSTIC_CODES,
    generate_composite_candidates,
)
from semantic_structuring.run_block_candidate_discovery_test import ROUTER_CONTRACT_VERSION  # noqa: E402
from scripts.evaluate_existing_composite_shadow import _native_projection_pack  # noqa: E402


REPORT_SCHEMA_VERSION = "existing_a_routing_canary/v1"
REPORT_FILE_NAME = "existing-a-routing-canary.v1.json"
BASELINE_ARCHIVE_SHA256 = "6649f1a5aab36f659d688634103950d3b73f8a5903a453aabdbbd9f5bc0f7f0d"
GOLD_FREEZE_MANIFEST_SHA256 = "a2c35fb4c98c92c23ff34faa045a16ec4e8ed1ea4bae5397caab547cb3db6987"
CORRECTED_NOTICE_IDS = (
    "PBLN_000000000103645",
    "PBLN_000000000112425",
    "PBLN_000000000117175",
    "PBLN_000000000121019",
    "PBLN_000000000121309",
    "PBLN_000000000122023",
)
MAX_OPENAI_CALLS = 8
PINNED_OPENAI_MODEL_ID = "gpt-5.6-terra"
ROUTER_PROMPT_SHA256 = "9ff379ff0a6f2a1f4b152bcbf034b4d9e53ef44fab8aa1331dcab86230a676e9"
SECTION_SCOPE_PROMPT_SHA256 = "55e40c4e9297556d3cfea7ce3c26652f386ce3fb5bb834ff34e7e396b4c39396"
GOLD_ADJ_EXPECTATION_COUNT = 30
ALL_NATIVE_SAFE_MATCH_COUNT = 12
# This is the digest of the private, sorted expectation keys (notice id plus
# occurrence set), not a digest of raw notice text or model output.
ALL_NATIVE_REACHABLE_KEY_SHA256 = "dc0bfab35cd61f4d10da42e1df23638a9d08cd3f71df1041127a796c807edb5f"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_COMMON_IR_BYTES = 64 * 1024 * 1024
MAX_TOTAL_COMMON_IR_BYTES = 128 * 1024 * 1024
SUPPORTED_ZIP_COMPRESSION = frozenset({ZIP_STORED, ZIP_DEFLATED})
_SAFE_STAGE_CODES = frozenset(
    {
        "common_ir_v1_preparation",
        "section_scope_discovery",
        "section_scope_apply",
        "block_candidate_router",
    }
)
_SAFE_REASON_CODES = frozenset(
    {
        "COMMON_IR_INVALID",
        "LLM_INVALID_RESPONSE",
        "LLM_TIMEOUT",
        "LLM_UNAVAILABLE",
        "CANDIDATE_PACK_EMPTY",
    }
)
_TEXT_PAYLOAD_KEYS = frozenset(
    {
        "content",
        "excerpt",
        "normalized_text",
        "raw_text",
        "text",
        "value_raw",
    }
)
_PROVENANCE_MARKER_KEYS = frozenset(
    {
        "generator",
        "lineage",
        "method",
        "origin",
        "parser",
        "producer",
        "source_location",
        "transform",
    }
)
_PROVENANCE_CONTAINER_KEYS = frozenset({"llm_provenance", "provenance"})
_ADJUDICATION_LABEL_KEYS = frozenset({"source_block_label"})
_ADJUDICATION_MARKER_FRAGMENTS = (
    "adjudicat",
    "gold_patch",
    "goldpatch",
    "manual_gold",
)
MAX_METADATA_SCAN_DEPTH = 128


class ExistingARoutingCanaryError(ValueError):
    """The fixed canary cannot safely proceed."""


class GoldVerifier(Protocol):
    def __call__(self, gold_root: str | Path, *, expected_notice_count: int) -> Any: ...


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExistingARoutingCanaryError(message)


def _sha256_stream(stream: Any) -> str:
    digest = sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _safe_zip_member(info: ZipInfo) -> str:
    name = info.filename
    _require(name and "\x00" not in name and "\\" not in name, "unsafe baseline ZIP member name")
    _require(not name.startswith("/"), "absolute baseline ZIP member is forbidden")
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    _require(bool(trimmed) and all(part not in {"", ".", ".."} for part in parts), "unsafe baseline ZIP member path")
    _require(PurePosixPath(trimmed).as_posix() == trimmed, "non-canonical baseline ZIP member path")
    _require(not (info.flag_bits & 0x1), "encrypted baseline ZIP member is forbidden")
    _require(info.compress_type in SUPPORTED_ZIP_COMPRESSION, "unsupported baseline ZIP compression")
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    _require(not stat.S_ISLNK(mode), "symlink baseline ZIP member is forbidden")
    if file_type:
        _require(file_type == (stat.S_IFDIR if info.is_dir() else stat.S_IFREG), "special baseline ZIP member is forbidden")
    return trimmed


def _parse_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ExistingARoutingCanaryError(f"duplicate JSON key in {label}")
            value[key] = item
        return value

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ExistingARoutingCanaryError(f"cannot parse baseline Common IR JSON: {label}") from error
    _require(isinstance(value, dict), f"baseline Common IR must be a JSON object: {label}")
    return value


def _contains_adjudication_only_metadata(document: Mapping[str, Any]) -> bool:
    """Detect manual-oracle lineage without inspecting source text payloads."""

    def marker(value: str) -> bool:
        lowered = value.strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
        return lowered.startswith("adj:") or any(
            fragment in normalized for fragment in _ADJUDICATION_MARKER_FRAGMENTS
        )

    def visit(
        value: Any,
        *,
        parent_key: str = "",
        provenance_context: bool = False,
        depth: int = 0,
    ) -> bool:
        _require(
            depth <= MAX_METADATA_SCAN_DEPTH,
            "baseline Common IR metadata nesting exceeds limit",
        )
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key).strip().lower()
                child_provenance_context = (
                    provenance_context or key in _PROVENANCE_CONTAINER_KEYS
                )
                if (
                    key in _TEXT_PAYLOAD_KEYS
                    and isinstance(item, str)
                    and not child_provenance_context
                ):
                    continue
                if marker(key):
                    return True
                if isinstance(item, str):
                    if (
                        child_provenance_context
                        or key.endswith("_id")
                        or key in _PROVENANCE_MARKER_KEYS
                        or key in _ADJUDICATION_LABEL_KEYS
                    ) and marker(item):
                        return True
                elif isinstance(item, list) and key.endswith("_ids"):
                    if any(isinstance(member, str) and marker(member) for member in item):
                        return True
                elif isinstance(item, list) and (
                    child_provenance_context or key in _PROVENANCE_MARKER_KEYS
                ):
                    if any(isinstance(member, str) and marker(member) for member in item):
                        return True
                if visit(
                    item,
                    parent_key=key,
                    provenance_context=child_provenance_context,
                    depth=depth + 1,
                ):
                    return True
            return False
        if isinstance(value, list):
            if (
                parent_key.endswith("_ids") or provenance_context
            ) and any(isinstance(member, str) and marker(member) for member in value):
                return True
            return any(
                visit(
                    item,
                    parent_key=parent_key,
                    provenance_context=provenance_context,
                    depth=depth + 1,
                )
                for item in value
            )
        return False

    return visit(document)


def _require_automatic_common_ir(document: Mapping[str, Any]) -> None:
    try:
        contaminated = _contains_adjudication_only_metadata(document)
    except RecursionError as error:
        raise ExistingARoutingCanaryError(
            "baseline Common IR metadata nesting exceeds limit"
        ) from error
    _require(not contaminated, "baseline Common IR contains manual adjudication provenance")


def _read_baseline_common_ir(
    archive_path: Path,
) -> tuple[dict[str, dict[str, Any]], str, dict[str, str]]:
    """Read exactly six bounded Common-IR members, without extracting the ZIP."""

    supplied = archive_path.expanduser()
    _require(not supplied.is_symlink(), "baseline ZIP must not be a symlink")
    descriptor: int | None = None
    try:
        descriptor = os.open(supplied, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), "baseline ZIP must be a regular file")
            _require(0 < before.st_size <= MAX_ARCHIVE_BYTES, "baseline ZIP exceeds physical size cap")
            archive_sha256 = _sha256_stream(stream)
            _require(archive_sha256 == BASELINE_ARCHIVE_SHA256, "baseline ZIP SHA-256 pin mismatch")
            try:
                with ZipFile(stream) as archive:
                    members = archive.infolist()
                    _require(len(members) <= MAX_ARCHIVE_MEMBERS, "baseline ZIP has too many members")
                    seen_names: set[str] = set()
                    seen_offsets: set[int] = set()
                    selected: dict[str, ZipInfo] = {}
                    for info in members:
                        member = _safe_zip_member(info)
                        _require(member not in seen_names, "duplicate baseline ZIP member")
                        seen_names.add(member)
                        _require(isinstance(info.header_offset, int) and 0 <= info.header_offset < before.st_size, "invalid baseline ZIP local-header offset")
                        _require(info.header_offset not in seen_offsets, "duplicate baseline ZIP local-header offset")
                        seen_offsets.add(info.header_offset)
                        if info.is_dir():
                            continue
                        parts = PurePosixPath(member).parts
                        if len(parts) != 4 or parts[0] not in CORRECTED_NOTICE_IDS:
                            continue
                        if parts[1:3] != ("pipeline", "common_ir_v1") or not parts[3].endswith(".json"):
                            continue
                        _require(parts[3].startswith(parts[0] + "."), "baseline Common IR filename does not match notice")
                        _require(parts[0] not in selected, "duplicate baseline Common IR for corrected notice")
                        _require(0 <= info.file_size <= MAX_COMMON_IR_BYTES, "baseline Common IR member exceeds size cap")
                        selected[parts[0]] = info
                    _require(tuple(sorted(selected)) == tuple(sorted(CORRECTED_NOTICE_IDS)), "baseline ZIP is missing a corrected notice Common IR")
                    _require(sum(info.file_size for info in selected.values()) <= MAX_TOTAL_COMMON_IR_BYTES, "baseline Common IR total exceeds size cap")
                    documents: dict[str, dict[str, Any]] = {}
                    member_sha256: dict[str, str] = {}
                    retained_total = 0
                    for notice_id in CORRECTED_NOTICE_IDS:
                        info = selected[notice_id]
                        try:
                            with archive.open(info, "r") as member_stream:
                                raw = member_stream.read(MAX_COMMON_IR_BYTES + 1)
                        except (BadZipFile, OSError, RuntimeError) as error:
                            raise ExistingARoutingCanaryError("cannot read baseline Common IR member") from error
                        _require(len(raw) == info.file_size and len(raw) <= MAX_COMMON_IR_BYTES, "baseline Common IR member size mismatch")
                        retained_total += len(raw)
                        _require(retained_total <= MAX_TOTAL_COMMON_IR_BYTES, "baseline Common IR retained total exceeds size cap")
                        document = _parse_json_object(raw, label=info.filename)
                        identity = document.get("document")
                        _require(document.get("schema_version") == "common_ir_v1", "baseline input is not common_ir_v1")
                        document_id = identity.get("document_id") if isinstance(identity, dict) else None
                        _require(isinstance(document_id, str) and document_id.endswith(f":{notice_id}"), "baseline Common IR identity mismatch")
                        _require_automatic_common_ir(document)
                        documents[notice_id] = document
                        member_sha256[notice_id] = sha256(raw).hexdigest()
            except BadZipFile as error:
                raise ExistingARoutingCanaryError("baseline ZIP is not valid") from error
            after = os.fstat(stream.fileno())
        _require(_stat_identity(before) == _stat_identity(after), "baseline ZIP changed while Common IR was read")
        return documents, archive_sha256, member_sha256
    except ExistingARoutingCanaryError:
        raise
    except OSError as error:
        raise ExistingARoutingCanaryError("cannot safely open baseline ZIP") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_gold_pins(gold_root: Path) -> dict[str, Any]:
    """Read only the pinned freeze metadata before any model call.

    Full Gold verification reads Common IR, selection, and Profile artifacts,
    so it deliberately belongs to the post-call audit instead of this input
    planning phase.
    """

    root = gold_root.expanduser().resolve()
    freeze_path = root / "freeze_manifest.json"
    _require(freeze_path.is_file() and not freeze_path.is_symlink(), "Gold freeze manifest is unavailable")
    freeze_raw = freeze_path.read_bytes()
    freeze_sha256 = sha256(freeze_raw).hexdigest()
    _require(freeze_sha256 == GOLD_FREEZE_MANIFEST_SHA256, "Gold freeze manifest SHA-256 pin mismatch")
    freeze = _parse_json_object(freeze_raw, label="Gold freeze manifest")
    dataset_version = freeze.get("dataset_version")
    _require(isinstance(dataset_version, str) and bool(dataset_version), "Gold dataset version is invalid")
    _require(freeze.get("notice_count") == 100, "Gold notice count pin mismatch")
    return {
        "dataset_version": dataset_version,
        "freeze_manifest_sha256": freeze_sha256,
    }


def _attachment_count(document: dict[str, Any]) -> int:
    prepared, _projection = prepare_common_ir_v1(document)
    return sum(not _is_main_notice_section(section.section_id) for section in prepared.sections)


def _verify_prompt_pins() -> None:
    """Fail closed if production routing prompts drift from this canary."""

    actual_scope = sha256(
        announcement_profiles._scope_instructions().encode("utf-8")
    ).hexdigest()
    actual_router = sha256(
        announcement_profiles._router_instructions().encode("utf-8")
    ).hexdigest()
    _require(actual_scope == SECTION_SCOPE_PROMPT_SHA256, "section-scope prompt SHA-256 pin mismatch")
    _require(actual_router == ROUTER_PROMPT_SHA256, "router prompt SHA-256 pin mismatch")


class _CountingLlm:
    """Call-count/latency boundary that never retains request or response content."""

    def __init__(
        self,
        delegate: LLMClient,
        *,
        task_budget: Mapping[str, int] | None = None,
    ) -> None:
        self._delegate = delegate
        self.calls: list[dict[str, int | str]] = []
        self._allowed_tasks = frozenset(
            {
                announcement_profiles.SECTION_SCOPE_TASK,
                announcement_profiles.BLOCK_ROUTER_TASK,
            }
        )
        supplied_budget = task_budget or {
            task_name: MAX_OPENAI_CALLS for task_name in self._allowed_tasks
        }
        _require(
            set(supplied_budget) == self._allowed_tasks
            and all(type(value) is int and 0 <= value <= MAX_OPENAI_CALLS for value in supplied_budget.values()),
            "canary task budget is invalid",
        )
        self._task_budget = dict(supplied_budget)

    async def generate_structured(self, **kwargs: Any) -> Any:
        task_name = str(kwargs.get("task_name", ""))
        _require(task_name in self._allowed_tasks, "canary attempted a disallowed LLM task")
        _require(len(self.calls) < MAX_OPENAI_CALLS, "OpenAI hard call budget exceeded")
        _require(
            sum(call["task_name"] == task_name for call in self.calls)
            < self._task_budget[task_name],
            "canary attempted more calls for a task than its pinned plan",
        )
        # Reserve the budget before handing control to the provider.  A failed
        # transport attempt still costs one call and must remain observable.
        call: dict[str, int | str] = {"task_name": task_name, "duration_ms": 0}
        self.calls.append(call)
        started = time.perf_counter()
        try:
            return await self._delegate.generate_structured(**kwargs)
        finally:
            call["duration_ms"] = max(0, round((time.perf_counter() - started) * 1000))


def _safe_stage_error(error: Exception) -> dict[str, str]:
    diagnostic = error.diagnostic if isinstance(error, StageError) else None
    if diagnostic is not None:
        stage = str(getattr(diagnostic, "stage", ""))
        reason_code = str(getattr(diagnostic, "reason_code", ""))
        return {
            "stage": stage if stage in _SAFE_STAGE_CODES else "canary",
            "reason_code": reason_code if reason_code in _SAFE_REASON_CODES else "UNKNOWN",
        }
    return {"stage": "canary", "reason_code": type(error).__name__}


def _composite_summary(document: dict[str, Any], pack: Any) -> dict[str, Any]:
    generation = generate_composite_candidates(document, pack)
    kinds = Counter(candidate.kind for candidate in generation.candidates)
    diagnostics = Counter(item.code for item in generation.diagnostics)
    fatal = sorted(set(diagnostics).intersection(FATAL_COMPOSITE_DIAGNOSTIC_CODES))
    return {
        "generator_version": COMPOSITE_CANDIDATE_GENERATOR_VERSION,
        "candidate_count": len(generation.candidates),
        "candidate_kind_counts": dict(sorted(kinds.items())),
        "diagnostic_code_counts": dict(sorted(diagnostics.items())),
        "fatal_diagnostic_codes": fatal,
    }


def _gold_audit_expectations(gold_root: Path) -> dict[str, tuple[str, frozenset[str]]]:
    """Read the private Gold adj:* occurrence universe after model calls only."""

    root = gold_root.expanduser().resolve()
    freeze_raw = (root / "freeze_manifest.json").read_bytes()
    _require(sha256(freeze_raw).hexdigest() == GOLD_FREEZE_MANIFEST_SHA256, "Gold freeze manifest SHA-256 pin mismatch")
    freeze = _parse_json_object(freeze_raw, label="Gold freeze manifest")
    manifest_raw = (root / "profile_manifest.json").read_bytes()
    _require(sha256(manifest_raw).hexdigest() == freeze.get("profile_manifest_sha256"), "Gold profile manifest SHA-256 mismatch")
    manifest = _parse_json_object(manifest_raw, label="Gold profile manifest")
    rows = manifest.get("profiles")
    _require(isinstance(rows, list), "Gold profile manifest profiles is invalid")
    expected: dict[str, tuple[str, frozenset[str]]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("pblanc_id") not in CORRECTED_NOTICE_IDS:
            continue
        notice_id = row["pblanc_id"]
        frozen = row.get("frozen")
        selection = frozen.get("selection") if isinstance(frozen, dict) else None
        path = selection.get("path") if isinstance(selection, dict) else None
        digest = selection.get("sha256") if isinstance(selection, dict) else None
        _require(isinstance(path, str) and isinstance(digest, str) and len(digest) == 64, "Gold selection provenance is invalid")
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ExistingARoutingCanaryError("Gold selection path escapes Gold root") from error
        _require(candidate.is_file() and not candidate.is_symlink(), "Gold selection artifact is unavailable")
        raw = candidate.read_bytes()
        _require(len(raw) <= MAX_COMMON_IR_BYTES and sha256(raw).hexdigest() == digest, "Gold selection SHA-256 mismatch")
        artifact = _parse_json_object(raw, label="Gold selection artifact")
        evidence = artifact.get("materialized_evidence")
        _require(isinstance(evidence, list), "Gold selection evidence is invalid")
        for item in evidence:
            if not isinstance(item, dict):
                continue
            source_blocks = item.get("source_blocks")
            if not isinstance(source_blocks, list):
                continue
            for source in source_blocks:
                if not isinstance(source, dict):
                    continue
                source_block_id = source.get("source_block_id")
                occurrence_ids = source.get("common_ir_occurrence_ids")
                if (
                    not isinstance(source_block_id, str)
                    or not source_block_id.startswith("adj:")
                    or not isinstance(occurrence_ids, list)
                    or len(occurrence_ids) < 2
                    or not all(isinstance(value, str) and value for value in occurrence_ids)
                ):
                    continue
                occurrence_set = frozenset(occurrence_ids)
                _require(len(occurrence_set) >= 2, "Gold adj evidence repeats an occurrence")
                key = sha256(
                    (notice_id + "\0" + "\0".join(sorted(occurrence_set))).encode("utf-8")
                ).hexdigest()
                _require(key not in expected, "duplicate Gold adj evidence occurrence set")
                expected[key] = (notice_id, occurrence_set)
    _require(len(expected) == GOLD_ADJ_EXPECTATION_COUNT, "Gold adj expectation cardinality pin mismatch")
    return expected


def _matching_candidate_count(expectation: frozenset[str], candidates: Any) -> int:
    """Apply the narrow occurrence/primary/header relation contract privately."""

    matches = 0
    for candidate in candidates:
        if candidate.kind != "table_axis_context":
            continue
        atoms = tuple(candidate.atoms)
        occurrence_ids = {atom.occurrence_id for atom in atoms}
        if not expectation.issubset(occurrence_ids):
            continue
        primary = [atom for atom in atoms if atom.role == "primary_value"]
        if len(primary) != 1 or primary[0].occurrence_id not in expectation:
            continue
        table_ids = {atom.common_ir_block_id for atom in atoms}
        if len(table_ids) != 1 or any(atom.common_ir_cell_id is None for atom in atoms):
            continue
        extras = [atom for atom in atoms if atom.occurrence_id not in expectation]
        if any(atom.role not in {"row_header", "column_header"} for atom in extras):
            continue
        matches += 1
    return matches


def _reachable_keys(
    expectations: Mapping[str, tuple[str, frozenset[str]]],
    candidates_by_notice: Mapping[str, Any],
) -> set[str]:
    """Return only unambiguous one-expectation/one-candidate matches."""

    candidates = {
        notice_id: tuple(items) for notice_id, items in candidates_by_notice.items()
    }
    unique_match_by_key: dict[str, tuple[str, int]] = {}
    for key, (notice_id, expectation) in expectations.items():
        matches = tuple(
            index
            for index, candidate in enumerate(candidates.get(notice_id, ()))
            if _matching_candidate_count(expectation, (candidate,)) == 1
        )
        if len(matches) == 1:
            unique_match_by_key[key] = (notice_id, matches[0])
    reused_candidates = Counter(unique_match_by_key.values())
    return {
        key
        for key, candidate_identity in unique_match_by_key.items()
        if reused_candidates[candidate_identity] == 1
    }


def _reachable_key_digest(keys: set[str]) -> str:
    return sha256("\n".join(sorted(keys)).encode("ascii")).hexdigest()


def run_post_call_gold_audit(
    *,
    gold_root: Path,
    baseline_documents: Mapping[str, dict[str, Any]],
    routed_packs: Mapping[str, Any],
    verifier: GoldVerifier | None = None,
) -> dict[str, Any]:
    """Compare private Gold expectations only after the OpenAI phase ends."""

    verification = (verifier or _gold_verifier())(
        gold_root, expected_notice_count=100
    )
    _require(
        getattr(verification, "status", None) == "valid",
        "Gold verification did not succeed",
    )
    expectations = _gold_audit_expectations(gold_root)
    all_native = {
        notice_id: generate_composite_candidates(
            document, _native_projection_pack(document, notice_id)
        )
        for notice_id, document in baseline_documents.items()
    }
    native_candidates = {
        notice_id: generation.candidates for notice_id, generation in all_native.items()
    }
    native_keys = _reachable_keys(expectations, native_candidates)
    native_digest = _reachable_key_digest(native_keys)
    native_fatal_codes = sorted(
        {
            diagnostic.code
            for generation in all_native.values()
            for diagnostic in generation.diagnostics
            if diagnostic.code in FATAL_COMPOSITE_DIAGNOSTIC_CODES
        }
    )
    native_cross_block_count = sum(
        len({atom.common_ir_block_id for atom in candidate.atoms}) != 1
        for generation in all_native.values()
        for candidate in generation.candidates
    )
    _require(len(native_keys) == ALL_NATIVE_SAFE_MATCH_COUNT, "all-native reachable cardinality pin mismatch")
    _require(native_digest == ALL_NATIVE_REACHABLE_KEY_SHA256, "all-native reachable key digest pin mismatch")
    _require(not native_fatal_codes, "all-native generation has fatal diagnostics")
    _require(native_cross_block_count == 0, "all-native generation crosses Common IR blocks")

    routed_generations = {
        notice_id: generate_composite_candidates(baseline_documents[notice_id], pack)
        for notice_id, pack in routed_packs.items()
    }
    routed_candidates = {
        notice_id: generation.candidates for notice_id, generation in routed_generations.items()
    }
    routed_keys = _reachable_keys(expectations, routed_candidates)
    fatal_codes = sorted(
        {
            diagnostic.code
            for generation in routed_generations.values()
            for diagnostic in generation.diagnostics
            if diagnostic.code in FATAL_COMPOSITE_DIAGNOSTIC_CODES
        }
    )
    cross_block_count = sum(
        len({atom.common_ir_block_id for atom in candidate.atoms}) != 1
        for generation in routed_generations.values()
        for candidate in generation.candidates
    )
    retention_ok = (
        routed_keys == native_keys
        and not fatal_codes
        and cross_block_count == 0
    )
    return {
        "expectation_count": len(expectations),
        "all_native_safe_match_count": len(native_keys),
        "all_native_reachable_key_sha256": native_digest,
        "all_native_fatal_diagnostic_codes": native_fatal_codes,
        "all_native_cross_common_ir_block_candidate_count": native_cross_block_count,
        "routed_reachable_key_count": len(routed_keys),
        "routed_reachable_key_sha256": _reachable_key_digest(routed_keys),
        "sets_equal": routed_keys == native_keys,
        "fatal_diagnostic_codes": fatal_codes,
        "cross_common_ir_block_candidate_count": cross_block_count,
        "generator_expressibility": f"{len(native_keys)}/{len(expectations)}",
        "routed_a_retention": f"{len(routed_keys)}/{len(native_keys)}",
        "semantic_profile": "NOT_RUN",
        "status": "passed" if retention_ok else "failed",
    }


def build_plan(
    *,
    baseline_zip: Path,
    gold_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Perform all offline input validation and return baseline-only documents."""

    _verify_prompt_pins()
    gold = _load_gold_pins(gold_root)
    documents, baseline_sha256, member_sha256 = _read_baseline_common_ir(baseline_zip)
    attachment_counts = {notice_id: _attachment_count(document) for notice_id, document in documents.items()}
    planned_by_task = {
        announcement_profiles.SECTION_SCOPE_TASK: sum(count > 0 for count in attachment_counts.values()),
        announcement_profiles.BLOCK_ROUTER_TASK: len(documents),
    }
    planned_calls = sum(planned_by_task.values())
    _require(planned_calls == MAX_OPENAI_CALLS, "pinned baseline call plan no longer matches the eight-call budget")
    return documents, {
        "schema_version": REPORT_SCHEMA_VERSION,
        "execution_status": "planned",
        "routing_retention_status": "not_run",
        "semantic_profile_status": "not_run",
        "dataset_version": gold["dataset_version"],
        "baseline": {"archive_sha256": baseline_sha256},
        "gold_oracle": {
            "freeze_manifest_sha256": gold["freeze_manifest_sha256"],
        },
        "notice_ids": list(CORRECTED_NOTICE_IDS),
        "model": None,
        "pins": {
            "router_task": announcement_profiles.BLOCK_ROUTER_TASK,
            "section_scope_task": announcement_profiles.SECTION_SCOPE_TASK,
            "router_contract_version": ROUTER_CONTRACT_VERSION,
            "router_prompt_sha256": ROUTER_PROMPT_SHA256,
            "section_scope_prompt_sha256": SECTION_SCOPE_PROMPT_SHA256,
            "prompt_bundle_version": announcement_profiles.PROMPT_BUNDLE_VERSION,
            "openai_model_id": PINNED_OPENAI_MODEL_ID,
        },
        "calls": {"hard_budget": MAX_OPENAI_CALLS, "planned": planned_calls, "by_task": planned_by_task},
        "notices": [
            {
                "notice_id": notice_id,
                "baseline_common_ir_sha256": member_sha256[notice_id],
                "attachment_section_count": attachment_counts[notice_id],
            }
            for notice_id in CORRECTED_NOTICE_IDS
        ],
        "limitations": [
            "baseline_common_ir_only_in_openai_requests",
            "manual_adjudication_metadata_rejected_before_provider_construction",
            "does_not_run_source_selection_or_profile_assembly",
            "does_not_measure_gold_semantic_accuracy",
            "model_outputs_are_not_deterministic",
        ],
    }


def execute_canary(
    *,
    documents: Mapping[str, dict[str, Any]],
    plan: dict[str, Any],
    llm_client: LLMClient,
    model_id: str,
    timeout_seconds: float,
    gold_root: Path,
    telemetry: list[OpenAICompletionTelemetry] | None = None,
    audit_runner: Callable[..., dict[str, Any]] = run_post_call_gold_audit,
) -> dict[str, Any]:
    """Execute only scope/router calls over baseline documents and summarize safely."""

    _require(isinstance(model_id, str) and bool(model_id.strip()), "an exact model id is required")
    _require(model_id.strip() == PINNED_OPENAI_MODEL_ID, "model id does not match the pinned canary model")
    counting_llm = _CountingLlm(llm_client, task_budget=plan["calls"]["by_task"])
    results: list[dict[str, Any]] = []
    routed_packs: dict[str, Any] = {}
    execution_status = "succeeded"
    for notice_id in CORRECTED_NOTICE_IDS:
        document = documents[notice_id]
        row = next(item for item in plan["notices"] if item["notice_id"] == notice_id).copy()
        try:
            pack, metrics = announcement_profiles.route_announcement_a_pack(
                document, counting_llm, model_profile="existing_a_routing_canary"
            )
            routed_packs[notice_id] = pack
            composite = _composite_summary(document, pack)
            row["router"] = {
                "status": "valid",
                "a_pack_block_count": len(pack.blocks),
                "a_table_cell_total": int(metrics.get("a_table_cell_total", 0)),
                "composite_shadow": composite,
            }
            if composite["fatal_diagnostic_codes"]:
                # Structural diagnostics are evaluated by the post-call
                # retention gate; the transport/routing execution succeeded.
                pass
        except Exception as error:  # stage errors are deliberately reduced to safe codes
            row["router"] = {"status": "failed", "error": _safe_stage_error(error)}
            execution_status = "failed"
            results.append(row)
            break
        results.append(row)

    calls_by_task = Counter(str(call["task_name"]) for call in counting_llm.calls)
    _require(
        execution_status != "succeeded" or len(counting_llm.calls) == int(plan["calls"]["planned"]),
        "successful canary did not consume its pinned call plan",
    )
    try:
        audit = audit_runner(
            gold_root=gold_root,
            baseline_documents=documents,
            routed_packs=routed_packs,
        )
    except Exception as error:
        audit = {
            "status": "failed",
            "error": {"stage": "post_call_gold_audit", "reason_code": type(error).__name__},
        }
    provider_usage = {
        "prompt_tokens": sum(item.prompt_tokens or 0 for item in telemetry or []),
        "completion_tokens": sum(item.completion_tokens or 0 for item in telemetry or []),
        "total_tokens": sum(item.total_tokens or 0 for item in telemetry or []),
        "provider_completed_calls": len(telemetry or []),
    }
    report = {**plan}
    report["execution_status"] = execution_status
    report["routing_retention_status"] = audit["status"]
    report["semantic_profile_status"] = "not_run"
    report["model"] = {
        "provider": "openai",
        "model_id": model_id,
        "transport": "chat_completions_json_schema",
        "store": False,
        "timeout_seconds": timeout_seconds,
    }
    report["calls"] = {
        **plan["calls"],
        "attempted": len(counting_llm.calls),
        "attempted_by_task": dict(sorted(calls_by_task.items())),
        "latency_ms": sum(int(call["duration_ms"]) for call in counting_llm.calls),
        "usage": provider_usage,
    }
    report["notices"] = results
    report["routing_audit"] = audit
    return report


def _prepare_output_directory(output_dir: Path, gold_root: Path) -> Path:
    supplied = output_dir.expanduser()
    _require(supplied.exists() and supplied.is_dir() and not supplied.is_symlink(), "output directory must be an existing non-symlink directory")
    resolved = supplied.resolve()
    gold = gold_root.expanduser().resolve()
    try:
        resolved.relative_to(gold)
    except ValueError:
        pass
    else:
        raise ExistingARoutingCanaryError("output directory must be outside the frozen Gold root")
    _require(not any(resolved.iterdir()), "output directory must be empty")
    return resolved


def write_report(report: Mapping[str, Any], *, output_dir: Path, gold_root: Path) -> Path:
    target_dir = _prepare_output_directory(output_dir, gold_root)
    target = target_dir / REPORT_FILE_NAME
    temporary = target_dir / f".{REPORT_FILE_NAME}.{secrets.token_hex(16)}.tmp"
    payload = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as error:
        raise ExistingARoutingCanaryError("cannot write canary report") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def _gold_verifier() -> GoldVerifier:
    from scripts.verify_existing_gold100 import verify_gold_root
    return verify_gold_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-zip", required=True, type=Path)
    parser.add_argument("--gold-root", required=True, type=Path)
    parser.add_argument("--execute-openai", action="store_true")
    parser.add_argument("--model", help="Exact OpenAI model id; required with --execute-openai")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, help="Required with --execute-openai; must be an empty directory outside Gold")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        documents, plan = build_plan(baseline_zip=args.baseline_zip, gold_root=args.gold_root)
        if not args.execute_openai:
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
            return 0
        _require(args.model is not None and args.model.strip(), "--model is required with --execute-openai")
        _require(args.model.strip() == PINNED_OPENAI_MODEL_ID, "--model does not match the pinned canary model")
        _require(args.output_dir is not None, "--output-dir is required with --execute-openai")
        _require(args.timeout_seconds > 0, "--timeout-seconds must be positive")
        output_dir = _prepare_output_directory(args.output_dir, args.gold_root)
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        _require(bool(api_key), "OPENAI_API_KEY is required with --execute-openai")
        telemetry: list[OpenAICompletionTelemetry] = []
        client = OpenAILLMClient(
            api_key=api_key,
            model_profiles={"existing_a_routing_canary": args.model.strip()},
            timeout_seconds=args.timeout_seconds,
            telemetry_callback=telemetry.append,
        )
        report = execute_canary(documents=documents, plan=plan, llm_client=client, model_id=args.model.strip(), timeout_seconds=args.timeout_seconds, gold_root=args.gold_root, telemetry=telemetry)
        report_path = write_report(report, output_dir=output_dir, gold_root=args.gold_root)
    except ExistingARoutingCanaryError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # never print a provider response, path, or source text
        print(f"ERROR: canary failed: {type(error).__name__}", file=sys.stderr)
        return 1
    succeeded = (
        report["execution_status"] == "succeeded"
        and report["routing_retention_status"] == "passed"
    )
    print(json.dumps({"execution_status": report["execution_status"], "routing_retention_status": report["routing_retention_status"], "report_file": report_path.name, "attempted_calls": report["calls"]["attempted"]}, sort_keys=True))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
