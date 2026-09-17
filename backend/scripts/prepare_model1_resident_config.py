#!/usr/bin/env python3
"""Prepare the protected token and pinned identity for resident Model 1.

The command never prints either value.  By default it is idempotent only when
both existing outputs are private regular files and still match the current
model, preprocessor, and service bytes.  ``--refresh-identity`` may atomically
replace only a valid stale identity; it never replaces the existing token.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from typing import Callable, Sequence


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from prereview_model1_service.runtime import verified_runtime_manifest_sha256


DEFAULT_RUNTIME = REPOSITORY_ROOT / ".runtime" / "model1-serving" / "model1"
DEFAULT_TOKEN_FILE = (
    REPOSITORY_ROOT / ".runtime" / "model1-serving" / "model1-service.token"
)
DEFAULT_ENV_FILE = (
    REPOSITORY_ROOT / ".runtime" / "model1-serving" / "model1-resident.env"
)
PREPROCESSOR = REPOSITORY_ROOT / "ml" / "pipelines" / "model1" / "dl07_m1_apply.py"
MANIFEST_ENV = "PREREVIEW_MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ResidentConfigurationError(RuntimeError):
    """Safe deployment error which never contains secret material."""


def _private_regular_file(
    path: Path,
    *,
    expected_owner: tuple[int, int] | None = None,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise ResidentConfigurationError("configuration output is not a private file") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (
            expected_owner is not None
            and (metadata.st_uid, metadata.st_gid) != expected_owner
        )
    ):
        raise ResidentConfigurationError("configuration output is not a private file")
    return metadata


def _runtime_owner_for_configuration(runtime: Path) -> tuple[int, int] | None:
    """Return the POSIX owner shared by runtime, token, and Compose users."""

    if os.name != "posix":
        return None
    metadata = runtime.stat()
    runtime_owner = (metadata.st_uid, metadata.st_gid)
    caller_owner = (os.geteuid(), os.getegid())
    if 0 in runtime_owner or 0 in caller_owner or runtime_owner != caller_owner:
        raise ResidentConfigurationError(
            "Model 1 runtime owner must match the non-root effective caller UID:GID"
        )
    return runtime_owner


def _read_existing_token(
    path: Path,
    *,
    expected_owner: tuple[int, int] | None,
) -> None:
    metadata = _private_regular_file(path, expected_owner=expected_owner)
    if not 32 <= metadata.st_size <= 513:
        raise ResidentConfigurationError("resident token file is invalid")
    try:
        value = path.read_text(encoding="ascii").rstrip("\n")
    except (OSError, UnicodeDecodeError):
        raise ResidentConfigurationError("resident token file is invalid") from None
    if "\r" in value or "\n" in value or _TOKEN.fullmatch(value) is None:
        raise ResidentConfigurationError("resident token file is invalid")


def _read_existing_manifest(
    path: Path,
    *,
    expected_owner: tuple[int, int] | None,
) -> str:
    _private_regular_file(path, expected_owner=expected_owner)
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        raise ResidentConfigurationError("resident identity file is invalid") from None
    if len(lines) != 1:
        raise ResidentConfigurationError("resident identity file is invalid")
    name, separator, value = lines[0].partition("=")
    if name != MANIFEST_ENV or separator != "=" or _SHA256.fullmatch(value) is None:
        raise ResidentConfigurationError("resident identity file is invalid")
    return value


def _write_exclusive(
    path: Path,
    value: str,
    *,
    expected_owner: tuple[int, int] | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            descriptor = None
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        _private_regular_file(path, expected_owner=expected_owner)
    except FileExistsError:
        raise ResidentConfigurationError("configuration output already exists") from None
    except Exception:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort durability barrier for a completed atomic replacement."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is unavailable on some supported filesystems and
        # platforms.  The file itself was fsynced before os.replace below.
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _replace_identity_atomically(
    path: Path,
    value: str,
    *,
    expected_manifest: str,
    expected_owner: tuple[int, int] | None,
) -> None:
    """Replace one already-private identity without touching the token."""

    # Validate before creating any replacement artifact.  Re-check the same
    # identity immediately before os.replace so a changed/shared target fails
    # closed instead of being silently overwritten.
    if (
        _read_existing_manifest(path, expected_owner=expected_owner)
        != expected_manifest
    ):
        raise ResidentConfigurationError("resident identity changed during refresh")
    original = _private_regular_file(path, expected_owner=expected_owner)
    original_identity = _file_identity(original)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.refresh-",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            descriptor = None
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if (
            stat.S_IMODE(
                _private_regular_file(
                    temporary,
                    expected_owner=expected_owner,
                ).st_mode
            )
            != 0o600
        ):
            raise ResidentConfigurationError("resident identity refresh failed")
        current = _private_regular_file(path, expected_owner=expected_owner)
        if _file_identity(current) != original_identity:
            raise ResidentConfigurationError("resident identity changed during refresh")
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except ResidentConfigurationError:
        raise
    except (OSError, UnicodeError):
        raise ResidentConfigurationError("resident identity refresh failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def prepare_configuration(
    *,
    runtime: Path,
    token_file: Path,
    env_file: Path,
    manifest_resolver: Callable[[Path, Path], str] = verified_runtime_manifest_sha256,
    refresh_identity: bool = False,
) -> bool:
    """Create both files, verify a pair, or atomically refresh its identity.

    Returns ``True`` when files were created or the identity was refreshed,
    and ``False`` when a valid existing pair was reused.  The default keeps
    the original fail-closed stale-identity contract.
    """

    if type(refresh_identity) is not bool:
        raise ResidentConfigurationError("refresh_identity must be a boolean")

    runtime = runtime.expanduser().resolve(strict=True)
    expected_owner = _runtime_owner_for_configuration(runtime)
    token_file = Path(os.path.abspath(token_file.expanduser()))
    env_file = Path(os.path.abspath(env_file.expanduser()))
    if token_file == env_file or token_file.is_symlink() or env_file.is_symlink():
        raise ResidentConfigurationError("configuration output paths are invalid")
    token_exists = os.path.lexists(token_file)
    env_exists = os.path.lexists(env_file)
    existing_manifest: str | None = None
    if refresh_identity and (token_exists or env_exists):
        if not (token_exists and env_exists):
            raise ResidentConfigurationError("resident configuration is only partially present")
        # Refresh authority is available only after both existing outputs have
        # passed their complete private-file and content contracts.
        _read_existing_token(token_file, expected_owner=expected_owner)
        existing_manifest = _read_existing_manifest(
            env_file,
            expected_owner=expected_owner,
        )
    try:
        manifest = manifest_resolver(runtime, PREPROCESSOR)
    except Exception:
        raise ResidentConfigurationError("resident runtime identity verification failed") from None
    if _SHA256.fullmatch(manifest) is None:
        raise ResidentConfigurationError("resident runtime identity verification failed")

    if token_exists or env_exists:
        if not (token_exists and env_exists):
            raise ResidentConfigurationError("resident configuration is only partially present")
        if existing_manifest is None:
            _read_existing_token(token_file, expected_owner=expected_owner)
            existing_manifest = _read_existing_manifest(
                env_file,
                expected_owner=expected_owner,
            )
        if existing_manifest == manifest:
            return False
        if not refresh_identity:
            raise ResidentConfigurationError("resident runtime identity has changed")
        _replace_identity_atomically(
            env_file,
            f"{MANIFEST_ENV}={manifest}\n",
            expected_manifest=existing_manifest,
            expected_owner=expected_owner,
        )
        return True

    token_created = False
    try:
        token = secrets.token_urlsafe(48)
        if _TOKEN.fullmatch(token) is None:  # pragma: no cover - stdlib contract guard
            raise ResidentConfigurationError("could not generate resident token")
        _write_exclusive(
            token_file,
            token + "\n",
            expected_owner=expected_owner,
        )
        token_created = True
        _write_exclusive(
            env_file,
            f"{MANIFEST_ENV}={manifest}\n",
            expected_owner=expected_owner,
        )
    except Exception:
        if token_created:
            try:
                token_file.unlink()
            except OSError:
                pass
        raise
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--refresh-identity",
        action="store_true",
        help=(
            "atomically replace a changed, valid private identity while "
            "preserving the existing private token"
        ),
    )
    args = parser.parse_args(argv)
    requested_token_file = Path(os.path.abspath(args.token_file.expanduser()))
    requested_env_file = Path(os.path.abspath(args.env_file.expanduser()))
    complete_pair_existed = os.path.lexists(requested_token_file) and os.path.lexists(
        requested_env_file
    )
    try:
        created = prepare_configuration(
            runtime=args.runtime,
            token_file=args.token_file,
            env_file=args.env_file,
            refresh_identity=args.refresh_identity,
        )
    except (OSError, ResidentConfigurationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if created and args.refresh_identity and complete_pair_existed:
        action = "identity 갱신"
    elif created:
        action = "생성"
    else:
        action = "검증 후 재사용"
    print(f"Model 1 상주 서비스 설정을 {action}했습니다. 비밀값은 출력하지 않았습니다.")
    print(f"token: {requested_token_file}")
    print(f"identity: {requested_env_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
