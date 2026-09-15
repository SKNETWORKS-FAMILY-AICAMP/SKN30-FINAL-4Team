#!/usr/bin/env python3
"""Read-only, offline verifier for an Existing Profile Gold-100 freeze.

The Gold corpus is an external, post-run oracle.  This module deliberately
does not import application code, connect to a runtime, or materialize data.
It verifies the frozen corpus itself only; it does not compare a candidate
runtime result to Gold (and therefore cannot accidentally turn Gold examples
into production inputs).
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Iterable, Mapping, Sequence


FREEZE_CONTRACT = "existing_profile_gold_100_freeze/v1"
ARTIFACT_INDEX_SCHEMA = "gold_100_artifact_index/v1"
PROFILE_MANIFEST_SCHEMA = "existing_profile_gold_100_manifest/v1"
PROFILE_SCHEMA = "existing_program_profile/v0.2"
COMMON_IR_SCHEMA = "common_ir_v1"
SELECTION_CONTRACT = "v0.2_anchor"
TEXT_BASIS = "common_ir_v1_candidate_pack"
DEFAULT_EXPECTED_NOTICE_COUNT = 100

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


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _load_json(path: Path, label: str) -> Any:
    if not path.is_file() or path.is_symlink():
        _fail(f"{label} is missing or is not a regular file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        _fail(f"{label} is not valid UTF-8 JSON: {error}")


def _load_json_object(path: Path, label: str) -> Mapping[str, Any]:
    return _mapping(_load_json(path, label), label)


def _relative_target(root: Path, relative_path: object, label: str) -> Path:
    raw = _nonempty_string(relative_path, label)
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        _fail(f"{label} must be a safe relative POSIX path")
    target = root.joinpath(*candidate.parts)
    try:
        target.resolve().relative_to(root)
    except ValueError:
        _fail(f"{label} escapes --gold-root")
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            _fail(f"{label} must not traverse a symlink")
    if not target.is_file():
        _fail(f"{label} does not name a regular file: {raw}")
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


def _read_csv(path: Path, label: str) -> list[dict[str, str]]:
    if not path.is_file() or path.is_symlink():
        _fail(f"{label} is missing or is not a regular file")
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                _fail(f"{label} has no header")
            return list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        _fail(f"{label} is not a readable UTF-8 CSV: {error}")


def _verify_freeze_manifest(root: Path, expected_notice_count: int) -> Mapping[str, Any]:
    manifest_path = root / "freeze_manifest.json"
    manifest = _load_json_object(manifest_path, "freeze_manifest.json")
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
        if _digest(target) != expected_digest:
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


def _verify_artifact_index(root: Path) -> dict[str, Mapping[str, Any]]:
    document = _load_json_object(root / "artifact_index.json", "artifact_index.json")
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
        target = _relative_target(root, path_text, f"{label}.path")
        expected_digest = _sha256_value(row["sha256"], f"{label}.sha256")
        if not _is_int(row["bytes"]) or row["bytes"] < 0:
            _fail(f"{label}.bytes must be a non-negative integer")
        if target.stat().st_size != row["bytes"]:
            _fail(f"artifact index byte-size mismatch: {path_text}")
        if _digest(target) != expected_digest:
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
            actual_files.add(_relative_path(root, path))
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
    target = _relative_target(root, row["path"], f"{label}.path")
    if _digest(target) != expected_digest:
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
) -> dict[str, Mapping[str, Any]]:
    document = _load_json_object(root / "profile_manifest.json", "profile_manifest.json")
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
            _manifest_artifact(root, artifact_index, frozen_entry, pblanc_id=pblanc_id, kind=kind)
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
    _require_exact_keys(selection_lineage, lineage_keys, f"{label}.selection.candidate_pack_lineage")
    _require_exact_keys(profile_lineage, lineage_keys, f"{label}.profile.processing_metadata.candidate_pack")
    for key in lineage_keys:
        if selection_lineage.get(key) != profile_lineage.get(key):
            _fail(f"{label} candidate-pack lineage differs between selection and profile: {key}")
    if selection_lineage["common_ir_document_id"] != expected["document_id"]:
        _fail(f"{label} candidate-pack lineage document_id does not match Common IR")
    if selection_lineage["common_ir_source_sha256"] != expected["source_sha256"]:
        _fail(f"{label} candidate-pack lineage source SHA-256 does not match Common IR")
    if selection_lineage["text_basis"] != TEXT_BASIS:
        _fail(f"{label} candidate-pack lineage must declare {TEXT_BASIS!r}")


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
    label: str,
) -> None:
    evidence = _list(fact.get("evidence"), f"{label}.evidence")
    if not evidence:
        _fail(f"{label}.evidence must not be empty")
    for index, raw_evidence in enumerate(evidence):
        evidence_label = f"{label}.evidence[{index}]"
        item = _mapping(raw_evidence, evidence_label)
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
        if not occurrence_ids or not set(occurrence_ids).issubset(block_occurrences[common_ir_block_id]):
            _fail(f"{evidence_label} has unknown Common IR occurrence provenance")
    for relation_name in ("modifies_fact_ids", "recipient_fact_ids", "basis_fact_ids"):
        for relation_id in _require_string_list(fact.get(relation_name, []), f"{label}.{relation_name}"):
            if relation_id not in facts:
                _fail(f"{label}.{relation_name} contains a dangling fact id: {relation_id}")


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


def _verify_profile_document(
    profile_path: Path,
    selection_path: Path,
    common_ir_path: Path,
    *,
    pblanc_id: str,
) -> tuple[int, Counter[str]]:
    label = f"notice {pblanc_id}"
    profile = _load_json_object(profile_path, f"{label} profile")
    selection = _load_json_object(selection_path, f"{label} source selection")
    common_ir = _load_json_object(common_ir_path, f"{label} Common IR")
    if profile.get("schema_version") != PROFILE_SCHEMA:
        _fail(f"{label} profile schema_version must be {PROFILE_SCHEMA!r}")
    if profile.get("notice_id") != f"bizinfo:{pblanc_id}":
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
    used_spans: set[tuple[str, int, int]] = set()
    common_ir_document_id = str(document["document_id"])
    for fact_id, fact in facts.items():
        fact_label = f"{label} fact {fact_id}"
        _validate_value_source(fact, source_block_texts, used_spans, fact_label)
        _validate_evidence(facts, fact, source_block_texts, common_ir_document_id, block_occurrences, fact_label)
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


def _verify_sample_and_answer_set(
    root: Path,
    profiles: Mapping[str, Mapping[str, Any]],
    expected_notice_count: int,
) -> None:
    sample = _read_csv(root / "sample_100.csv", "sample_100.csv")
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

    answers = _load_json_object(root / "answer_set.json", "answer_set.json")
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

    competitors = _read_csv(root / "competitor_mapping.csv", "competitor_mapping.csv")
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


def _load_jsonl(path: Path, label: str) -> list[Mapping[str, Any]]:
    if not path.is_file() or path.is_symlink():
        _fail(f"{label} is missing or is not a regular file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        _fail(f"{label} is unreadable: {error}")
    if not lines:
        _fail(f"{label} must not be empty")
    records: list[Mapping[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            _fail(f"{label} contains a blank line at {index}")
        try:
            records.append(_mapping(json.loads(line, parse_constant=_reject_json_constant), f"{label}:{index}"))
        except (ValueError, json.JSONDecodeError) as error:
            _fail(f"{label} line {index} is invalid JSON: {error}")
    return records


def _verify_passed_report(path: Path, label: str, expected_ids: set[str]) -> None:
    report = _load_json_object(path, label)
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


def _verify_governance(root: Path, manifest: Mapping[str, Any], profiles: Mapping[str, Mapping[str, Any]]) -> None:
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
    if _digest(ledger_path) != _sha256_value(overlay["decision_ledger_sha256"], "retrieval ambiguity decision_ledger_sha256"):
        _fail("retrieval ambiguity decision ledger SHA-256 does not match its governed artifact")
    if _digest(semantic_path) != _sha256_value(overlay["semantic_regression_report_sha256"], "retrieval ambiguity semantic_regression_report_sha256"):
        _fail("retrieval ambiguity semantic report SHA-256 does not match its governed artifact")
    ledger_ids = {
        _nonempty_string(record.get("notice_id"), "governance/decisions.jsonl notice_id")
        for record in _load_jsonl(ledger_path, "governance/decisions.jsonl")
    }
    if ledger_ids != overlay_ids:
        _fail("governance/decisions.jsonl notice ids do not match retrieval ambiguity overlay")
    _verify_passed_report(governance / "materialization_report.json", "governance/materialization_report.json", overlay_ids)
    _verify_passed_report(semantic_path, "governance/semantic_regression_report.json", overlay_ids)

    v4_ledger = _load_jsonl(governance / "v4_decisions.jsonl", "governance/v4_decisions.jsonl")
    v4_ids = {
        _nonempty_string(record.get("notice_id"), "governance/v4_decisions.jsonl notice_id")
        for record in v4_ledger
    }
    if not v4_ids.issubset(profiles):
        _fail("governance/v4_decisions.jsonl names a notice absent from profiles")
    _verify_passed_report(governance / "v4_materialization_report.json", "governance/v4_materialization_report.json", v4_ids)
    _verify_passed_report(governance / "v4_semantic_regression_report.json", "governance/v4_semantic_regression_report.json", v4_ids)
    v4_manifest = _load_json_object(governance / "v4_v3_manifest.json", "governance/v4_v3_manifest.json")
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


def verify_gold_root(gold_root: str | Path, *, expected_notice_count: int = DEFAULT_EXPECTED_NOTICE_COUNT) -> VerificationReport:
    """Verify a Gold freeze without changing it or reaching any runtime service."""

    if not _is_int(expected_notice_count) or expected_notice_count <= 0:
        raise ValueError("expected_notice_count must be a positive integer")
    root = Path(gold_root).expanduser().resolve()
    if not root.is_dir():
        _fail(f"--gold-root is not a directory: {root}")
    manifest = _verify_freeze_manifest(root, expected_notice_count)
    artifact_index = _verify_artifact_index(root)
    profiles = _verify_profile_manifest(root, manifest, artifact_index, expected_notice_count)
    _verify_sample_and_answer_set(root, profiles, expected_notice_count)
    total_facts = 0
    fact_status_counts: Counter[str] = Counter()
    for pblanc_id, profile_manifest_row in profiles.items():
        frozen = _mapping(profile_manifest_row["frozen"], f"profile_manifest[{pblanc_id}].frozen")
        fact_count, statuses = _verify_profile_document(
            _relative_target(root, _mapping(frozen["profile"], "profile frozen profile")["path"], "profile path"),
            _relative_target(root, _mapping(frozen["selection"], "profile frozen selection")["path"], "selection path"),
            _relative_target(root, _mapping(frozen["common_ir"], "profile frozen Common IR")["path"], "Common IR path"),
            pblanc_id=pblanc_id,
        )
        total_facts += fact_count
        fact_status_counts.update(statuses)
    if total_facts != manifest["fact_count"]:
        _fail("freeze_manifest.json.fact_count does not match all profile facts")
    if dict(sorted(fact_status_counts.items())) != dict(manifest["fact_status_counts"]):
        _fail("freeze_manifest.json.fact_status_counts does not match all profile facts")
    _verify_governance(root, manifest, profiles)
    return VerificationReport(
        status="valid",
        gold_root=str(root),
        dataset_version=str(manifest["dataset_version"]),
        notice_count=expected_notice_count,
        artifact_count=len(artifact_index),
        fact_count=total_facts,
    )


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
