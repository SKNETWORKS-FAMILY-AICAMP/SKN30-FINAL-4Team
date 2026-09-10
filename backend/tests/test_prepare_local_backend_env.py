from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
from urllib.parse import quote

from dotenv import dotenv_values
import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "prepare_local_backend_env.py"
SPEC = importlib.util.spec_from_file_location("prepare_local_backend_env", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _valid_inputs(tmp_path: Path) -> tuple[Path, Path]:
    provider = tmp_path / "provider.env"
    supabase = tmp_path / "supabase.env"
    _write_private(
        provider,
        "OPENAI_API_KEY='openai-test-secret'\n"
        "OPENAI_LLM_MODEL='test-luna'\n"
        "OPENAI_EMBEDDING_MODEL='text-embedding-3-small'\n",
    )
    _write_private(
        supabase,
        "POSTGRES_PASSWORD='db @:/#% secret'\n"
        "POSTGRES_DB='postgres'\n"
        "POOLER_TENANT_ID='tenant-id'\n"
        "ANON_KEY='anon-test-secret'\n"
        "SERVICE_ROLE_KEY='service-test-secret'\n",
    )
    return provider, supabase


def test_build_settings_uses_container_gateway_pooler_and_url_encoding() -> None:
    settings = dict(
        MODULE.build_settings(
            {
                "OPENAI_API_KEY": "openai-test-secret",
                "OPENAI_LLM_MODEL": "test-luna",
            },
            {
                "POSTGRES_PASSWORD": "db @:/#% secret",
                "POSTGRES_DB": "app db",
                "POOLER_TENANT_ID": "tenant/name",
                "ANON_KEY": "anon-test-secret",
                "SERVICE_ROLE_KEY": "service-test-secret",
            },
            extra_frontend_origins=["http://192.168.0.67:3000"],
        )
    )

    expected_user = quote("postgres.tenant/name", safe="")
    expected_password = quote("db @:/#% secret", safe="")
    expected_database = quote("app db", safe="")
    assert settings["SUPABASE_URL"] == "http://host.docker.internal:8000"
    assert settings["DATABASE_URL"] == (
        f"postgresql://{expected_user}:{expected_password}"
        f"@host.docker.internal:5432/{expected_database}?sslmode=disable"
    )
    assert settings["SUPABASE_ANON_KEY"] == "anon-test-secret"
    assert settings["SUPABASE_SERVICE_ROLE_KEY"] == "service-test-secret"
    assert settings["SUPABASE_SECRET_KEY"] == ""
    assert settings["OPENAI_API_KEY"] == "openai-test-secret"
    assert settings["OPENAI_LLM_MODEL"] == "test-luna"
    assert settings["PREREVIEW_OFFLINE_MODE"] == "false"
    assert settings["PREREVIEW_API_BIND_ADDRESS"] == "127.0.0.1"
    assert "http://192.168.0.67:3000" in settings[
        "PREREVIEW_AUTH_ALLOWED_ORIGINS"
    ].split(",")


def test_cli_creates_mode_600_env_without_printing_secrets(tmp_path: Path) -> None:
    provider, supabase = _valid_inputs(tmp_path)
    output = tmp_path / "backend.env"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--provider-env",
            str(provider),
            "--supabase-env",
            str(supabase),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    combined_output = result.stdout + result.stderr
    for secret in (
        "openai-test-secret",
        "anon-test-secret",
        "service-test-secret",
        "db @:/#% secret",
    ):
        assert secret not in combined_output

    generated = dotenv_values(output, interpolate=False)
    assert generated["SUPABASE_URL"] == "http://host.docker.internal:8000"
    assert generated["DATABASE_URL"] == (
        "postgresql://postgres.tenant-id:db%20%40%3A%2F%23%25%20secret"
        "@host.docker.internal:5432/postgres?sslmode=disable"
    )
    assert generated["OPENAI_API_KEY"] == "openai-test-secret"


def test_cli_never_overwrites_existing_output(tmp_path: Path) -> None:
    provider, supabase = _valid_inputs(tmp_path)
    output = tmp_path / "backend.env"
    sentinel = "keep-existing-file\n"
    _write_private(output, sentinel)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--provider-env",
            str(provider),
            "--supabase-env",
            str(supabase),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == sentinel
    assert "덮어쓰지 않습니다" in result.stderr


def test_cli_never_follows_existing_output_symlink(tmp_path: Path) -> None:
    provider, supabase = _valid_inputs(tmp_path)
    target = tmp_path / "target.env"
    output = tmp_path / "backend.env"
    output.symlink_to(target)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--provider-env",
            str(provider),
            "--supabase-env",
            str(supabase),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert output.is_symlink()
    assert not target.exists()


def test_cli_rejects_world_readable_secret_inputs_without_reading_values(
    tmp_path: Path,
) -> None:
    provider, supabase = _valid_inputs(tmp_path)
    provider.chmod(0o644)
    output = tmp_path / "backend.env"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--provider-env",
            str(provider),
            "--supabase-env",
            str(supabase),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "chmod 600" in result.stderr
    assert "openai-test-secret" not in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "origin",
    (
        "*",
        "http://localhost:3000/path",
        "http://user:password@localhost:3000",
        "ftp://localhost:3000",
    ),
)
def test_build_settings_rejects_non_origin_cors_values(origin: str) -> None:
    with pytest.raises(MODULE.ConfigurationError):
        MODULE.build_settings(
            {"OPENAI_API_KEY": "safe-test-value"},
            {
                "POSTGRES_PASSWORD": "safe-test-value",
                "POOLER_TENANT_ID": "tenant",
                "ANON_KEY": "safe-test-value",
                "SERVICE_ROLE_KEY": "safe-test-value",
            },
            extra_frontend_origins=[origin],
        )


def test_shell_help_does_not_require_or_read_source_files() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONWARNINGS": "error"},
    )

    assert result.returncode == 0
    assert "비밀값은 출력하지 않으며" in result.stdout


def test_docker_context_excludes_runtime_env_files_but_keeps_examples() -> None:
    dockerignore = (BACKEND_ROOT / ".dockerignore").read_text(encoding="utf-8")
    rules = {
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {".env", ".env.*", "**/.env", "**/.env.*"} <= rules
    assert {"!.env.example", "!**/.env.example"} <= rules
