#!/usr/bin/env python3
"""Create or reuse one confirmed, ordinary user in local Supabase Auth.

This is an explicit development-only, host-side one-shot. It never writes
``auth.users`` directly and never prints credentials, account identifiers,
tokens, provider response bodies, or secret environment values.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from io import StringIO
import os
from pathlib import Path
import stat
import sys
from typing import Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DEFAULT_SUPABASE_DIR = REPOSITORY_ROOT / ".runtime" / "supabase-dev"
DEFAULT_CREDENTIALS = REPOSITORY_ROOT / ".runtime" / "pre-review-dev-auth.env"
INSTALLER_MARKER = ".pre-review-supabase-version"
PINNED_SUPABASE_REF = "self-hosted/v0.8.0"
PINNED_SUPABASE_COMMIT = "e1af732589cd468edb49500ebc04e4367d4c56ad"
DEVELOPMENT_ENVIRONMENT = "development"
ENABLE_VALUE = "true"


class BootstrapError(RuntimeError):
    """Safe operator-facing failure which contains no supplied values."""


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    base_url: str
    anon_key: str
    service_role_key: str
    email: str
    password: str


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--supabase-dir",
        type=Path,
        default=DEFAULT_SUPABASE_DIR,
        help="local self-hosted Supabase Compose directory",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help="mode-0600 development account credential file",
    )
    parser.add_argument(
        "--allow-unmanaged-local",
        action="store_true",
        help=(
            "allow a legacy local Supabase directory with no installer marker; "
            "all other development and loopback guards remain mandatory"
        ),
    )
    return parser.parse_args(argv)


def _require_development_opt_in(environ: Mapping[str, str]) -> None:
    """Check non-secret guards before opening either credential-bearing file."""

    if environ.get("PREREVIEW_ENVIRONMENT", "").strip().lower() != DEVELOPMENT_ENVIRONMENT:
        raise BootstrapError("PREREVIEW_ENVIRONMENT must be development")
    if (
        environ.get("PREREVIEW_DEV_AUTH_BOOTSTRAP_ENABLED", "").strip().lower()
        != ENABLE_VALUE
    ):
        raise BootstrapError("PREREVIEW_DEV_AUTH_BOOTSTRAP_ENABLED must be true")


def _assert_directory(path: Path) -> Path:
    expanded = Path(os.path.abspath(path.expanduser()))
    if expanded.is_symlink():
        raise BootstrapError("Supabase directory must not be a symlink")
    try:
        path_stat = expanded.stat()
    except FileNotFoundError:
        raise BootstrapError("Supabase directory does not exist") from None
    if not stat.S_ISDIR(path_stat.st_mode):
        raise BootstrapError("Supabase directory is not a directory")
    return expanded


def _read_regular_file(path: Path, *, private: bool, label: str) -> str:
    """Read one regular non-symlink file through an O_NOFOLLOW descriptor."""

    expanded = Path(os.path.abspath(path.expanduser()))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(expanded, flags)
    except FileNotFoundError:
        raise BootstrapError(f"{label} file does not exist") from None
    except OSError:
        raise BootstrapError(f"{label} file could not be opened safely") from None
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise BootstrapError(f"{label} must be a regular non-symlink file")
        if private and stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise BootstrapError(f"{label} must have mode 0600")
        if private and hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
            raise BootstrapError(f"{label} must be owned by the current user")
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
            descriptor = -1
            return stream.read()
    except UnicodeError:
        raise BootstrapError(f"{label} is not valid UTF-8") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_private_env(path: Path, *, label: str) -> dict[str, str]:
    content = _read_regular_file(path, private=True, label=label)
    parsed = dotenv_values(stream=StringIO(content), interpolate=False)
    return {
        str(name): str(value)
        for name, value in parsed.items()
        if value is not None
    }


def _required(values: Mapping[str, str], name: str, *, label: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise BootstrapError(f"{label} is missing required setting {name}")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise BootstrapError(f"{label} setting {name} is invalid")
    return value


def _validate_marker(supabase_dir: Path, *, allow_unmanaged_local: bool) -> None:
    marker = supabase_dir / INSTALLER_MARKER
    if not marker.exists():
        if allow_unmanaged_local:
            return
        raise BootstrapError(
            "Supabase installer marker is missing; use --allow-unmanaged-local "
            "only for a reviewed legacy local installation"
        )
    content = _read_regular_file(marker, private=False, label="Supabase installer marker")
    expected = {
        "ref": PINNED_SUPABASE_REF,
        "commit": PINNED_SUPABASE_COMMIT,
    }
    actual: dict[str, str] = {}
    for line in content.splitlines():
        if "=" not in line:
            raise BootstrapError("Supabase installer marker is invalid")
        name, value = line.split("=", 1)
        if name in actual:
            raise BootstrapError("Supabase installer marker is invalid")
        actual[name] = value
    if actual != expected:
        raise BootstrapError("Supabase installer marker does not match the pinned bundle")


def _loopback_base_url(port: str) -> str:
    if not port.isascii() or not port.isdigit():
        raise BootstrapError("Supabase gateway port is invalid")
    port_number = int(port)
    if not 1 <= port_number <= 65535:
        raise BootstrapError("Supabase gateway port is invalid")
    base_url = f"http://127.0.0.1:{port_number}"
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise BootstrapError("Supabase Auth bootstrap requires a loopback URL")
    return base_url


def _validate_account(values: Mapping[str, str]) -> tuple[str, str]:
    email = _required(values, "PREREVIEW_DEV_AUTH_EMAIL", label="credentials").lower()
    password = _required(values, "PREREVIEW_DEV_AUTH_PASSWORD", label="credentials")
    role = values.get("PREREVIEW_DEV_AUTH_ROLE", "user").strip().lower() or "user"
    if (
        len(email) > 254
        or not email.endswith("@example.invalid")
        or email.startswith("@")
        or any(character.isspace() for character in email)
    ):
        raise BootstrapError("development account email must use @example.invalid")
    if role != "user":
        raise BootstrapError("development Auth bootstrap creates ordinary users only")
    try:
        password_bytes = password.encode("utf-8")
    except UnicodeEncodeError:
        raise BootstrapError("development account password length is invalid") from None
    if len(password) < 12 or len(password_bytes) > 72:
        raise BootstrapError("development account password length is invalid")
    return email, password


def load_settings(
    *,
    supabase_dir: Path,
    credentials_path: Path,
    allow_unmanaged_local: bool,
    environ: Mapping[str, str],
) -> BootstrapSettings:
    _require_development_opt_in(environ)
    local_dir = _assert_directory(supabase_dir)
    _validate_marker(local_dir, allow_unmanaged_local=allow_unmanaged_local)

    # Only after the non-sensitive environment and installation guards pass do
    # we open either file containing account or infrastructure credentials.
    account = _read_private_env(credentials_path, label="credentials")
    supabase = _read_private_env(local_dir / ".env", label="Supabase env")
    email, password = _validate_account(account)
    anon_key = _required(supabase, "ANON_KEY", label="Supabase env")
    service_role_key = _required(supabase, "SERVICE_ROLE_KEY", label="Supabase env")
    port = (
        supabase.get("API_GW_HTTP_PORT", "").strip()
        or supabase.get("KONG_HTTP_PORT", "").strip()
        or "8000"
    )
    return BootstrapSettings(
        base_url=_loopback_base_url(port),
        anon_key=anon_key,
        service_role_key=service_role_key,
        email=email,
        password=password,
    )


def _confirmed_ordinary_user(response: httpx.Response, expected_email: str) -> bool:
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
    ):
        return False
    user = payload.get("user")
    if not isinstance(user, dict):
        return False
    email = user.get("email")
    user_id = user.get("id")
    auth_role = user.get("role")
    metadata = user.get("app_metadata")
    if not isinstance(email, str) or email.strip().lower() != expected_email:
        return False
    if not isinstance(user_id, str) or not user_id:
        return False
    if auth_role != "authenticated":
        return False
    if isinstance(metadata, dict) and metadata.get("role") not in {None, "user"}:
        return False
    return True


async def bootstrap_user(
    settings: BootstrapSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Return ``created`` or ``reused`` without returning identity material."""

    async def sign_in(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": settings.anon_key},
            json={"email": settings.email, "password": settings.password},
        )

    try:
        async with httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=20.0,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            first_login = await sign_in(client)
            if _confirmed_ordinary_user(first_login, settings.email):
                return "reused"
            if first_login.status_code not in {400, 401}:
                raise BootstrapError(
                    f"Supabase sign-in probe failed with HTTP {first_login.status_code}"
                )

            created = await client.post(
                "/auth/v1/admin/users",
                headers={
                    "apikey": settings.service_role_key,
                    "Authorization": f"Bearer {settings.service_role_key}",
                },
                json={
                    "email": settings.email,
                    "password": settings.password,
                    "email_confirm": True,
                    "app_metadata": {"role": "user"},
                },
            )

            # Always verify through the ordinary password grant. This confirms
            # email confirmation and also resolves an ambiguous/concurrent
            # create without inspecting or exposing provider error bodies.
            verified = await sign_in(client)
            if not _confirmed_ordinary_user(verified, settings.email):
                raise BootstrapError(
                    "Supabase Auth user could not be verified after the create attempt"
                )
            return "created" if created.status_code in {200, 201} else "reused"
    except BootstrapError:
        raise
    except httpx.HTTPError:
        raise BootstrapError("Supabase Auth request failed") from None


def run(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    args = _parse_args(argv)
    settings = load_settings(
        supabase_dir=args.supabase_dir,
        credentials_path=args.credentials,
        allow_unmanaged_local=args.allow_unmanaged_local,
        environ=os.environ if environ is None else environ,
    )
    return asyncio.run(bootstrap_user(settings, transport=transport))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        outcome = run(argv)
    except BootstrapError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Supabase Auth development user: {outcome}")
    print("No credential, account identifier, token, or provider body was printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
