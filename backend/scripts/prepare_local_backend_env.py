#!/usr/bin/env python3
"""Create ``backend/.env`` from existing local secret files without echoing values.

The generated file is intended for ``backend/compose.yaml``: FastAPI and the
polling worker run in containers while the official self-hosted Supabase stack
runs on the same Docker host.  This command only prepares configuration.  It
does not start containers, contact Supabase/OpenAI, or mutate either source
file.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys
from typing import Mapping, Sequence
from urllib.parse import quote, urlsplit

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - exercised only before dependencies exist
    dotenv_values = None  # type: ignore[assignment]


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DEFAULT_PROVIDER_ENV = REPOSITORY_ROOT / ".env"
DEFAULT_SUPABASE_ENV = REPOSITORY_ROOT / ".runtime" / "supabase-dev" / ".env"
DEFAULT_OUTPUT = BACKEND_ROOT / ".env"

DEFAULT_FRONTEND_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
DEFAULT_SWAGGER_ORIGINS = (
    "http://localhost:8001",
    "http://127.0.0.1:8001",
)


class ConfigurationError(RuntimeError):
    """Safe operator-facing error that never includes a setting value."""


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "기존 로컬 .env 두 개에서 backend/.env를 안전하게 새로 만듭니다. "
            "비밀값은 출력하지 않으며 기존 출력 파일은 덮어쓰지 않습니다."
        )
    )
    parser.add_argument(
        "--provider-env",
        type=Path,
        default=DEFAULT_PROVIDER_ENV,
        help="OPENAI_*가 있는 파일 (기본: 저장소 루트 .env)",
    )
    parser.add_argument(
        "--supabase-env",
        type=Path,
        default=DEFAULT_SUPABASE_ENV,
        help="공식 self-hosted Supabase .env",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="새로 만들 backend runtime env (기본: backend/.env)",
    )
    parser.add_argument(
        "--frontend-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help=(
            "추가로 허용할 프론트 origin. 여러 번 지정할 수 있으며 기본 localhost/"
            "127.0.0.1 origin은 유지됩니다."
        ),
    )
    return parser.parse_args(argv)


def _assert_private_regular_file(path: Path, *, label: str) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    try:
        file_stat = candidate.stat()
    except FileNotFoundError:
        raise ConfigurationError(f"{label} 파일을 찾을 수 없습니다: {candidate}") from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise ConfigurationError(f"{label} 경로가 일반 파일이 아닙니다: {candidate}")
    if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ConfigurationError(
            f"{label} 파일 권한이 너무 넓습니다. 먼저 chmod 600 {candidate} 를 실행하세요."
        )
    return candidate


def _load_env(path: Path, *, label: str) -> dict[str, str]:
    if dotenv_values is None:
        raise ConfigurationError(
            "python-dotenv가 필요합니다. backend에서 uv sync --extra dev를 먼저 실행하세요."
        )
    loaded = dotenv_values(path, interpolate=False)
    values: dict[str, str] = {}
    for name, raw_value in loaded.items():
        if raw_value is not None:
            values[name] = str(raw_value)
    if not values:
        raise ConfigurationError(f"{label} 파일에서 설정을 읽지 못했습니다: {path}")
    return values


def _required(values: Mapping[str, str], name: str, *, label: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{label} 파일에 필수 변수 {name}가 없습니다.")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigurationError(f"{label} 파일의 {name} 값 형식이 안전하지 않습니다.")
    return value


def _optional(
    values: Mapping[str, str], name: str, default: str, *, label: str
) -> str:
    value = values.get(name, "").strip() or default
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigurationError(f"{label} 파일의 {name} 값 형식이 안전하지 않습니다.")
    return value


def _validate_origin(origin: str) -> str:
    value = origin.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "--frontend-origin은 path/query가 없는 정확한 http(s) origin이어야 합니다."
        )
    try:
        parsed.port
    except ValueError:
        raise ConfigurationError("--frontend-origin의 포트가 올바르지 않습니다.") from None
    return value


def build_settings(
    provider: Mapping[str, str],
    supabase: Mapping[str, str],
    *,
    extra_frontend_origins: Sequence[str] = (),
) -> list[tuple[str, str]]:
    """Return ordered runtime settings without performing I/O."""

    provider_label = "provider env"
    supabase_label = "Supabase env"
    postgres_password = _required(
        supabase, "POSTGRES_PASSWORD", label=supabase_label
    )
    pooler_tenant = _required(supabase, "POOLER_TENANT_ID", label=supabase_label)
    postgres_database = _optional(
        supabase, "POSTGRES_DB", "postgres", label=supabase_label
    )

    pooler_username = f"postgres.{pooler_tenant}"
    database_url = (
        f"postgresql://{quote(pooler_username, safe='')}:"
        f"{quote(postgres_password, safe='')}"
        f"@host.docker.internal:5432/{quote(postgres_database, safe='')}"
        "?sslmode=disable"
    )

    origins: list[str] = []
    for raw_origin in (
        *DEFAULT_FRONTEND_ORIGINS,
        *DEFAULT_SWAGGER_ORIGINS,
        *extra_frontend_origins,
    ):
        origin = _validate_origin(raw_origin)
        if origin not in origins:
            origins.append(origin)

    return [
        ("PREREVIEW_OFFLINE_MODE", "false"),
        ("PREREVIEW_API_BIND_ADDRESS", "127.0.0.1"),
        ("PREREVIEW_API_PORT", "8001"),
        ("PREREVIEW_UPLOAD_MAX_BYTES", "52428800"),
        ("PREREVIEW_AUTH_ALLOWED_ORIGINS", ",".join(origins)),
        ("PREREVIEW_AUTH_COOKIE_SECURE", "false"),
        ("PREREVIEW_AUTH_COOKIE_SAMESITE", "lax"),
        ("PREREVIEW_AUTH_COOKIE_DOMAIN", ""),
        ("PREREVIEW_AUTH_REFRESH_COOKIE_MAX_AGE", "2592000"),
        (
            "PREREVIEW_AUTH_PASSWORD_RESET_REDIRECT_TO",
            "http://localhost:3000/reset-password",
        ),
        ("SUPABASE_URL", "http://host.docker.internal:8000"),
        ("SUPABASE_ANON_KEY", _required(supabase, "ANON_KEY", label=supabase_label)),
        ("SUPABASE_SECRET_KEY", ""),
        (
            "SUPABASE_SERVICE_ROLE_KEY",
            _required(supabase, "SERVICE_ROLE_KEY", label=supabase_label),
        ),
        ("DATABASE_URL", database_url),
        ("SUPABASE_DB_URL", ""),
        (
            "OPENAI_API_KEY",
            _required(provider, "OPENAI_API_KEY", label=provider_label),
        ),
        (
            "OPENAI_LLM_MODEL",
            _optional(provider, "OPENAI_LLM_MODEL", "gpt-5.6-luna", label=provider_label),
        ),
        (
            "OPENAI_EMBEDDING_MODEL",
            _optional(
                provider,
                "OPENAI_EMBEDDING_MODEL",
                "text-embedding-3-small",
                label=provider_label,
            ),
        ),
        ("OPENAI_EMBEDDING_DIMENSIONS", "1536"),
        (
            "OPENAI_TIMEOUT_SECONDS",
            _optional(provider, "OPENAI_TIMEOUT_SECONDS", "60", label=provider_label),
        ),
        (
            "OPENAI_MAX_REPAIRS",
            _optional(provider, "OPENAI_MAX_REPAIRS", "1", label=provider_label),
        ),
        ("PREREVIEW_WORKER_HEARTBEAT_SECONDS", "30"),
        ("PREREVIEW_WORKER_LEASE_SECONDS", "120"),
        ("PREREVIEW_WORKER_IDLE_POLL_SECONDS", "1"),
        ("PREREVIEW_WORKER_TOP_K", "5"),
        ("PREREVIEW_WORKER_STORAGE_TIMEOUT_SECONDS", "30"),
        ("PREREVIEW_WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS", "10"),
        ("PREREVIEW_WORKER_PARSE_TIMEOUT_SECONDS", "120"),
        (
            "PREREVIEW_FREETYPE_LIB",
            "/usr/lib/x86_64-linux-gnu/libfreetype.so.6",
        ),
    ]


def _dotenv_quote(value: str) -> str:
    """Quote literally for Docker Compose and python-dotenv readers."""

    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigurationError("환경변수 값에 지원하지 않는 제어 문자가 있습니다.")
    return "'" + value.replace("'", "\\'") + "'"


def render_env(settings: Sequence[tuple[str, str]]) -> str:
    groups = {
        "PREREVIEW_OFFLINE_MODE": "# FastAPI / browser-local configuration",
        "SUPABASE_URL": "\n# Same-host self-hosted Supabase (from inside containers)",
        "DATABASE_URL": "\n# PostgreSQL pooler session port (server-only)",
        "OPENAI_API_KEY": "\n# OpenAI worker configuration (server-only)",
        "PREREVIEW_WORKER_HEARTBEAT_SECONDS": "\n# PostgreSQL polling worker",
    }
    lines = [
        "# Generated by scripts/prepare_local_backend_env.py.",
        "# Do not commit, paste into chat, or expose this file to the browser.",
    ]
    for name, value in settings:
        heading = groups.get(name)
        if heading:
            lines.append(heading)
        lines.append(f"{name}={_dotenv_quote(value)}")
    return "\n".join(lines) + "\n"


def write_new_private_file(path: Path, content: str) -> Path:
    """Create a mode-0600 file with an exclusive destination."""

    expanded = path.expanduser()
    output = Path(os.path.abspath(expanded))
    if os.path.lexists(output):
        raise ConfigurationError(
            f"출력 파일이 이미 있어 덮어쓰지 않습니다: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(output, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(output, 0o600)
    except FileExistsError:
        raise ConfigurationError(
            f"출력 파일이 이미 있어 덮어쓰지 않습니다: {output}"
        ) from None
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        # A failed exclusive create may leave only our incomplete new file.
        # Remove it without ever touching a path that pre-existed this call.
        if created:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return output


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        provider_path = _assert_private_regular_file(
            args.provider_env, label="provider env"
        )
        supabase_path = _assert_private_regular_file(
            args.supabase_env, label="Supabase env"
        )
        output_path = Path(os.path.abspath(args.output.expanduser()))
        if os.path.lexists(output_path):
            raise ConfigurationError(
                f"출력 파일이 이미 있어 덮어쓰지 않습니다: {output_path}"
            )
        settings = build_settings(
            _load_env(provider_path, label="provider env"),
            _load_env(supabase_path, label="Supabase env"),
            extra_frontend_origins=args.frontend_origin,
        )
        written = write_new_private_file(output_path, render_env(settings))
    except ConfigurationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("backend runtime env를 만들었습니다. 비밀값은 출력하지 않았습니다.")
    print(f"경로: {written}")
    print("권한: 600")
    print("컨테이너를 시작하거나 외부 네트워크를 호출하지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
