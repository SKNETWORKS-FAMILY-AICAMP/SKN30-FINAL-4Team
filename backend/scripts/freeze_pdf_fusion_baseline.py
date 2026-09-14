#!/usr/bin/env python3
"""Freeze or verify a deterministic PDF-fusion corpus baseline.

The command only reads the corpus.  It emits a canonical JSON inventory whose
entries are bound to source and optional pipeline-artifact SHA-256 values.  It
is deliberately stricter than an importer: ambiguity in a source path or an
inventory is a baseline failure, not a best-effort selection.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Iterable


SCHEMA_VERSION = "pdf_fusion_corpus_baseline/v1"
INVENTORY_SCHEMA_VERSION = "pdf_fusion_corpus_inventory/v1"
ALLOWED_EXTENSIONS = frozenset({"pdf", "hwp", "hwpx"})
LOGICAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_ARTIFACT_PATHS = {
    "structured_profile": "pipeline/structured_profile.v0.2.json",
    "candidate_pack": "pipeline/source_selection.json",
}
REQUIRED_ARTIFACTS = frozenset(DEFAULT_ARTIFACT_PATHS) | {"common_ir"}
_INVENTORY_KEYS = frozenset({"schema_version", "corpus_id", "expected", "runs"})
_INVENTORY_REQUIRED_KEYS = frozenset({"schema_version", "runs"})
_INVENTORY_RUN_KEYS = frozenset({"logical_id", "source_path", "artifacts"})
_INVENTORY_RUN_REQUIRED_KEYS = frozenset({"logical_id", "source_path"})
_EXPECTED_KEYS = frozenset({"all_runs", "pdf_runs"})


class BaselineError(ValueError):
    """A corpus cannot safely become a reproducible baseline."""


@dataclass(frozen=True, slots=True)
class CorpusRun:
    logical_id: str
    source_path: str
    artifact_paths: dict[str, str]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BaselineError(message)


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise BaselineError("baseline is not canonically serializable JSON") from error


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _sha256_file(path: Path) -> str:
    digest = sha256()
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None  # fdopen now owns cleanup.
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), f"artifact must be a regular file: {path.name}")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise BaselineError(f"cannot read artifact {path.name}: {error}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    _require(_stat_identity(before) == _stat_identity(after), f"file changed while hashing: {path.name}")
    return digest.hexdigest()


def _json_object_from_bytes(raw: bytes, *, name: str) -> dict[str, Any]:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BaselineError(f"duplicate JSON key {key!r} in {name}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise BaselineError(f"non-finite JSON number {value!r} in {name}")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BaselineError(f"cannot read JSON {name}: {error}") from error
    _require(isinstance(value, dict), f"JSON root must be an object: {name}")
    return value


def _json_object(path: Path) -> dict[str, Any]:
    descriptor: int | None = None

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None  # fdopen now owns cleanup.
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), f"JSON must be a regular file: {path.name}")
            raw = stream.read()
            after = os.fstat(stream.fileno())
        _require(_stat_identity(before) == _stat_identity(after), f"JSON changed while reading: {path.name}")
    except OSError as error:
        raise BaselineError(f"cannot read JSON {path.name}: {error}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return _json_object_from_bytes(raw, name=path.name)


def _safe_relative(value: object, *, field: str) -> str:
    _require(isinstance(value, str) and value.strip(), f"{field} must be a non-empty relative path")
    _require("\\" not in value and "\x00" not in value, f"unsafe {field}")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and path.parts, f"{field} must be relative")
    _require(all(part not in {"", ".", ".."} for part in path.parts), f"unsafe {field}")
    return path.as_posix()


def _assert_safe_root(root: Path) -> Path:
    supplied = root.expanduser()
    _require(not supplied.is_symlink(), "corpus root must not be a symlink")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise BaselineError(f"cannot resolve corpus root: {error}") from error
    _require(resolved.is_dir(), "corpus root must be a directory")
    return resolved


def _assert_tree_has_no_symlinks(root: Path) -> None:
    """Reject links anywhere, including unused attachments and sidecars.

    A baseline is an assertion about one closed corpus tree.  Permitting an
    apparently unrelated link would make a later producer able to consume bytes
    outside that assertion.
    """

    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in [*directory_names, *file_names]:
            candidate = current / name
            if stat.S_ISLNK(candidate.lstat().st_mode):
                raise BaselineError(f"symlink is not allowed in corpus tree: {candidate.relative_to(root).as_posix()}")


def _resolve_corpus_file(root: Path, relative: str, *, field: str) -> Path:
    safe = _safe_relative(relative, field=field)
    current = root
    for part in PurePosixPath(safe).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise BaselineError(f"missing {field}: {safe}") from None
        _require(not stat.S_ISLNK(mode), f"symlink is not allowed in {field}: {safe}")
    _require(current.is_file(), f"{field} must be a regular file: {safe}")
    return current


def _safe_logical_id(value: object) -> str:
    _require(isinstance(value, str) and LOGICAL_ID_PATTERN.fullmatch(value) is not None, "invalid logical_id")
    return value


def _artifact_counts(name: str, path: Path) -> dict[str, int]:
    value = _json_object(path)
    if name == "common_ir":
        blocks = value.get("blocks")
        relations = value.get("relations")
        _require(isinstance(blocks, list) and isinstance(relations, list), "common_ir must contain blocks and relations arrays")
        return {"blocks": len(blocks), "relations": len(relations)}
    if name == "structured_profile":
        components = value.get("support_components")
        comparison = value.get("comparison_profile")
        _require(isinstance(components, list) and isinstance(comparison, dict), "structured_profile has unexpected shape")
        fact_count = sum(len(item) for item in comparison.values() if isinstance(item, list))
        return {"comparison_facts": fact_count, "support_components": len(components)}
    if name == "candidate_pack":
        blocks = value.get("source_block_texts")
        _require(isinstance(blocks, dict), "candidate_pack must contain source_block_texts object")
        return {"source_blocks": len(blocks)}
    raise AssertionError(f"unknown artifact kind: {name}")


def _record_artifact(root: Path, name: str, relative: str) -> dict[str, object]:
    path = _resolve_corpus_file(root, relative, field=f"{name}_path")
    before = path.stat(follow_symlinks=False)
    artifact_sha256 = _sha256_file(path)
    counts = _artifact_counts(name, path)
    after = path.stat(follow_symlinks=False)
    _require(_stat_identity(before) == _stat_identity(after), f"artifact changed while scanning: {relative}")
    return {
        "path": relative,
        "sha256": artifact_sha256,
        "counts": counts,
    }


def _auto_artifact_paths(root: Path, logical_id: str) -> dict[str, str]:
    run_root = root / logical_id
    paths: dict[str, str] = {}
    for name, suffix in DEFAULT_ARTIFACT_PATHS.items():
        candidate = run_root / suffix
        if candidate.exists():
            paths[name] = f"{logical_id}/{suffix}"
    common_root = run_root / "pipeline" / "common_ir_v1"
    if common_root.exists():
        _require(common_root.is_dir() and not common_root.is_symlink(), f"invalid common_ir directory for {logical_id}")
        candidates = sorted(path for path in common_root.iterdir() if path.suffix == ".json")
        _require(len(candidates) == 1, f"{logical_id}: common_ir directory must contain exactly one JSON file")
        _require(not candidates[0].is_symlink(), f"{logical_id}: common_ir must not be a symlink")
        paths["common_ir"] = candidates[0].relative_to(root).as_posix()
    return paths


def _discover_runs(root: Path) -> list[CorpusRun]:
    children = sorted(root.iterdir(), key=lambda path: path.name)
    _require(bool(children), "corpus root is empty")
    runs: list[CorpusRun] = []
    for child in children:
        _require(not child.is_symlink(), f"symlink is not allowed at corpus root: {child.name}")
        _require(child.is_dir(), f"corpus root must contain run directories only: {child.name}")
        logical_id = _safe_logical_id(child.name)
        attachments = child / "attachments"
        _require(attachments.is_dir() and not attachments.is_symlink(), f"{logical_id}: attachments directory is required")
        source_files = sorted(
            (
                path
                for path in attachments.rglob("*")
                if path.is_file() and path.suffix.lower().lstrip(".") in ALLOWED_EXTENSIONS
            ),
            key=lambda path: path.as_posix(),
        )
        _require(len(source_files) == 1, f"{logical_id}: exactly one HWP/HWPX/PDF source is required")
        source = source_files[0]
        # rglob follows no symlinks, but explicitly inspect the selected path and parents.
        source_relative = source.relative_to(root).as_posix()
        _resolve_corpus_file(root, source_relative, field="source_path")
        runs.append(CorpusRun(logical_id, source_relative, _auto_artifact_paths(root, logical_id)))
    return runs


def _load_inventory(root: Path, inventory_path: Path) -> tuple[list[CorpusRun], dict[str, int] | None, str | None]:
    supplied = inventory_path.expanduser()
    _require(not supplied.is_symlink(), "inventory must not be a symlink")
    try:
        resolved_inventory = supplied.resolve(strict=True)
    except OSError as error:
        raise BaselineError(f"cannot resolve inventory: {error}") from error
    inventory = _json_object(resolved_inventory)
    inventory_keys = set(inventory)
    _require(
        _INVENTORY_REQUIRED_KEYS.issubset(inventory_keys)
        and inventory_keys.issubset(_INVENTORY_KEYS),
        "inventory keys are invalid",
    )
    _require(inventory.get("schema_version") == INVENTORY_SCHEMA_VERSION, "unsupported inventory schema_version")
    corpus_id = inventory.get("corpus_id")
    _require(corpus_id is None or (isinstance(corpus_id, str) and corpus_id.strip()), "invalid corpus_id")
    raw_runs = inventory.get("runs")
    _require(isinstance(raw_runs, list) and raw_runs, "inventory runs must be a non-empty array")
    expected = inventory.get("expected")
    expected_counts: dict[str, int] | None = None
    if expected is not None:
        _require(isinstance(expected, dict), "inventory expected must be an object")
        _require(set(expected).issubset(_EXPECTED_KEYS), "inventory expected keys are invalid")
        expected_counts = {}
        for key in ("all_runs", "pdf_runs"):
            if key in expected:
                value = expected[key]
                _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0, f"invalid expected.{key}")
                expected_counts[key] = value

    runs: list[CorpusRun] = []
    for raw in raw_runs:
        _require(isinstance(raw, dict), "inventory run must be an object")
        raw_keys = set(raw)
        _require(
            _INVENTORY_RUN_REQUIRED_KEYS.issubset(raw_keys)
            and raw_keys.issubset(_INVENTORY_RUN_KEYS),
            "inventory run keys are invalid",
        )
        logical_id = _safe_logical_id(raw.get("logical_id"))
        source_path = _safe_relative(raw.get("source_path"), field=f"{logical_id}.source_path")
        artifacts = raw.get("artifacts", {})
        _require(isinstance(artifacts, dict), f"{logical_id}: artifacts must be an object")
        _require(set(artifacts).issubset({"common_ir", "structured_profile", "candidate_pack"}), f"{logical_id}: unsupported artifact name")
        artifact_paths = {
            name: _safe_relative(path, field=f"{logical_id}.{name}_path")
            for name, path in artifacts.items()
        }
        runs.append(CorpusRun(logical_id, source_path, artifact_paths))
    return runs, expected_counts, corpus_id.strip() if isinstance(corpus_id, str) else None


def _validate_and_materialize(root: Path, runs: Iterable[CorpusRun]) -> list[dict[str, object]]:
    ordered_runs = sorted(runs, key=lambda item: item.logical_id)
    # Diagnose duplicate logical IDs before checking their artifact layout so a
    # malformed duplicate cannot hide the more fundamental inventory conflict.
    declared_ids: dict[str, str] = {}
    for run in ordered_runs:
        source = _resolve_corpus_file(root, run.source_path, field=f"{run.logical_id}.source_path")
        source_hash = _sha256_file(source)
        prior = declared_ids.get(run.logical_id)
        if prior is not None:
            if prior != source_hash:
                raise BaselineError(f"duplicate logical_id with different source SHA-256: {run.logical_id}")
            raise BaselineError(f"duplicate logical_id: {run.logical_id}")
        declared_ids[run.logical_id] = source_hash
    seen_logical_ids: dict[str, str] = {}
    seen_source_paths: dict[str, str] = {}
    seen_source_hashes: dict[str, str] = {}
    seen_artifact_paths: set[str] = set()
    output: list[dict[str, object]] = []
    for run in ordered_runs:
        missing_artifacts = sorted(REQUIRED_ARTIFACTS - set(run.artifact_paths))
        _require(
            not missing_artifacts,
            f"{run.logical_id}: required artifacts are missing: {', '.join(missing_artifacts)}",
        )
        source = _resolve_corpus_file(root, run.source_path, field=f"{run.logical_id}.source_path")
        extension = source.suffix.lower().lstrip(".")
        _require(extension in ALLOWED_EXTENSIONS, f"{run.logical_id}: unsupported source extension")
        source_hash = _sha256_file(source)
        prior = seen_logical_ids.get(run.logical_id)
        if prior is not None:
            if prior != source_hash:
                raise BaselineError(f"duplicate logical_id with different source SHA-256: {run.logical_id}")
            raise BaselineError(f"duplicate logical_id: {run.logical_id}")
        seen_logical_ids[run.logical_id] = source_hash
        prior_owner = seen_source_paths.setdefault(run.source_path, run.logical_id)
        _require(prior_owner == run.logical_id, f"duplicate source path across logical_ids: {run.source_path}")
        prior_owner = seen_source_hashes.setdefault(source_hash, run.logical_id)
        _require(prior_owner == run.logical_id, f"duplicate source SHA-256 across logical_ids: {run.logical_id}")
        for name, relative in run.artifact_paths.items():
            parts = PurePosixPath(relative).parts
            _require(parts and parts[0] == run.logical_id, f"{run.logical_id}: {name}_path must stay under its logical_id tree")
            _require(relative not in seen_artifact_paths, f"duplicate artifact path: {relative}")
            seen_artifact_paths.add(relative)
        artifacts = {
            name: _record_artifact(root, name, relative)
            for name, relative in sorted(run.artifact_paths.items())
        }
        output.append(
            {
                "logical_id": run.logical_id,
                "source": {
                    "path": run.source_path,
                    "extension": extension,
                    "sha256": source_hash,
                },
                "artifact_count": len(artifacts),
                "artifacts": artifacts,
            }
        )
    _require(bool(output), "corpus contains no runs")
    return output


def build_baseline(
    corpus_root: Path,
    *,
    inventory_path: Path | None = None,
    corpus_id: str | None = None,
) -> dict[str, object]:
    """Read a corpus and return the canonical baseline object without writing it."""

    root = _assert_safe_root(corpus_root)
    _assert_tree_has_no_symlinks(root)
    expected_counts: dict[str, int] | None = None
    inventory_corpus_id: str | None = None
    if inventory_path is None:
        runs = _discover_runs(root)
    else:
        runs, expected_counts, inventory_corpus_id = _load_inventory(root, inventory_path)
    _require(corpus_id is None or (isinstance(corpus_id, str) and corpus_id.strip()), "invalid corpus_id")
    resolved_corpus_id = corpus_id.strip() if corpus_id else (inventory_corpus_id or root.name)
    entries = _validate_and_materialize(root, runs)
    pdf_entries = [entry for entry in entries if entry["source"]["extension"] == "pdf"]  # type: ignore[index]
    extension_counts = Counter(entry["source"]["extension"] for entry in entries)  # type: ignore[index]
    artifact_counts = Counter(
        artifact_name for entry in entries for artifact_name in entry["artifacts"]  # type: ignore[index]
    )
    if expected_counts is not None:
        _require(expected_counts.get("all_runs", len(entries)) == len(entries), "inventory expected.all_runs mismatch")
        _require(expected_counts.get("pdf_runs", len(pdf_entries)) == len(pdf_entries), "inventory expected.pdf_runs mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "corpus_id": resolved_corpus_id,
        "counts": {
            "all_runs": len(entries),
            "pdf_runs": len(pdf_entries),
            "extensions": dict(sorted(extension_counts.items())),
            "artifacts": dict(sorted(artifact_counts.items())),
        },
        "all_runs": entries,
        "pdf_subset": {
            "logical_ids_sha256": sha256(
                ("\n".join(entry["logical_id"] for entry in pdf_entries) + "\n").encode("utf-8")  # type: ignore[arg-type]
            ).hexdigest(),
            "runs": pdf_entries,
        },
    }


def _atomic_create(output: Path, payload: bytes) -> None:
    supplied = output.expanduser()
    _require(not supplied.exists() and not supplied.is_symlink(), "refusing to overwrite an existing manifest")
    parent = supplied.parent
    _require(parent.is_dir() and not parent.is_symlink(), "manifest output parent must be an existing non-symlink directory")
    try:
        descriptor = os.open(supplied, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise BaselineError("refusing to overwrite an existing manifest") from None
    except OSError as error:
        raise BaselineError(f"cannot create baseline manifest: {error.strerror or type(error).__name__}") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        try:
            supplied.unlink()
        except OSError:
            pass
        raise BaselineError(f"cannot persist baseline manifest: {error.strerror or type(error).__name__}") from None


def _check_existing(output: Path, expected: bytes) -> None:
    supplied = output.expanduser()
    _require(not supplied.is_symlink(), "manifest must not be a symlink")
    _require(supplied.is_file(), "manifest does not exist for --check")
    descriptor: int | None = None
    try:
        descriptor = os.open(supplied, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), "manifest must be a regular file")
            actual = stream.read()
            after = os.fstat(stream.fileno())
        _require(_stat_identity(before) == _stat_identity(after), "manifest changed while reading")
    except OSError as error:
        raise BaselineError(f"cannot read manifest: {error}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    # Parse the bytes just verified; do not reopen a pathname for this check.
    _require(_canonical_json(_json_object_from_bytes(actual, name=supplied.name)) == actual, "baseline manifest is not canonical JSON")
    _require(actual == expected, "baseline manifest differs from current corpus scan")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-root", required=True, type=Path, help="read-only corpus root")
    parser.add_argument("--output", required=True, type=Path, help="new baseline manifest path, or existing path with --check")
    parser.add_argument("--inventory", type=Path, help="optional explicit corpus inventory JSON")
    parser.add_argument("--corpus-id", help="stable corpus identifier (defaults to inventory value or directory name)")
    parser.add_argument("--check", action="store_true", help="rescan and compare against the existing manifest; never writes")
    args = parser.parse_args(argv)
    try:
        baseline = build_baseline(
            args.corpus_root,
            inventory_path=args.inventory,
            corpus_id=args.corpus_id,
        )
        payload = _canonical_json(baseline)
        if args.check:
            _check_existing(args.output, payload)
            status = "valid"
        else:
            _atomic_create(args.output, payload)
            status = "created"
    except BaselineError as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except OSError as error:
        print(
            json.dumps(
                {
                    "status": "invalid",
                    "error": f"filesystem operation failed: {type(error).__name__}",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"status": status, "manifest_sha256": sha256(payload).hexdigest()},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
