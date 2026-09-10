"""Build host-side connection settings from an official Supabase Compose env.

This helper never prints values.  It exists because ``backend/.env`` is meant
for containers and therefore uses ``host.docker.internal``; host-run operator
scripts must instead use the Compose-published loopback ports.
"""

from __future__ import annotations

from pathlib import Path
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


def load_local_supabase_settings(path: Path) -> dict[str, str]:
    try:
        from dotenv import dotenv_values
    except ImportError as error:
        raise LocalSupabaseEnvError(
            "python-dotenv is required to read the Supabase Compose env"
        ) from error
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
