#!/usr/bin/env python3
"""Compute the immutable identity of the FastAPI Docker ``COPY .`` context.

Keep the small, deliberately-supported rule set below synchronized with
``.dockerignore``; ``test_build_context_digest`` asserts the two declarations
are identical. Dockerfile and .dockerignore themselves are included because
the builder makes them available to ``COPY .``. The generated identity file is
always excluded even if an operator accidentally leaves one in the host
checkout.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import os
from pathlib import Path
import stat


# These entries mirror backend/.dockerignore exactly. Exceptions are represented
# directly rather than implementing Docker's full pattern language because this
# repository intentionally uses only these simple exclusions.
DOCKER_CONTEXT_EXCLUSIONS = (
    ".git",
    "**/.git",
    ".pytest_cache",
    "**/.pytest_cache",
    "__pycache__",
    "**/__pycache__",
    "*.py[cod]",
    "**/*.py[cod]",
    ".venv",
    "**/.venv",
    "venv",
    "**/venv",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "handover",
    ".prereview-build-id",
    "**/.prereview-build-id",
    "*.pem",
    "**/*.pem",
    "*.key",
    "**/*.key",
    "secrets",
    "**/secrets",
    ".eggs",
    "**/.eggs",
    "*.egg-info",
    "**/*.egg-info",
    "dist",
    "**/dist",
    "build",
    "**/build",
    "sdist",
    "**/sdist",
    "*.so",
    "**/*.so",
    ".ipynb_checkpoints",
    "**/.ipynb_checkpoints",
)
DOCKER_CONTEXT_EXCEPTIONS = (".env.example", "**/.env.example")
_DIGEST_DOMAIN = b"prereview-backend-docker-context-v1\0"


def _is_env_file(path: Path) -> bool:
    name = path.name
    return name == ".env" or name.startswith(".env.")


def _is_excluded(relative_path: Path, *, is_directory: bool) -> bool:
    """Return whether Docker would omit this path from ``COPY .``.

    All current patterns are basename patterns or root-only directory names.
    The checked-in exceptions retain ``.env.example`` at every depth.
    """

    parts = relative_path.parts
    name = relative_path.name
    if name == ".prereview-build-id":
        return True
    # The paired root and ** patterns in .dockerignore intentionally exclude
    # these names at every depth. Keep this basename handling in lockstep.
    if name in {
        "secrets",
        ".eggs",
        "dist",
        "build",
        "sdist",
        ".ipynb_checkpoints",
    }:
        return True
    if name in {".git", ".pytest_cache", "__pycache__", ".venv", "venv"}:
        return True
    if name == "handover" and len(parts) == 1:
        return True
    if _is_env_file(relative_path) and name != ".env.example":
        return True
    if name.endswith((".pyc", ".pyo", ".pyd", ".pem", ".key", ".so", ".egg-info")):
        return True
    return False


def _framed_update(digest: object, marker: bytes, value: bytes) -> None:
    # Length framing removes ambiguity between e.g. ("ab", "c") and
    # ("a", "bc") while keeping the representation platform-independent.
    # hashlib does not expose a portable public hash-object protocol.
    update = getattr(digest, "update")
    update(marker)
    update(len(value).to_bytes(8, "big"))
    update(value)


def backend_build_context_digest(root: Path) -> str:
    """Hash every directory, regular file, and symlink received by Docker COPY.

    Path, entry kind, executable mode and file bytes (or symlink target) all
    contribute. Unsupported filesystem objects fail closed instead of being
    silently omitted from release provenance.
    """

    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Docker context root must be a directory")
    digest = sha256(_DIGEST_DOMAIN)
    entries: list[Path] = []
    for directory, directory_names, file_names in os.walk(root, topdown=True):
        directory_path = Path(directory)
        retained_directories: list[str] = []
        for name in directory_names:
            entry = directory_path / name
            relative = entry.relative_to(root)
            if not _is_excluded(relative, is_directory=True):
                entries.append(relative)
                # os.walk does not descend into a directory symlink, but COPY
                # receives that single symlink entry just as it does a file.
                if not entry.is_symlink():
                    retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in file_names:
            relative = (directory_path / name).relative_to(root)
            if not _is_excluded(relative, is_directory=False):
                entries.append(relative)

    for relative in sorted(entries, key=lambda path: os.fsencode(path.as_posix())):
        path = root / relative
        path_stat = path.lstat()
        relative_bytes = os.fsencode(relative.as_posix())
        if stat.S_ISREG(path_stat.st_mode):
            kind = b"regular"
            payload = path.read_bytes()
        elif stat.S_ISLNK(path_stat.st_mode):
            kind = b"symlink"
            payload = os.fsencode(os.readlink(path))
        elif stat.S_ISDIR(path_stat.st_mode):
            kind = b"directory"
            payload = b""
        else:
            raise ValueError(f"unsupported Docker context entry: {relative.as_posix()}")
        _framed_update(digest, b"path\0", relative_bytes)
        _framed_update(digest, b"kind\0", kind)
        _framed_update(
            digest,
            b"mode\0",
            f"{stat.S_IMODE(path_stat.st_mode):04o}".encode("ascii"),
        )
        _framed_update(digest, b"payload\0", payload)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--write", type=Path)
    args = parser.parse_args()
    value = backend_build_context_digest(args.root)
    if args.write is not None:
        args.write.write_text(f"{value}\n", encoding="ascii")
    else:
        print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
