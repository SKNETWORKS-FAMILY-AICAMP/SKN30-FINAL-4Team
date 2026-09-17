"""Fail-closed reader for the frozen, source-safe pristine hard-6 inputs.

The archive is a model input, not a profile baseline.  Its public manifest is
committed with the application so callers cannot turn an arbitrary six-member
ZIP into an accepted canary input by supplying a matching manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from app.pipelines.common_ir_provenance import (
    CommonIrProvenanceError,
    require_automatic_common_ir,
)
from common_ir_pipeline.schema import validation_errors


PRISTINE_HARD6_SCHEMA_VERSION = "pristine_hard6_common_ir_freeze/v1"
PRISTINE_HARD6_ARCHIVE_SHA256 = "24d70a944f43446563a257958fc5b97a5c484e28b5ac653ccf3f2792880d7800"
PRISTINE_HARD6_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "baselines"
    / "existing_profile"
    / "pristine_hard6_common_ir.v1.json"
)
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_COMMON_IR_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class PristineCommonIrError(ValueError):
    """The supplied archive is not the pinned pristine hard-6 input."""


@dataclass(frozen=True, slots=True)
class LoadedPristineCommonIr:
    """Validated Common IR documents and their public, non-source identities."""

    documents: Mapping[str, Mapping[str, Any]]
    archive_sha256: str
    member_sha256: Mapping[str, str]
    manifest: Mapping[str, Any]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PristineCommonIrError(message)


def _sha256_stream(stream: Any) -> str:
    digest = sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PristineCommonIrError(f"duplicate JSON key in {label}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise PristineCommonIrError(f"non-finite JSON value in {label}: {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise PristineCommonIrError(f"cannot parse {label} JSON") from error
    _require(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def _sha256(value: object, *, label: str) -> str:
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None, f"{label} must be a lowercase SHA-256")
    return value


def _safe_zip_member(info: ZipInfo) -> str:
    name = info.filename
    _require(name and "\\" not in name and "\x00" not in name and not name.startswith("/"), "unsafe pristine input ZIP member name")
    parts = PurePosixPath(name).parts
    _require(len(parts) == 4 and all(part not in {"", ".", ".."} for part in parts), "unsafe pristine input ZIP member path")
    _require(not info.is_dir() and not (info.flag_bits & 0x1) and info.compress_type == ZIP_STORED, "unsafe pristine input ZIP member")
    mode = (info.external_attr >> 16) & 0xFFFF
    _require(not stat.S_ISLNK(mode), "pristine input ZIP symlink is forbidden")
    if stat.S_IFMT(mode):
        _require(stat.S_IFMT(mode) == stat.S_IFREG, "special pristine input ZIP member is forbidden")
    return name


def _read_regular(path: Path, *, label: str, max_bytes: int) -> bytes:
    supplied = path.expanduser()
    _require(not supplied.is_symlink(), f"{label} must not be a symlink")
    descriptor: int | None = None
    try:
        descriptor = os.open(supplied, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), f"{label} must be a regular file")
            _require(0 < before.st_size <= max_bytes, f"{label} exceeds physical size cap")
            raw = stream.read(max_bytes + 1)
            _require(len(raw) == before.st_size and len(raw) <= max_bytes, f"{label} changed while read")
            after = os.fstat(stream.fileno())
        _require(_stat_identity(before) == _stat_identity(after), f"{label} changed while read")
        return raw
    except PristineCommonIrError:
        raise
    except OSError as error:
        raise PristineCommonIrError(f"cannot safely open {label}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_expected_manifest(path: Path, *, label: str) -> tuple[dict[str, Mapping[str, str]], dict[str, Any]]:
    raw = _read_regular(path, label=label, max_bytes=MAX_MANIFEST_BYTES)
    manifest = _json_object(raw, label=label)
    _require(set(manifest) == {"schema_version", "notice_count", "notices", "archive_sha256"}, "pristine input manifest keys are invalid")
    _require(manifest.get("schema_version") == PRISTINE_HARD6_SCHEMA_VERSION, "pristine input manifest schema mismatch")
    _require(manifest.get("notice_count") == 6, "pristine input manifest notice count mismatch")
    _require(manifest.get("archive_sha256") == PRISTINE_HARD6_ARCHIVE_SHA256, "pristine input manifest archive SHA-256 pin mismatch")
    notices = manifest.get("notices")
    _require(isinstance(notices, list) and len(notices) == 6, "pristine input manifest notices are invalid")
    expected: dict[str, Mapping[str, str]] = {}
    for item in notices:
        _require(isinstance(item, dict) and set(item) == {"notice_id", "format", "archive_member", "source_sha256", "common_ir_sha256"}, "pristine input manifest notice keys are invalid")
        notice_id = item.get("notice_id")
        source_format = item.get("format")
        member = item.get("archive_member")
        _require(isinstance(notice_id, str) and re.fullmatch(r"PBLN_[0-9]{15}", notice_id) is not None and notice_id not in expected, "pristine input manifest notice ID is invalid")
        _require(source_format in {"pdf", "hwp"}, f"{notice_id}: pristine input format is invalid")
        _require(member == f"{notice_id}/pipeline/common_ir_v1/{notice_id}.{source_format}.json", f"{notice_id}: pristine input member mismatch")
        _sha256(item.get("source_sha256"), label=f"{notice_id} source SHA-256")
        _sha256(item.get("common_ir_sha256"), label=f"{notice_id} Common IR SHA-256")
        expected[notice_id] = item
    _require(len(expected) == 6, "pristine input manifest must name exactly six notices")
    return expected, manifest


def _expected_manifest(manifest_path: Path | None) -> dict[str, Mapping[str, str]]:
    """Anchor expectations in the checked-in public pin, never caller input."""

    expected, checked_in = _read_expected_manifest(
        PRISTINE_HARD6_MANIFEST_PATH, label="checked-in pristine input manifest"
    )
    if manifest_path is not None:
        supplied, external = _read_expected_manifest(
            manifest_path, label="supplied pristine input manifest"
        )
        _require(
            external == checked_in and supplied == expected,
            "supplied pristine input manifest does not match checked-in pin",
        )
    return expected


def _validate_document(document: Mapping[str, Any], *, notice_id: str, expected: Mapping[str, str]) -> None:
    _require(document.get("schema_version") == "common_ir_v1", f"{notice_id}: Common IR schema mismatch")
    identity = document.get("document")
    _require(isinstance(identity, Mapping), f"{notice_id}: Common IR document identity is invalid")
    _require(
        identity.get("artifact_role") == "production",
        f"{notice_id}: Common IR artifact role is not production",
    )
    try:
        schema_errors = validation_errors(dict(document))
    except Exception as error:
        raise PristineCommonIrError(
            f"{notice_id}: Common IR schema validation failed"
        ) from error
    _require(not schema_errors, f"{notice_id}: Common IR schema validation failed")
    source_format = expected["format"]
    _require(identity.get("document_id") == f"{source_format}:{notice_id}", f"{notice_id}: Common IR document ID mismatch")
    _require(identity.get("source_kind") == source_format, f"{notice_id}: Common IR source kind mismatch")
    provenance = identity.get("provenance")
    _require(isinstance(provenance, Mapping) and provenance.get("source_sha256") == expected["source_sha256"], f"{notice_id}: Common IR source SHA-256 mismatch")
    _require(provenance.get("source_location") == f"source.{source_format}", f"{notice_id}: Common IR source location is not canonical")
    try:
        require_automatic_common_ir(document)
    except CommonIrProvenanceError as error:
        raise PristineCommonIrError(f"{notice_id}: Common IR contains manual adjudication provenance") from error
    automatic_producers = {
        "pdf": (
            "pdf_native_only",
            "common_ir_v1_adapters",
            "1.1.0",
            "pdf_inspector",
            "1.17.0",
        ),
        "hwp": (
            "rhwp",
            "common_ir_v1_adapters",
            "1.1.0",
            "rhwp",
            "0.8.1",
        ),
    }
    _require(
        (
            provenance.get("method"),
            provenance.get("generator"),
            provenance.get("generator_version"),
            provenance.get("parser"),
            provenance.get("parser_version"),
        )
        == automatic_producers[source_format],
        f"{notice_id}: Common IR producer is not in the automatic allowlist",
    )


def load_pristine_common_ir(
    archive_path: Path,
    *,
    manifest_path: Path | None = None,
) -> LoadedPristineCommonIr:
    """Load exactly the public-pinned, automatic Common IR documents.

    ``manifest_path`` supports a separately deployed copy of the public
    manifest, but it must still carry the committed archive pin.  It cannot
    supply a new trust root.
    """

    expected = _expected_manifest(manifest_path)
    supplied = archive_path.expanduser()
    _require(not supplied.is_symlink(), "pristine input ZIP must not be a symlink")
    descriptor: int | None = None
    try:
        descriptor = os.open(supplied, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode), "pristine input ZIP must be a regular file")
            _require(0 < before.st_size <= MAX_ARCHIVE_BYTES, "pristine input ZIP exceeds physical size cap")
            archive_sha256 = _sha256_stream(stream)
            _require(archive_sha256 == PRISTINE_HARD6_ARCHIVE_SHA256, "pristine input ZIP SHA-256 pin mismatch")
            try:
                with ZipFile(stream) as archive:
                    infos = archive.infolist()
                    _require(len(infos) == 6, "pristine input ZIP must contain exactly six members")
                    selected: dict[str, ZipInfo] = {}
                    seen_names: set[str] = set()
                    seen_offsets: set[int] = set()
                    for info in infos:
                        member = _safe_zip_member(info)
                        _require(member not in seen_names, "duplicate pristine input ZIP member")
                        seen_names.add(member)
                        _require(0 <= info.header_offset < before.st_size and info.header_offset not in seen_offsets, "invalid pristine input ZIP local-header offset")
                        seen_offsets.add(info.header_offset)
                        parts = PurePosixPath(member).parts
                        notice_id = parts[0]
                        _require(notice_id in expected and member == expected[notice_id]["archive_member"], "pristine input ZIP has an unexpected member")
                        _require(notice_id not in selected and 0 <= info.file_size <= MAX_COMMON_IR_BYTES, "invalid pristine input Common IR member")
                        selected[notice_id] = info
                    _require(set(selected) == set(expected), "pristine input ZIP member set mismatch")
                    documents: dict[str, Mapping[str, Any]] = {}
                    member_sha256: dict[str, str] = {}
                    for notice_id in sorted(expected):
                        info = selected[notice_id]
                        try:
                            with archive.open(info, "r") as member_stream:
                                raw = member_stream.read(MAX_COMMON_IR_BYTES + 1)
                        except (BadZipFile, OSError, RuntimeError) as error:
                            raise PristineCommonIrError("cannot read pristine input Common IR member") from error
                        _require(len(raw) == info.file_size and len(raw) <= MAX_COMMON_IR_BYTES, "pristine input Common IR member size mismatch")
                        _require(sha256(raw).hexdigest() == expected[notice_id]["common_ir_sha256"], f"{notice_id}: pristine input Common IR SHA-256 mismatch")
                        document = _json_object(raw, label=info.filename)
                        _validate_document(document, notice_id=notice_id, expected=expected[notice_id])
                        documents[notice_id] = document
                        member_sha256[notice_id] = sha256(raw).hexdigest()
            except BadZipFile as error:
                raise PristineCommonIrError("pristine input ZIP is not valid") from error
            after = os.fstat(stream.fileno())
        _require(_stat_identity(before) == _stat_identity(after), "pristine input ZIP changed while Common IR was read")
        return LoadedPristineCommonIr(documents, archive_sha256, member_sha256, {"archive_sha256": PRISTINE_HARD6_ARCHIVE_SHA256, "notice_ids": tuple(sorted(expected))})
    except PristineCommonIrError:
        raise
    except OSError as error:
        raise PristineCommonIrError("cannot safely open pristine input ZIP") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
