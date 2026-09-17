#!/usr/bin/env python3
"""Build or verify the source-safe, deterministic pristine hard-6 Common IR ZIP.

The archive contains only the six Common IR JSON documents.  The accompanying
public manifest contains IDs, formats, paths, and SHA-256 pins -- never source
text or a source document.  Five PDFs must agree across independent replay
roots; the HWP document must agree across its independent parser outputs.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Mapping
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VENDOR_COMMON_IR_SRC = BACKEND_ROOT / "vendor" / "common_ir_pipeline" / "src"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(VENDOR_COMMON_IR_SRC) not in sys.path:
    sys.path.insert(0, str(VENDOR_COMMON_IR_SRC))

from app.pipelines.common_ir_provenance import (  # noqa: E402
    CommonIrProvenanceError,
    require_automatic_common_ir,
)
from common_ir_pipeline.schema import validation_errors  # noqa: E402


SCHEMA_VERSION = "pristine_hard6_common_ir_freeze/v1"
EXPECTED_NOTICES = (
    ("PBLN_000000000103645", "pdf"),
    ("PBLN_000000000112425", "pdf"),
    ("PBLN_000000000117175", "hwp"),
    ("PBLN_000000000121019", "pdf"),
    ("PBLN_000000000121309", "pdf"),
    ("PBLN_000000000122023", "pdf"),
)
EXPECTED_IDS = tuple(notice_id for notice_id, _ in EXPECTED_NOTICES)
EXPECTED_FORMATS = dict(EXPECTED_NOTICES)
MAX_COMMON_IR_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_NATIVE_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 128 * 1024 * 1024
_SHA256_HEX_LENGTH = 64
DEFAULT_EXPECTED_MANIFEST = (
    BACKEND_ROOT / "baselines" / "existing_profile" / "pristine_hard6_common_ir.v1.json"
)


class Hard6FreezeError(ValueError):
    """The proposed hard-6 freeze is incomplete, unsafe, or non-reproducible."""


@dataclass(frozen=True, slots=True)
class SourceNotice:
    notice_id: str
    source_format: str
    source_path: Path
    source_sha256: str


@dataclass(frozen=True, slots=True)
class FrozenMember:
    notice_id: str
    source_format: str
    source_sha256: str
    member_path: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class PdfReplayMember:
    paths: dict[str, Path]
    payloads: dict[str, bytes]
    canonical_common_ir: bytes


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Hard6FreezeError(message)


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Hard6FreezeError("value is not canonically serializable JSON") from error


def _parse_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise Hard6FreezeError(f"duplicate JSON key in {label}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise Hard6FreezeError(f"non-finite JSON value in {label}: {value}")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Hard6FreezeError(f"cannot parse JSON object: {label}") from error
    _require(isinstance(value, dict), f"JSON root must be an object: {label}")
    return value


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _assert_trusted_directory(path: Path, *, label: str) -> None:
    """Require a non-link parent that is not mutable by arbitrary peers.

    This is intentionally a trusted-private-parent contract rather than a
    complete openat walk.  A sticky temporary directory is permitted for
    explicitly supplied scratch outputs; every child we open still uses
    O_NOFOLLOW and output creation uses O_EXCL.
    """

    _require(not path.is_symlink(), f"{label} must not be a symlink")
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as error:
        raise Hard6FreezeError(f"cannot stat {label}") from error
    _require(stat.S_ISDIR(details.st_mode), f"{label} must be a directory")
    peer_writable = bool(details.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    _require(not peer_writable or bool(details.st_mode & stat.S_ISVTX), f"{label} must not be group/world writable")


def _read_regular_file(path: Path, *, label: str, max_bytes: int | None = None) -> bytes:
    _assert_trusted_directory(path.parent, label=f"{label} parent")
    _require(not path.is_symlink(), f"{label} must not be a symlink")
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
            if max_bytes is not None:
                _require(before.st_size <= max_bytes, f"{label} exceeds size limit")
            raw = stream.read((max_bytes + 1) if max_bytes is not None else -1)
            after = os.fstat(stream.fileno())
        _require(_stat_identity(before) == _stat_identity(after), f"{label} changed while reading")
        if max_bytes is not None:
            _require(len(raw) <= max_bytes, f"{label} exceeds size limit")
        return raw
    except OSError as error:
        raise Hard6FreezeError(f"cannot read {label}: {error.strerror or type(error).__name__}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _safe_existing_directory(path: Path, *, label: str) -> Path:
    supplied = path.expanduser()
    _require(not supplied.is_symlink(), f"{label} must not be a symlink")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise Hard6FreezeError(f"cannot resolve {label}") from error
    _require(resolved.is_dir(), f"{label} must be a directory")
    _assert_trusted_directory(resolved.parent, label=f"{label} parent")
    _assert_trusted_directory(resolved, label=label)
    return resolved


def _safe_child(root: Path, relative: str, *, label: str) -> Path:
    path = PurePosixPath(relative)
    _require(not path.is_absolute() and path.parts and all(part not in {"", ".", ".."} for part in path.parts), f"unsafe {label}")
    current = root
    for part in path.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise Hard6FreezeError(f"missing {label}: {relative}") from None
        _require(not stat.S_ISLNK(mode), f"symlink is forbidden in {label}: {relative}")
        if current.is_dir():
            _assert_trusted_directory(current, label=f"{label} directory")
    _require(current.is_file(), f"{label} must be a regular file: {relative}")
    return current


def _reject_same_file(first: Path, second: Path, *, label: str) -> None:
    try:
        first_stat = first.stat(follow_symlinks=False)
        second_stat = second.stat(follow_symlinks=False)
    except OSError as error:
        raise Hard6FreezeError(f"cannot stat {label}") from error
    _require(
        (first_stat.st_dev, first_stat.st_ino) != (second_stat.st_dev, second_stat.st_ino),
        f"{label} A/B paths must not alias or hardlink the same inode",
    )


def _reject_same_directory(first: Path, second: Path, *, label: str) -> None:
    _reject_same_file(first, second, label=label)


def _valid_sha256(value: object) -> str:
    _require(isinstance(value, str) and len(value) == _SHA256_HEX_LENGTH and all(ch in "0123456789abcdef" for ch in value), "invalid SHA-256")
    return value


def _load_source_inventory(path: Path) -> dict[str, SourceNotice]:
    raw = _read_regular_file(path, label="source inventory", max_bytes=4 * 1024 * 1024)
    document = _parse_object(raw, label="source inventory")
    notices = document.get("notices")
    _require(isinstance(notices, list), "source inventory notices must be an array")
    result: dict[str, SourceNotice] = {}
    for item in notices:
        _require(isinstance(item, dict), "source inventory notice must be an object")
        notice_id = item.get("notice_id")
        source_format = item.get("format")
        source_path = item.get("input_path")
        _require(isinstance(notice_id, str) and notice_id in EXPECTED_FORMATS, "source inventory has an unexpected notice ID")
        _require(source_format == EXPECTED_FORMATS[notice_id], f"{notice_id}: source format mismatch")
        _require(isinstance(source_path, str) and source_path, f"{notice_id}: invalid input_path")
        path_value = Path(source_path).expanduser()
        _require(path_value.is_absolute(), f"{notice_id}: input_path must be absolute")
        source_hash = _valid_sha256(item.get("source_sha256"))
        _require(notice_id not in result, f"duplicate source inventory notice: {notice_id}")
        actual = _read_regular_file(path_value, label=f"{notice_id} source", max_bytes=MAX_SOURCE_BYTES)
        _require(sha256(actual).hexdigest() == source_hash, f"{notice_id}: source SHA-256 mismatch")
        result[notice_id] = SourceNotice(notice_id, source_format, path_value, source_hash)
    _require(tuple(sorted(result)) == tuple(sorted(EXPECTED_IDS)), "source inventory must contain exactly the pristine hard-6 IDs")
    return result


def _validate_common_ir(document: Mapping[str, Any], *, notice: SourceNotice) -> None:
    try:
        require_automatic_common_ir(document)
    except CommonIrProvenanceError as error:
        raise Hard6FreezeError(str(error)) from error
    _require(document.get("schema_version") == "common_ir_v1", f"{notice.notice_id}: unsupported Common IR schema")
    errors = validation_errors(dict(document))
    _require(not errors, f"{notice.notice_id}: Common IR schema validation failed")
    identity = document.get("document")
    _require(isinstance(identity, Mapping), f"{notice.notice_id}: Common IR document identity is missing")
    _require(identity.get("document_id") == f"{notice.source_format}:{notice.notice_id}", f"{notice.notice_id}: Common IR document ID mismatch")
    _require(identity.get("source_kind") == notice.source_format, f"{notice.notice_id}: Common IR source kind mismatch")
    provenance = identity.get("provenance")
    _require(isinstance(provenance, Mapping), f"{notice.notice_id}: Common IR provenance is missing")
    _require(provenance.get("source_sha256") == notice.source_sha256, f"{notice.notice_id}: Common IR source SHA-256 mismatch")
    _require(identity.get("artifact_role") == "production", f"{notice.notice_id}: Common IR artifact role is not production")
    _require(provenance.get("generator") == "common_ir_v1_adapters", f"{notice.notice_id}: Common IR generator is not allowlisted")
    _require(provenance.get("generator_version") == "1.1.0", f"{notice.notice_id}: Common IR generator version is not allowlisted")
    if notice.source_format == "pdf":
        _require(provenance.get("method") == "pdf_native_only", f"{notice.notice_id}: PDF producer method is not allowlisted")
        _require(provenance.get("parser") == "pdf_inspector", f"{notice.notice_id}: PDF parser is not allowlisted")
        _require(provenance.get("parser_version") == "1.17.0", f"{notice.notice_id}: PDF parser version is not allowlisted")
    else:
        _require(provenance.get("method") == "rhwp", f"{notice.notice_id}: HWP producer method is not allowlisted")
        _require(provenance.get("parser") == "rhwp", f"{notice.notice_id}: HWP parser is not allowlisted")
        _require(provenance.get("parser_version") == "0.8.1", f"{notice.notice_id}: HWP parser version is not allowlisted")


def _is_portable_source_location(value: object, *, notice: SourceNotice) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    if value == f"source.{notice.source_format}" or value == "native.json":
        return True
    if notice.source_format == "pdf":
        return bool(re.fullmatch(r"text_items\[[0-9]+(?::[0-9]+)?\]", value))
    raw_name = f"raw/rhwp_full_ir/{notice.notice_id}.hwp.json"
    if value == raw_name:
        return True
    prefix = raw_name + "#/"
    return value.startswith(prefix) and ".." not in value[len(prefix):].split("/") and not value.endswith("/")


def _is_portable_raw_artifact(value: object, *, notice: SourceNotice) -> bool:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        return False
    return value == ("native.json" if notice.source_format == "pdf" else f"raw/rhwp_full_ir/{notice.notice_id}.hwp.json")


def _validate_portable_lineage(value: Any, *, notice: SourceNotice, key: str = "", depth: int = 0) -> None:
    _require(depth <= 128, f"{notice.notice_id}: Common IR nesting exceeds portable-lineage limit")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _require(isinstance(child_key, str), f"{notice.notice_id}: non-string Common IR key")
            if child_key == "source_location":
                _require(_is_portable_source_location(child, notice=notice), f"{notice.notice_id}: nonportable provenance source_location")
            elif child_key == "raw_artifact_ids":
                _require(isinstance(child, list) and all(_is_portable_raw_artifact(item, notice=notice) for item in child), f"{notice.notice_id}: nonportable raw_artifact_ids")
            _validate_portable_lineage(child, notice=notice, key=child_key, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_portable_lineage(child, notice=notice, key=key, depth=depth + 1)


def _canonicalize_for_archive(document: Mapping[str, Any], *, notice: SourceNotice) -> bytes:
    """Remove machine-local source paths before they become a frozen hash input."""

    frozen = deepcopy(dict(document))
    identity = frozen["document"]
    provenance = identity["provenance"]
    provenance["source_location"] = f"source.{notice.source_format}"
    _validate_common_ir(frozen, notice=notice)
    _validate_portable_lineage(frozen, notice=notice)
    return _canonical_json(frozen)


def _load_pdf_member(root: Path, *, notice: SourceNotice) -> PdfReplayMember:
    source_path = _safe_child(root, f"{notice.notice_id}/source.pdf", label="PDF replay source")
    source = _read_regular_file(source_path, label=f"{notice.notice_id} PDF replay source", max_bytes=MAX_SOURCE_BYTES)
    _require(sha256(source).hexdigest() == notice.source_sha256, f"{notice.notice_id}: PDF replay source SHA-256 mismatch")
    manifest_path = _safe_child(root, f"{notice.notice_id}/manifest.json", label="PDF replay manifest")
    manifest_raw = _read_regular_file(
        manifest_path,
        label=f"{notice.notice_id} PDF replay manifest",
        max_bytes=MAX_MANIFEST_BYTES,
    )
    manifest = _parse_object(
        manifest_raw,
        label=f"{notice.notice_id} PDF replay manifest",
    )
    _require(manifest.get("notice_id") == notice.notice_id, f"{notice.notice_id}: PDF replay manifest identity mismatch")
    source_artifact = manifest.get("artifacts", {}).get("source_pdf") if isinstance(manifest.get("artifacts"), Mapping) else None
    ir_artifact = manifest.get("artifacts", {}).get("common_ir") if isinstance(manifest.get("artifacts"), Mapping) else None
    native_artifact = manifest.get("artifacts", {}).get("native_capture") if isinstance(manifest.get("artifacts"), Mapping) else None
    _require(isinstance(source_artifact, Mapping) and source_artifact.get("sha256") == notice.source_sha256, f"{notice.notice_id}: PDF replay manifest source hash mismatch")
    native_path = _safe_child(root, f"{notice.notice_id}/native.json", label="PDF replay native capture")
    native_raw = _read_regular_file(
        native_path,
        label=f"{notice.notice_id} PDF replay native capture",
        max_bytes=MAX_NATIVE_CAPTURE_BYTES,
    )
    _require(
        isinstance(native_artifact, Mapping) and native_artifact.get("sha256") == sha256(native_raw).hexdigest(),
        f"{notice.notice_id}: PDF replay manifest native capture hash mismatch",
    )
    common_ir_path = _safe_child(root, f"{notice.notice_id}/common_ir.json", label="PDF replay Common IR")
    raw = _read_regular_file(common_ir_path, label=f"{notice.notice_id} PDF replay Common IR", max_bytes=MAX_COMMON_IR_BYTES)
    _require(isinstance(ir_artifact, Mapping) and ir_artifact.get("sha256") == sha256(raw).hexdigest(), f"{notice.notice_id}: PDF replay manifest Common IR hash mismatch")
    document = _parse_object(raw, label=f"{notice.notice_id} PDF replay Common IR")
    _validate_common_ir(document, notice=notice)
    return PdfReplayMember(
        paths={"source.pdf": source_path, "native.json": native_path, "common_ir.json": common_ir_path, "manifest.json": manifest_path},
        payloads={"source.pdf": source, "native.json": native_raw, "common_ir.json": raw, "manifest.json": manifest_raw},
        canonical_common_ir=_canonicalize_for_archive(document, notice=notice),
    )


def _load_hwp_member(path: Path, *, notice: SourceNotice) -> tuple[bytes, bytes]:
    raw = _read_regular_file(path, label=f"{notice.notice_id} HWP Common IR", max_bytes=MAX_COMMON_IR_BYTES)
    document = _parse_object(raw, label=f"{notice.notice_id} HWP Common IR")
    _validate_common_ir(document, notice=notice)
    return raw, _canonicalize_for_archive(document, notice=notice)


def _member_path(notice: SourceNotice) -> str:
    return f"{notice.notice_id}/pipeline/common_ir_v1/{notice.notice_id}.{notice.source_format}.json"


def _deterministic_archive(members: list[FrozenMember]) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_STORED, strict_timestamps=True) as archive:
        archive.comment = b""
        for member in sorted(members, key=lambda item: item.member_path):
            info = ZipInfo(member.member_path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = ZIP_STORED
            archive.writestr(info, member.payload)
    return stream.getvalue()


def build_freeze(
    *,
    source_inventory: Path,
    pdf_replay_root_a: Path,
    pdf_replay_root_b: Path,
    hwp_common_ir_a: Path,
    hwp_common_ir_b: Path,
) -> tuple[bytes, dict[str, object]]:
    """Validate independent replays and return deterministic archive bytes and manifest."""

    sources = _load_source_inventory(source_inventory)
    pdf_a = _safe_existing_directory(pdf_replay_root_a, label="PDF replay root A")
    pdf_b = _safe_existing_directory(pdf_replay_root_b, label="PDF replay root B")
    _reject_same_directory(pdf_a, pdf_b, label="PDF replay roots")
    _reject_same_file(hwp_common_ir_a, hwp_common_ir_b, label="HWP Common IR")
    members: list[FrozenMember] = []
    for notice_id, source_format in EXPECTED_NOTICES:
        notice = sources[notice_id]
        if source_format == "pdf":
            first = _load_pdf_member(pdf_a, notice=notice)
            second = _load_pdf_member(pdf_b, notice=notice)
            for artifact_name in ("source.pdf", "native.json", "common_ir.json", "manifest.json"):
                _reject_same_file(first.paths[artifact_name], second.paths[artifact_name], label=f"{notice_id}: PDF replay {artifact_name}")
                _require(
                    first.payloads[artifact_name] == second.payloads[artifact_name],
                    f"{notice_id}: PDF replay {artifact_name} A/B byte identity mismatch",
                )
            payload = first.canonical_common_ir
        else:
            first_raw, first_canonical = _load_hwp_member(hwp_common_ir_a, notice=notice)
            second_raw, _second_canonical = _load_hwp_member(hwp_common_ir_b, notice=notice)
            _require(first_raw == second_raw, f"{notice_id}: HWP Common IR A/B byte identity mismatch")
            payload = first_canonical
        members.append(FrozenMember(notice_id, source_format, notice.source_sha256, _member_path(notice), payload))
    archive = _deterministic_archive(members)
    _require(sum(len(member.payload) for member in members) <= MAX_TOTAL_MEMBER_BYTES, "archive Common IR members exceed total size limit")
    archive_hash = sha256(archive).hexdigest()
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "notice_count": len(members),
        "notices": [
            {
                "notice_id": member.notice_id,
                "format": member.source_format,
                "archive_member": member.member_path,
                "source_sha256": member.source_sha256,
                "common_ir_sha256": sha256(member.payload).hexdigest(),
            }
            for member in members
        ],
        "archive_sha256": archive_hash,
    }
    _verify_archive_and_manifest(archive, manifest)
    return archive, manifest


def _safe_archive_member(info: ZipInfo) -> str:
    name = info.filename
    _require(name and "\\" not in name and "\x00" not in name and not name.startswith("/"), "unsafe archive member name")
    parts = PurePosixPath(name).parts
    _require(len(parts) == 4 and all(part not in {"", ".", ".."} for part in parts), "unsafe archive member path")
    _require(not info.is_dir() and info.flag_bits == 0 and info.compress_type == ZIP_STORED, "unsafe archive member")
    mode = (info.external_attr >> 16) & 0xFFFF
    _require(mode == (stat.S_IFREG | 0o600), "archive member mode is not deterministic private regular-file mode")
    _require(info.create_system == 3 and info.date_time == (1980, 1, 1, 0, 0, 0), "archive member metadata is not deterministic")
    _require(0 <= info.file_size <= MAX_COMMON_IR_BYTES, "archive Common IR member exceeds size limit")
    return name


def _verify_archive_and_manifest(archive_bytes: bytes, manifest: Mapping[str, Any]) -> None:
    _require(0 < len(archive_bytes) <= MAX_ARCHIVE_BYTES, "archive exceeds size limit")
    _require(set(manifest) == {"schema_version", "notice_count", "notices", "archive_sha256"}, "public manifest keys are invalid")
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "public manifest schema mismatch")
    _require(manifest.get("notice_count") == len(EXPECTED_NOTICES), "public manifest notice count mismatch")
    _require(manifest.get("archive_sha256") == sha256(archive_bytes).hexdigest(), "archive SHA-256 mismatch")
    items = manifest.get("notices")
    _require(isinstance(items, list) and len(items) == len(EXPECTED_NOTICES), "public manifest notices are invalid")
    expected_members: dict[str, Mapping[str, Any]] = {}
    for item in items:
        _require(isinstance(item, Mapping) and set(item) == {"notice_id", "format", "archive_member", "source_sha256", "common_ir_sha256"}, "public manifest notice keys are invalid")
        notice_id = item.get("notice_id")
        _require(isinstance(notice_id, str) and notice_id in EXPECTED_FORMATS and notice_id not in expected_members, "public manifest notice IDs are invalid")
        _require(item.get("format") == EXPECTED_FORMATS[notice_id], f"{notice_id}: public manifest format mismatch")
        _require(item.get("archive_member") == f"{notice_id}/pipeline/common_ir_v1/{notice_id}.{EXPECTED_FORMATS[notice_id]}.json", f"{notice_id}: public manifest member mismatch")
        _valid_sha256(item.get("source_sha256"))
        _valid_sha256(item.get("common_ir_sha256"))
        expected_members[notice_id] = item
    _require(tuple(sorted(expected_members)) == tuple(sorted(EXPECTED_IDS)), "public manifest must contain exactly the pristine hard-6 IDs")
    try:
        with ZipFile(BytesIO(archive_bytes)) as archive:
            infos = archive.infolist()
            _require(len(infos) == len(EXPECTED_NOTICES), "archive must contain exactly six members")
            _require(archive.comment == b"", "archive comment must be empty")
            seen: set[str] = set()
            retained_total = 0
            ordered_members: list[str] = []
            for info in infos:
                member_path = _safe_archive_member(info)
                _require(member_path not in seen, "duplicate archive member")
                seen.add(member_path)
                parts = PurePosixPath(member_path).parts
                notice_id = parts[0]
                _require(notice_id in expected_members and member_path == expected_members[notice_id]["archive_member"], "archive has an unexpected member")
                raw = archive.read(info)
                _require(len(raw) == info.file_size, "archive Common IR member size mismatch")
                retained_total += len(raw)
                _require(retained_total <= MAX_TOTAL_MEMBER_BYTES, "archive Common IR members exceed total size limit")
                _require(sha256(raw).hexdigest() == expected_members[notice_id]["common_ir_sha256"], f"{notice_id}: archive Common IR SHA-256 mismatch")
                notice = SourceNotice(notice_id, EXPECTED_FORMATS[notice_id], Path("."), str(expected_members[notice_id]["source_sha256"]))
                document = _parse_object(raw, label=member_path)
                _validate_common_ir(document, notice=notice)
                provenance = document["document"]["provenance"]
                _require(provenance.get("source_location") == f"source.{notice.source_format}", f"{notice_id}: frozen source location is not canonical")
                _validate_portable_lineage(document, notice=notice)
                ordered_members.append(member_path)
            _require(seen == {str(item["archive_member"]) for item in expected_members.values()}, "archive member set mismatch")
            _require(ordered_members == sorted(ordered_members), "archive members are not in deterministic order")
    except BadZipFile as error:
        raise Hard6FreezeError("archive is not a valid ZIP") from error


def _load_expected_manifest(path: Path) -> dict[str, Any]:
    raw = _read_regular_file(path, label="expected baseline manifest", max_bytes=MAX_MANIFEST_BYTES)
    manifest = _parse_object(raw, label="expected baseline manifest")
    _require(_canonical_json(manifest) == raw, "expected baseline manifest is not canonical JSON")
    return manifest


def _require_expected_manifest(
    candidate: Mapping[str, Any],
    *,
    expected_manifest: Path | None,
) -> dict[str, Any]:
    """Bind every normal build/check to the checked-in reviewed trust root.

    ``expected_manifest`` may point at a separately deployed copy, but it is
    never allowed to replace the repository pin.  Creating a new candidate
    trust root is restricted to the explicit bootstrap path.
    """

    reviewed = _load_expected_manifest(DEFAULT_EXPECTED_MANIFEST)
    if expected_manifest is not None:
        supplied = _load_expected_manifest(expected_manifest)
        _require(
            supplied == reviewed,
            "supplied expected manifest differs from the checked-in reviewed baseline",
        )
    _require(
        candidate == reviewed,
        "candidate manifest differs from independently reviewed baseline",
    )
    return reviewed


def verify_freeze(
    *,
    archive_path: Path,
    manifest_path: Path,
    expected_manifest: Path | None = None,
) -> None:
    archive = _read_regular_file(archive_path, label="archive", max_bytes=MAX_ARCHIVE_BYTES)
    raw_manifest = _read_regular_file(manifest_path, label="public manifest", max_bytes=MAX_MANIFEST_BYTES)
    manifest = _parse_object(raw_manifest, label="public manifest")
    _require(_canonical_json(manifest) == raw_manifest, "public manifest is not canonical JSON")
    _verify_archive_and_manifest(archive, manifest)
    _require_expected_manifest(manifest, expected_manifest=expected_manifest)


def _atomic_create(path: Path, payload: bytes, *, label: str) -> None:
    _require(not path.exists() and not path.is_symlink(), f"refusing to overwrite existing {label}")
    _require(path.parent.is_dir() and not path.parent.is_symlink(), f"{label} parent must be an existing non-symlink directory")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise Hard6FreezeError(f"cannot create {label}: {error.strerror or type(error).__name__}") from error


def _create_archive_manifest_pair(
    *,
    archive_path: Path,
    archive_payload: bytes,
    manifest_path: Path,
    manifest_payload: bytes,
) -> None:
    """Publish a new pair or roll the first file back if the second fails.

    The CLI permits separate output paths, so a cross-directory atomic rename
    is impossible.  Both parents are trusted before this function runs and
    both targets are created with ``O_EXCL``.  Removing only the archive that
    this invocation just created keeps a failed publication retryable.
    """

    _atomic_create(archive_path, archive_payload, label="archive")
    try:
        _atomic_create(manifest_path, manifest_payload, label="public manifest")
    except Hard6FreezeError:
        try:
            archive_path.unlink()
        except OSError as rollback_error:
            raise Hard6FreezeError(
                "cannot roll back archive after public manifest publication failed"
            ) from rollback_error
        raise


def _assert_creatable(path: Path, *, label: str) -> None:
    _require(not path.exists() and not path.is_symlink(), f"refusing to overwrite existing {label}")
    _assert_trusted_directory(path.parent, label=f"{label} parent")


def _assert_distinct_paths(first: Path, second: Path, *, label: str) -> None:
    try:
        first_resolved = first.expanduser().resolve(strict=False)
        second_resolved = second.expanduser().resolve(strict=False)
    except OSError as error:
        raise Hard6FreezeError(f"cannot resolve {label}") from error
    _require(first_resolved != second_resolved, f"{label} paths must be distinct")
    if first.exists() and second.exists():
        _reject_same_file(first, second, label=label)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-inventory", required=True, type=Path)
    parser.add_argument("--pdf-replay-root-a", required=True, type=Path)
    parser.add_argument("--pdf-replay-root-b", required=True, type=Path)
    parser.add_argument("--hwp-common-ir-a", required=True, type=Path)
    parser.add_argument("--hwp-common-ir-b", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--expected-manifest",
        type=Path,
        help=(
            "optional separately deployed copy of the reviewed manifest; it must "
            "exactly match the checked-in trust root"
        ),
    )
    parser.add_argument(
        "--bootstrap-candidate",
        action="store_true",
        help="explicitly create an unpinned candidate; never use this for a normal freeze",
    )
    parser.add_argument("--check", action="store_true", help="verify existing archive and manifest without writing")
    args = parser.parse_args(argv)
    try:
        _assert_distinct_paths(args.archive, args.manifest, label="archive and public manifest")
        _require(not args.bootstrap_candidate or not args.check, "bootstrap candidate mode cannot be used with --check")
        if args.check:
            verify_freeze(
                archive_path=args.archive,
                manifest_path=args.manifest,
                expected_manifest=args.expected_manifest,
            )
            expected_archive, expected_manifest = build_freeze(
                source_inventory=args.source_inventory,
                pdf_replay_root_a=args.pdf_replay_root_a,
                pdf_replay_root_b=args.pdf_replay_root_b,
                hwp_common_ir_a=args.hwp_common_ir_a,
                hwp_common_ir_b=args.hwp_common_ir_b,
            )
            _require_expected_manifest(expected_manifest, expected_manifest=args.expected_manifest)
            _require(
                _read_regular_file(args.archive, label="archive", max_bytes=MAX_ARCHIVE_BYTES) == expected_archive,
                "archive differs from current pristine hard-6 inputs",
            )
            _require(
                _read_regular_file(args.manifest, label="public manifest", max_bytes=4 * 1024 * 1024)
                == _canonical_json(expected_manifest),
                "public manifest differs from current pristine hard-6 inputs",
            )
            status = "valid"
            manifest = expected_manifest
        else:
            archive, manifest = build_freeze(
                source_inventory=args.source_inventory,
                pdf_replay_root_a=args.pdf_replay_root_a,
                pdf_replay_root_b=args.pdf_replay_root_b,
                hwp_common_ir_a=args.hwp_common_ir_a,
                hwp_common_ir_b=args.hwp_common_ir_b,
            )
            if not args.bootstrap_candidate:
                manifest = _require_expected_manifest(manifest, expected_manifest=args.expected_manifest)
            _assert_creatable(args.archive, label="archive")
            _assert_creatable(args.manifest, label="public manifest")
            _create_archive_manifest_pair(
                archive_path=args.archive,
                archive_payload=archive,
                manifest_path=args.manifest,
                manifest_payload=_canonical_json(manifest),
            )
            status = "candidate_created" if args.bootstrap_candidate else "created"
    except Hard6FreezeError as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"status": status, "archive_sha256": manifest["archive_sha256"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
