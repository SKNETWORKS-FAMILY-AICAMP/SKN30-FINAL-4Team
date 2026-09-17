#!/usr/bin/env python3
"""Replay one native-text PDF into Common IR for offline Existing KB work.

This command is deliberately not part of the request-upload parser.  It stages
one complete PDF in a private directory, captures the pinned pdf-inspector
artifact, runs the existing native-PDF Common IR adapter, and publishes a
self-contained directory with deterministic relative artifact names.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - the supported replay host is POSIX.
    resource = None  # type: ignore[assignment]


SCHEMA_VERSION = "existing_pdf_native_replay/v1"
SOURCE_NAME = "source.pdf"
NATIVE_NAME = "native.json"
COMMON_IR_NAME = "common_ir.json"
MANIFEST_NAME = "manifest.json"
PUBLISHED_NAMES = (SOURCE_NAME, NATIVE_NAME, COMMON_IR_NAME, MANIFEST_NAME)
RENDER_MANIFEST_NAME = "render_manifest.json"
SURYA_LAYOUT_ARTIFACT_NAME = "surya_layout_artifact.json"
MAX_SURYA_LAYOUT_ARTIFACT_BYTES = 16 * 1024 * 1024
CAPTURE_MODULE = "common_ir_pipeline.workers.pdf_inspector_capture"
ADAPTER_MODULE = "common_ir_pipeline.adapters.pdf_native"
DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3600.0
CHILD_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024
CHILD_FILE_SIZE_BYTES = 256 * 1024 * 1024
CHILD_OPEN_FILES = 128
NOTICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_INHERITED_ENVIRONMENT = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SYSTEMROOT",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)


class ExistingPdfReplayError(RuntimeError):
    """The offline replay cannot safely produce a complete artifact set."""


def _load_native_capture_api() -> ModuleType:
    try:
        from common_ir_pipeline.pdf_fusion import native_capture
    except ImportError as error:  # pragma: no cover - exercised by the real CLI environment.
        raise ExistingPdfReplayError(
            "common_ir_pipeline.pdf_fusion.native_capture is unavailable; "
            "install this repository's pinned common-ir-pipeline package"
        ) from error
    return native_capture


def _sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the small, non-secret environment allowed across the parser boundary."""

    supplied = os.environ if source is None else source
    environment = {
        key: supplied[key]
        for key in sorted(_INHERITED_ENVIRONMENT)
        if supplied.get(key)
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _child_resource_limiter(timeout_seconds: float) -> Callable[[], None]:
    """Build the POSIX pre-exec limiter used for both parser subprocesses."""

    if resource is None or os.name != "posix":
        raise ExistingPdfReplayError(
            "offline PDF replay requires POSIX resource limits (run it on Linux/WSL)"
        )
    cpu_seconds = max(1, math.ceil(timeout_seconds) + 5)

    def apply_limit(limit_name: str, requested: int) -> None:
        limit_id = getattr(resource, limit_name, None)
        if limit_id is None:
            return
        _soft, hard = resource.getrlimit(limit_id)
        bounded = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(limit_id, (bounded, bounded))

    def apply_limits() -> None:
        apply_limit("RLIMIT_AS", CHILD_ADDRESS_SPACE_BYTES)
        apply_limit("RLIMIT_DATA", CHILD_ADDRESS_SPACE_BYTES)
        apply_limit("RLIMIT_FSIZE", CHILD_FILE_SIZE_BYTES)
        apply_limit("RLIMIT_CPU", cpu_seconds)
        apply_limit("RLIMIT_NOFILE", CHILD_OPEN_FILES)
        apply_limit("RLIMIT_CORE", 0)

    return apply_limits


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Run one fixed argv without a shell and reap its whole process group."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ExistingPdfReplayError("subprocess command must be a non-empty string argv")
    limiter = _child_resource_limiter(timeout_seconds)
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(environment or _sanitized_environment()),
            shell=False,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            # Child output is diagnostic only; artifacts are exchanged through
            # bounded files.  Discard it instead of buffering untrusted parser
            # output in parent memory.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=limiter,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExistingPdfReplayError(f"cannot start offline PDF replay subprocess: {error}") from error

    try:
        process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _kill_process_group(process)
        process.communicate()
        raise ExistingPdfReplayError(
            f"offline PDF replay subprocess exceeded {timeout_seconds:g} seconds"
        ) from error
    # A successful main child is not allowed to leave parser descendants in
    # the private staging directory. Kill the dedicated process group before
    # the parent reopens or rewrites any artifact path.
    _kill_process_group(process)
    if process.returncode != 0:
        raise ExistingPdfReplayError(
            f"offline PDF replay subprocess exited with {process.returncode}"
        )
    return "", ""


def _write_exclusive_file(path: Path, data: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
    except OSError as error:
        raise ExistingPdfReplayError(f"cannot create private artifact {path.name}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _replace_with_canonical_file(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.canonical")
    _write_exclusive_file(temporary, data)
    try:
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ExistingPdfReplayError(f"cannot replace canonical artifact {path.name}: {error}") from error


def _regular_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as error:
        raise ExistingPdfReplayError(f"cannot inspect input PDF: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise ExistingPdfReplayError("input PDF must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise ExistingPdfReplayError("input PDF must be a regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _copy_source_pdf(source: Path, destination: Path, *, max_bytes: int) -> tuple[str, int]:
    """Copy a stable regular PDF without following a final-component symlink."""

    identity = _regular_file_identity(source)
    if identity[2] > max_bytes:
        raise ExistingPdfReplayError("input PDF exceeds the native capture size limit")
    if source.suffix.casefold() != ".pdf":
        raise ExistingPdfReplayError("input source must have a .pdf extension")
    descriptor: int | None = None
    output_descriptor: int | None = None
    digest = sha256()
    copied = 0
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        output_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.fchmod(output_descriptor, 0o600)
        with os.fdopen(descriptor, "rb") as input_stream, os.fdopen(
            output_descriptor, "wb"
        ) as output_stream:
            descriptor = None
            output_descriptor = None
            before = os.fstat(input_stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ExistingPdfReplayError("input PDF must be a regular file")
            header = input_stream.read(5)
            if header != b"%PDF-":
                raise ExistingPdfReplayError("input does not have a PDF file signature")
            output_stream.write(header)
            digest.update(header)
            copied += len(header)
            while chunk := input_stream.read(1024 * 1024):
                output_stream.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
                if copied > max_bytes:
                    raise ExistingPdfReplayError("input PDF exceeds the native capture size limit")
            output_stream.flush()
            os.fsync(output_stream.fileno())
            after = os.fstat(input_stream.fileno())
    except OSError as error:
        raise ExistingPdfReplayError(f"cannot stage input PDF: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
    observed = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if observed != identity or (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != identity:
        raise ExistingPdfReplayError("input PDF changed while it was being staged")
    return digest.hexdigest(), copied


def _json_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ExistingPdfReplayError("JSON artifact limit must be a positive integer")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ExistingPdfReplayError(f"duplicate JSON key {key!r} in {path.name}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ExistingPdfReplayError(f"non-finite JSON number {value!r} in {path.name}")

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ExistingPdfReplayError(f"expected regular JSON artifact: {path.name}")
            if before.st_size > max_bytes:
                raise ExistingPdfReplayError(f"JSON artifact exceeds its size limit: {path.name}")
            encoded = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(encoded) > max_bytes:
            raise ExistingPdfReplayError(f"JSON artifact exceeds its size limit: {path.name}")
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_identity != after_identity or len(encoded) != before.st_size:
            raise ExistingPdfReplayError(f"JSON artifact changed while being read: {path.name}")
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except ExistingPdfReplayError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExistingPdfReplayError(f"cannot read {path.name}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise ExistingPdfReplayError(f"JSON root must be an object: {path.name}")
    return payload


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file_sha256_and_size(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> tuple[str, int]:
    """Hash one stable regular file through a no-follow descriptor."""

    descriptor: int | None = None
    digest = sha256()
    observed_size = 0
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ExistingPdfReplayError(f"{label} must remain a regular file")
            if before.st_size > max_bytes:
                raise ExistingPdfReplayError(f"{label} exceeds its size limit")
            while chunk := stream.read(min(1024 * 1024, max_bytes + 1 - observed_size)):
                observed_size += len(chunk)
                if observed_size > max_bytes:
                    raise ExistingPdfReplayError(f"{label} exceeds its size limit")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_identity != after_identity or observed_size != before.st_size:
            raise ExistingPdfReplayError(f"{label} changed while it was being verified")
    except ExistingPdfReplayError:
        raise
    except OSError as error:
        raise ExistingPdfReplayError(f"cannot verify {label}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest(), observed_size


def _stage_regular_sidecar(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
    label: str,
) -> tuple[str, int]:
    """Copy one stable, non-symlink sidecar into the private replay root."""
    identity = _regular_file_identity(source)
    if identity[2] < 1 or identity[2] > max_bytes:
        raise ExistingPdfReplayError(f"{label} exceeds its size limit")
    input_descriptor: int | None = None
    output_descriptor: int | None = None
    digest = sha256()
    copied = 0
    try:
        input_descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        output_descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(output_descriptor, 0o600)
        with os.fdopen(input_descriptor, "rb") as input_stream, os.fdopen(output_descriptor, "wb") as output_stream:
            input_descriptor = output_descriptor = None
            before = os.fstat(input_stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ExistingPdfReplayError(f"{label} must be a regular file")
            while chunk := input_stream.read(1024 * 1024):
                copied += len(chunk)
                if copied > max_bytes:
                    raise ExistingPdfReplayError(f"{label} exceeds its size limit")
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            after = os.fstat(input_stream.fileno())
    except OSError as error:
        raise ExistingPdfReplayError(f"cannot stage {label}: {error}") from error
    finally:
        if input_descriptor is not None:
            os.close(input_descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
    observed = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    final = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if observed != identity or final != identity or copied != identity[2]:
        raise ExistingPdfReplayError(f"{label} changed while being staged")
    return digest.hexdigest(), copied


def _require_relative_artifact_references(
    common_ir: Mapping[str, Any],
    *,
    allowed_raw_artifact_ids: Sequence[str] = (NATIVE_NAME,),
) -> None:
    document = common_ir.get("document")
    if not isinstance(document, Mapping):
        raise ExistingPdfReplayError("Common IR document object is missing")
    provenance = document.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("source_location") != SOURCE_NAME:
        raise ExistingPdfReplayError("Common IR source_location must be the relative source.pdf path")
    raw_ids = document.get("raw_artifact_ids")
    if raw_ids != list(allowed_raw_artifact_ids):
        raise ExistingPdfReplayError("Common IR raw_artifact_ids do not match staged artifacts")
    for value in [provenance.get("source_location"), *raw_ids]:
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise ExistingPdfReplayError("Common IR contains an unsafe artifact path")


def _validate_common_ir_output(
    common_ir: Mapping[str, Any],
    *,
    notice_id: str,
    source_sha256: str,
    page_count: int,
    pinned_inspector_version: str,
    allowed_raw_artifact_ids: Sequence[str] = (NATIVE_NAME,),
) -> None:
    """Re-check adapter output before it becomes a published artifact."""

    try:
        from common_ir_pipeline.schema import validation_errors
    except ImportError as error:  # pragma: no cover - packaging failure in the real environment.
        raise ExistingPdfReplayError("Common IR schema validator is unavailable") from error

    errors = validation_errors(dict(common_ir))
    if errors:
        raise ExistingPdfReplayError("adapter output does not satisfy the Common IR schema")
    if common_ir.get("schema_version") != "common_ir_v1":
        raise ExistingPdfReplayError("adapter output has an unexpected Common IR schema version")
    document = common_ir.get("document")
    if not isinstance(document, Mapping):
        raise ExistingPdfReplayError("adapter output is missing its document contract")
    expected_document = {
        "document_id": f"pdf:{notice_id}",
        "source_kind": "pdf",
        "artifact_role": "production",
        "page_count": page_count,
    }
    for key, expected in expected_document.items():
        if document.get(key) != expected:
            raise ExistingPdfReplayError(f"adapter output document.{key} is not source-bound")
    provenance = document.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ExistingPdfReplayError("adapter output provenance is missing")
    expected_provenance = {
        "source_sha256": source_sha256,
        "method": "pdf_native_only",
        "parser": "pdf_inspector",
        "parser_version": pinned_inspector_version,
        "generator": "common_ir_v1_adapters",
        "generator_version": "1.1.0",
        "schema_version": "common_ir_v1",
    }
    for key, expected in expected_provenance.items():
        if provenance.get(key) != expected:
            raise ExistingPdfReplayError(f"adapter output provenance.{key} is not source-bound")
    _require_relative_artifact_references(common_ir, allowed_raw_artifact_ids=allowed_raw_artifact_ids)


def _build_manifest(
    *,
    notice_id: str,
    source_sha256: str,
    source_size: int,
    native: Mapping[str, Any],
    common_ir: Mapping[str, Any],
    staging: Path,
    capture_limits: Mapping[str, Any],
    pinned_inspector_version: str,
    render_manifest: Mapping[str, Any] | None = None,
    surya_layout_artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    document = common_ir["document"]
    provenance = document["provenance"]
    native_pages = sorted(
        {
            item.get("page")
            for item in native.get("text_items", [])
            if isinstance(item, Mapping) and isinstance(item.get("page"), int)
        }
    )
    common_ir_pages = sorted(
        {
            block.get("page")
            for block in common_ir.get("blocks", [])
            if isinstance(block, Mapping) and isinstance(block.get("page"), int)
        }
    )
    native_path = staging / NATIVE_NAME
    common_ir_path = staging / COMMON_IR_NAME
    artifacts: dict[str, Any] = {
        "source_pdf": {
            "path": SOURCE_NAME,
            "sha256": source_sha256,
            "size_bytes": source_size,
        },
        "native_capture": {
            "path": NATIVE_NAME,
            "sha256": _sha256_file(native_path),
            "size_bytes": native_path.stat().st_size,
            "method": native.get("method"),
            "version": native.get("version"),
            "page_count": native.get("process_result", {}).get("page_count"),
        },
        "common_ir": {
            "path": COMMON_IR_NAME,
            "sha256": _sha256_file(common_ir_path),
            "size_bytes": common_ir_path.stat().st_size,
            "schema_version": common_ir.get("schema_version"),
            "generator": provenance.get("generator"),
            "generator_version": provenance.get("generator_version"),
            "page_count": document.get("page_count"),
        },
    }
    if render_manifest is not None:
        render_path = staging / RENDER_MANIFEST_NAME
        artifacts["render_manifest"] = {
            "path": RENDER_MANIFEST_NAME,
            "sha256": _sha256_file(render_path),
            "size_bytes": render_path.stat().st_size,
            "schema_version": render_manifest.get("schema_version"),
            "source_pdf_sha256": render_manifest.get("source_pdf_sha256"),
            "page_count": render_manifest.get("page_count"),
        }
    if surya_layout_artifact is not None:
        artifact_path = staging / SURYA_LAYOUT_ARTIFACT_NAME
        artifacts["surya_layout_artifact"] = {
            "path": SURYA_LAYOUT_ARTIFACT_NAME,
            "sha256": _sha256_file(artifact_path),
            "size_bytes": artifact_path.stat().st_size,
            "schema_version": surya_layout_artifact.get("schema_version"),
            "source_sha256": surya_layout_artifact.get("source_sha256"),
            "render_manifest_sha256": surya_layout_artifact.get("render_manifest_sha256"),
            "logical_compute_key": surya_layout_artifact.get("logical_compute_key"),
            "producer": surya_layout_artifact.get("producer"),
            "requested_pages": surya_layout_artifact.get("requested_pages"),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "existing_kb_offline_only",
        "notice_id": notice_id,
        "whole_document": True,
        "artifacts": artifacts,
        "pipeline": {
            "capture_module": CAPTURE_MODULE,
            "adapter_module": ADAPTER_MODULE,
            "pdf_inspector_version": pinned_inspector_version,
            "capture_limits": dict(capture_limits),
        },
        "coverage": {
            "document_page_count": document.get("page_count"),
            "native_text_item_count": len(native.get("text_items", [])),
            "native_text_pages": native_pages,
            "common_ir_block_count": len(common_ir.get("blocks", [])),
            "common_ir_pages": common_ir_pages,
        },
    }


def _capture_limits_manifest(capture_api: ModuleType) -> tuple[Any, dict[str, Any]]:
    limits = capture_api.DEFAULT_LIMITS
    if dataclasses.is_dataclass(limits):
        payload = dataclasses.asdict(limits)
    elif isinstance(limits, Mapping):
        payload = dict(limits)
    else:
        raise ExistingPdfReplayError("native capture limits are not manifest-serializable")
    if not all(isinstance(key, str) for key in payload):
        raise ExistingPdfReplayError("native capture limit names must be strings")
    max_source_bytes = payload.get("max_source_bytes")
    if isinstance(max_source_bytes, bool) or not isinstance(max_source_bytes, int) or max_source_bytes < 1:
        raise ExistingPdfReplayError("native capture max_source_bytes must be a positive integer")
    return limits, payload


def _capture_api_call(capture_api: ModuleType, operation: str, function, *args, **kwargs):
    error_type = getattr(capture_api, "NativeCaptureError", ValueError)
    try:
        return function(*args, **kwargs)
    except error_type as error:
        raise ExistingPdfReplayError(f"native capture {operation} failed: {error}") from error


def _publish(
    staging: Path,
    output_directory: Path,
    *,
    published_names: Sequence[str] = PUBLISHED_NAMES,
) -> None:
    if output_directory.exists() or output_directory.is_symlink():
        raise ExistingPdfReplayError("output directory already exists; replay never overwrites")
    output_descriptor: int | None = None
    staging_descriptor: int | None = None
    try:
        output_directory.mkdir(mode=0o700)
        reserved = output_directory.lstat()
        output_descriptor = os.open(
            output_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(output_descriptor)
        if (reserved.st_dev, reserved.st_ino) != (opened.st_dev, opened.st_ino):
            raise ExistingPdfReplayError("reserved output directory was replaced")
        os.fchmod(output_descriptor, 0o700)
        staging_descriptor = os.open(
            staging,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except Exception as error:
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
        if isinstance(error, ExistingPdfReplayError):
            raise
        raise ExistingPdfReplayError(f"cannot reserve output directory: {error}") from error
    try:
        # manifest.json is intentionally last: a partial publication is never
        # mistaken for a terminal successful replay.
        for name in published_names:
            source_info = os.stat(name, dir_fd=staging_descriptor, follow_symlinks=False)
            if not stat.S_ISREG(source_info.st_mode):
                raise ExistingPdfReplayError(f"staged artifact is missing or unsafe: {name}")
            # hard-link creation is the POSIX no-replace primitive for these
            # regular artifacts: EEXIST fails atomically instead of replacing
            # a concurrently created destination.
            os.link(
                name,
                name,
                src_dir_fd=staging_descriptor,
                dst_dir_fd=output_descriptor,
                follow_symlinks=False,
            )
            artifact_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=output_descriptor,
            )
            try:
                artifact_info = os.fstat(artifact_descriptor)
                if not stat.S_ISREG(artifact_info.st_mode):
                    raise ExistingPdfReplayError(f"published artifact is not regular: {name}")
                os.fchmod(artifact_descriptor, 0o600)
            finally:
                os.close(artifact_descriptor)
            os.unlink(name, dir_fd=staging_descriptor)
        published = output_directory.lstat()
        if (published.st_dev, published.st_ino) != (opened.st_dev, opened.st_ino):
            raise ExistingPdfReplayError("published output directory was replaced")
    except OSError as error:
        # Never recursively delete the pathname here: an attacker could swap
        # it after reservation. A partial 0700 directory is left without the
        # terminal manifest and requires explicit operator inspection.
        raise ExistingPdfReplayError(f"cannot publish replay artifacts: {error}") from error
    finally:
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)


def replay_existing_pdf(
    *,
    notice_id: str,
    source_pdf: Path,
    output_directory: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    surya_layout_artifact: Path | None = None,
    render_manifest: Path | None = None,
) -> dict[str, Any]:
    """Build and publish one closed, deterministic Existing-PDF replay directory."""

    if not NOTICE_ID_PATTERN.fullmatch(notice_id):
        raise ExistingPdfReplayError("notice-id has an unsupported format")
    if not (0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise ExistingPdfReplayError(
            f"timeout-seconds must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g}"
        )
    if (surya_layout_artifact is None) != (render_manifest is None):
        raise ExistingPdfReplayError("--surya-layout-artifact and --render-manifest must be provided together")
    output_directory = output_directory.expanduser()
    if output_directory.exists() or output_directory.is_symlink():
        raise ExistingPdfReplayError("output directory already exists; replay never overwrites")
    parent = output_directory.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ExistingPdfReplayError("output parent must be an existing non-symlink directory")
    parent_info = parent.stat(follow_symlinks=False)
    if parent_info.st_mode & 0o022 and not parent_info.st_mode & stat.S_ISVTX:
        raise ExistingPdfReplayError(
            "output parent must not be group/world writable unless it has the sticky bit"
        )
    environment = _sanitized_environment()
    capture_api = _load_native_capture_api()
    capture_limits, capture_limits_manifest = _capture_limits_manifest(capture_api)
    try:
        staging = Path(tempfile.mkdtemp(prefix=".existing-pdf-replay-", dir=parent))
    except OSError as error:
        raise ExistingPdfReplayError(f"cannot create private replay directory: {error}") from error
    try:
        source_sha256, source_size = _copy_source_pdf(
            source_pdf.expanduser(),
            staging / SOURCE_NAME,
            max_bytes=capture_limits_manifest["max_source_bytes"],
        )
        staged_render_manifest: dict[str, Any] | None = None
        staged_surya_layout_artifact: dict[str, Any] | None = None
        staged_raw_artifact_ids = [NATIVE_NAME]
        published_names: tuple[str, ...] = PUBLISHED_NAMES
        if surya_layout_artifact is not None and render_manifest is not None:
            _stage_regular_sidecar(
                render_manifest.expanduser(), staging / RENDER_MANIFEST_NAME,
                max_bytes=MAX_SURYA_LAYOUT_ARTIFACT_BYTES, label="render manifest",
            )
            _stage_regular_sidecar(
                surya_layout_artifact.expanduser(), staging / SURYA_LAYOUT_ARTIFACT_NAME,
                max_bytes=MAX_SURYA_LAYOUT_ARTIFACT_BYTES, label="Surya layout artifact",
            )
            staged_render_manifest = _json_object(
                staging / RENDER_MANIFEST_NAME, max_bytes=MAX_SURYA_LAYOUT_ARTIFACT_BYTES,
            )
            staged_surya_layout_artifact = _json_object(
                staging / SURYA_LAYOUT_ARTIFACT_NAME, max_bytes=MAX_SURYA_LAYOUT_ARTIFACT_BYTES,
            )
            staged_raw_artifact_ids.extend((RENDER_MANIFEST_NAME, SURYA_LAYOUT_ARTIFACT_NAME))
            published_names = (
                SOURCE_NAME, NATIVE_NAME, RENDER_MANIFEST_NAME,
                SURYA_LAYOUT_ARTIFACT_NAME, COMMON_IR_NAME, MANIFEST_NAME,
            )
        _run_command(
            (
                sys.executable,
                "-m",
                CAPTURE_MODULE,
                "--notice-id",
                notice_id,
                "--pdf",
                SOURCE_NAME,
                "--source-relative-path",
                SOURCE_NAME,
                "--output",
                NATIVE_NAME,
            ),
            cwd=staging,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        native = _json_object(
            staging / NATIVE_NAME,
            max_bytes=capture_limits_manifest["max_capture_bytes"],
        )
        native = _capture_api_call(
            capture_api,
            "validation",
            capture_api.validate_native_capture,
            native,
            source_pdf=staging / SOURCE_NAME,
            expected_notice_id=notice_id,
            expected_source_relative_path=SOURCE_NAME,
            limits=capture_limits,
        )
        native_bytes = _capture_api_call(
            capture_api,
            "serialization",
            capture_api.canonical_json_bytes,
            native,
        )
        _replace_with_canonical_file(staging / NATIVE_NAME, native_bytes)

        adapter_command = [
            sys.executable,
            "-m",
            ADAPTER_MODULE,
            "--notice-id",
            notice_id,
            "--native",
            NATIVE_NAME,
            "--source-path",
            SOURCE_NAME,
            "--source-sha256",
            source_sha256,
            "--output",
            COMMON_IR_NAME,
        ]
        if staged_surya_layout_artifact is not None:
            adapter_command.extend((
                "--render-manifest", RENDER_MANIFEST_NAME,
                "--surya-layout-artifact", SURYA_LAYOUT_ARTIFACT_NAME,
            ))
        _run_command(
            tuple(adapter_command),
            cwd=staging,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        observed_source = _regular_file_sha256_and_size(
            staging / SOURCE_NAME,
            max_bytes=capture_limits_manifest["max_source_bytes"],
            label="staged source PDF",
        )
        if observed_source != (source_sha256, source_size):
            raise ExistingPdfReplayError("staged source PDF changed after the adapter ran")
        observed_native = _regular_file_sha256_and_size(
            staging / NATIVE_NAME,
            max_bytes=capture_limits_manifest["max_capture_bytes"],
            label="canonical native capture",
        )
        expected_native = (sha256(native_bytes).hexdigest(), len(native_bytes))
        if observed_native != expected_native:
            raise ExistingPdfReplayError("canonical native capture changed after the adapter ran")
        common_ir = _json_object(
            staging / COMMON_IR_NAME,
            max_bytes=CHILD_FILE_SIZE_BYTES,
        )
        _validate_common_ir_output(
            common_ir,
            notice_id=notice_id,
            source_sha256=source_sha256,
            page_count=native["process_result"]["page_count"],
            pinned_inspector_version=capture_api.PINNED_PDF_INSPECTOR_VERSION,
            allowed_raw_artifact_ids=staged_raw_artifact_ids,
        )
        common_ir_bytes = _capture_api_call(
            capture_api,
            "serialization",
            capture_api.canonical_json_bytes,
            common_ir,
        )
        _replace_with_canonical_file(staging / COMMON_IR_NAME, common_ir_bytes)

        manifest = _build_manifest(
            notice_id=notice_id,
            source_sha256=source_sha256,
            source_size=source_size,
            native=native,
            common_ir=common_ir,
            staging=staging,
            capture_limits=capture_limits_manifest,
            pinned_inspector_version=capture_api.PINNED_PDF_INSPECTOR_VERSION,
            render_manifest=staged_render_manifest,
            surya_layout_artifact=staged_surya_layout_artifact,
        )
        manifest_bytes = _capture_api_call(
            capture_api,
            "serialization",
            capture_api.canonical_json_bytes,
            manifest,
        )
        _write_exclusive_file(staging / MANIFEST_NAME, manifest_bytes)
        _publish(staging, output_directory, published_names=published_names)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--pdf", type=Path, required=True, help="one complete native-text PDF")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--render-manifest",
        type=Path,
        help="canonical trusted render manifest; required with --surya-layout-artifact",
    )
    parser.add_argument(
        "--surya-layout-artifact",
        type=Path,
        help="optional canonical textless surya_layout_artifact/v1 sidecar",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-subprocess deadline (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        manifest = replay_existing_pdf(
            notice_id=args.notice_id,
            source_pdf=args.pdf,
            output_directory=args.output_dir,
            timeout_seconds=args.timeout_seconds,
            render_manifest=args.render_manifest,
            surya_layout_artifact=args.surya_layout_artifact,
        )
    except ExistingPdfReplayError as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "notice_id": manifest["notice_id"],
                "artifacts": [entry["path"] for entry in manifest["artifacts"].values()],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
