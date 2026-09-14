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


def _valid_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
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
    model1 = tmp_path / "model1"
    (model1 / "model").mkdir(parents=True)
    (model1 / "inference.py").write_text("# test runtime\n", encoding="utf-8")
    (model1 / "model" / "model.safetensors").write_bytes(b"test-weight")
    model1.chmod(0o700)
    if os.name == "posix" and os.geteuid() == 0:
        # Production rejects a root-owned mount. Keep the general test fixture
        # valid even when CI itself happens to run pytest as root.
        model1.chown(65534, 65534)
    return provider, supabase, model1


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
            model1_serving_host_dir="/srv/prereview/model1",
            model1_runtime_uid="1001",
            model1_runtime_gid="1002",
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
    assert settings["PREREVIEW_LLM_PROVIDER"] == "openai"
    assert settings["PREREVIEW_EMBEDDING_PROVIDER"] == "openai"
    assert settings["OPENAI_LLM_MODEL"] == "test-luna"
    assert settings["OPENAI_REQUEST_PROFILE_MODEL"] == "gpt-5.6-terra"
    assert settings["OPENAI_CPL_MODEL"] == ""
    assert settings["OPENAI_TIMEOUT_SECONDS"] == "120"
    assert settings["OPENAI_MAX_REPAIRS"] == "2"
    assert settings["VLLM_BASE_URL"] == ""
    assert settings["VLLM_TIMEOUT_SECONDS"] == "120"
    assert settings["VLLM_MAX_OUTPUT_TOKENS"] == "16384"
    assert settings["VLLM_MAX_RESPONSE_BYTES"] == "1048576"
    assert settings["PREREVIEW_OFFLINE_MODE"] == "false"
    assert settings["PREREVIEW_API_BIND_ADDRESS"] == "127.0.0.1"
    assert settings["PREREVIEW_AUTH_PASSWORD_RESET_CALLBACK_URL"] == (
        "http://localhost:3000/api/v1/auth/password-recovery/callback"
    )
    assert settings["PREREVIEW_AUTH_PASSWORD_RESET_REDIRECT_TO"] == (
        "http://localhost:3000/password-reset/update"
    )
    assert settings["PREREVIEW_MODEL1_SERVING_HOST_DIR"] == "/srv/prereview/model1"
    assert settings["PREREVIEW_MODEL1_RUNTIME_UID"] == "1001"
    assert settings["PREREVIEW_MODEL1_RUNTIME_GID"] == "1002"
    assert "http://192.168.0.67:3000" in settings[
        "PREREVIEW_AUTH_ALLOWED_ORIGINS"
    ].split(",")


def test_build_settings_preserves_selectable_vllm_configuration() -> None:
    settings = dict(
        MODULE.build_settings(
            {
                "PREREVIEW_LLM_PROVIDER": "vllm",
                "PREREVIEW_EMBEDDING_PROVIDER": "openai",
                "OPENAI_API_KEY": "embedding-only-secret",
                "OPENAI_EMBEDDING_MODEL": "text-embedding-test",
                "VLLM_BASE_URL": "https://gpu.internal/v1",
                "VLLM_API_KEY": "vllm-secret",
                "VLLM_LLM_MODEL": "gemma-test",
                "VLLM_CHAT_MODEL": "gemma-chat",
            },
            {
                "POSTGRES_PASSWORD": "db-secret",
                "POSTGRES_DB": "postgres",
                "POOLER_TENANT_ID": "tenant-id",
                "ANON_KEY": "anon-secret",
                "SERVICE_ROLE_KEY": "service-secret",
            },
            model1_serving_host_dir="/srv/prereview/model1",
            model1_runtime_uid="1001",
            model1_runtime_gid="1002",
        )
    )

    assert settings["PREREVIEW_LLM_PROVIDER"] == "vllm"
    assert settings["PREREVIEW_EMBEDDING_PROVIDER"] == "openai"
    assert settings["VLLM_BASE_URL"] == "https://gpu.internal/v1"
    assert settings["VLLM_API_KEY"] == "vllm-secret"
    assert settings["VLLM_LLM_MODEL"] == "gemma-test"
    assert settings["VLLM_CHAT_MODEL"] == "gemma-chat"


@pytest.mark.parametrize("missing", ["VLLM_BASE_URL", "VLLM_API_KEY", "VLLM_LLM_MODEL"])
def test_build_settings_fails_closed_for_selected_vllm_missing_required_value(
    missing: str,
) -> None:
    provider = {
        "PREREVIEW_LLM_PROVIDER": "vllm",
        "OPENAI_API_KEY": "embedding-secret",
        "VLLM_BASE_URL": "https://gpu.internal/v1",
        "VLLM_API_KEY": "vllm-secret",
        "VLLM_LLM_MODEL": "gemma",
    }
    del provider[missing]
    with pytest.raises(MODULE.ConfigurationError, match=missing):
        MODULE.build_settings(
            provider,
            {
                "POSTGRES_PASSWORD": "db-secret",
                "POOLER_TENANT_ID": "tenant-id",
                "ANON_KEY": "anon-secret",
                "SERVICE_ROLE_KEY": "service-secret",
            },
        )


