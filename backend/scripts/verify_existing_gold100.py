#!/usr/bin/env python3
"""Read-only, offline verifier for an Existing Profile Gold-100 freeze.

The Gold corpus is an external, post-run oracle.  This module has no runtime,
network, database, object-storage, or LLM I/O.  It imports the vendored
Common-IR/CandidatePack contracts solely to replay deterministic source
projections; it does not compare a candidate runtime result to Gold (and
therefore cannot accidentally turn Gold examples into production inputs).
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, Sequence

from worker import vendor as _vendor  # noqa: F401 - install vendored contracts only
from semantic_structuring.candidate_assembly import MAX_A_TABLE_CELL_CANDIDATES
from semantic_structuring.common_ir_v1 import prepare_common_ir_v1
from semantic_structuring.native_exact_transform import (
    LEGACY_NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
    NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
    NativeExactTransformOptions,
    augment_pack_with_native_exact_transforms,
    replay_persisted_native_exact_transforms,
)
from semantic_structuring.models import CandidatePack, SourceBlock
from semantic_structuring.native_provenance import stable_unique_occurrence_ids


FREEZE_CONTRACT = "existing_profile_gold_100_freeze/v1"
ARTIFACT_INDEX_SCHEMA = "gold_100_artifact_index/v1"
PROFILE_MANIFEST_SCHEMA = "existing_profile_gold_100_manifest/v1"
PROFILE_SCHEMA = "existing_program_profile/v0.2"
COMMON_IR_SCHEMA = "common_ir_v1"
SELECTION_CONTRACT = "v0.2_anchor"
TEXT_BASIS = "common_ir_v1_candidate_pack"
DEFAULT_EXPECTED_NOTICE_COUNT = 100
NATIVE_EXACT_TRANSFORM_GENERATOR = "semantic_structuring.native_exact_transform"
_NATIVE_EXACT_TRANSFORM_VERSIONS = frozenset(
    {
        f"{producer_version}:{variant}"
        for producer_version in (
            LEGACY_NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
            NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
        )
        for variant in ("lines", "lines+continuations")
    }
)
_NATIVE_DERIVED_BLOCK_PREFIXES = ("line:", "composite:")
_LEGACY_RUNPOD_014_CANDIDATE_PACK_GENERATOR = "semantic_structuring.common_ir_v1"
_LEGACY_RUNPOD_014_CANDIDATE_PACK_GENERATOR_VERSION = "1"
_EXISTING_A_CANDIDATE_PACK_SUFFIX = "-a-profile-v0.2"

# The reviewed Gold-100 freeze is about 74 MiB and its largest known file
# (reserve_pool.csv) is about 17 MiB.  Keep the verifier deliberately below a
# general-purpose file importer while leaving headroom for the frozen corpus.
# These module constants are intentionally simple so CI can lower them in
# focused resource-limit tests without changing normal call sites.
MAX_GOLD_FILE_BYTES = 32 * 1024 * 1024
MAX_GOLD_TOTAL_UNIQUE_FILE_BYTES = 128 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

_SHA256_HEX_LENGTH = 64
_FREEZE_PINNED_FILES = {
    "artifact_index_sha256": "artifact_index.json",
    "profile_manifest_sha256": "profile_manifest.json",
    "sample_100_sha256": "sample_100.csv",
}
_NOTICE_ARTIFACTS = {
    "profile": "existing_profile.v0.2.json",
    "selection": "source_selection.v0.2.json",
    "common_ir": "common_ir_v1.json",
}
_UNINDEXED_ROOT_FILES = {
    "freeze_manifest.json",
    "artifact_index.json",
    "README.md",
}
_V5_GOVERNANCE_FILES = {
    "decisions.jsonl",
    "materialization_report.json",
    "semantic_regression_report.json",
}
_V4_GOVERNANCE_FILES = {
    "v4_decisions.jsonl",
    "v4_materialization_report.json",
    "v4_semantic_regression_report.json",
    "v4_v3_manifest.json",
}


class GoldVerificationError(ValueError):
    """Raised when the external Gold corpus does not satisfy its contract."""


@dataclass(frozen=True)
class VerificationReport:
    """Small, non-sensitive summary suitable for CLI or CI output."""

    status: str
    gold_root: str
    dataset_version: str
    notice_count: int
    artifact_count: int
    fact_count: int


def _fail(message: str) -> None:
    raise GoldVerificationError(message)


@dataclass(frozen=True)
class _FileSnapshot:
    """Identity captured at a checked read boundary.

    The verifier is read-only, but a path can still be replaced between two
    validations.  Comparing the descriptor snapshot on every read makes that
    race fail closed for the normal replacement/truncation cases.
    """

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


class _GoldReadBudget:
    """Per-verification, non-global file safety and byte-budget guard."""

    def __init__(
        self,
        *,
        max_file_bytes: int = MAX_GOLD_FILE_BYTES,
        max_total_unique_file_bytes: int = MAX_GOLD_TOTAL_UNIQUE_FILE_BYTES,
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._max_total_unique_file_bytes = max_total_unique_file_bytes
        self._snapshots: dict[tuple[int, int], _FileSnapshot] = {}
        self._path_snapshots: dict[Path, _FileSnapshot] = {}
        self._total_unique_file_bytes = 0

    @staticmethod
    def _snapshot_from_stat(metadata: os.stat_result) -> _FileSnapshot:
        return _FileSnapshot(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )

    def _register(self, path: Path, label: str, snapshot: _FileSnapshot) -> None:
        if snapshot.size < 0:
            _fail(f"{label} has an invalid file size")
        if snapshot.size > self._max_file_bytes:
            _fail(
                f"{label} exceeds the per-file byte budget "
                f"({snapshot.size} > {self._max_file_bytes})"
            )
        path_key = Path(os.path.abspath(path))
        prior_path_snapshot = self._path_snapshots.get(path_key)
        if prior_path_snapshot is not None and prior_path_snapshot != snapshot:
            _fail(f"{label} changed while the Gold corpus was being verified")
        self._path_snapshots[path_key] = snapshot
        identity = (snapshot.device, snapshot.inode)
        prior = self._snapshots.get(identity)
        if prior is not None:
            if prior != snapshot:
                _fail(f"{label} changed while the Gold corpus was being verified")
            return
        proposed_total = self._total_unique_file_bytes + snapshot.size
        if proposed_total > self._max_total_unique_file_bytes:
            _fail(
                "Gold corpus exceeds the aggregate unique-file byte budget "
                f"({proposed_total} > {self._max_total_unique_file_bytes})"
            )
        self._snapshots[identity] = snapshot
        self._total_unique_file_bytes = proposed_total

    def check_file(self, path: Path, label: str) -> _FileSnapshot:
        """Reject symlinks/non-regular files and account their immutable size."""

        try:
            link_metadata = path.lstat()
        except OSError as error:
            _fail(f"{label} is missing or unreadable: {error}")
        if stat.S_ISLNK(link_metadata.st_mode):
            _fail(f"{label} is missing or is not a regular file: {path}")
        if not stat.S_ISREG(link_metadata.st_mode):
            _fail(f"{label} is missing or is not a regular file: {path}")
        try:
            metadata = path.stat()
        except OSError as error:
            _fail(f"{label} is missing or unreadable: {error}")
        snapshot = self._snapshot_from_stat(metadata)
        if (
            link_metadata.st_dev != snapshot.device
            or link_metadata.st_ino != snapshot.inode
            or link_metadata.st_size != snapshot.size
        ):
            _fail(f"{label} changed while the Gold corpus was being verified")
        self._register(path, label, snapshot)
        return snapshot

    @contextmanager
    def open_binary(self, path: Path, label: str) -> Iterator[tuple[BinaryIO, int]]:
        """Open a previously size-checked regular file without following it."""

        initial = self.check_file(path, label)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            _fail(f"{label} is unreadable: {error}")
        try:
            stream = os.fdopen(descriptor, "rb")
        except OSError as error:
            os.close(descriptor)
            _fail(f"{label} is unreadable: {error}")
        try:
            opened = self._snapshot_from_stat(os.fstat(descriptor))
            if opened != initial:
                _fail(f"{label} changed while the Gold corpus was being verified")
            self._register(path, label, opened)
            yield stream, opened.size
        finally:
            try:
                try:
                    final_snapshot = self._snapshot_from_stat(os.fstat(descriptor))
                except OSError as error:
                    _fail(f"{label} became unreadable while the Gold corpus was being verified: {error}")
                if final_snapshot != initial:
                    _fail(f"{label} changed while the Gold corpus was being verified")
            finally:
                stream.close()


def _standalone_read_budget() -> _GoldReadBudget:
    """Keep private helpers safe when a focused caller invokes one directly."""

    return _GoldReadBudget(
        max_file_bytes=MAX_GOLD_FILE_BYTES,
        max_total_unique_file_bytes=MAX_GOLD_TOTAL_UNIQUE_FILE_BYTES,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be a JSON object")
    return value


def _list(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be a JSON array")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    return value


def _sha256_value(value: object, label: str) -> str:
    text = _nonempty_string(value, label)
    if len(text) != _SHA256_HEX_LENGTH or any(character not in "0123456789abcdef" for character in text):
        _fail(f"{label} must be a lowercase SHA-256 hex digest")
    return text


def _digest(path: Path, label: str, *, budget: _GoldReadBudget | None = None) -> str:
    guard = budget or _standalone_read_budget()
    digest = sha256()
    with guard.open_binary(path, label) as (stream, expected_size):
        remaining = expected_size
        while remaining:
            chunk = stream.read(min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                _fail(f"{label} changed while the Gold corpus was being verified")
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            _fail(f"{label} changed while the Gold corpus was being verified")
    return digest.hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _load_json(path: Path, label: str, *, budget: _GoldReadBudget | None = None) -> Any:
    guard = budget or _standalone_read_budget()
    try:
        with guard.open_binary(path, label) as (stream, expected_size):
            payload = stream.read(expected_size + 1)
        if len(payload) != expected_size:
            _fail(f"{label} changed while the Gold corpus was being verified")
        return json.loads(payload.decode("utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        _fail(f"{label} is not valid UTF-8 JSON: {error}")


def _load_json_object(
    path: Path,
    label: str,
    *,
    budget: _GoldReadBudget | None = None,
) -> Mapping[str, Any]:
    return _mapping(_load_json(path, label, budget=budget), label)


def _relative_target(
    root: Path,
    relative_path: object,
    label: str,
    *,
    budget: _GoldReadBudget | None = None,
) -> Path:
    raw = _nonempty_string(relative_path, label)
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        _fail(f"{label} must be a safe relative POSIX path")
    target = root.joinpath(*candidate.parts)
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            _fail(f"{label} must not traverse a symlink")
    try:
        target.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        _fail(f"{label} escapes --gold-root")
    (budget or _standalone_read_budget()).check_file(target, label)
    return target


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _require_exact_keys(value: Mapping[str, Any], keys: Iterable[str], label: str) -> None:
    missing = sorted(set(keys) - set(value))
    if missing:
        _fail(f"{label} is missing required keys: {missing}")


def _require_string_list(value: object, label: str) -> list[str]:
    items = _list(value, label)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(_nonempty_string(item, f"{label}[{index}]"))
    return result


def _unordered_unique_strings(value: object, label: str) -> frozenset[str]:
    """Validate one schema-declared set-like string collection."""

    items = _require_string_list(value, label)
    if len(items) != len(set(items)):
        _fail(f"{label} must not contain duplicates")
    return frozenset(items)


def _unordered_unique_json_rows(value: object, label: str) -> tuple[str, ...]:
    """Canonicalize one schema-declared set-like JSON-object collection."""

    rows = _list(value, label)
    encoded: list[str] = []
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, f"{label}[{index}]")
        encoded.append(
            json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    if len(encoded) != len(set(encoded)):
        _fail(f"{label} must not contain duplicate rows")
    return tuple(sorted(encoded))


def _read_csv(path: Path, label: str, *, budget: _GoldReadBudget | None = None) -> list[dict[str, str]]:
    guard = budget or _standalone_read_budget()
    try:
        with guard.open_binary(path, label) as (binary_stream, _expected_size):
            # TextIOWrapper keeps CSV streaming; unlike read_text/list(splitlines),
            # it does not first materialize the entire input as one string.
            import io

            stream = io.TextIOWrapper(binary_stream, encoding="utf-8", newline="")
            try:
                reader = csv.DictReader(stream)
                if reader.fieldnames is None:
                    _fail(f"{label} has no header")
                return list(reader)
            finally:
                # Keep the binary descriptor alive for ``open_binary`` to
                # compare its final snapshot, including exceptional paths.
                stream.detach()
    except (OSError, UnicodeDecodeError, csv.Error, RecursionError) as error:
        _fail(f"{label} is not a readable UTF-8 CSV: {error}")


def _verify_freeze_manifest(
    root: Path,
    expected_notice_count: int,
    *,
    budget: _GoldReadBudget,
) -> Mapping[str, Any]:
    manifest_path = root / "freeze_manifest.json"
    manifest = _load_json_object(manifest_path, "freeze_manifest.json", budget=budget)
    _require_exact_keys(
        manifest,
        (
            "contract",
            "freeze_status",
            "dataset_version",
            "notice_count",
            "artifact_index_sha256",
            "profile_manifest_sha256",
            "sample_100_sha256",
            "fact_count",
            "fact_status_counts",
            "role_counts",
            "source_counts",
            "strict_validation",
            "freeze_policy",
        ),
        "freeze_manifest.json",
    )
    if manifest["contract"] != FREEZE_CONTRACT:
        _fail(f"freeze_manifest.json.contract must be {FREEZE_CONTRACT!r}")
    if manifest["freeze_status"] != "FROZEN":
        _fail("freeze_manifest.json.freeze_status must be 'FROZEN'")
    _nonempty_string(manifest["dataset_version"], "freeze_manifest.json.dataset_version")
    if not _is_int(manifest["notice_count"]) or manifest["notice_count"] != expected_notice_count:
        _fail(
            "freeze_manifest.json.notice_count must equal "
            f"the expected notice count ({expected_notice_count})"
        )
    for pin_name, filename in _FREEZE_PINNED_FILES.items():
        expected_digest = _sha256_value(manifest[pin_name], f"freeze_manifest.json.{pin_name}")
        target = root / filename
        if not target.is_file() or target.is_symlink():
            _fail(f"freeze manifest pinned file is missing or unsafe: {filename}")
        if _digest(target, f"freeze manifest pinned file {filename}", budget=budget) != expected_digest:
            _fail(f"freeze manifest SHA-256 pin mismatch: {filename}")
    if not _is_int(manifest["fact_count"]) or manifest["fact_count"] < 0:
        _fail("freeze_manifest.json.fact_count must be a non-negative integer")
    _mapping(manifest["fact_status_counts"], "freeze_manifest.json.fact_status_counts")
    _mapping(manifest["role_counts"], "freeze_manifest.json.role_counts")
    _mapping(manifest["source_counts"], "freeze_manifest.json.source_counts")
    strict = _mapping(manifest["strict_validation"], "freeze_manifest.json.strict_validation")
    if strict.get("passed") != expected_notice_count or strict.get("failed") != 0:
        _fail("freeze_manifest.json.strict_validation must record all notices passed and none failed")
    policy = _mapping(manifest["freeze_policy"], "freeze_manifest.json.freeze_policy")
    if policy.get("source_mutation") != "forbidden" or policy.get("ocr_only_core_evidence") != "forbidden":
        _fail("freeze_manifest.json.freeze_policy must forbid source mutation and OCR-only core evidence")
    if policy.get("exact_provenance_required") is not True:
        _fail("freeze_manifest.json.freeze_policy.exact_provenance_required must be true")
    return manifest


def _verify_artifact_index(root: Path, *, budget: _GoldReadBudget) -> dict[str, Mapping[str, Any]]:
    document = _load_json_object(root / "artifact_index.json", "artifact_index.json", budget=budget)
    if document.get("schema_version") != ARTIFACT_INDEX_SCHEMA:
        _fail(f"artifact_index.json.schema_version must be {ARTIFACT_INDEX_SCHEMA!r}")
    rows = _list(document.get("artifacts"), "artifact_index.json.artifacts")
    indexed: dict[str, Mapping[str, Any]] = {}
    for index, raw_row in enumerate(rows):
        label = f"artifact_index.json.artifacts[{index}]"
        row = _mapping(raw_row, label)
        _require_exact_keys(row, ("notice_id", "artifact", "path", "sha256", "bytes"), label)
        path_text = _nonempty_string(row["path"], f"{label}.path")
        if path_text in indexed:
            _fail(f"artifact_index.json has a duplicate path: {path_text}")
        target = _relative_target(root, path_text, f"{label}.path", budget=budget)
        expected_digest = _sha256_value(row["sha256"], f"{label}.sha256")
        if not _is_int(row["bytes"]) or row["bytes"] < 0:
            _fail(f"{label}.bytes must be a non-negative integer")
        if budget.check_file(target, f"{label}.path").size != row["bytes"]:
            _fail(f"artifact index byte-size mismatch: {path_text}")
        if _digest(target, f"{label}.path", budget=budget) != expected_digest:
            _fail(f"artifact index SHA-256 mismatch: {path_text}")
        _nonempty_string(row["artifact"], f"{label}.artifact")
        notice_id = row["notice_id"]
        if notice_id is not None:
            _nonempty_string(notice_id, f"{label}.notice_id")
        indexed[path_text] = row

    required_files = {
        "sample_100.csv",
        "answer_set.json",
        "competitor_mapping.csv",
        "reserve_pool.csv",
        "replacement_log.csv",
        "profile_manifest.json",
    }
    for relative_path in required_files:
        if relative_path not in indexed:
            _fail(f"artifact index omits required frozen artifact: {relative_path}")

    notice_directory = root / "notices"
    if not notice_directory.is_dir() or notice_directory.is_symlink():
        _fail("notices directory is missing or unsafe")
    governed_directory = root / "governance"
    if not governed_directory.is_dir() or governed_directory.is_symlink():
        _fail("governance directory is missing or unsafe")
    for required in _V5_GOVERNANCE_FILES | _V4_GOVERNANCE_FILES:
        if f"governance/{required}" not in indexed:
            _fail(f"artifact index omits required governance artifact: governance/{required}")

    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            _fail(f"Gold corpus must not contain symlinks: {_relative_path(root, path)}")
        if path.is_file():
            # Even intentionally unindexed root metadata (currently README.md
            # plus the two index roots) belongs to the same bounded corpus.
            # Registering every regular file prevents an otherwise ignored
            # file from bypassing both per-file and aggregate limits.
            budget.check_file(path, "Gold corpus file")
            actual_files.add(_relative_path(root, path))
        elif not path.is_dir():
            _fail(f"Gold corpus must not contain non-regular entries: {_relative_path(root, path)}")
    unindexed = actual_files - set(indexed) - _UNINDEXED_ROOT_FILES
    if unindexed:
        _fail(f"artifact index omits corpus files: {sorted(unindexed)[:5]}")
    unexpected_index_paths = set(indexed) - actual_files
    if unexpected_index_paths:
        _fail(f"artifact index names missing files: {sorted(unexpected_index_paths)[:5]}")
    return indexed


def _manifest_artifact(
    root: Path,
    artifact_index: Mapping[str, Mapping[str, Any]],
    raw: object,
    *,
    pblanc_id: str,
    kind: str,
    budget: _GoldReadBudget,
) -> Path:
    label = f"profile_manifest[{pblanc_id}].frozen.{kind}"
    row = _mapping(raw, label)
    _require_exact_keys(row, ("path", "sha256"), label)
    expected_path = f"notices/{pblanc_id}/{_NOTICE_ARTIFACTS[kind]}"
    if row["path"] != expected_path:
        _fail(f"{label}.path must be {expected_path!r}")
    expected_digest = _sha256_value(row["sha256"], f"{label}.sha256")
    indexed = artifact_index.get(expected_path)
    if indexed is None:
        _fail(f"{label} is not represented in artifact_index.json")
    if indexed.get("sha256") != expected_digest:
        _fail(f"{label} SHA-256 does not match artifact_index.json")
    if indexed.get("notice_id") != pblanc_id:
        _fail(f"{label} artifact index notice_id does not match its notice directory")
    target = _relative_target(root, row["path"], f"{label}.path", budget=budget)
    if _digest(target, f"{label}.path", budget=budget) != expected_digest:
        _fail(f"{label} SHA-256 does not match its file")
    return target


def _check_external_source(raw: object, frozen_sha256: str, label: str) -> None:
    source = _mapping(raw, label)
    _require_exact_keys(source, ("path", "sha256"), label)
    _nonempty_string(source["path"], f"{label}.path")
    if _sha256_value(source["sha256"], f"{label}.sha256") != frozen_sha256:
        _fail(f"{label}.sha256 must equal the frozen artifact SHA-256")


def _verify_profile_manifest(
    root: Path,
    manifest: Mapping[str, Any],
    artifact_index: Mapping[str, Mapping[str, Any]],
    expected_notice_count: int,
    *,
    budget: _GoldReadBudget,
) -> dict[str, Mapping[str, Any]]:
    document = _load_json_object(root / "profile_manifest.json", "profile_manifest.json", budget=budget)
    if document.get("schema_version") != PROFILE_MANIFEST_SCHEMA:
        _fail(f"profile_manifest.json.schema_version must be {PROFILE_MANIFEST_SCHEMA!r}")
    rows = _list(document.get("profiles"), "profile_manifest.json.profiles")
    if len(rows) != expected_notice_count:
        _fail("profile_manifest.json profile count does not match the expected notice count")
    profiles: dict[str, Mapping[str, Any]] = {}
    for index, raw_row in enumerate(rows):
        label = f"profile_manifest.json.profiles[{index}]"
        row = _mapping(raw_row, label)
        _require_exact_keys(row, ("pblanc_id", "notice_id", "role", "source_kind", "source", "frozen"), label)
        pblanc_id = _nonempty_string(row["pblanc_id"], f"{label}.pblanc_id")
        if pblanc_id in profiles:
            _fail(f"profile_manifest.json has duplicate pblanc_id: {pblanc_id}")
        if row["notice_id"] != f"bizinfo:{pblanc_id}":
            _fail(f"{label}.notice_id must be the canonical bizinfo identity for its pblanc_id")
        _nonempty_string(row["role"], f"{label}.role")
        _nonempty_string(row["source_kind"], f"{label}.source_kind")
        frozen = _mapping(row["frozen"], f"{label}.frozen")
        source = _mapping(row["source"], f"{label}.source")
        _require_exact_keys(frozen, _NOTICE_ARTIFACTS, f"{label}.frozen")
        _require_exact_keys(source, _NOTICE_ARTIFACTS, f"{label}.source")
        for kind in _NOTICE_ARTIFACTS:
            frozen_entry = _mapping(frozen[kind], f"{label}.frozen.{kind}")
            _manifest_artifact(
                root,
                artifact_index,
                frozen_entry,
                pblanc_id=pblanc_id,
                kind=kind,
                budget=budget,
            )
            _check_external_source(
                source[kind],
                _sha256_value(frozen_entry["sha256"], f"{label}.frozen.{kind}.sha256"),
                f"{label}.source.{kind}",
            )
        profiles[pblanc_id] = row

    role_counts = Counter(str(row["role"]) for row in profiles.values())
    source_counts = Counter(str(row["source_kind"]) for row in profiles.values())
    if dict(sorted(role_counts.items())) != dict(manifest["role_counts"]):
        _fail("freeze_manifest.json.role_counts does not match profile_manifest.json")
    if dict(sorted(source_counts.items())) != dict(manifest["source_counts"]):
        _fail("freeze_manifest.json.source_counts does not match profile_manifest.json")
    notice_dirs = {path.name for path in (root / "notices").iterdir() if path.is_dir()}
    if notice_dirs != set(profiles):
        _fail("notice directories and profile_manifest.json pblanc_ids do not match")
    return profiles


def _common_ir_blocks(common_ir: Mapping[str, Any], label: str) -> tuple[Mapping[str, Any], dict[str, set[str]]]:
    if common_ir.get("schema_version") != COMMON_IR_SCHEMA:
        _fail(f"{label}.schema_version must be {COMMON_IR_SCHEMA!r}")
    document = _mapping(common_ir.get("document"), f"{label}.document")
    _require_exact_keys(document, ("document_id", "source_kind", "provenance"), f"{label}.document")
    _nonempty_string(document["document_id"], f"{label}.document.document_id")
    _nonempty_string(document["source_kind"], f"{label}.document.source_kind")
    provenance = _mapping(document["provenance"], f"{label}.document.provenance")
    _require_exact_keys(provenance, ("source_sha256", "source_location"), f"{label}.document.provenance")
    _sha256_value(provenance["source_sha256"], f"{label}.document.provenance.source_sha256")
    _nonempty_string(provenance["source_location"], f"{label}.document.provenance.source_location")
    block_occurrences: dict[str, set[str]] = {}
    for index, raw_block in enumerate(_list(common_ir.get("blocks"), f"{label}.blocks")):
        block_label = f"{label}.blocks[{index}]"
        block = _mapping(raw_block, block_label)
        block_id = _nonempty_string(block.get("block_id"), f"{block_label}.block_id")
        if block_id in block_occurrences:
            _fail(f"{label}.blocks has duplicate block_id: {block_id}")
        if not isinstance(block.get("text"), str):
            _fail(f"{block_label}.text must be a string")
        occurrences = _require_string_list(block.get("text_occurrence_ids"), f"{block_label}.text_occurrence_ids")
        if not occurrences:
            _fail(f"{block_label}.text_occurrence_ids must not be empty")
        if len(occurrences) != len(set(occurrences)):
            _fail(f"{block_label}.text_occurrence_ids must be unique")
        occurrence_ids = set(occurrences)
        for occurrence_index, raw_occurrence in enumerate(block.get("occurrences", [])):
            occurrence = _mapping(raw_occurrence, f"{block_label}.occurrences[{occurrence_index}]")
            occurrence_ids.add(
                _nonempty_string(
                    occurrence.get("occurrence_id"),
                    f"{block_label}.occurrences[{occurrence_index}].occurrence_id",
                )
            )
        block_occurrences[block_id] = occurrence_ids
    if not block_occurrences:
        _fail(f"{label}.blocks must not be empty")
    return document, block_occurrences


def _verify_common_ir_identity(
    profile: Mapping[str, Any],
    selection: Mapping[str, Any],
    document: Mapping[str, Any],
    *,
    label: str,
) -> None:
    provenance = _mapping(document["provenance"], f"{label}.common_ir.document.provenance")
    expected = {
        "document_id": document["document_id"],
        "source_kind": document["source_kind"],
        "source_sha256": provenance["source_sha256"],
    }
    identity = _mapping(selection.get("common_ir_identity"), f"{label}.selection.common_ir_identity")
    _require_exact_keys(identity, expected, f"{label}.selection.common_ir_identity")
    if set(identity) != set(expected):
        _fail(f"{label}.selection.common_ir_identity has unsupported fields")
    for key, value in expected.items():
        if identity[key] != value:
            _fail(f"{label}.selection.common_ir_identity.{key} does not match common_ir_v1.json")

    source_documents = _list(profile.get("source_documents"), f"{label}.profile.source_documents")
    if not source_documents:
        _fail(f"{label}.profile.source_documents must not be empty")
    matched_lineage = False
    for index, raw_source_document in enumerate(source_documents):
        source_document = _mapping(raw_source_document, f"{label}.profile.source_documents[{index}]")
        lineage = _mapping(source_document.get("common_ir"), f"{label}.profile.source_documents[{index}].common_ir")
        _require_exact_keys(
            lineage,
            ("document_id", "schema_version", "source_kind", "source_sha256", "source_location"),
            f"{label}.profile.source_documents[{index}].common_ir",
        )
        if lineage.get("schema_version") != COMMON_IR_SCHEMA:
            _fail(f"{label}.profile Common IR lineage has an invalid schema_version")
        for key, value in expected.items():
            if lineage.get(key) != value:
                break
        else:
            if lineage.get("source_location") != provenance["source_location"]:
                _fail(f"{label}.profile Common IR source_location does not match common_ir_v1.json")
            matched_lineage = True
    if not matched_lineage:
        _fail(f"{label}.profile has no Common IR lineage matching common_ir_v1.json")
    if profile.get("source_profile_id") != document["document_id"]:
        _fail(f"{label}.profile.source_profile_id does not match common_ir_v1.json document_id")

    selection_lineage = _mapping(selection.get("candidate_pack_lineage"), f"{label}.selection.candidate_pack_lineage")
    profile_metadata = _mapping(profile.get("processing_metadata"), f"{label}.profile.processing_metadata")
    profile_lineage = _mapping(profile_metadata.get("candidate_pack"), f"{label}.profile.processing_metadata.candidate_pack")
    lineage_keys = ("candidate_pack_id", "common_ir_document_id", "common_ir_source_sha256", "text_basis")
    lineage_optional_keys = {
        "candidate_pack_generator", "candidate_pack_generator_version", "recovery_source",
        "parent_pack_id", "parent_generator", "parent_generator_version",
    }
    _require_exact_keys(selection_lineage, lineage_keys, f"{label}.selection.candidate_pack_lineage")
    _require_exact_keys(profile_lineage, lineage_keys, f"{label}.profile.processing_metadata.candidate_pack")
    allowed_lineage_keys = set(lineage_keys) | lineage_optional_keys
    if set(selection_lineage) - allowed_lineage_keys or set(profile_lineage) - allowed_lineage_keys:
        _fail(f"{label} candidate-pack lineage has unsupported fields")
    if selection_lineage != profile_lineage:
        _fail(f"{label} candidate-pack lineage differs between selection and profile")
    if selection_lineage["common_ir_document_id"] != expected["document_id"]:
        _fail(f"{label} candidate-pack lineage document_id does not match Common IR")
    if selection_lineage["common_ir_source_sha256"] != expected["source_sha256"]:
        _fail(f"{label} candidate-pack lineage source SHA-256 does not match Common IR")
    if selection_lineage["text_basis"] != TEXT_BASIS:
        _fail(f"{label} candidate-pack lineage must declare {TEXT_BASIS!r}")
    _validate_native_candidate_pack_lineage(selection_lineage, label=label)


def _validate_native_candidate_pack_lineage(
    lineage: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """Validate optional native-transform ancestry without changing legacy packs.

    An atomic/legacy CandidatePack has no parent tuple (the equivalent of the
    ``off`` transform mode).  Once any parent field appears, the exact-native
    transform identity and its complete three-field parent identity become a
    durable part of the frozen artifact contract.
    """

    parent_keys = (
        "parent_pack_id",
        "parent_generator",
        "parent_generator_version",
    )
    present = [key for key in parent_keys if key in lineage]
    if present and len(present) != len(parent_keys):
        _fail(f"{label} native CandidatePack parent lineage must be all-or-none")
    generator = lineage.get("candidate_pack_generator")
    version = lineage.get("candidate_pack_generator_version")
    if not present:
        if generator == NATIVE_EXACT_TRANSFORM_GENERATOR:
            _fail(f"{label} native CandidatePack transform requires parent lineage")
        return
    for key in parent_keys:
        _nonempty_string(lineage.get(key), f"{label}.{key}")
    if generator != NATIVE_EXACT_TRANSFORM_GENERATOR:
        _fail(f"{label} native CandidatePack has an unsupported generator")
    if version not in _NATIVE_EXACT_TRANSFORM_VERSIONS:
        _fail(f"{label} native CandidatePack has an unsupported transform version")


def _validate_legacy_runpod_014_native_lineage(
    lineage: Mapping[str, Any],
    *,
    pblanc_id: str,
    label: str,
) -> None:
    """Restrict the no-parent seam to the reviewed RunPod 0.1.4 base pack."""

    expected = {
        "candidate_pack_id": f"{pblanc_id}{_EXISTING_A_CANDIDATE_PACK_SUFFIX}",
        "candidate_pack_generator": _LEGACY_RUNPOD_014_CANDIDATE_PACK_GENERATOR,
        "candidate_pack_generator_version": (
            _LEGACY_RUNPOD_014_CANDIDATE_PACK_GENERATOR_VERSION
        ),
    }
    for key, value in expected.items():
        if lineage.get(key) != value:
            _fail(
                f"{label} no-parent native evidence is not a reviewed RunPod "
                f"0.1.4 CandidatePack: {key}"
            )


def _is_legacy_native_source_id(block_id: str) -> bool:
    return block_id.startswith(_NATIVE_DERIVED_BLOCK_PREFIXES) or "#native:" in block_id


def _existing_a_block_order(block: SourceBlock) -> tuple[int, int, int, int, int]:
    """Match ``build_combined_a_candidate_pack`` ordering without router I/O."""

    import re

    body, _, cell = block.block_id.partition("#")
    base_order = block.source_order
    if base_order is None:
        try:
            base_order = int(body.removeprefix("body[").removesuffix("]"))
        except ValueError:
            base_order = 0
    match = re.fullmatch(r"r(\d+)c(\d+)p(\d+)", cell) if cell else None
    if not cell:
        return (base_order, 0, 0, 0, 0)
    if match is not None:
        row, column, paragraph = (int(value) for value in match.groups())
        return (base_order, 1, row, column, paragraph)
    return (base_order, 2, 0, 0, 0)


def _is_existing_a_section(section_id: str | None) -> bool:
    return section_id == "main_notice" or bool(
        section_id and section_id.startswith("main_notice_resume_")
    )


def _existing_a_base_candidate_pack(
    common_ir: Mapping[str, Any],
    source_block_ids: Iterable[str],
    *,
    label: str,
) -> CandidatePack:
    """Rebuild the production A-pack from canonical Common IR atoms only.

    The production router decides *which* A sources to retain, but that LLM
    decision is not reconstructible from Common IR alone.  Its persisted
    source id set is therefore used strictly as a set membership witness.  A
    source's text, locator, occurrence and cell metadata are always taken
    from a fresh Common-IR projection, never from ``source_block_texts``.
    Whole tables are deliberately absent: the production combined A-pack uses
    routed prose plus native table-cell candidates.
    """

    try:
        prepared, _projection = prepare_common_ir_v1(dict(common_ir))
    except Exception as error:  # noqa: BLE001 - normalize vendored input errors
        _fail(f"{label} native CandidatePack Common IR projection failed: {type(error).__name__}")

    main_source_block_ids = {
        block_id
        for section in prepared.sections
        if _is_existing_a_section(section.section_id)
        for block_id in section.source_block_ids
    }

    def belongs_to_main_notice(block: SourceBlock) -> bool:
        # A table-cell id contains ``#r…`` and is not itself present in the
        # section projection.  Production scopes cells through their parent
        # table id, so replay must do the same instead of trusting the cell's
        # defaulted ``section_id``.
        common_ir_block_id = block.common_ir_block_id or block.block_id.split("#", 1)[0]
        return common_ir_block_id in main_source_block_ids

    canonical_blocks = [
        block
        for block in [
            *prepared.fact_candidate_blocks,
            *prepared.table_cell_candidate_blocks,
        ]
        if belongs_to_main_notice(block)
    ]
    canonical = {
        block.block_id: block
        for block in canonical_blocks
    }
    requested = set(source_block_ids)
    base_blocks = [canonical[block_id] for block_id in requested if block_id in canonical]
    if not base_blocks:
        _fail(f"{label} native CandidatePack has no trusted atomic A-pack source ids")
    selected_table_cells = [
        block for block in base_blocks if block.block_kind == "table_cell"
    ]
    if len(selected_table_cells) > MAX_A_TABLE_CELL_CANDIDATES:
        _fail(f"{label} native CandidatePack exceeds the production A-table cell cap")
    selected_cell_ids = {block.block_id for block in selected_table_cells}
    selected_table_ids = {
        block.common_ir_block_id for block in selected_table_cells
    }
    for table_id in selected_table_ids:
        expected_cell_ids = {
            block.block_id
            for block in canonical_blocks
            if block.block_kind == "table_cell"
            and block.common_ir_block_id == table_id
        }
        if not expected_cell_ids.issubset(selected_cell_ids):
            _fail(
                f"{label} native CandidatePack contains a partial production A-table cell group"
            )
    return CandidatePack(
        pack_id=f"{prepared.notice_id}-a-profile-v0.2",
        notice_id=prepared.notice_id,
        extraction_scope="candidate_pack",
        question="A comparison-profile extraction only; never sent to the model.",
        blocks=sorted(base_blocks, key=_existing_a_block_order),
        generator=prepared.candidate_pack_generator,
        generator_version=prepared.candidate_pack_generator_version,
        common_ir_document_id=prepared.common_ir_document_id,
    )


def regenerate_existing_native_candidate_pack(
    common_ir: Mapping[str, Any],
    source_block_texts: Mapping[str, Any],
    lineage: Mapping[str, Any],
    *,
    label: str,
) -> CandidatePack | None:
    """Regenerate and bind the production exact-native A pack.

    ``source_block_texts`` supplies only the routed atomic-id subset.  It is
    then required to be the complete, byte-exact map of the regenerated
    transformed pack, so an invented derived id/text or a removed provenance
    block cannot be presented as an ordinary atomic candidate.
    """

    parent_keys = ("parent_pack_id", "parent_generator", "parent_generator_version")
    if not any(key in lineage for key in parent_keys):
        return None
    try:
        base_pack = _existing_a_base_candidate_pack(
            common_ir, source_block_texts.keys(), label=label
        )
        _require_native_base_identity(base_pack, lineage, label=label)
        version = lineage["candidate_pack_generator_version"]
        producer_version, variant = version.split(":", 1)
        transformed = replay_persisted_native_exact_transforms(
            base_pack,
            producer_version=producer_version,
            include_line_atoms=True,
            include_continuations=variant == "lines+continuations",
        )
    except GoldVerificationError:
        raise
    except Exception as error:  # noqa: BLE001 - normalize vendored input errors
        _fail(f"{label} native CandidatePack cannot be regenerated: {type(error).__name__}")
    current = {
        "candidate_pack_id": transformed.pack_id,
        "candidate_pack_generator": transformed.generator,
        "candidate_pack_generator_version": transformed.generator_version,
        "common_ir_document_id": transformed.common_ir_document_id,
        "parent_pack_id": transformed.parent_pack_id,
        "parent_generator": transformed.parent_generator,
        "parent_generator_version": transformed.parent_generator_version,
    }
    for key, expected in current.items():
        if lineage.get(key) != expected:
            _fail(f"{label} native CandidatePack lineage does not match the regenerated pack: {key}")
    expected_texts = {block.block_id: block.text for block in transformed.blocks}
    if set(source_block_texts) != set(expected_texts):
        _fail(f"{label} native CandidatePack source_block_texts must be the complete regenerated pack map")
    for block_id, expected_text in expected_texts.items():
        if source_block_texts.get(block_id) != expected_text:
            _fail(f"{label} native CandidatePack source_block_texts differs from regenerated Common IR text: {block_id}")
    return transformed


def _require_native_base_identity(
    base_pack: CandidatePack,
    lineage: Mapping[str, Any],
    *,
    label: str,
) -> None:
    if lineage.get("parent_pack_id") == lineage.get("candidate_pack_id"):
        _fail(f"{label} native CandidatePack parent_pack_id must differ from candidate_pack_id")
    expected = {
        "parent_pack_id": base_pack.pack_id,
        "parent_generator": base_pack.generator,
        "parent_generator_version": base_pack.generator_version,
    }
    for key, value in expected.items():
        if lineage.get(key) != value:
            _fail(f"{label} native CandidatePack parent lineage does not match regenerated base pack: {key}")


def _native_block_payload(block: SourceBlock) -> dict[str, Any]:
    """Exact persisted provenance fields expected for a regenerated block."""

    payload = {
        "source_block_id": block.block_id,
        "section_id": block.section_id,
        "source_occurrence_ids": stable_unique_occurrence_ids(
            block.source_occurrence_ids
        ),
        "common_ir_block_id": block.common_ir_block_id,
        "common_ir_occurrence_ids": stable_unique_occurrence_ids(
            block.common_ir_occurrence_ids
        ),
    }
    if block.common_ir_cell_id is not None:
        payload["common_ir_cell_id"] = block.common_ir_cell_id
    if block.native_parent_block_id is not None:
        payload["native_parent_span"] = {
            "source_block_id": block.native_parent_block_id,
            "start_char": block.native_start_char,
            "end_char": block.native_end_char,
            "exact_text": block.text,
        }
    if block.source_spans:
        payload["source_spans"] = [span.model_dump(mode="json") for span in block.source_spans]
    return payload


def _require_native_lineage_when_used(
    *,
    profile: Mapping[str, Any],
    materialized: Sequence[Any],
    selection: Mapping[str, Any],
    label: str,
    allow_legacy_native_without_parent: bool,
) -> None:
    """A final native line/composite reference cannot lose its pack ancestry."""

    def has_native_provenance(source: object) -> bool:
        return isinstance(source, Mapping) and (
            "native_parent_span" in source or "source_spans" in source
        )

    used = any(
        has_native_provenance(raw_evidence)
        for raw_fact in _facts_from_profile(profile, label)[0].values()
        for raw_evidence in _list(raw_fact.get("evidence"), f"{label}.evidence")
    ) or any(
        has_native_provenance(raw_source)
        for raw_row in materialized
        if isinstance(raw_row, Mapping)
        for key in ("source_blocks", "context_blocks")
        for raw_source in (
            raw_row.get(key, []) if isinstance(raw_row.get(key, []), list) else []
        )
    )
    if not used:
        return
    if allow_legacy_native_without_parent:
        return
    lineage = _mapping(
        selection.get("candidate_pack_lineage"),
        f"{label}.selection.candidate_pack_lineage",
    )
    if not all(
        isinstance(lineage.get(key), str) and lineage[key]
        for key in ("parent_pack_id", "parent_generator", "parent_generator_version")
    ):
        _fail(f"{label} native evidence requires complete CandidatePack parent lineage")


def _validate_value_source(
    fact: Mapping[str, Any],
    source_block_texts: Mapping[str, Any],
    used_spans: set[tuple[str, int, int]],
    label: str,
) -> None:
    value_raw = _nonempty_string(fact.get("value_raw"), f"{label}.value_raw")
    source = _mapping(fact.get("value_source"), f"{label}.value_source")
    _require_exact_keys(source, ("source_block_id", "start_char", "end_char", "text_basis"), f"{label}.value_source")
    block_id = _nonempty_string(source["source_block_id"], f"{label}.value_source.source_block_id")
    if source["text_basis"] != TEXT_BASIS:
        _fail(f"{label}.value_source.text_basis must be {TEXT_BASIS!r}")
    start = source["start_char"]
    end = source["end_char"]
    if not _is_int(start) or not _is_int(end) or start < 0 or end <= start:
        _fail(f"{label}.value_source must use ordered non-negative character offsets")
    text = source_block_texts.get(block_id)
    if not isinstance(text, str):
        _fail(f"{label}.value_source references an unknown source block")
    if end > len(text) or text[start:end] != value_raw:
        _fail(f"{label}.value_raw does not exactly match its value_source span")
    span = (block_id, start, end)
    if span in used_spans:
        _fail(f"{label}.value_source duplicates an exact span used by another fact")
    used_spans.add(span)


def _validate_evidence(
    facts: Mapping[str, Mapping[str, Any]],
    fact: Mapping[str, Any],
    source_block_texts: Mapping[str, Any],
    common_ir_document_id: str,
    block_occurrences: Mapping[str, set[str]],
    trusted_native_blocks: Mapping[str, SourceBlock] | None,
    label: str,
    *,
    allow_legacy_native_without_parent: bool,
) -> None:
    evidence = _list(fact.get("evidence"), f"{label}.evidence")
    if not evidence:
        _fail(f"{label}.evidence must not be empty")
    for index, raw_evidence in enumerate(evidence):
        evidence_label = f"{label}.evidence[{index}]"
        item = _mapping(raw_evidence, evidence_label)
        allowed_evidence_keys = {
            "source_block_id", "common_ir_document_id", "common_ir_block_id",
            "common_ir_occurrence_ids", "source_occurrence_ids", "section_id",
            "common_ir_cell_id", "text", "source_spans", "native_parent_span",
        }
        unknown_evidence_keys = set(item) - allowed_evidence_keys
        if unknown_evidence_keys:
            _fail(f"{evidence_label} has unsupported fields: {sorted(unknown_evidence_keys)}")
        _require_exact_keys(
            item,
            ("source_block_id", "common_ir_document_id", "common_ir_block_id", "common_ir_occurrence_ids"),
            evidence_label,
        )
        source_block_id = _nonempty_string(item["source_block_id"], f"{evidence_label}.source_block_id")
        if source_block_id not in source_block_texts:
            _fail(f"{evidence_label}.source_block_id is absent from source_block_texts")
        if item["common_ir_document_id"] != common_ir_document_id:
            _fail(f"{evidence_label}.common_ir_document_id does not match Common IR")
        common_ir_block_id = _nonempty_string(item["common_ir_block_id"], f"{evidence_label}.common_ir_block_id")
        if common_ir_block_id not in block_occurrences:
            _fail(f"{evidence_label}.common_ir_block_id is absent from Common IR")
        occurrence_ids = _require_string_list(item["common_ir_occurrence_ids"], f"{evidence_label}.common_ir_occurrence_ids")
        if not occurrence_ids or len(occurrence_ids) != len(set(occurrence_ids)):
            _fail(f"{evidence_label}.common_ir_occurrence_ids must be non-empty and unique")
        source_occurrence_ids = item.get("source_occurrence_ids", occurrence_ids)
        source_occurrences = _require_string_list(
            source_occurrence_ids, f"{evidence_label}.source_occurrence_ids"
        )
        if not source_occurrences or len(source_occurrences) != len(set(source_occurrences)):
            _fail(f"{evidence_label}.source_occurrence_ids must be non-empty and unique")
        source_text = source_block_texts[source_block_id]
        source_spans = item.get("source_spans")
        native_parent_span = item.get("native_parent_span")
        if source_spans is not None and native_parent_span is not None:
            _fail(f"{evidence_label} cannot mix native line and composite provenance")
        trusted_block = (
            trusted_native_blocks.get(source_block_id)
            if trusted_native_blocks is not None
            else None
        )
        if trusted_native_blocks is not None and trusted_block is not None:
            expected = _native_block_payload(trusted_block)
            is_derived = (
                trusted_block.native_parent_block_id is not None
                or bool(trusted_block.source_spans)
            )
            if is_derived and source_spans is None and native_parent_span is None:
                _fail(f"{evidence_label} removed native provenance from a regenerated derived block")
            persisted_locator_keys = allowed_evidence_keys - {
                "common_ir_document_id",
                "text",
            }
            if set(item).intersection(persisted_locator_keys) != set(expected):
                _fail(
                    f"{evidence_label} locator fields differ from the regenerated "
                    "native CandidatePack block"
                )
            if not set(expected).issubset(item):
                _fail(f"{evidence_label} is missing regenerated native CandidatePack locators")
            if any(item[key] != value for key, value in expected.items()):
                _fail(f"{evidence_label} differs from the regenerated native CandidatePack block")
            if source_text != trusted_block.text:
                _fail(f"{evidence_label} CandidatePack text differs from the regenerated block")
        elif (
            source_spans is not None or native_parent_span is not None
        ) and not allow_legacy_native_without_parent:
            _fail(f"{evidence_label} native provenance has no regenerated CandidatePack block")
        if native_parent_span is not None:
            parent = _mapping(native_parent_span, f"{evidence_label}.native_parent_span")
            expected_parent_keys = {"source_block_id", "start_char", "end_char", "exact_text"}
            if set(parent) != expected_parent_keys:
                _fail(f"{evidence_label}.native_parent_span must use the exact line-slice contract")
            parent_block_id = _nonempty_string(
                parent.get("source_block_id"), f"{evidence_label}.native_parent_span.source_block_id"
            )
            parent_text = source_block_texts.get(parent_block_id)
            if not isinstance(parent_text, str):
                _fail(f"{evidence_label}.native_parent_span references an unknown parent source block")
            start = parent.get("start_char")
            end = parent.get("end_char")
            exact_text = _nonempty_string(
                parent.get("exact_text"), f"{evidence_label}.native_parent_span.exact_text"
            )
            if not _is_int(start) or not _is_int(end) or start < 0 or end <= start:
                _fail(f"{evidence_label}.native_parent_span must use ordered non-negative character offsets")
            if end > len(parent_text) or parent_text[start:end] != exact_text:
                _fail(f"{evidence_label}.native_parent_span does not exactly match its parent text")
            if source_text != exact_text:
                _fail(f"{evidence_label}.native_parent_span does not match its derived CandidatePack text")
        elif source_spans is None:
            if not occurrence_ids or not set(occurrence_ids).issubset(block_occurrences[common_ir_block_id]):
                _fail(f"{evidence_label} has unknown Common IR occurrence provenance")
        else:
            spans = _list(source_spans, f"{evidence_label}.source_spans")
            if len(spans) not in {2, 3}:
                _fail(f"{evidence_label}.source_spans must contain two or three spans")
            span_occurrences: list[str] = []
            previous_order: int | None = None
            span_sections: set[str] = set()
            for span_index, raw_span in enumerate(spans):
                span_label = f"{evidence_label}.source_spans[{span_index}]"
                span = _mapping(raw_span, span_label)
                allowed_span_keys = {
                    "source_block_id", "exact_text", "start_char", "end_char",
                    "separator_after", "source_order", "section_id",
                    "common_ir_block_id", "common_ir_occurrence_ids", "common_ir_cell_id",
                }
                if set(span) - allowed_span_keys:
                    _fail(f"{span_label} has unsupported fields")
                _require_exact_keys(
                    span,
                    (
                        "source_block_id", "exact_text", "start_char", "end_char",
                        "separator_after", "source_order", "section_id",
                        "common_ir_block_id", "common_ir_occurrence_ids",
                    ),
                    span_label,
                )
                _nonempty_string(span.get("source_block_id"), f"{span_label}.source_block_id")
                span_source_block_id = _nonempty_string(
                    span.get("source_block_id"), f"{span_label}.source_block_id"
                )
                exact_text = _nonempty_string(span.get("exact_text"), f"{span_label}.exact_text")
                start = span.get("start_char")
                end = span.get("end_char")
                if not _is_int(start) or not _is_int(end) or start < 0 or end <= start:
                    _fail(f"{span_label} has invalid character offsets")
                parent_text = source_block_texts.get(span_source_block_id)
                if not isinstance(parent_text, str):
                    _fail(f"{span_label}.source_block_id references an unknown source block")
                if end > len(parent_text) or parent_text[start:end] != exact_text:
                    _fail(f"{span_label} does not exactly match its parent source block")
                if span.get("separator_after") not in {"", " "}:
                    _fail(f"{span_label}.separator_after is unsupported")
                source_order = span.get("source_order")
                if not _is_int(source_order) or source_order < 0:
                    _fail(f"{span_label}.source_order must be a non-negative integer")
                if previous_order is not None and source_order <= previous_order:
                    _fail(f"{evidence_label}.source_spans are not strictly ordered")
                previous_order = source_order
                section_id = _nonempty_string(span.get("section_id"), f"{span_label}.section_id")
                span_sections.add(section_id)
                span_block_id = _nonempty_string(
                    span.get("common_ir_block_id"), f"{span_label}.common_ir_block_id"
                )
                if span_block_id not in block_occurrences:
                    _fail(f"{span_label}.common_ir_block_id is absent from Common IR")
                span_ids = _require_string_list(
                    span.get("common_ir_occurrence_ids"),
                    f"{span_label}.common_ir_occurrence_ids",
                )
                if (
                    not span_ids
                    or len(span_ids) != len(set(span_ids))
                    or not set(span_ids).issubset(block_occurrences[span_block_id])
                ):
                    _fail(f"{span_label} has unknown Common IR occurrence provenance")
                span_occurrences.extend(span_ids)
            if len(span_sections) != 1:
                _fail(f"{evidence_label}.source_spans cross section boundaries")
            if spans[-1].get("separator_after"):
                _fail(f"{evidence_label}.source_spans have a trailing separator")
            if common_ir_block_id != spans[0].get("common_ir_block_id"):
                _fail(f"{evidence_label}.common_ir_block_id is not the first composite span")
            if occurrence_ids != span_occurrences:
                _fail(f"{evidence_label} composite occurrence provenance is incomplete")
            if source_text != "".join(
                str(span["exact_text"]) + str(span["separator_after"])
                for span in spans
            ):
                _fail(f"{evidence_label}.source_spans do not losslessly reconstruct derived CandidatePack text")
    for relation_name in ("modifies_fact_ids", "recipient_fact_ids", "basis_fact_ids"):
        for relation_id in _require_string_list(fact.get(relation_name, []), f"{label}.{relation_name}"):
            if relation_id not in facts:
                _fail(f"{label}.{relation_name} contains a dangling fact id: {relation_id}")


def _profile_evidence_reference(
    raw_source: object,
    *,
    include_text: bool,
    label: str,
) -> dict[str, Any]:
    """Project one materialized source block exactly as the v0.2 assembler does."""

    source = _mapping(raw_source, label)
    required = (
        "source_block_id",
        "section_id",
        "source_occurrence_ids",
        "common_ir_document_id",
        "common_ir_block_id",
        "common_ir_occurrence_ids",
        "text",
    )
    _require_exact_keys(source, required, label)
    reference = {
        "source_block_id": source["source_block_id"],
        "section_id": source["section_id"],
        "source_occurrence_ids": source["source_occurrence_ids"],
        "common_ir_document_id": source["common_ir_document_id"],
        "common_ir_block_id": source["common_ir_block_id"],
        "common_ir_occurrence_ids": source["common_ir_occurrence_ids"],
    }
    if include_text:
        reference["text"] = source["text"]
    if source.get("common_ir_cell_id") is not None:
        reference["common_ir_cell_id"] = source["common_ir_cell_id"]
    if source.get("source_spans") is not None:
        reference["source_spans"] = source["source_spans"]
    if source.get("native_parent_span") is not None:
        reference["native_parent_span"] = source["native_parent_span"]
    return reference


def _validate_selection_profile_materialization(
    profile: Mapping[str, Any],
    selection: Mapping[str, Any],
    facts: Mapping[str, Mapping[str, Any]],
    component_ids: set[str],
    materialized_by_id: Mapping[str, Mapping[str, Any]],
    *,
    common_ir_document_id: str,
    block_occurrences: Mapping[str, set[str]],
    trusted_native_blocks: Mapping[str, SourceBlock] | None,
    allow_legacy_native_without_parent: bool,
    label: str,
) -> None:
    """Require selection, server materialization, and Profile to agree exactly."""

    selection_payload = _mapping(selection.get("selection"), f"{label} source selection.selection")
    source_block_texts = _mapping(
        selection.get("source_block_texts"),
        f"{label} source selection.source_block_texts",
    )
    selected_facts: dict[str, Mapping[str, Any]] = {}
    for index, raw_fact in enumerate(_list(selection_payload.get("facts"), f"{label} selection.facts")):
        selected_label = f"{label} selection.facts[{index}]"
        selected = _mapping(raw_fact, selected_label)
        fact_id = _nonempty_string(selected.get("fact_id"), f"{selected_label}.fact_id")
        if fact_id in selected_facts:
            _fail(f"{label} selection.facts has duplicate fact_id: {fact_id}")
        selected_facts[fact_id] = selected
    if set(selected_facts) != set(facts):
        _fail(f"{label} selected fact ids do not exactly match profile fact ids")

    direct_keys = (
        "field_name",
        "status",
        "subject_role",
        "semantic_role",
        "canonical_role",
    )
    relation_keys = (
        "applicability_component_ids",
        "modifies_fact_ids",
        "recipient_fact_ids",
        "basis_fact_ids",
    )
    for fact_id, fact in facts.items():
        item = materialized_by_id[fact_id]
        selected = selected_facts[fact_id]
        fact_label = f"{label} fact {fact_id}"
        for key in direct_keys:
            if selected.get(key) != item.get(key):
                _fail(f"{fact_label} selection/materialized evidence differs: {key}")
            if item.get(key) != fact.get(key):
                _fail(f"{fact_label} materialized evidence/profile differs: {key}")
        for key in relation_keys:
            selected_relations = _unordered_unique_strings(
                selected.get(key), f"{fact_label} selection.{key}"
            )
            materialized_relations = _unordered_unique_strings(
                item.get(key), f"{fact_label} materialized.{key}"
            )
            profile_relations = _unordered_unique_strings(
                fact.get(key), f"{fact_label} profile.{key}"
            )
            if selected_relations != materialized_relations:
                _fail(f"{fact_label} selection/materialized evidence differs: {key}")
            if materialized_relations != profile_relations:
                _fail(f"{fact_label} materialized evidence/profile differs: {key}")

        primary_component_id = item.get("primary_component_id")
        if selected.get("primary_component_id") != primary_component_id:
            _fail(f"{fact_label} selection/materialized evidence differs: primary_component_id")
        if primary_component_id != fact.get("support_component_id"):
            _fail(f"{fact_label} materialized evidence/profile differs: support_component_id")
        expected_scope = "component" if primary_component_id is not None else "notice"
        if fact.get("scope") != expected_scope:
            _fail(f"{fact_label}.scope disagrees with its component ownership")

        value_anchor = _mapping(selected.get("value_anchor"), f"{fact_label} selection.value_anchor")
        value_source = _mapping(item.get("value_source"), f"{fact_label} materialized value_source")
        if value_anchor.get("source_block_id") != value_source.get("source_block_id"):
            _fail(f"{fact_label} value anchor and materialized source block differ")
        if value_anchor.get("anchor_text") != fact.get("value_raw"):
            _fail(f"{fact_label} value anchor and profile value_raw differ")
        if value_source != fact.get("value_source"):
            _fail(f"{fact_label} materialized evidence/profile differs: value_source")

        source_blocks = _list(item.get("source_blocks"), f"{fact_label} materialized source_blocks")
        if len(source_blocks) != 1:
            _fail(f"{fact_label} must materialize exactly one v0.2 source block")
        source_block = _mapping(
            source_blocks[0], f"{fact_label} materialized source block"
        )
        if source_block.get("source_block_id") != value_source.get("source_block_id"):
            _fail(f"{fact_label} value_source and materialized source block differ")
        source_text = source_block.get("text")
        if source_text != fact.get("value_raw"):
            _fail(f"{fact_label} materialized source text and profile value_raw differ")
        expected_evidence = [
            _profile_evidence_reference(
                source_blocks[0], include_text=False, label=f"{fact_label} materialized source block"
            )
        ]
        if expected_evidence != fact.get("evidence"):
            _fail(f"{fact_label} materialized source_blocks/profile evidence differ")

        context_blocks = _list(item.get("context_blocks"), f"{fact_label} materialized context_blocks")
        for context_index, raw_context in enumerate(context_blocks):
            context_label = f"{fact_label} materialized context_blocks[{context_index}]"
            context = _mapping(raw_context, context_label)
            context_block_id = _nonempty_string(
                context.get("source_block_id"), f"{context_label}.source_block_id"
            )
            if context.get("text") != source_block_texts.get(context_block_id):
                _fail(f"{context_label}.text differs from its CandidatePack source")
            _validate_evidence(
                facts,
                {**fact, "evidence": [context]},
                source_block_texts,
                common_ir_document_id,
                block_occurrences,
                trusted_native_blocks,
                context_label,
                allow_legacy_native_without_parent=allow_legacy_native_without_parent,
            )
        if _unordered_unique_strings(
            [block.get("source_block_id") for block in context_blocks],
            f"{fact_label} materialized context block ids",
        ) != _unordered_unique_strings(
            selected.get("context_source_block_ids"),
            f"{fact_label} selected context block ids",
        ):
            _fail(f"{fact_label} selection/materialized context blocks differ")
        expected_context = [
            _profile_evidence_reference(
                block,
                include_text=True,
                label=f"{fact_label} materialized context_blocks[{index}]",
            )
            for index, block in enumerate(context_blocks)
        ]
        if _unordered_unique_json_rows(
            expected_context, f"{fact_label} materialized context evidence"
        ) != _unordered_unique_json_rows(
            fact.get("context_evidence"), f"{fact_label} profile context evidence"
        ):
            _fail(f"{fact_label} materialized context_blocks/profile context_evidence differ")

        if fact.get("field_name") == "delivery_roles":
            names = _list(item.get("organization_names"), f"{fact_label} organization_names")
            sources = _list(item.get("organization_sources"), f"{fact_label} organization_sources")
            if not names:
                _fail(
                    f"{fact_label} delivery role requires at least one explicit organization"
                )
            if len(names) != len(sources):
                _fail(f"{fact_label} organization names/sources lengths differ")
            expected_organization_anchors: list[dict[str, str]] = []
            for index, (raw_name, raw_source) in enumerate(
                zip(names, sources, strict=True)
            ):
                name = _nonempty_string(
                    raw_name, f"{fact_label} organization_names[{index}]"
                )
                source = _mapping(
                    raw_source, f"{fact_label} organization_sources[{index}]"
                )
                source_block_id = _nonempty_string(
                    source.get("source_block_id"),
                    f"{fact_label} organization_sources[{index}].source_block_id",
                )
                expected_organization_anchors.append(
                    {"source_block_id": source_block_id, "anchor_text": name}
                )
            if _unordered_unique_json_rows(
                selected.get("organization_anchors"),
                f"{fact_label} selection.organization_anchors",
            ) != _unordered_unique_json_rows(
                expected_organization_anchors,
                f"{fact_label} materialized organization anchors",
            ):
                _fail(
                    f"{fact_label} selection/materialized organization anchors differ"
                )
            expected_organizations = [
                {"value_raw": name, "value_source": source}
                for name, source in zip(names, sources, strict=True)
            ]
            if _unordered_unique_json_rows(
                fact.get("organization_names"), f"{fact_label} profile organization_names"
            ) != _unordered_unique_json_rows(
                expected_organizations, f"{fact_label} materialized organization_names"
            ):
                _fail(f"{fact_label} materialized/profile organization names differ")
            for key in ("role_raw", "role_source_block_id", "role_source", "canonical_role"):
                if item.get(key) != fact.get(key):
                    _fail(f"{fact_label} materialized evidence/profile differs: {key}")
            role_raw = item.get("role_raw")
            role_source = item.get("role_source")
            role_source_block_id = item.get("role_source_block_id")
            selected_role_anchor = selected.get("role_anchor")
            if role_raw is None:
                _fail(f"{fact_label} delivery role requires an explicit role anchor")
            else:
                role_text = _nonempty_string(role_raw, f"{fact_label} role_raw")
                role_source_map = _mapping(
                    role_source, f"{fact_label} role_source"
                )
                role_source_id = _nonempty_string(
                    role_source_map.get("source_block_id"),
                    f"{fact_label} role_source.source_block_id",
                )
                if role_source_block_id != role_source_id:
                    _fail(
                        f"{fact_label} role_source_block_id and role_source differ"
                    )
                expected_role_anchor = {
                    "source_block_id": role_source_id,
                    "anchor_text": role_text,
                }
                if selected_role_anchor != expected_role_anchor:
                    _fail(
                        f"{fact_label} selection/materialized role anchor differs"
                    )

    profile_components: dict[str, Mapping[str, Any]] = {}
    for index, raw_component in enumerate(_list(profile.get("support_components"), f"{label} profile.support_components")):
        component = _mapping(raw_component, f"{label} profile.support_components[{index}]")
        component_id = _nonempty_string(component.get("support_component_id"), f"{label} profile component id")
        profile_components[component_id] = component
    selected_components: dict[str, Mapping[str, Any]] = {}
    for index, raw_component in enumerate(_list(selection_payload.get("support_components"), f"{label} selection.support_components")):
        component = _mapping(raw_component, f"{label} selection.support_components[{index}]")
        component_id = _nonempty_string(component.get("support_component_id"), f"{label} selected component id")
        if component_id in selected_components:
            _fail(f"{label} selection.support_components has duplicate id: {component_id}")
        selected_components[component_id] = component
    materialized_components: dict[str, Mapping[str, Any]] = {}
    for index, raw_component in enumerate(_list(selection.get("materialized_components"), f"{label} materialized_components")):
        component = _mapping(raw_component, f"{label} materialized_components[{index}]")
        component_id = _nonempty_string(component.get("support_component_id"), f"{label} materialized component id")
        if component_id in materialized_components:
            _fail(f"{label} materialized_components has duplicate id: {component_id}")
        materialized_components[component_id] = component
    if set(profile_components) != component_ids or set(selected_components) != component_ids or set(materialized_components) != component_ids:
        _fail(f"{label} selected/materialized/profile component ids differ")
    for component_id in sorted(component_ids):
        selected = selected_components[component_id]
        materialized = materialized_components[component_id]
        profile_component = profile_components[component_id]
        if selected.get("component_kind") != profile_component.get("component_kind"):
            _fail(f"{label} component {component_id} selection/profile differs: component_kind")
        for key in ("source_block_ids", "table_block_ids"):
            if _unordered_unique_strings(
                selected.get(key), f"{label} component {component_id} selection.{key}"
            ) != _unordered_unique_strings(
                profile_component.get(key), f"{label} component {component_id} profile.{key}"
            ):
                _fail(f"{label} component {component_id} selection/profile differs: {key}")
        for key in ("name_raw", "name_source_block_id"):
            if materialized.get(key) != profile_component.get(key):
                _fail(f"{label} component {component_id} materialized/profile differs: {key}")
        expected_name_status = "identified" if materialized.get("name_raw") else "not_extracted"
        if profile_component.get("name_status") != expected_name_status:
            _fail(f"{label} component {component_id} has inconsistent name_status")
        selected_name_anchor = selected.get("name_anchor")
        if materialized.get("name_raw") is None:
            if selected_name_anchor is not None or materialized.get("name_source_block_id") is not None:
                _fail(f"{label} component {component_id} empty name has anchor provenance")
        else:
            expected_name_anchor = {
                "source_block_id": _nonempty_string(
                    materialized.get("name_source_block_id"),
                    f"{label} component {component_id} name_source_block_id",
                ),
                "anchor_text": _nonempty_string(
                    materialized.get("name_raw"),
                    f"{label} component {component_id} name_raw",
                ),
            }
            if selected_name_anchor != expected_name_anchor:
                _fail(f"{label} component {component_id} selection/materialized name anchor differs")
            component_source_ids = set(
                _unordered_unique_strings(
                    selected.get("source_block_ids"),
                    f"{label} component {component_id} selection.source_block_ids",
                )
            ) | set(
                _unordered_unique_strings(
                    selected.get("table_block_ids"),
                    f"{label} component {component_id} selection.table_block_ids",
                )
            )
            name_source_block_id = expected_name_anchor["source_block_id"]
            if name_source_block_id not in component_source_ids:
                _fail(
                    f"{label} component {component_id} name anchor is outside its component sources"
                )
            source_text = source_block_texts.get(name_source_block_id)
            if not isinstance(source_text, str):
                _fail(
                    f"{label} component {component_id} name anchor is absent from CandidatePack"
                )
            if source_text.count(expected_name_anchor["anchor_text"]) != 1:
                _fail(
                    f"{label} component {component_id} name anchor must occur exactly once in its source block"
                )


def _facts_from_profile(
    profile: Mapping[str, Any],
    label: str,
) -> tuple[dict[str, Mapping[str, Any]], set[str]]:
    comparison = _mapping(profile.get("comparison_profile"), f"{label}.comparison_profile")
    components = _list(profile.get("support_components"), f"{label}.support_components")
    facts: dict[str, Mapping[str, Any]] = {}
    component_ids: set[str] = set()
    for component_index, raw_component in enumerate(components):
        component_label = f"{label}.support_components[{component_index}]"
        component = _mapping(raw_component, component_label)
        component_id = _nonempty_string(component.get("support_component_id"), f"{component_label}.support_component_id")
        if component_id in component_ids:
            _fail(f"{label}.support_components has duplicate support_component_id: {component_id}")
        component_ids.add(component_id)
        _list(component.get("facts"), f"{component_label}.facts")
    groups: list[tuple[str, Sequence[Any], str | None]] = []
    for field_name, raw_facts in comparison.items():
        groups.append((str(field_name), _list(raw_facts, f"{label}.comparison_profile.{field_name}"), None))
    for component_index, raw_component in enumerate(components):
        component = _mapping(raw_component, f"{label}.support_components[{component_index}]")
        groups.append(("", _list(component["facts"], f"{label}.support_components[{component_index}].facts"), str(component["support_component_id"])))
    for field_name, group, expected_component_id in groups:
        for fact_index, raw_fact in enumerate(group):
            fact_label = f"{label}.fact[{len(facts) + fact_index}]"
            fact = _mapping(raw_fact, fact_label)
            _require_exact_keys(
                fact,
                ("fact_id", "field_name", "value_raw", "status", "value_source", "evidence"),
                fact_label,
            )
            fact_id = _nonempty_string(fact["fact_id"], f"{fact_label}.fact_id")
            if fact_id in facts:
                _fail(f"{label} has duplicate fact_id: {fact_id}")
            if expected_component_id is None and fact["field_name"] != field_name:
                _fail(f"{fact_label}.field_name does not match its comparison_profile field")
            if expected_component_id is not None and fact.get("support_component_id") != expected_component_id:
                _fail(f"{fact_label}.support_component_id does not match its containing component")
            if fact.get("status") not in {"identified", "partially_identified"}:
                _fail(f"{fact_label}.status must be an active, identified status")
            facts[fact_id] = fact
    if not facts:
        _fail(f"{label} has no facts")
    return facts, component_ids


def verify_profile_artifact_triple(
    profile: Mapping[str, Any],
    selection: Mapping[str, Any],
    common_ir: Mapping[str, Any],
    *,
    pblanc_id: str,
    allow_unnamespaced_notice_id: bool = False,
    allow_legacy_native_without_parent: bool = False,
) -> tuple[int, Counter[str]]:
    """Validate one in-memory Profile/selection/Common-IR artifact triple.

    This is the notice-level contract used by the frozen Gold verifier.  It is
    public so offline candidate evaluators can apply the same provenance and
    exact-span checks to baseline and candidate artifacts before comparing
    their meaning.  The function is read-only and performs no network or
    filesystem I/O.  ``allow_legacy_native_without_parent`` is a narrow
    compatibility seam for the semantic comparator, which subsequently binds
    RunPod 0.1.4 line/composite provenance to its deterministic broad source
    universe.  Standalone verification remains strict by default.
    """

    label = f"notice {pblanc_id}"
    profile = _mapping(profile, f"{label} profile")
    selection = _mapping(selection, f"{label} source selection")
    common_ir = _mapping(common_ir, f"{label} Common IR")
    if profile.get("schema_version") != PROFILE_SCHEMA:
        _fail(f"{label} profile schema_version must be {PROFILE_SCHEMA!r}")
    accepted_notice_ids = {f"bizinfo:{pblanc_id}"}
    if allow_unnamespaced_notice_id:
        accepted_notice_ids.add(pblanc_id)
    if profile.get("notice_id") not in accepted_notice_ids:
        _fail(f"{label} profile.notice_id does not match its directory")
    if selection.get("selection_contract") != SELECTION_CONTRACT:
        _fail(f"{label} source selection contract must be {SELECTION_CONTRACT!r}")
    selection_payload = _mapping(selection.get("selection"), f"{label} source selection.selection")
    if selection_payload.get("notice_id") != pblanc_id:
        _fail(f"{label} source selection.notice_id does not match its directory")
    source_block_texts = _mapping(selection.get("source_block_texts"), f"{label} source selection.source_block_texts")
    if not source_block_texts:
        _fail(f"{label} source selection.source_block_texts must not be empty")
    for block_id, text in source_block_texts.items():
        _nonempty_string(block_id, f"{label} source selection source block id")
        if not isinstance(text, str):
            _fail(f"{label} source selection source block text must be a string")
    document, block_occurrences = _common_ir_blocks(common_ir, f"{label} Common IR")
    _verify_common_ir_identity(profile, selection, document, label=label)
    facts, component_ids = _facts_from_profile(profile, label)
    selection_lineage = _mapping(
        selection.get("candidate_pack_lineage"),
        f"{label}.selection.candidate_pack_lineage",
    )
    selection_candidate_pack_id = _nonempty_string(
        selection_payload.get("candidate_pack_id"),
        f"{label}.selection.candidate_pack_id",
    )
    if selection_candidate_pack_id != selection_lineage.get("candidate_pack_id"):
        _fail(
            f"{label}.selection.candidate_pack_id does not match CandidatePack lineage"
        )
    has_native_profile_evidence = any(
        isinstance(raw_evidence, Mapping)
        and ("native_parent_span" in raw_evidence or "source_spans" in raw_evidence)
        for fact in facts.values()
        for raw_evidence in _list(fact.get("evidence"), f"{label}.fact evidence")
    )
    has_parent_lineage = any(
        key in selection_lineage
        for key in ("parent_pack_id", "parent_generator", "parent_generator_version")
    )
    has_native_source_id = any(
        _is_legacy_native_source_id(block_id)
        for block_id in source_block_texts
    )
    has_no_parent_native = (
        has_native_profile_evidence or has_native_source_id
    ) and not has_parent_lineage
    if has_no_parent_native:
        if not allow_legacy_native_without_parent:
            _fail(f"{label} native evidence requires complete CandidatePack parent lineage")
        _validate_legacy_runpod_014_native_lineage(
            selection_lineage,
            pblanc_id=pblanc_id,
            label=label,
        )
    regenerated_pack = (
        regenerate_existing_native_candidate_pack(
            common_ir, source_block_texts, selection_lineage, label=label
        )
        if has_parent_lineage
        else None
    )
    trusted_native_blocks = (
        {block.block_id: block for block in regenerated_pack.blocks}
        if regenerated_pack is not None
        else None
    )
    used_spans: set[tuple[str, int, int]] = set()
    common_ir_document_id = str(document["document_id"])
    for fact_id, fact in facts.items():
        fact_label = f"{label} fact {fact_id}"
        _validate_value_source(fact, source_block_texts, used_spans, fact_label)
        _validate_evidence(
            facts, fact, source_block_texts, common_ir_document_id,
            block_occurrences, trusted_native_blocks, fact_label,
            allow_legacy_native_without_parent=allow_legacy_native_without_parent,
        )
        for component_id in _require_string_list(fact.get("applicability_component_ids", []), f"{fact_label}.applicability_component_ids"):
            if component_id not in component_ids:
                _fail(f"{fact_label}.applicability_component_ids contains a dangling component id: {component_id}")
        attached_component = fact.get("support_component_id")
        if attached_component is not None and attached_component not in component_ids:
            _fail(f"{fact_label}.support_component_id contains a dangling component id")

    materialized = _list(selection.get("materialized_evidence"), f"{label} source selection.materialized_evidence")
    materialized_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_materialized in enumerate(materialized):
        materialized_label = f"{label} source selection.materialized_evidence[{index}]"
        item = _mapping(raw_materialized, materialized_label)
        fact_id = _nonempty_string(item.get("fact_id"), f"{materialized_label}.fact_id")
        if fact_id in materialized_by_id:
            _fail(f"{label} materialized evidence has duplicate fact_id: {fact_id}")
        materialized_by_id[fact_id] = item
    if set(materialized_by_id) != set(facts):
        _fail(f"{label} materialized evidence fact ids do not exactly match profile fact ids")
    for fact_id, fact in facts.items():
        item = materialized_by_id[fact_id]
        for key in ("field_name", "status", "value_source"):
            if item.get(key) != fact.get(key):
                _fail(f"{label} materialized evidence differs from profile fact {fact_id}: {key}")
    materialized_components = _list(selection.get("materialized_components"), f"{label} source selection.materialized_components")
    materialized_component_ids = {
        _nonempty_string(_mapping(item, f"{label} materialized component").get("support_component_id"), f"{label} materialized component.support_component_id")
        for item in materialized_components
    }
    if materialized_component_ids != component_ids:
        _fail(f"{label} materialized component ids do not match profile components")

    _require_native_lineage_when_used(
        profile=profile,
        materialized=materialized,
        selection=selection,
        label=label,
        allow_legacy_native_without_parent=allow_legacy_native_without_parent,
    )

    _validate_selection_profile_materialization(
        profile,
        selection,
        facts,
        component_ids,
        materialized_by_id,
        common_ir_document_id=common_ir_document_id,
        block_occurrences=block_occurrences,
        trusted_native_blocks=trusted_native_blocks,
        allow_legacy_native_without_parent=allow_legacy_native_without_parent,
        label=label,
    )

    for index, raw_projection in enumerate(_list(profile.get("derived_projections", []), f"{label} profile.derived_projections")):
        projection = _mapping(raw_projection, f"{label} profile.derived_projections[{index}]")
        references: list[str] = []
        for key in ("source_fact_ids", "positive_source_fact_ids", "exclusion_source_fact_ids"):
            if key in projection:
                references.extend(_require_string_list(projection[key], f"{label} projection.{key}"))
        missing = set(references) - set(facts)
        if missing:
            _fail(f"{label} derived projection has dangling source fact ids: {sorted(missing)}")
    return len(facts), Counter(str(fact["status"]) for fact in facts.values())


def _verify_profile_document(
    profile_path: Path,
    selection_path: Path,
    common_ir_path: Path,
    *,
    pblanc_id: str,
    budget: _GoldReadBudget,
) -> tuple[int, Counter[str]]:
    """Load and validate one frozen notice triple."""

    label = f"notice {pblanc_id}"
    return verify_profile_artifact_triple(
        _load_json_object(profile_path, f"{label} profile", budget=budget),
        _load_json_object(selection_path, f"{label} source selection", budget=budget),
        _load_json_object(common_ir_path, f"{label} Common IR", budget=budget),
        pblanc_id=pblanc_id,
    )


def _verify_sample_and_answer_set(
    root: Path,
    profiles: Mapping[str, Mapping[str, Any]],
    expected_notice_count: int,
    *,
    budget: _GoldReadBudget,
) -> None:
    sample = _read_csv(root / "sample_100.csv", "sample_100.csv", budget=budget)
    if len(sample) != expected_notice_count:
        _fail("sample_100.csv row count does not match the expected notice count")
    sample_by_id: dict[str, dict[str, str]] = {}
    for index, row in enumerate(sample):
        label = f"sample_100.csv row {index + 2}"
        for field in ("pblanc_id", "notice_id", "role", "title", "support_field"):
            _nonempty_string(row.get(field), f"{label}.{field}")
        pblanc_id = row["pblanc_id"]
        if pblanc_id in sample_by_id:
            _fail(f"sample_100.csv has duplicate pblanc_id: {pblanc_id}")
        profile = profiles.get(pblanc_id)
        if profile is None:
            _fail(f"sample_100.csv names a pblanc_id absent from profile_manifest.json: {pblanc_id}")
        if row["notice_id"] != profile["notice_id"] or row["role"] != profile["role"]:
            _fail(f"sample_100.csv identity or role disagrees with profile_manifest.json: {pblanc_id}")
        for field in ("title", "support_field"):
            if field in profile and profile[field] != row[field]:
                _fail(f"sample_100.csv {field} disagrees with profile_manifest.json: {pblanc_id}")
        sample_by_id[pblanc_id] = row
    if set(sample_by_id) != set(profiles):
        _fail("sample_100.csv pblanc_ids do not exactly match profile_manifest.json")

    answers = _load_json_object(root / "answer_set.json", "answer_set.json", budget=budget)
    if answers.get("schema_version") != "answer_set.v1":
        _fail("answer_set.json.schema_version must be 'answer_set.v1'")
    answer_rows = _list(answers.get("answers"), "answer_set.json.answers")
    expected_answers = {pblanc_id for pblanc_id, profile in profiles.items() if profile["role"] == "answer"}
    answer_ids: set[str] = set()
    for index, raw_answer in enumerate(answer_rows):
        label = f"answer_set.json.answers[{index}]"
        answer = _mapping(raw_answer, label)
        _require_exact_keys(answer, ("pblanc_id", "title", "support_field"), label)
        pblanc_id = _nonempty_string(answer["pblanc_id"], f"{label}.pblanc_id")
        if pblanc_id in answer_ids:
            _fail(f"answer_set.json has duplicate pblanc_id: {pblanc_id}")
        sample_row = sample_by_id.get(pblanc_id)
        if sample_row is None or answer["title"] != sample_row["title"] or answer["support_field"] != sample_row["support_field"]:
            _fail(f"answer_set.json metadata does not match sample_100.csv: {pblanc_id}")
        answer_ids.add(pblanc_id)
    if answer_ids != expected_answers:
        _fail("answer_set.json pblanc_ids do not exactly match answer-role profiles")

    competitors = _read_csv(root / "competitor_mapping.csv", "competitor_mapping.csv", budget=budget)
    expected_competitors = {pblanc_id for pblanc_id, profile in profiles.items() if profile["role"] == "competitor"}
    competitor_ids: set[str] = set()
    for index, row in enumerate(competitors):
        label = f"competitor_mapping.csv row {index + 2}"
        answer_id = _nonempty_string(row.get("answer_pblanc_id"), f"{label}.answer_pblanc_id")
        competitor_id = _nonempty_string(row.get("competitor_pblanc_id"), f"{label}.competitor_pblanc_id")
        if answer_id not in expected_answers or competitor_id not in expected_competitors:
            _fail(f"{label} does not link answer and competitor-role sample rows")
        if competitor_id in competitor_ids:
            _fail(f"competitor_mapping.csv has duplicate competitor_pblanc_id: {competitor_id}")
        competitor_ids.add(competitor_id)
    if competitor_ids != expected_competitors:
        _fail("competitor_mapping.csv pblanc_ids do not exactly match competitor-role profiles")


def _load_jsonl(path: Path, label: str, *, budget: _GoldReadBudget | None = None) -> list[Mapping[str, Any]]:
    guard = budget or _standalone_read_budget()
    try:
        with guard.open_binary(path, label) as (binary_stream, _expected_size):
            import io

            stream = io.TextIOWrapper(binary_stream, encoding="utf-8", newline="")
            records: list[Mapping[str, Any]] = []
            try:
                for index, line in enumerate(stream, start=1):
                    if not line.strip():
                        _fail(f"{label} contains a blank line at {index}")
                    try:
                        records.append(
                            _mapping(json.loads(line, parse_constant=_reject_json_constant), f"{label}:{index}")
                        )
                    except (ValueError, json.JSONDecodeError, RecursionError) as error:
                        _fail(f"{label} line {index} is invalid JSON: {error}")
            finally:
                stream.detach()
    except (OSError, UnicodeDecodeError, RecursionError) as error:
        _fail(f"{label} is unreadable: {error}")
    if not records:
        _fail(f"{label} must not be empty")
    return records


def _verify_passed_report(
    path: Path,
    label: str,
    expected_ids: set[str],
    *,
    budget: _GoldReadBudget,
) -> None:
    report = _load_json_object(path, label, budget=budget)
    if report.get("status") != "passed":
        _fail(f"{label}.status must be 'passed'")
    if report.get("notice_count") != len(expected_ids):
        _fail(f"{label}.notice_count does not match its governed notice ids")
    rows = _list(report.get("reports"), f"{label}.reports")
    report_ids: set[str] = set()
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, f"{label}.reports[{index}]")
        notice_id = _nonempty_string(row.get("notice_id"), f"{label}.reports[{index}].notice_id")
        if row.get("status") != "passed":
            _fail(f"{label}.reports[{index}].status must be 'passed'")
        if notice_id in report_ids:
            _fail(f"{label}.reports has duplicate notice_id: {notice_id}")
        report_ids.add(notice_id)
    if report_ids != expected_ids:
        _fail(f"{label}.reports notice ids do not match its governance overlay")


def _verify_governance(
    root: Path,
    manifest: Mapping[str, Any],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    budget: _GoldReadBudget,
) -> None:
    governance = root / "governance"
    overlay = _mapping(manifest.get("retrieval_ambiguity_overlay"), "freeze_manifest.json.retrieval_ambiguity_overlay")
    _require_exact_keys(
        overlay,
        ("notice_count", "notice_ids", "decision_ledger_sha256", "semantic_regression_report_sha256"),
        "freeze_manifest.json.retrieval_ambiguity_overlay",
    )
    overlay_ids = set(_require_string_list(overlay["notice_ids"], "freeze_manifest.json.retrieval_ambiguity_overlay.notice_ids"))
    if len(overlay_ids) != len(overlay["notice_ids"]) or overlay.get("notice_count") != len(overlay_ids):
        _fail("freeze_manifest.json retrieval ambiguity overlay has invalid notice-count or duplicate ids")
    if not overlay_ids.issubset(profiles):
        _fail("freeze_manifest.json retrieval ambiguity overlay names a notice absent from profiles")
    ledger_path = governance / "decisions.jsonl"
    semantic_path = governance / "semantic_regression_report.json"
    if _digest(ledger_path, "governance/decisions.jsonl", budget=budget) != _sha256_value(overlay["decision_ledger_sha256"], "retrieval ambiguity decision_ledger_sha256"):
        _fail("retrieval ambiguity decision ledger SHA-256 does not match its governed artifact")
    if _digest(semantic_path, "governance/semantic_regression_report.json", budget=budget) != _sha256_value(overlay["semantic_regression_report_sha256"], "retrieval ambiguity semantic_regression_report_sha256"):
        _fail("retrieval ambiguity semantic report SHA-256 does not match its governed artifact")
    ledger_ids = {
        _nonempty_string(record.get("notice_id"), "governance/decisions.jsonl notice_id")
        for record in _load_jsonl(ledger_path, "governance/decisions.jsonl", budget=budget)
    }
    if ledger_ids != overlay_ids:
        _fail("governance/decisions.jsonl notice ids do not match retrieval ambiguity overlay")
    _verify_passed_report(
        governance / "materialization_report.json",
        "governance/materialization_report.json",
        overlay_ids,
        budget=budget,
    )
    _verify_passed_report(
        semantic_path,
        "governance/semantic_regression_report.json",
        overlay_ids,
        budget=budget,
    )

    v4_ledger = _load_jsonl(
        governance / "v4_decisions.jsonl", "governance/v4_decisions.jsonl", budget=budget
    )
    v4_ids = {
        _nonempty_string(record.get("notice_id"), "governance/v4_decisions.jsonl notice_id")
        for record in v4_ledger
    }
    if not v4_ids.issubset(profiles):
        _fail("governance/v4_decisions.jsonl names a notice absent from profiles")
    _verify_passed_report(
        governance / "v4_materialization_report.json",
        "governance/v4_materialization_report.json",
        v4_ids,
        budget=budget,
    )
    _verify_passed_report(
        governance / "v4_semantic_regression_report.json",
        "governance/v4_semantic_regression_report.json",
        v4_ids,
        budget=budget,
    )
    v4_manifest = _load_json_object(
        governance / "v4_v3_manifest.json", "governance/v4_v3_manifest.json", budget=budget
    )
    for key in (
        "all_contract_valid",
        "all_exact_span_valid",
        "all_common_ir_provenance_valid",
        "all_native_occurrence_valid",
        "all_component_relation_valid",
        "all_placeholder_and_duplicate_valid",
    ):
        if v4_manifest.get(key) is not True:
            _fail(f"governance/v4_v3_manifest.json.{key} must be true")


def _verify_gold_root(
    gold_root: str | Path,
    *,
    expected_notice_count: int = DEFAULT_EXPECTED_NOTICE_COUNT,
) -> VerificationReport:
    """Verify a Gold freeze without changing it or reaching any runtime service."""

    if not _is_int(expected_notice_count) or expected_notice_count <= 0:
        raise ValueError("expected_notice_count must be a positive integer")
    root = Path(gold_root).expanduser().resolve()
    if not root.is_dir():
        _fail(f"--gold-root is not a directory: {root}")
    budget = _GoldReadBudget(
        max_file_bytes=MAX_GOLD_FILE_BYTES,
        max_total_unique_file_bytes=MAX_GOLD_TOTAL_UNIQUE_FILE_BYTES,
    )
    manifest = _verify_freeze_manifest(root, expected_notice_count, budget=budget)
    artifact_index = _verify_artifact_index(root, budget=budget)
    profiles = _verify_profile_manifest(
        root,
        manifest,
        artifact_index,
        expected_notice_count,
        budget=budget,
    )
    _verify_sample_and_answer_set(root, profiles, expected_notice_count, budget=budget)
    total_facts = 0
    fact_status_counts: Counter[str] = Counter()
    for pblanc_id, profile_manifest_row in profiles.items():
        frozen = _mapping(profile_manifest_row["frozen"], f"profile_manifest[{pblanc_id}].frozen")
        fact_count, statuses = _verify_profile_document(
            _relative_target(
                root,
                _mapping(frozen["profile"], "profile frozen profile")["path"],
                "profile path",
                budget=budget,
            ),
            _relative_target(
                root,
                _mapping(frozen["selection"], "profile frozen selection")["path"],
                "selection path",
                budget=budget,
            ),
            _relative_target(
                root,
                _mapping(frozen["common_ir"], "profile frozen Common IR")["path"],
                "Common IR path",
                budget=budget,
            ),
            pblanc_id=pblanc_id,
            budget=budget,
        )
        total_facts += fact_count
        fact_status_counts.update(statuses)
    if total_facts != manifest["fact_count"]:
        _fail("freeze_manifest.json.fact_count does not match all profile facts")
    if dict(sorted(fact_status_counts.items())) != dict(manifest["fact_status_counts"]):
        _fail("freeze_manifest.json.fact_status_counts does not match all profile facts")
    _verify_governance(root, manifest, profiles, budget=budget)
    return VerificationReport(
        status="valid",
        gold_root=str(root),
        dataset_version=str(manifest["dataset_version"]),
        notice_count=expected_notice_count,
        artifact_count=len(artifact_index),
        fact_count=total_facts,
    )


def verify_gold_root(
    gold_root: str | Path,
    *,
    expected_notice_count: int = DEFAULT_EXPECTED_NOTICE_COUNT,
) -> VerificationReport:
    """Verify a Gold freeze without changing it or reaching any runtime service.

    JSON's decoder and deeply nested semantic objects can raise RecursionError.
    A malformed external corpus is a validation failure, never an uncaught CLI
    traceback.
    """

    try:
        return _verify_gold_root(gold_root, expected_notice_count=expected_notice_count)
    except RecursionError as error:
        _fail(f"Gold corpus exceeds the supported JSON nesting depth: {error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only offline verification of an Existing Profile Gold freeze.")
    parser.add_argument("--gold-root", required=True, help="Explicit path to the external, frozen Gold corpus.")
    parser.add_argument(
        "--expected-notice-count",
        type=int,
        default=DEFAULT_EXPECTED_NOTICE_COUNT,
        help=f"Expected number of frozen notices (default: {DEFAULT_EXPECTED_NOTICE_COUNT}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = verify_gold_root(args.gold_root, expected_notice_count=args.expected_notice_count)
    except (GoldVerificationError, OSError, ValueError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
