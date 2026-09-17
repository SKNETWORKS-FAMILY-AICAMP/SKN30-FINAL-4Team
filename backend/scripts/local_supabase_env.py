"""Build host-side connection settings from an official Supabase Compose env.

This helper never prints values.  It exists because ``backend/.env`` is meant
for containers and therefore uses ``host.docker.internal``; host-run operator
scripts must instead use the Compose-published loopback ports.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import sys
from urllib.parse import quote


class LocalSupabaseEnvError(ValueError):
    pass


def _required(values: dict[str, str | None], name: str) -> str:
    value = str(values.get(name) or "").strip()
    if not value:
        raise LocalSupabaseEnvError(
            f"Supabase Compose env is missing required setting: {name}"
        )
    return value


def _read_private_secret_file(path: Path, *, max_bytes: int) -> bytes:
    """Read a current-user-owned, mode-0600, single-link regular file."""

    try:
        link_info = os.lstat(path)
        current_uid = getattr(os, "geteuid", lambda: -1)()
        if (
            current_uid < 0
            or stat.S_ISLNK(link_info.st_mode)
            or not stat.S_ISREG(link_info.st_mode)
            or link_info.st_uid != current_uid
            or link_info.st_nlink != 1
            or stat.S_IMODE(link_info.st_mode) != 0o600
        ):
            raise OSError("unsafe secret file metadata")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size < 1
                or before.st_size > max_bytes
                or before.st_uid != current_uid
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise OSError("unsafe secret file metadata")
            content = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError:
        raise LocalSupabaseEnvError("Supabase Compose env is not a safe secret file") from None
    if (
        not content
        or len(content) > max_bytes
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or after.st_uid != current_uid
        or after.st_nlink != 1
        or stat.S_IMODE(after.st_mode) != 0o600
    ):
        raise LocalSupabaseEnvError("Supabase Compose env is not a stable secret file")
    return content


def load_local_supabase_settings(
    path: Path,
    *,
    require_private_secret_file: bool = False,
) -> dict[str, str]:
    try:
        from dotenv import dotenv_values
    except ImportError as error:
        raise LocalSupabaseEnvError(
            "python-dotenv is required to read the Supabase Compose env"
        ) from error
    if require_private_secret_file:
        try:
            raw = _read_private_secret_file(path, max_bytes=64 * 1024).decode("utf-8")
        except UnicodeDecodeError:
            raise LocalSupabaseEnvError("Supabase Compose env is not valid UTF-8") from None
        values = dotenv_values(stream=io.StringIO(raw))
    else:
        values = dotenv_values(path.resolve())
    password = _required(values, "POSTGRES_PASSWORD")
    tenant = _required(values, "POOLER_TENANT_ID")
    service_role_key = _required(values, "SERVICE_ROLE_KEY")
    database = str(values.get("POSTGRES_DB") or "postgres").strip() or "postgres"
    database_port = str(values.get("POSTGRES_PORT") or "5432").strip() or "5432"
    gateway_port = str(
        values.get("API_GW_HTTP_PORT")
        or values.get("KONG_HTTP_PORT")
        or "8000"
    ).strip()
    if not database_port.isdigit() or not gateway_port.isdigit():
        raise LocalSupabaseEnvError(
            "Supabase Compose host ports must be numeric"
        )
    username = quote(f"postgres.{tenant}", safe="")
    if sys.platform == "win32":
        # Docker Desktop exposes PostgreSQL directly to the Windows host.
        username = "postgres"
        database_port = "55432"
    database_url = (
        f"postgresql://{username}:{quote(password, safe='')}"
        f"@127.0.0.1:{database_port}/{quote(database, safe='')}?sslmode=disable"
    )
    return {
        "SUPABASE_URL": f"http://127.0.0.1:{gateway_port}",
        "SUPABASE_SERVICE_ROLE_KEY": service_role_key,
        "SUPABASE_DB_URL": database_url,
        "DATABASE_URL": database_url,
    }
