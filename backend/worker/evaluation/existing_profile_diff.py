"""Strict offline comparison for automatic and reviewed Existing Profiles.

The automatic 100-notice archive is a baseline, not an oracle.  The frozen
v5 directory is the human-reviewed and corrected oracle.  This module keeps
that direction explicit and deliberately performs only two normalizations:

* JSON object key order is canonicalized.
* the *root* ``notice_id`` may be either ``PBLN_*`` or ``bizinfo:PBLN_*``.

Array order and every other value remain significant.  There is no fuzzy
matching, generated-text comparison, production application startup, network
access, database access, or object-storage access in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Any, Mapping, Protocol, Sequence
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo


COMPARISON_SCHEMA_VERSION = "existing_profile_baseline_gold_comparison/v1"
PROFILE_MEMBER_NAME = "pipeline/structured_profile.v0.2.json"
GOLD_PROFILE_NAME = "existing_profile.v0.2.json"
REPORT_FILE_NAME = "existing-profile-comparison.v1.json"

NOTICE_ID_PATTERN = re.compile(r"PBLN_[0-9]{15}")
DEFAULT_EXPECTED_PROFILE_COUNT = 100
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_PROFILE_COUNT = 1_000
MAX_PROFILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_PROFILE_BYTES = 128 * 1024 * 1024
SUPPORTED_ZIP_COMPRESSION = frozenset({ZIP_STORED, ZIP_DEFLATED})


class ExistingProfileComparisonError(ValueError):
    """An input cannot be compared without weakening the strict contract."""


class ExistingProfileExpectationError(ExistingProfileComparisonError):
    """The comparison result differs from an explicitly asserted baseline."""


@dataclass(frozen=True, slots=True)
class LoadedProfiles:
    """Profiles plus a non-sensitive identity for the corpus that supplied them."""

    profiles: Mapping[str, Mapping[str, Any]]
    identity: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _JsonNumber:
    """A lossless JSON number token used only by the strict comparator."""

    lexeme: str


@dataclass(frozen=True, slots=True)
class _JsonSnapshot:
    value: Mapping[str, Any]
    digest: str
    identity: tuple[int, int, int, int, int]
    size: int


class _GoldVerificationReport(Protocol):
    status: str
    gold_root: str
    dataset_version: str
    notice_count: int


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExistingProfileComparisonError(message)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _canonical_json(value: object) -> bytes:
    def encode(item: object) -> str:
        if isinstance(item, _JsonNumber):
            return item.lexeme
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, allow_nan=False)
        if isinstance(item, int):
            return str(item)
        if isinstance(item, float):
            return json.dumps(item, ensure_ascii=False, allow_nan=False)
        if isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                raise TypeError("JSON object keys must be strings")
            return "{" + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{encode(item[key])}"
                for key in sorted(item)
            ) + "}"
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(element) for element in item) + "]"
        raise TypeError(f"unsupported JSON value type: {type(item).__name__}")

    try:
        return (encode(value) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ExistingProfileComparisonError(
            "profile is not canonically serializable JSON"
        ) from error


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ExistingProfileComparisonError(
                    f"duplicate JSON key {key!r} in {label}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ExistingProfileComparisonError(
            f"non-finite JSON number {value!r} in {label}"
        )

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicate_keys,
            parse_constant=reject_constant,
            parse_float=_JsonNumber,
            parse_int=_JsonNumber,
        )
    except ExistingProfileComparisonError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ExistingProfileComparisonError(
            f"cannot read UTF-8 JSON {label}: {error}"
        ) from error
    _require(isinstance(value, dict), f"JSON root must be an object: {label}")
    return value


def _safe_notice_id(value: object, *, label: str) -> str:
    _require(
        isinstance(value, str) and NOTICE_ID_PATTERN.fullmatch(value) is not None,
        f"invalid notice id in {label}",
    )
    return value


def _nonnegative_json_integer(value: object, *, label: str) -> int:
    if isinstance(value, _JsonNumber) and re.fullmatch(r"0|[1-9][0-9]*", value.lexeme):
        return int(value.lexeme)
    raise ExistingProfileComparisonError(f"{label} must be a non-negative JSON integer")


def _normalized_profile(
    profile: Mapping[str, Any],
    *,
    logical_notice_id: str,
    label: str,
) -> dict[str, Any]:
    """Return a shallow copy with only the root notice namespace normalized."""

    notice_id = profile.get("notice_id")
    accepted = {logical_notice_id, f"bizinfo:{logical_notice_id}"}
    _require(
        isinstance(notice_id, str) and notice_id in accepted,
        f"root notice_id does not match {logical_notice_id} in {label}",
    )
    normalized = dict(profile)
    normalized["notice_id"] = f"bizinfo:{logical_notice_id}"
    return normalized


def _sha256_open_stream(stream: Any) -> str:
    """Hash a seekable binary stream and restore it to the first byte."""

    digest = sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _safe_zip_member(info: ZipInfo) -> str:
    name = info.filename
    _require(name != "" and "\x00" not in name and "\\" not in name, "unsafe ZIP member name")
    _require(not name.startswith("/"), f"absolute ZIP member is forbidden: {name!r}")
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    _require(
        bool(trimmed) and all(part not in {"", ".", ".."} for part in parts),
        f"unsafe ZIP member path: {name!r}",
    )
    _require(PurePosixPath(trimmed).as_posix() == trimmed, f"non-canonical ZIP member path: {name!r}")
    _require(not (info.flag_bits & 0x1), f"encrypted ZIP member is forbidden: {name!r}")
    _require(
        info.compress_type in SUPPORTED_ZIP_COMPRESSION,
        f"unsupported ZIP compression for member: {name!r}",
    )

    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    _require(not stat.S_ISLNK(mode), f"symlink ZIP member is forbidden: {name!r}")
    if file_type:
        expected = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
        _require(file_type == expected, f"special ZIP member is forbidden: {name!r}")
    return trimmed


def _read_zip_profile(archive: ZipFile, info: ZipInfo) -> bytes:
    _require(
        0 <= info.file_size <= MAX_PROFILE_BYTES,
        f"profile ZIP member exceeds {MAX_PROFILE_BYTES} bytes: {info.filename!r}",
    )
    try:
        with archive.open(info, "r") as stream:
            raw = stream.read(MAX_PROFILE_BYTES + 1)
    except (BadZipFile, OSError, RuntimeError) as error:
        raise ExistingProfileComparisonError(
            f"cannot read profile ZIP member {info.filename!r}: {error}"
        ) from error
    _require(len(raw) <= MAX_PROFILE_BYTES, f"profile ZIP member is too large: {info.filename!r}")
    _require(len(raw) == info.file_size, f"profile ZIP member size mismatch: {info.filename!r}")
    return raw


def load_automatic_baseline(
    archive_path: Path,
    *,
    expected_profile_count: int = DEFAULT_EXPECTED_PROFILE_COUNT,
) -> LoadedProfiles:
    """Read Existing Profiles directly from an archive without extracting it."""

    supplied = archive_path.expanduser()
    _require(not supplied.is_symlink(), "baseline ZIP must not be a symlink")
    _require(
        isinstance(expected_profile_count, int)
        and not isinstance(expected_profile_count, bool)
        and 0 < expected_profile_count <= MAX_PROFILE_COUNT,
        f"expected profile count must be between 1 and {MAX_PROFILE_COUNT}",
    )
    profiles: dict[str, Mapping[str, Any]] = {}
    descriptor: int | None = None
    try:
        descriptor = os.open(
            supplied,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), "baseline ZIP must be a regular file")
            _require(
                0 < before.st_size <= MAX_ARCHIVE_BYTES,
                f"baseline ZIP exceeds the {MAX_ARCHIVE_BYTES}-byte physical size cap",
            )
            archive_sha256 = _sha256_open_stream(stream)
            try:
                with ZipFile(stream) as archive:
                    members = archive.infolist()
                    _require(
                        len(members) <= MAX_ARCHIVE_MEMBERS,
                        f"baseline ZIP has more than {MAX_ARCHIVE_MEMBERS} members",
                    )
                    seen_names: set[str] = set()
                    seen_header_offsets: set[int] = set()
                    profile_members: list[tuple[str, ZipInfo]] = []
                    for info in members:
                        member = _safe_zip_member(info)
                        _require(member not in seen_names, f"duplicate ZIP member: {member!r}")
                        seen_names.add(member)
                        _require(
                            isinstance(info.header_offset, int)
                            and 0 <= info.header_offset < before.st_size,
                            f"invalid ZIP local-header offset: {member!r}",
                        )
                        _require(
                            info.header_offset not in seen_header_offsets,
                            f"duplicate ZIP local-header offset: {info.header_offset}",
                        )
                        seen_header_offsets.add(info.header_offset)
                        if info.is_dir():
                            continue
                        parts = PurePosixPath(member).parts
                        if len(parts) != 3 or "/".join(parts[1:]) != PROFILE_MEMBER_NAME:
                            continue
                        notice_id = _safe_notice_id(parts[0], label=member)
                        _require(
                            all(notice_id != existing_id for existing_id, _ in profile_members),
                            f"duplicate baseline profile for {notice_id}",
                        )
                        profile_members.append((notice_id, info))

                    _require(
                        len(profile_members) == expected_profile_count,
                        "baseline profile count does not match the explicit expected count: "
                        f"expected {expected_profile_count}, observed {len(profile_members)}",
                    )
                    declared_total = sum(info.file_size for _, info in profile_members)
                    _require(
                        declared_total <= MAX_TOTAL_PROFILE_BYTES,
                        "baseline declared Profile bytes exceed the corpus cap: "
                        f"{declared_total} > {MAX_TOTAL_PROFILE_BYTES}",
                    )

                    retained_total = 0
                    for notice_id, info in profile_members:
                        raw = _read_zip_profile(archive, info)
                        retained_total += len(raw)
                        _require(
                            retained_total <= MAX_TOTAL_PROFILE_BYTES,
                            "baseline retained Profile bytes exceed the corpus cap: "
                            f"{retained_total} > {MAX_TOTAL_PROFILE_BYTES}",
                        )
                        profiles[notice_id] = _json_object(raw, label=info.filename)
            except BadZipFile as error:
                raise ExistingProfileComparisonError(
                    f"baseline ZIP is not a valid ZIP archive: {error}"
                ) from error
            after = os.fstat(stream.fileno())
        _require(
            _stat_identity(before) == _stat_identity(after),
            "baseline ZIP changed while profiles were read",
        )
    except ExistingProfileComparisonError:
        raise
    except OSError as error:
        raise ExistingProfileComparisonError(f"cannot open baseline ZIP: {error}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    _require(bool(profiles), "baseline ZIP contains no Existing Profile artifacts")
    return LoadedProfiles(
        profiles=profiles,
        identity={
            "role": "unreviewed_automatic_baseline",
            "archive_name": supplied.name,
            "archive_sha256": archive_sha256,
            "profile_count": len(profiles),
        },
    )


def _assert_directory_tree_has_no_symlinks(root: Path) -> None:
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in [*directory_names, *file_names]:
            candidate = current / name
            try:
                mode = candidate.lstat().st_mode
            except OSError as error:
                raise ExistingProfileComparisonError(
                    f"cannot inspect Gold tree entry: {error}"
                ) from error
            _require(
                not stat.S_ISLNK(mode),
                f"symlink is forbidden in Gold tree: {candidate.relative_to(root).as_posix()}",
            )


def _read_regular_json_snapshot(path: Path, *, label: str) -> _JsonSnapshot:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), f"{label} must be a regular file")
            _require(before.st_size <= MAX_PROFILE_BYTES, f"{label} exceeds {MAX_PROFILE_BYTES} bytes")
            raw = stream.read(MAX_PROFILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        _require(len(raw) <= MAX_PROFILE_BYTES, f"{label} is too large")
        _require(_stat_identity(before) == _stat_identity(after), f"{label} changed while it was read")
    except ExistingProfileComparisonError:
        raise
    except OSError as error:
        raise ExistingProfileComparisonError(f"cannot read {label}: {error}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return _JsonSnapshot(
        value=_json_object(raw, label=label),
        digest=sha256(raw).hexdigest(),
        identity=_stat_identity(after),
        size=len(raw),
    )


def _assert_snapshot_is_current(path: Path, snapshot: _JsonSnapshot, *, label: str) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ExistingProfileComparisonError(f"cannot recheck {label}: {error}") from error
    _require(not stat.S_ISLNK(current.st_mode), f"{label} became a symlink")
    _require(
        _stat_identity(current) == snapshot.identity,
        f"{label} changed after it was read",
    )


def _verify_gold(root: Path, *, expected_profile_count: int) -> _GoldVerificationReport:
    try:
        from scripts.verify_existing_gold100 import GoldVerificationError, verify_gold_root
    except ImportError as error:
        raise ExistingProfileComparisonError(
            "Gold verifier is unavailable; run through the documented backend CLI"
        ) from error
    try:
        report = verify_gold_root(root, expected_notice_count=expected_profile_count)
    except (GoldVerificationError, ValueError, OSError) as error:
        raise ExistingProfileComparisonError(f"Gold verification failed: {error}") from error
    _require(report.status == "valid", "Gold verifier did not return valid status")
    _require(
        Path(report.gold_root).resolve() == root,
        "Gold verifier returned a different root",
    )
    _require(
        report.notice_count == expected_profile_count,
        "Gold verifier returned an unexpected notice count",
    )
    return report


def _indexed_gold_profiles(
    artifact_index: Mapping[str, Any],
) -> dict[str, tuple[str, int]]:
    rows = artifact_index.get("artifacts")
    _require(isinstance(rows, list), "Gold artifact_index artifacts must be an array")
    result: dict[str, tuple[str, int]] = {}
    for index, row in enumerate(rows):
        _require(isinstance(row, Mapping), f"Gold artifact index row {index} must be an object")
        relative = row.get("path")
        if not isinstance(relative, str):
            continue
        parts = PurePosixPath(relative).parts
        if len(parts) != 3 or parts[0] != "notices" or parts[2] != GOLD_PROFILE_NAME:
            continue
        notice_id = _safe_notice_id(parts[1], label=f"Gold artifact index row {index}")
        digest = row.get("sha256")
        size = _nonnegative_json_integer(
            row.get("bytes"),
            label=f"Gold profile index byte count for {notice_id}",
        )
        _require(
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest),
            f"Gold profile index SHA-256 is invalid for {notice_id}",
        )
        _require(notice_id not in result, f"duplicate indexed Gold profile for {notice_id}")
        result[notice_id] = (digest, size)
    return result


def load_reviewed_gold(
    gold_root: Path,
    *,
    expected_profile_count: int = DEFAULT_EXPECTED_PROFILE_COUNT,
) -> LoadedProfiles:
    """Load only the frozen Profile artifacts from a symlink-free Gold tree."""

    supplied = gold_root.expanduser()
    _require(not supplied.is_symlink(), "Gold root must not be a symlink")
    try:
        root = supplied.resolve(strict=True)
    except OSError as error:
        raise ExistingProfileComparisonError(f"cannot resolve Gold root: {error}") from error
    _require(root.is_dir(), "Gold root must be a directory")
    _assert_directory_tree_has_no_symlinks(root)
    _require(
        isinstance(expected_profile_count, int)
        and not isinstance(expected_profile_count, bool)
        and 0 < expected_profile_count <= MAX_PROFILE_COUNT,
        f"expected profile count must be between 1 and {MAX_PROFILE_COUNT}",
    )

    verification_before = _verify_gold(
        root,
        expected_profile_count=expected_profile_count,
    )

    manifest_path = root / "freeze_manifest.json"
    manifest_snapshot = _read_regular_json_snapshot(
        manifest_path,
        label="freeze_manifest.json",
    )
    manifest = manifest_snapshot.value
    _require(manifest.get("freeze_status") == "FROZEN", "Gold freeze_status must be 'FROZEN'")
    dataset_version = manifest.get("dataset_version")
    _require(
        isinstance(dataset_version, str) and bool(dataset_version.strip()),
        "Gold dataset_version must be a non-empty string",
    )
    _require(
        dataset_version == verification_before.dataset_version,
        "Gold manifest dataset_version differs from verifier result",
    )

    artifact_index_path = root / "artifact_index.json"
    artifact_index_snapshot = _read_regular_json_snapshot(
        artifact_index_path,
        label="artifact_index.json",
    )
    expected_index_digest = manifest.get("artifact_index_sha256")
    _require(
        expected_index_digest == artifact_index_snapshot.digest,
        "Gold artifact index no longer matches its freeze-manifest pin",
    )
    indexed_profiles = _indexed_gold_profiles(artifact_index_snapshot.value)

    notices_root = root / "notices"
    _require(notices_root.is_dir(), "Gold notices directory is missing")
    profiles: dict[str, Mapping[str, Any]] = {}
    profile_snapshots: dict[Path, _JsonSnapshot] = {}
    retained_total = 0
    try:
        entries = sorted(notices_root.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise ExistingProfileComparisonError(f"cannot list Gold notices: {error}") from error
    for notice_dir in entries:
        if not notice_dir.is_dir():
            continue
        notice_id = _safe_notice_id(notice_dir.name, label="Gold notice directory")
        profile_path = notice_dir / GOLD_PROFILE_NAME
        _require(profile_path.is_file(), f"Gold profile is missing for {notice_id}")
        snapshot = _read_regular_json_snapshot(
            profile_path,
            label=f"Gold profile {notice_id}",
        )
        indexed = indexed_profiles.get(notice_id)
        _require(indexed is not None, f"Gold profile is absent from artifact index: {notice_id}")
        _require(
            indexed == (snapshot.digest, snapshot.size),
            f"Gold profile bytes no longer match artifact index: {notice_id}",
        )
        retained_total += snapshot.size
        _require(
            retained_total <= MAX_TOTAL_PROFILE_BYTES,
            "Gold retained Profile bytes exceed the corpus cap: "
            f"{retained_total} > {MAX_TOTAL_PROFILE_BYTES}",
        )
        profiles[notice_id] = snapshot.value
        profile_snapshots[profile_path] = snapshot

    _require(bool(profiles), "Gold root contains no Existing Profile artifacts")
    manifest_count = _nonnegative_json_integer(
        manifest.get("notice_count"),
        label="Gold manifest notice_count",
    )
    _require(
        manifest_count == len(profiles),
        "Gold manifest notice_count does not match discovered profiles",
    )
    _require(
        set(indexed_profiles) == set(profiles),
        "Gold artifact-index Profile ids do not match discovered Profiles",
    )

    verification_after = _verify_gold(
        root,
        expected_profile_count=expected_profile_count,
    )
    _require(
        verification_after == verification_before,
        "Gold verifier result changed while profiles were loaded",
    )
    _assert_snapshot_is_current(
        manifest_path,
        manifest_snapshot,
        label="Gold freeze manifest",
    )
    _assert_snapshot_is_current(
        artifact_index_path,
        artifact_index_snapshot,
        label="Gold artifact index",
    )
    for profile_path, snapshot in profile_snapshots.items():
        _assert_snapshot_is_current(
            profile_path,
            snapshot,
            label=f"Gold profile {profile_path.parent.name}",
        )
    return LoadedProfiles(
        profiles=profiles,
        identity={
            "role": "human_reviewed_corrected_gold",
            "dataset_version": dataset_version,
            "freeze_manifest_sha256": manifest_snapshot.digest,
            "profile_count": len(profiles),
        },
    )


def _changed_top_level_fields(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    changed: list[str] = []
    for key in sorted(set(left) | set(right)):
        if key not in left or key not in right:
            changed.append(key)
        elif _canonical_json(left[key]) != _canonical_json(right[key]):
            changed.append(key)
    return changed


def compare_loaded_profiles(
    baseline: LoadedProfiles,
    gold: LoadedProfiles,
) -> dict[str, Any]:
    """Compare loaded corpora with exact canonical equality after safe normalization."""

    baseline_ids = set(baseline.profiles)
    gold_ids = set(gold.profiles)
    shared_ids = sorted(baseline_ids & gold_ids)
    baseline_only = sorted(baseline_ids - gold_ids)
    gold_only = sorted(gold_ids - baseline_ids)

    unchanged: list[str] = []
    changed: list[dict[str, Any]] = []
    for notice_id in shared_ids:
        baseline_profile = _normalized_profile(
            baseline.profiles[notice_id],
            logical_notice_id=notice_id,
            label=f"baseline profile {notice_id}",
        )
        gold_profile = _normalized_profile(
            gold.profiles[notice_id],
            logical_notice_id=notice_id,
            label=f"Gold profile {notice_id}",
        )
        baseline_bytes = _canonical_json(baseline_profile)
        gold_bytes = _canonical_json(gold_profile)
        if baseline_bytes == gold_bytes:
            unchanged.append(notice_id)
            continue
        changed.append(
            {
                "notice_id": notice_id,
                "baseline_canonical_sha256": sha256(baseline_bytes).hexdigest(),
                "gold_canonical_sha256": sha256(gold_bytes).hexdigest(),
                "changed_top_level_fields": _changed_top_level_fields(
                    baseline_profile,
                    gold_profile,
                ),
            }
        )

    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "comparison_mode": "strict_canonical_after_root_notice_id_namespace_normalization",
        "normalization": {
            "root_notice_id": "PBLN_* and bizinfo:PBLN_* are canonicalized to bizinfo:PBLN_*",
            "json_object_key_order": "lexicographically sorted",
            "array_order": "preserved",
            "all_other_values": "preserved",
            "fuzzy_matching": "forbidden",
        },
        "baseline": dict(baseline.identity),
        "gold": dict(gold.identity),
        "counts": {
            "baseline": len(baseline_ids),
            "gold": len(gold_ids),
            "shared": len(shared_ids),
            "unchanged": len(unchanged),
            "changed": len(changed),
            "baseline_only": len(baseline_only),
            "gold_only": len(gold_only),
        },
        "unchanged_notice_ids": unchanged,
        "changed": changed,
        "baseline_only_notice_ids": baseline_only,
        "gold_only_notice_ids": gold_only,
    }


def compare_existing_profile_corpora(
    baseline_archive: Path,
    gold_root: Path,
    *,
    expected_profile_count: int = DEFAULT_EXPECTED_PROFILE_COUNT,
) -> dict[str, Any]:
    return compare_loaded_profiles(
        load_automatic_baseline(
            baseline_archive,
            expected_profile_count=expected_profile_count,
        ),
        load_reviewed_gold(
            gold_root,
            expected_profile_count=expected_profile_count,
        ),
    )


def assert_expected_comparison(
    report: Mapping[str, Any],
    *,
    shared: int | None = None,
    unchanged: int | None = None,
    changed: int | None = None,
    baseline: int | None = None,
    gold: int | None = None,
    baseline_only: int | None = None,
    gold_only: int | None = None,
    changed_notice_ids: Sequence[str] | None = None,
    baseline_archive_sha256: str | None = None,
    gold_freeze_manifest_sha256: str | None = None,
) -> None:
    """Fail closed when an explicitly pinned comparison result drifts."""

    counts = report.get("counts")
    _require(isinstance(counts, Mapping), "comparison report counts are missing")
    expected_counts = {
        "baseline": baseline,
        "gold": gold,
        "shared": shared,
        "unchanged": unchanged,
        "changed": changed,
        "baseline_only": baseline_only,
        "gold_only": gold_only,
    }
    for key, expected in expected_counts.items():
        if expected is not None and counts.get(key) != expected:
            raise ExistingProfileExpectationError(
                f"expected {key}={expected}, observed {counts.get(key)!r}"
            )
    if changed_notice_ids is not None:
        expected_ids = sorted(
            _safe_notice_id(value, label="expected changed notice id")
            for value in changed_notice_ids
        )
        _require(
            len(expected_ids) == len(set(expected_ids)),
            "expected changed notice ids contain duplicates",
        )
        rows = report.get("changed")
        _require(isinstance(rows, list), "comparison report changed rows are missing")
        observed_ids = sorted(
            row.get("notice_id")
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("notice_id"), str)
        )
        if observed_ids != expected_ids:
            raise ExistingProfileExpectationError(
                f"expected changed notice ids {expected_ids!r}, observed {observed_ids!r}"
            )

    identity_pins = (
        ("baseline", "archive_sha256", baseline_archive_sha256),
        ("gold", "freeze_manifest_sha256", gold_freeze_manifest_sha256),
    )
    for corpus_key, digest_key, expected_digest in identity_pins:
        if expected_digest is None:
            continue
        _require(
            len(expected_digest) == 64
            and all(character in "0123456789abcdef" for character in expected_digest),
            f"expected {corpus_key} identity pin must be a lowercase SHA-256 digest",
        )
        identity = report.get(corpus_key)
        _require(isinstance(identity, Mapping), f"comparison report {corpus_key} identity is missing")
        observed_digest = identity.get(digest_key)
        if observed_digest != expected_digest:
            raise ExistingProfileExpectationError(
                f"expected {corpus_key} {digest_key}={expected_digest}, "
                f"observed {observed_digest!r}"
            )


def _directory_open_flags() -> int:
    _require(
        hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"),
        "this platform cannot enforce no-follow directory traversal",
    )
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _close_descriptors(descriptors: Sequence[int]) -> None:
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _open_absolute_directory_chain_no_follow(path: Path, *, label: str) -> list[int]:
    """Open and retain every component from ``/`` without following links."""

    _require(path.is_absolute() and path.anchor == os.sep, f"{label} must be an absolute POSIX path")
    flags = _directory_open_flags()
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(os.sep, flags))
        for component in path.parts[1:]:
            _require(component not in {"", ".", ".."}, f"unsafe component in {label}")
            descriptor = os.open(component, flags, dir_fd=descriptors[-1])
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(descriptor)
                raise ExistingProfileComparisonError(f"non-directory component in {label}")
            descriptors.append(descriptor)
    except ExistingProfileComparisonError:
        _close_descriptors(descriptors)
        raise
    except OSError as error:
        _close_descriptors(descriptors)
        raise ExistingProfileComparisonError(
            f"cannot open {label} without following ancestor symlinks: {error}"
        ) from error
    return descriptors


def _inode_identity(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _directory_fd_is_within(directory_descriptor: int, ancestor_descriptor: int) -> bool:
    """Walk real ``..`` links from an opened directory and compare inode identities."""

    ancestor_identity = _inode_identity(os.fstat(ancestor_descriptor))
    flags = _directory_open_flags()
    current = os.dup(directory_descriptor)
    try:
        for _ in range(4096):
            current_identity = _inode_identity(os.fstat(current))
            if current_identity == ancestor_identity:
                return True
            parent = os.open("..", flags, dir_fd=current)
            parent_identity = _inode_identity(os.fstat(parent))
            os.close(current)
            current = parent
            if parent_identity == current_identity:
                return False
    except OSError as error:
        raise ExistingProfileComparisonError(
            f"cannot verify opened output directory ancestry: {error}"
        ) from error
    finally:
        try:
            os.close(current)
        except OSError:
            pass
    raise ExistingProfileComparisonError("opened output directory ancestry is unreasonably deep")


def write_comparison_report(
    report: Mapping[str, Any],
    *,
    output_dir: Path,
    gold_root: Path,
    report_file_name: str = REPORT_FILE_NAME,
) -> Path:
    """Atomically publish one report through an opened, no-follow directory FD.

    ``report_file_name`` is exposed for sibling offline evaluators that need
    the same hardened publication boundary.  It must be one plain file name;
    existing callers retain the historical default.
    """

    _require(
        isinstance(report_file_name, str)
        and report_file_name not in {"", ".", ".."}
        and Path(report_file_name).name == report_file_name
        and "/" not in report_file_name
        and "\\" not in report_file_name,
        "report file name must be one plain file name",
    )

    supplied = output_dir.expanduser()
    lexical_output = Path(os.path.abspath(supplied))
    current = Path(lexical_output.anchor)
    for part in lexical_output.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ExistingProfileComparisonError(
                f"cannot inspect output directory boundary: {error}"
            ) from error
        _require(
            not stat.S_ISLNK(mode),
            "output directory path must not traverse a symlink",
        )
    try:
        resolved_output = lexical_output.resolve(strict=True)
        resolved_gold = gold_root.expanduser().resolve(strict=True)
    except OSError as error:
        raise ExistingProfileComparisonError(f"cannot resolve output boundary: {error}") from error
    _require(resolved_output.is_dir(), "output directory must be an existing directory")
    try:
        resolved_output.relative_to(resolved_gold)
    except ValueError:
        pass
    else:
        raise ExistingProfileComparisonError("output directory must be outside the read-only Gold root")

    expected_directory = lexical_output.stat(follow_symlinks=False)
    _require(stat.S_ISDIR(expected_directory.st_mode), "output directory must be a directory")
    expected_gold = resolved_gold.stat(follow_symlinks=False)
    _require(stat.S_ISDIR(expected_gold.st_mode), "Gold root must be a directory")

    report_path = resolved_output / report_file_name
    output_chain: list[int] = []
    gold_chain: list[int] = []
    directory_descriptor: int | None = None
    gold_descriptor: int | None = None
    temporary_descriptor: int | None = None
    temporary_name: str | None = None
    published = False
    publish_complete = False
    try:
        output_chain = _open_absolute_directory_chain_no_follow(
            lexical_output,
            label="output directory",
        )
        directory_descriptor = output_chain[-1]
        opened_directory = os.fstat(directory_descriptor)
        _require(
            stat.S_ISDIR(opened_directory.st_mode)
            and _inode_identity(opened_directory) == _inode_identity(expected_directory),
            "output directory changed while it was opened",
        )
        gold_chain = _open_absolute_directory_chain_no_follow(
            resolved_gold,
            label="Gold root",
        )
        gold_descriptor = gold_chain[-1]
        _require(
            _inode_identity(os.fstat(gold_descriptor)) == _inode_identity(expected_gold),
            "Gold root changed while it was opened",
        )
        _require(
            not _directory_fd_is_within(directory_descriptor, gold_descriptor),
            "opened output directory is inside the read-only Gold root",
        )
        temporary_name = f".{report_file_name}.{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        temporary_descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(temporary_descriptor, "wb") as stream:
            temporary_descriptor = None
            stream.write(_canonical_json(report))
            stream.flush()
            os.fsync(stream.fileno())
        _require(
            not _directory_fd_is_within(directory_descriptor, gold_descriptor),
            "opened output directory moved inside the read-only Gold root",
        )
        os.link(
            temporary_name,
            report_file_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        published = True
        if _directory_fd_is_within(directory_descriptor, gold_descriptor):
            os.unlink(report_file_name, dir_fd=directory_descriptor)
            published = False
            raise ExistingProfileComparisonError(
                "opened output directory moved inside the read-only Gold root during publish"
            )
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_name = None
        os.fsync(directory_descriptor)
        publish_complete = True
    except FileExistsError as error:
        raise ExistingProfileComparisonError(
            f"comparison report already exists: {report_file_name}"
        ) from error
    except OSError as error:
        raise ExistingProfileComparisonError(f"cannot write comparison report: {error}") from error
    finally:
        if temporary_descriptor is not None:
            try:
                os.close(temporary_descriptor)
            except OSError:
                pass
        if temporary_name is not None and directory_descriptor is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if published and not publish_complete and directory_descriptor is not None:
            try:
                os.unlink(report_file_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        _close_descriptors(gold_chain)
        _close_descriptors(output_chain)
        if not output_chain and directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
    return report_path


__all__ = [
    "COMPARISON_SCHEMA_VERSION",
    "DEFAULT_EXPECTED_PROFILE_COUNT",
    "REPORT_FILE_NAME",
    "ExistingProfileComparisonError",
    "ExistingProfileExpectationError",
    "LoadedProfiles",
    "assert_expected_comparison",
    "compare_existing_profile_corpora",
    "compare_loaded_profiles",
    "load_automatic_baseline",
    "load_reviewed_gold",
    "write_comparison_report",
]
