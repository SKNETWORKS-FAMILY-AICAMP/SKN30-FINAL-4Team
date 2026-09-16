#!/usr/bin/env python3
"""Render an Existing-KB PDF into a verified, portable page-image bundle.

The renderer itself runs in a private staging directory.  This wrapper owns
the process boundary and the publication boundary: no renderer output is
published until the parent has re-read the terminal manifest, re-validated
every source/page binding, and made parent-owned no-replace copies.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from hashlib import sha256
from types import ModuleType, SimpleNamespace
from typing import Any, Mapping, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - supported hosts are Linux/WSL.
    resource = None  # type: ignore[assignment]


SOURCE_NAME = "source.pdf"
RENDERED_NAME = "rendered"
MANIFEST_NAME = "render_manifest.json"
RENDERER_MODULE = "common_ir_pipeline.workers.pdfium_renderer"
DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3600.0
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_RENDER_BYTES = 512 * 1024 * 1024
CHILD_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024
CHILD_FILE_SIZE_BYTES = 256 * 1024 * 1024
CHILD_OPEN_FILES = 128
PRLIMIT_PATH = Path("/usr/bin/prlimit")
_INHERITED_ENVIRONMENT = frozenset({
    "LANG", "LC_ALL", "LC_CTYPE", "PATH", "SYSTEMROOT", "TZ", "WINDIR",
})


class ExistingPdfRenderError(RuntimeError):
    """The offline renderer cannot safely produce a complete artifact set."""


def _load_renderer_api() -> ModuleType:
    try:
        from common_ir_pipeline.pdf_fusion import render_manifest
        from common_ir_pipeline.workers import pdfium_renderer
    except ImportError as error:  # pragma: no cover - depends on install state.
        raise ExistingPdfRenderError(
            "common_ir_pipeline PDF renderer is unavailable; install the pinned package extras"
        ) from error
    # Keep a small module-shaped interface for test substitution while using
    # the renderer's limits and the independent manifest verifier.
    return SimpleNamespace(
        DEFAULT_LIMITS=pdfium_renderer.DEFAULT_LIMITS,
        validate_render_manifest_files=render_manifest.validate_render_manifest_files,
    )


def _sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    supplied = os.environ if source is None else source
    result = {key: supplied[key] for key in sorted(_INHERITED_ENVIRONMENT) if supplied.get(key)}
    result.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    })
    return result


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _resource_limited_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
) -> list[str]:
    if resource is None or os.name != "posix":
        raise ExistingPdfRenderError("offline PDF rendering requires POSIX resource limits (Linux/WSL)")
    try:
        prlimit_stat = PRLIMIT_PATH.stat()
    except OSError as error:
        raise ExistingPdfRenderError("offline PDF rendering requires /usr/bin/prlimit") from error
    if not stat.S_ISREG(prlimit_stat.st_mode) or not os.access(PRLIMIT_PATH, os.X_OK):
        raise ExistingPdfRenderError("offline PDF rendering requires executable /usr/bin/prlimit")
    cpu_seconds = max(1, math.ceil(timeout_seconds) + 5)

    def bounded(name: str, requested: int) -> int:
        limit = getattr(resource, name, None)
        if limit is None:
            raise ExistingPdfRenderError(f"host does not expose required {name}")
        _soft, hard = resource.getrlimit(limit)
        return requested if hard == resource.RLIM_INFINITY else min(requested, hard)

    options = (
        ("as", bounded("RLIMIT_AS", CHILD_ADDRESS_SPACE_BYTES)),
        ("data", bounded("RLIMIT_DATA", CHILD_ADDRESS_SPACE_BYTES)),
        ("fsize", bounded("RLIMIT_FSIZE", CHILD_FILE_SIZE_BYTES)),
        ("cpu", bounded("RLIMIT_CPU", cpu_seconds)),
        ("nofile", bounded("RLIMIT_NOFILE", CHILD_OPEN_FILES)),
        ("core", bounded("RLIMIT_CORE", 0)),
    )
    limited = [str(PRLIMIT_PATH)]
    limited.extend(f"--{name}={value}:{value}" for name, value in options)
    limited.append("--")
    limited.extend(command)
    return limited


def _kill_and_reap(process: subprocess.Popen[str]) -> None:
    _kill_process_group(process)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _run_command(
    command: Sequence[str], *, cwd: Path, timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    if not command or any(not isinstance(value, str) or not value for value in command):
        raise ExistingPdfRenderError("subprocess command must be a non-empty string argv")
    limited_command = _resource_limited_command(command, timeout_seconds=timeout_seconds)
    try:
        process = subprocess.Popen(
            limited_command, cwd=cwd, env=dict(environment or _sanitized_environment()), shell=False,
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExistingPdfRenderError(f"cannot start PDF renderer subprocess: {error}") from error
    try:
        process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _kill_and_reap(process)
        raise ExistingPdfRenderError(f"PDF renderer subprocess exceeded {timeout_seconds:g} seconds") from error
    except Exception as error:
        _kill_and_reap(process)
        raise ExistingPdfRenderError(f"PDF renderer subprocess failed while waiting: {error}") from error
    _kill_process_group(process)  # no successful child may retain the staging directory.
    if process.returncode != 0:
        raise ExistingPdfRenderError(f"PDF renderer subprocess exited with {process.returncode}")
    return "", ""


def _identity(path: Path, *, label: str) -> tuple[int, int, int, int, int]:
    try:
        value = path.lstat()
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot inspect {label}: {error}") from error
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise ExistingPdfRenderError(f"{label} must be a regular non-symlink file")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _copy_source_pdf(source: Path, destination: Path, *, max_bytes: int) -> tuple[str, int]:
    original = _identity(source, label="input PDF")
    if source.suffix.casefold() != ".pdf":
        raise ExistingPdfRenderError("input source must have a .pdf extension")
    if original[2] > max_bytes:
        raise ExistingPdfRenderError("input PDF exceeds the renderer size limit")
    read_fd: int | None = None
    write_fd: int | None = None
    digest = sha256()
    copied = 0
    try:
        read_fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        write_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb") as writer:
            read_fd = write_fd = None
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ExistingPdfRenderError("input PDF must remain a regular file")
            header = reader.read(5)
            if header != b"%PDF-":
                raise ExistingPdfRenderError("input does not have a PDF file signature")
            writer.write(header)
            digest.update(header)
            copied = len(header)
            while chunk := reader.read(1024 * 1024):
                copied += len(chunk)
                if copied > max_bytes:
                    raise ExistingPdfRenderError("input PDF exceeds the renderer size limit")
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            os.fchmod(writer.fileno(), 0o600)
            after = os.fstat(reader.fileno())
    except ExistingPdfRenderError:
        raise
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot stage input PDF: {error}") from error
    finally:
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
    observed_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    observed_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if original != observed_before or original != observed_after:
        raise ExistingPdfRenderError("input PDF changed while it was being staged")
    return digest.hexdigest(), copied


def _json_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ExistingPdfRenderError(f"duplicate JSON key {key!r} in {path.name}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ExistingPdfRenderError(f"non-finite JSON number {value!r} in {path.name}")

    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            fd = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ExistingPdfRenderError(f"expected regular JSON artifact: {path.name}")
            if before.st_size > max_bytes:
                raise ExistingPdfRenderError(f"JSON artifact exceeds its size limit: {path.name}")
            encoded = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(encoded) > max_bytes or len(encoded) != before.st_size:
            raise ExistingPdfRenderError(f"JSON artifact exceeds its size limit: {path.name}")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ExistingPdfRenderError(f"JSON artifact changed while being read: {path.name}")
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant)
    except ExistingPdfRenderError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ExistingPdfRenderError(f"cannot read {path.name}: {error}") from error
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(value, dict):
        raise ExistingPdfRenderError(f"JSON root must be an object: {path.name}")
    return value


def _replace_with_canonical_file(path: Path, data: bytes) -> None:
    """Atomically replace child JSON after validation in the private stage."""
    temporary = path.with_name(f".{path.name}.canonical")
    fd: int | None = None
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
        os.replace(temporary, path)
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot canonicalize render manifest: {error}") from error
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _regular_sha256_and_size(path: Path, *, max_bytes: int, label: str) -> tuple[str, int]:
    fd: int | None = None
    digest = sha256()
    total = 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            fd = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise ExistingPdfRenderError(f"{label} must remain a bounded regular file")
            while chunk := stream.read(min(1024 * 1024, max_bytes + 1 - total)):
                total += len(chunk)
                if total > max_bytes:
                    raise ExistingPdfRenderError(f"{label} exceeds its size limit")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if total != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ExistingPdfRenderError(f"{label} changed while it was being verified")
    except ExistingPdfRenderError:
        raise
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot verify {label}: {error}") from error
    finally:
        if fd is not None:
            os.close(fd)
    return digest.hexdigest(), total


def _safe_relative_path(value: Any, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ExistingPdfRenderError(f"{label} must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ExistingPdfRenderError(f"{label} must be a safe relative POSIX path")
    return path


def _validate_manifest(
    manifest: Mapping[str, Any], *, staging: Path, source_sha256: str, source_size: int,
    renderer_api: ModuleType,
) -> Any:
    if manifest.get("source_pdf_relative_path") != SOURCE_NAME:
        raise ExistingPdfRenderError("render manifest source must be source.pdf")
    if manifest.get("source_pdf_sha256") != source_sha256 or manifest.get("source_pdf_size_bytes") != source_size:
        raise ExistingPdfRenderError("render manifest source binding does not match staged PDF")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ExistingPdfRenderError("render manifest must contain rendered pages")
    limits = renderer_api.DEFAULT_LIMITS

    def positive_limit(name: str) -> int:
        value = getattr(limits, name, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ExistingPdfRenderError(f"renderer {name} limit is invalid")
        return value

    max_pages = positive_limit("max_pages")
    max_png_file_bytes = positive_limit("max_png_file_bytes")
    max_png_dimension_px = positive_limit("max_png_dimension_px")
    max_png_total_pixels = positive_limit("max_png_total_pixels")
    max_document_total_pixels = positive_limit("max_document_total_pixels")
    if len(pages) > max_pages:
        raise ExistingPdfRenderError("render manifest exceeds the document page limit")
    total_image_bytes = 0
    total_pixels = 0
    for page in pages:
        if not isinstance(page, Mapping):
            raise ExistingPdfRenderError("render manifest pages must be objects")
        relative = _safe_relative_path(page.get("image_relative_path"), label="page image path")
        if relative.parts[0] != RENDERED_NAME or relative.suffix != ".png":
            raise ExistingPdfRenderError("render manifest page image must be below rendered/ with .png extension")
        image_size = page.get("image_size_bytes")
        if (
            isinstance(image_size, bool)
            or not isinstance(image_size, int)
            or not 0 < image_size <= max_png_file_bytes
        ):
            raise ExistingPdfRenderError("render manifest page image size exceeds its safety cap")
        coordinate = page.get("coordinate_manifest")
        if not isinstance(coordinate, Mapping):
            raise ExistingPdfRenderError("render manifest coordinate manifest must be an object")
        width = coordinate.get("rendered_width_px")
        height = coordinate.get("rendered_height_px")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (width, height)
        ):
            raise ExistingPdfRenderError("render manifest page dimensions must be positive integers")
        assert isinstance(width, int) and isinstance(height, int)
        pixels = width * height
        if (
            width > max_png_dimension_px
            or height > max_png_dimension_px
            or pixels > max_png_total_pixels
        ):
            raise ExistingPdfRenderError("render manifest page dimensions exceed their safety cap")
        total_image_bytes += image_size
        total_pixels += pixels
        if total_image_bytes > MAX_DOCUMENT_RENDER_BYTES:
            raise ExistingPdfRenderError("render manifest exceeds the document image-byte limit")
        if total_pixels > max_document_total_pixels:
            raise ExistingPdfRenderError("render manifest exceeds the document pixel limit")
    try:
        return renderer_api.validate_render_manifest_files(manifest, artifact_root=staging)
    except ExistingPdfRenderError:
        raise
    except Exception as error:
        raise ExistingPdfRenderError(f"render manifest validation failed: {error}") from error


def _mkdirat_fd(parent_fd: int, name: str, *, label: str) -> int:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ExistingPdfRenderError(f"{label} must have one safe path component")
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        reserved = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(fd)
        if (reserved.st_dev, reserved.st_ino) != (opened.st_dev, opened.st_ino):
            os.close(fd)
            raise ExistingPdfRenderError(f"{label} was replaced")
        os.fchmod(fd, 0o700)
        return fd
    except ExistingPdfRenderError:
        raise
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot reserve {label}: {error}") from error


def _copy_regular_verified(
    source_name: str,
    *,
    source_fd: int,
    destination_name: str,
    destination_fd: int,
    expected_sha256: str,
    expected_size: int,
    label: str,
) -> None:
    """Copy a fixed staged inode into a new no-replace publication inode."""
    read_fd: int | None = None
    write_fd: int | None = None
    try:
        read_fd = os.open(
            source_name,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_fd,
        )
        before = os.fstat(read_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise ExistingPdfRenderError(f"{label} is missing, unsafe, or changed before publication")
        write_fd = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=destination_fd,
        )
        digest = sha256()
        copied = 0
        while True:
            chunk = os.read(read_fd, min(1024 * 1024, expected_size + 1 - copied))
            if not chunk:
                break
            copied += len(chunk)
            if copied > expected_size:
                raise ExistingPdfRenderError(f"{label} grew during publication")
            digest.update(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(write_fd, remaining)
                if written < 1:
                    raise ExistingPdfRenderError(f"cannot publish {label}")
                remaining = remaining[written:]
        after = os.fstat(read_fd)
        before_identity = (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        )
        if (
            copied != expected_size
            or digest.hexdigest() != expected_sha256
            or before_identity != after_identity
        ):
            raise ExistingPdfRenderError(f"{label} changed during publication")
        os.fsync(write_fd)
        os.fchmod(write_fd, 0o600)
        published = os.fstat(write_fd)
        if not stat.S_ISREG(published.st_mode) or published.st_size != expected_size:
            raise ExistingPdfRenderError(f"published {label} failed its final size check")
    except ExistingPdfRenderError:
        raise
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot publish {label}: {error}") from error
    finally:
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def _verify_published_file(
    name: str,
    *,
    directory_fd: int,
    expected_sha256: str,
    expected_size: int,
    label: str,
) -> None:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                raise ExistingPdfRenderError(f"published {label} is missing, unsafe, or changed")
            digest = sha256()
            size = 0
            while chunk := stream.read(min(1024 * 1024, expected_size + 1 - size)):
                size += len(chunk)
                if size > expected_size:
                    raise ExistingPdfRenderError(f"published {label} grew after publication")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except ExistingPdfRenderError:
        raise
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot verify published {label}: {error}") from error
    if (
        size != expected_size
        or digest.hexdigest() != expected_sha256
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise ExistingPdfRenderError(f"published {label} changed after publication")


def _assert_directory_binding(
    name: str,
    *,
    parent_fd: int,
    directory_fd: int,
    label: str,
) -> None:
    try:
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        held = os.fstat(directory_fd)
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot verify {label}: {error}") from error
    if (
        not stat.S_ISDIR(visible.st_mode)
        or (visible.st_dev, visible.st_ino) != (held.st_dev, held.st_ino)
    ):
        raise ExistingPdfRenderError(f"{label} was replaced")


def _publish(
    staging: Path,
    output_directory: Path,
    manifest: Mapping[str, Any],
    *,
    canonical_manifest: bytes,
) -> None:
    if output_directory.exists() or output_directory.is_symlink():
        raise ExistingPdfRenderError("output directory already exists; renderer never overwrites")
    parent_fd = output_fd = staging_fd = rendered_output_fd = rendered_staging_fd = None
    try:
        parent = output_directory.parent
        parent_before = parent.lstat()
        parent_fd = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_opened = os.fstat(parent_fd)
        if (parent_before.st_dev, parent_before.st_ino) != (parent_opened.st_dev, parent_opened.st_ino):
            raise ExistingPdfRenderError("output parent was replaced")
        output_fd = _mkdirat_fd(parent_fd, output_directory.name, label="output directory")
        staging_fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        _copy_regular_verified(
            SOURCE_NAME,
            source_fd=staging_fd,
            destination_name=SOURCE_NAME,
            destination_fd=output_fd,
            expected_sha256=manifest["source_pdf_sha256"],
            expected_size=manifest["source_pdf_size_bytes"],
            label="source PDF",
        )
        rendered_output_fd = _mkdirat_fd(
            output_fd,
            RENDERED_NAME,
            label="published rendered directory",
        )
        rendered_staging_fd = os.open(
            RENDERED_NAME,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=staging_fd,
        )
        for page in manifest["pages"]:
            relative = _safe_relative_path(page["image_relative_path"], label="page image path")
            if len(relative.parts) != 2:
                raise ExistingPdfRenderError("rendered page path must be exactly rendered/<file>.png")
            _copy_regular_verified(
                relative.name,
                source_fd=rendered_staging_fd,
                destination_name=relative.name,
                destination_fd=rendered_output_fd,
                expected_sha256=page["image_sha256"],
                expected_size=page["image_size_bytes"],
                label=f"rendered page {page['page']}",
            )
        _verify_published_file(
            SOURCE_NAME,
            directory_fd=output_fd,
            expected_sha256=manifest["source_pdf_sha256"],
            expected_size=manifest["source_pdf_size_bytes"],
            label="source PDF",
        )
        for page in manifest["pages"]:
            relative = _safe_relative_path(page["image_relative_path"], label="page image path")
            _verify_published_file(
                relative.name,
                directory_fd=rendered_output_fd,
                expected_sha256=page["image_sha256"],
                expected_size=page["image_size_bytes"],
                label=f"rendered page {page['page']}",
            )
        # Make all non-terminal files and directory entries durable before
        # publishing the success marker. Operations stay on held dirfds so a
        # rename/recreate race cannot split one logical bundle.
        os.fsync(rendered_output_fd)
        os.fsync(output_fd)
        _assert_directory_binding(
            output_directory.name,
            parent_fd=parent_fd,
            directory_fd=output_fd,
            label="published output directory",
        )
        _assert_directory_binding(
            RENDERED_NAME,
            parent_fd=output_fd,
            directory_fd=rendered_output_fd,
            label="published rendered directory",
        )
        # Terminal success marker: no caller may use a partial output without it.
        _copy_regular_verified(
            MANIFEST_NAME,
            source_fd=staging_fd,
            destination_name=MANIFEST_NAME,
            destination_fd=output_fd,
            expected_sha256=sha256(canonical_manifest).hexdigest(),
            expected_size=len(canonical_manifest),
            label="terminal render manifest",
        )
        os.fsync(output_fd)
        os.fsync(parent_fd)
        _assert_directory_binding(
            output_directory.name,
            parent_fd=parent_fd,
            directory_fd=output_fd,
            label="published output directory",
        )
        _assert_directory_binding(
            RENDERED_NAME,
            parent_fd=output_fd,
            directory_fd=rendered_output_fd,
            label="published rendered directory",
        )
    except ExistingPdfRenderError:
        raise
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot publish rendered artifacts: {error}") from error
    finally:
        for fd in (rendered_staging_fd, rendered_output_fd, staging_fd, output_fd, parent_fd):
            if fd is not None:
                os.close(fd)


def render_existing_pdf_pages(*, source_pdf: Path, output_directory: Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> Mapping[str, Any]:
    """Render one Existing PDF; output is a new root with a terminal manifest."""
    if not (0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise ExistingPdfRenderError(f"timeout-seconds must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g}")
    output_directory = output_directory.expanduser()
    if output_directory.exists() or output_directory.is_symlink():
        raise ExistingPdfRenderError("output directory already exists; renderer never overwrites")
    parent = output_directory.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ExistingPdfRenderError("output parent must be an existing non-symlink directory")
    parent_mode = parent.stat(follow_symlinks=False).st_mode
    if parent_mode & 0o022 and not parent_mode & stat.S_ISVTX:
        raise ExistingPdfRenderError("output parent must not be group/world writable unless it has the sticky bit")
    renderer_api = _load_renderer_api()
    limits = renderer_api.DEFAULT_LIMITS
    max_source_bytes = getattr(limits, "max_source_bytes", None)
    if isinstance(max_source_bytes, bool) or not isinstance(max_source_bytes, int) or max_source_bytes < 1:
        raise ExistingPdfRenderError("renderer source-size limit is invalid")
    try:
        staging = Path(tempfile.mkdtemp(prefix=".existing-pdf-render-", dir=parent))
    except OSError as error:
        raise ExistingPdfRenderError(f"cannot create private render directory: {error}") from error
    try:
        source_sha256, source_size = _copy_source_pdf(source_pdf.expanduser(), staging / SOURCE_NAME, max_bytes=max_source_bytes)
        child_tmp = staging / "tmp"
        child_tmp.mkdir(mode=0o700)
        child_environment = _sanitized_environment()
        child_environment["TMPDIR"] = str(child_tmp)
        _run_command(
            (sys.executable, "-m", RENDERER_MODULE, "--artifact-root", str(staging), "--pdf", SOURCE_NAME),
            cwd=staging,
            timeout_seconds=timeout_seconds,
            environment=child_environment,
        )
        manifest = _json_object(staging / MANIFEST_NAME, max_bytes=MAX_MANIFEST_BYTES)
        normalized = _validate_manifest(manifest, staging=staging, source_sha256=source_sha256, source_size=source_size, renderer_api=renderer_api)
        canonical_manifest = (
            normalized.canonical_json()
            if hasattr(normalized, "canonical_json")
            else json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        )
        _replace_with_canonical_file(staging / MANIFEST_NAME, canonical_manifest)
        observed_source = _regular_sha256_and_size(staging / SOURCE_NAME, max_bytes=max_source_bytes, label="staged source PDF")
        if observed_source != (source_sha256, source_size):
            raise ExistingPdfRenderError("staged source PDF changed after rendering")
        published_manifest = normalized.to_dict() if hasattr(normalized, "to_dict") else manifest
        _publish(
            staging,
            output_directory,
            published_manifest,
            canonical_manifest=canonical_manifest,
        )
        return published_manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    try:
        manifest = render_existing_pdf_pages(source_pdf=args.pdf, output_directory=args.output_dir, timeout_seconds=args.timeout_seconds)
    except ExistingPdfRenderError as error:
        parser.error(str(error))
    print(json.dumps({"render_manifest": MANIFEST_NAME, "page_count": manifest["page_count"], "source_pdf_sha256": manifest["source_pdf_sha256"]}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