def test_build_settings_fails_closed_for_unsupported_embedding_provider() -> None:
    with pytest.raises(
        MODULE.ConfigurationError,
        match="PREREVIEW_EMBEDDING_PROVIDER",
    ):
        MODULE.build_settings(
            {
                "PREREVIEW_EMBEDDING_PROVIDER": "vllm",
                "OPENAI_API_KEY": "embedding-secret",
            },
            {
                "POSTGRES_PASSWORD": "db-secret",
                "POOLER_TENANT_ID": "tenant-id",
                "ANON_KEY": "anon-secret",
                "SERVICE_ROLE_KEY": "service-secret",
            },
        )


def test_cli_creates_mode_600_env_without_printing_secrets(tmp_path: Path) -> None:
    provider, supabase, model1 = _valid_inputs(tmp_path)
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
            "--model1-serving-dir",
            str(model1),
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
    assert generated["PREREVIEW_LLM_PROVIDER"] == "openai"
    assert generated["PREREVIEW_EMBEDDING_PROVIDER"] == "openai"
    assert generated["VLLM_MAX_REPAIRS"] == "2"
    assert generated["PREREVIEW_MODEL1_SERVING_HOST_DIR"] == str(model1.resolve())
    assert generated["PREREVIEW_MODEL1_RUNTIME_UID"] == str(model1.stat().st_uid)
    assert generated["PREREVIEW_MODEL1_RUNTIME_GID"] == str(model1.stat().st_gid)


def test_cli_never_overwrites_existing_output(tmp_path: Path) -> None:
    provider, supabase, model1 = _valid_inputs(tmp_path)
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
            "--model1-serving-dir",
            str(model1),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == sentinel
    assert "덮어쓰지 않습니다" in result.stderr


def test_cli_never_follows_existing_output_symlink(tmp_path: Path) -> None:
    provider, supabase, model1 = _valid_inputs(tmp_path)
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
            "--model1-serving-dir",
            str(model1),
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
    provider, supabase, model1 = _valid_inputs(tmp_path)
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
            "--model1-serving-dir",
            str(model1),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "chmod 600" in result.stderr
    assert "openai-test-secret" not in result.stderr
    assert not output.exists()


def test_cli_requires_prepared_model1_runtime_before_writing_env(tmp_path: Path) -> None:
    provider, supabase, _model1 = _valid_inputs(tmp_path)
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
            "--model1-serving-dir",
            str(tmp_path / "missing-model1"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "prepare_model1_runtime.py" in result.stderr
    assert not output.exists()


def test_model1_mount_identity_rejects_symlink(tmp_path: Path) -> None:
    _provider, _supabase, model1 = _valid_inputs(tmp_path)
    linked = tmp_path / "linked-model1"
    linked.symlink_to(model1, target_is_directory=True)

    with pytest.raises(MODULE.ConfigurationError, match="symlink"):
        MODULE._model1_mount_identity(linked)


def test_model1_mount_identity_rejects_symlinked_model_directory(
    tmp_path: Path,
) -> None:
    model1 = tmp_path / "model1"
    external_model = tmp_path / "external-model"
    external_model.mkdir()
    (external_model / "model.safetensors").write_bytes(b"test-weight")
    model1.mkdir(mode=0o700)
    (model1 / "inference.py").write_text("# test runtime\n", encoding="utf-8")
    (model1 / "model").symlink_to(external_model, target_is_directory=True)

    with pytest.raises(MODULE.ConfigurationError, match="model.*symlink"):
        MODULE._model1_mount_identity(model1)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode policy")
def test_model1_mount_identity_rejects_group_or_world_access(tmp_path: Path) -> None:
    _provider, _supabase, model1 = _valid_inputs(tmp_path)
    model1.chmod(0o750)

    with pytest.raises(MODULE.ConfigurationError, match="chmod 700"):
        MODULE._model1_mount_identity(model1)


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0,
    reason="root-owned runtime can only be constructed by a root test process",
)
def test_model1_mount_identity_rejects_root_owner(tmp_path: Path) -> None:
    model1 = tmp_path / "model1"
    (model1 / "model").mkdir(parents=True)
    (model1 / "inference.py").write_text("# test runtime\n", encoding="utf-8")
    (model1 / "model" / "model.safetensors").write_bytes(b"test-weight")
    model1.chmod(0o700)

    with pytest.raises(MODULE.ConfigurationError, match="root가 아닌"):
        MODULE._model1_mount_identity(model1)


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
