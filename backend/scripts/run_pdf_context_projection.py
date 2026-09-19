#!/usr/bin/env python3
"""Build the evaluation-only A3 PDF context projection.

This command is intentionally *not* an authentication boundary.  Before it is
run, both input sidecars must already have passed their complete upstream
source-artifact replay validators. ``--acknowledge-upstream-replay`` requires
an operator self-declaration to run the command; it is not persisted as
authentication evidence and does not perform or replace those replays.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import stat
import sys
from typing import Any, Mapping, Sequence


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Direct script execution does not inherit the worker entrypoint's vendored
# Common IR bootstrap.
from worker import vendor as _worker_vendor  # noqa: E402,F401

from common_ir_pipeline.pdf_fusion.context_groups import (  # noqa: E402
    PdfContextGroupsError,
    build_pdf_context_groups,
    canonical_pdf_context_groups_json,
)


MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 2_000_000
MAX_JSON_STRING_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024


class PdfContextProjectionCliError(RuntimeError):
    """An operator-safe failure that never embeds artifact content."""


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _stable_regular_bytes(path: Path) -> bytes:
    """Read one bounded regular file while detecting replacement or mutation."""

    descriptor: int | None = None
    try:
        linked = os.lstat(path)
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
            raise OSError
        if not 0 < linked.st_size <= MAX_INPUT_BYTES:
            raise OSError

        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or _identity(linked) != _identity(before)
            or linked.st_size != before.st_size
            or not 0 < before.st_size <= MAX_INPUT_BYTES
        ):
            raise OSError

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, MAX_INPUT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_INPUT_BYTES:
                raise OSError

        after = os.fstat(descriptor)
        if (
            _identity(before) != _identity(after)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or total != before.st_size
        ):
            raise OSError
        return b"".join(chunks)
    except (OSError, OverflowError, ValueError):
        raise PdfContextProjectionCliError("input_file_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_json_limits(root: object) -> None:
    """Apply deterministic depth, node, and UTF-8 string bounds iteratively."""

    stack: list[tuple[object, int]] = [(root, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise PdfContextProjectionCliError("input_json_limits_exceeded")

        if isinstance(value, str):
            try:
                size = len(value.encode("utf-8", errors="strict"))
            except UnicodeEncodeError:
                raise PdfContextProjectionCliError("input_json_invalid") from None
            if size > MAX_JSON_STRING_BYTES:
                raise PdfContextProjectionCliError("input_json_limits_exceeded")
        elif isinstance(value, Mapping):
            for key, child in value.items():
                # JSON object names are strings, but count them as nodes too so
                # adversarial key-heavy objects cannot bypass the aggregate cap.
                nodes += 1
                if nodes > MAX_JSON_NODES:
                    raise PdfContextProjectionCliError("input_json_limits_exceeded")
                try:
                    key_size = len(key.encode("utf-8", errors="strict"))
                except (AttributeError, UnicodeEncodeError):
                    raise PdfContextProjectionCliError("input_json_invalid") from None
                if key_size > MAX_JSON_STRING_BYTES:
                    raise PdfContextProjectionCliError("input_json_limits_exceeded")
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise PdfContextProjectionCliError("input_json_invalid")


def _json_object(path: Path) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite")
        return parsed

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite")

    try:
        text = _stable_regular_bytes(path).decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_float=finite_float,
            parse_constant=reject_constant,
        )
    except PdfContextProjectionCliError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError, MemoryError):
        raise PdfContextProjectionCliError("input_json_invalid") from None
    if not isinstance(value, dict):
        raise PdfContextProjectionCliError("input_json_root_invalid")
    _validate_json_limits(value)
    return value


def _write_exclusive(path: Path, content: bytes) -> None:
    """Create one durable mode-0600 output below a trusted POSIX parent.

    The parent directory must be owned by this process' effective user and
    must not be group- or other-writable; its ancestor chain must likewise be
    trusted or sticky-protected. Final inode checks narrow accidental
    same-owner replacement races, but no pathname API can promise that a
    hostile process using the same uid will leave the path unchanged after
    this function returns.
    """

    if os.name != "posix" or len(content) > MAX_OUTPUT_BYTES:
        raise PdfContextProjectionCliError("output_target_invalid_or_exists")

    parent = path.parent
    parent_descriptor: int | None = None
    output_descriptor: int | None = None
    created = False
    cleanup_stat: os.stat_result | None = None
    created_stat: os.stat_result | None = None
    try:
        parent_link = os.lstat(parent)
        if stat.S_ISLNK(parent_link.st_mode) or not stat.S_ISDIR(parent_link.st_mode):
            raise OSError
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_open = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_open.st_mode)
            or _identity(parent_link) != _identity(parent_open)
            or parent_open.st_uid != os.geteuid()
            or stat.S_IMODE(parent_open.st_mode) & 0o022
        ):
            raise OSError

        # O_EXCL rejects every existing target, including symlinks and special
        # files, without first following it through a check/use race.
        output_descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        created = True
        output_stat = os.fstat(output_descriptor)
        if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_nlink != 1:
            raise OSError
        # Capture the created inode before permission hardening so failures in
        # fchmod or its follow-up fstat can still remove only our own file.
        cleanup_stat = output_stat
        os.fchmod(output_descriptor, 0o600)
        created_stat = os.fstat(output_descriptor)
        if (
            not _same_inode(output_stat, created_stat)
            or not stat.S_ISREG(created_stat.st_mode)
            or created_stat.st_nlink != 1
            or stat.S_IMODE(created_stat.st_mode) != 0o600
        ):
            raise OSError

        written = 0
        view = memoryview(content)
        while view:
            count = os.write(output_descriptor, view)
            if count < 1:
                raise OSError
            written += count
            view = view[count:]
        if written != len(content):
            raise OSError
        os.fsync(output_descriptor)
        final_stat = os.fstat(output_descriptor)
        if (
            not _same_inode(created_stat, final_stat)
            or final_stat.st_nlink != 1
            or final_stat.st_size != len(content)
            or stat.S_IMODE(final_stat.st_mode) != 0o600
        ):
            raise OSError
        # Bind the durable descriptor back to the requested directory entry.
        # A writer in a shared directory may rename the created file and put a
        # different inode at the same name while this process still owns the
        # original FD. Never report success for that swapped path.
        path_stat = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or not _same_inode(final_stat, path_stat)
            or path_stat.st_size != len(content)
            or stat.S_IMODE(path_stat.st_mode) != 0o600
        ):
            raise OSError
        final_parent_path = os.lstat(parent)
        if (
            not stat.S_ISDIR(final_parent_path.st_mode)
            or not _same_inode(parent_open, final_parent_path)
        ):
            raise OSError
        os.fsync(parent_descriptor)
        # fsync itself is a syscall boundary. Rebind both descriptors to their
        # requested path names once more before reporting success so a swap
        # performed while the directory was being synchronized fails closed.
        durable_stat = os.fstat(output_descriptor)
        durable_path_stat = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        durable_parent_path = os.lstat(parent)
        if (
            not _same_inode(final_stat, durable_stat)
            or durable_stat.st_nlink != 1
            or durable_stat.st_size != len(content)
            or stat.S_IMODE(durable_stat.st_mode) != 0o600
            or not stat.S_ISREG(durable_path_stat.st_mode)
            or not _same_inode(durable_stat, durable_path_stat)
            or durable_path_stat.st_size != len(content)
            or stat.S_IMODE(durable_path_stat.st_mode) != 0o600
            or not stat.S_ISDIR(durable_parent_path.st_mode)
            or not _same_inode(parent_open, durable_parent_path)
        ):
            raise OSError
    except (OSError, OverflowError, ValueError):
        if created and cleanup_stat is not None and parent_descriptor is not None:
            try:
                current = os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                # An attacker-controlled replacement at the requested name is
                # not ours to delete. Clean up only the exact inode created by
                # this invocation.
                if _same_inode(cleanup_stat, current):
                    os.unlink(path.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except OSError:
                pass
        raise PdfContextProjectionCliError("output_target_invalid_or_exists") from None
    finally:
        if output_descriptor is not None:
            os.close(output_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _canonical_summary(
    *,
    content: bytes,
    projection: Mapping[str, Any],
) -> bytes:
    summary = {
        "context_policy_version": projection["context_policy_version"],
        "metrics": projection["metrics"],
        "output_sha256": sha256(content).hexdigest(),
        "output_size_bytes": len(content),
        "schema_version": projection["schema_version"],
    }
    return json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an evaluation-only A3 PDF context projection. Both the "
            "reconstruction plan and fragment groups must already have passed "
            "their complete upstream source-artifact replay validators."
        )
    )
    parser.add_argument("--reconstruction-plan", required=True)
    parser.add_argument("--fragment-groups", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--acknowledge-upstream-replay",
        action="store_true",
        required=True,
        help=(
            "Acknowledge that both inputs already passed full source-artifact "
            "replay; this command does not perform that authentication."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if os.name != "posix":
            raise PdfContextProjectionCliError("platform_not_supported")
        plan = _json_object(Path(arguments.reconstruction_plan))
        fragments = _json_object(Path(arguments.fragment_groups))
        try:
            projection = build_pdf_context_groups(plan, fragments)
            encoded = canonical_pdf_context_groups_json(projection)
        except (PdfContextGroupsError, TypeError, ValueError, RecursionError):
            raise PdfContextProjectionCliError("input_artifacts_failed_projection") from None
        _write_exclusive(Path(arguments.output), encoded)
        summary = _canonical_summary(
            content=encoded,
            projection=projection,
        )
    except PdfContextProjectionCliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    # Exactly one canonical JSON line; no projected semantic content is emitted.
    sys.stdout.buffer.write(summary + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
