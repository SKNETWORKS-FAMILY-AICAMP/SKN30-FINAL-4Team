from __future__ import annotations

import asyncio
import argparse
from collections.abc import Callable
import importlib.util
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "run_local_live_e2e.py"
SPEC = importlib.util.spec_from_file_location("run_local_live_e2e", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def make_hwpx() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "mimetype", "application/hwp+zip", compress_type=ZIP_STORED
        )
        archive.writestr(
            "META-INF/container.xml", "<container/>", compress_type=ZIP_DEFLATED
        )
        archive.writestr(
            "Contents/content.hpf", "<opf/>", compress_type=ZIP_DEFLATED
        )
        archive.writestr(
            "Contents/header.xml", "<head/>", compress_type=ZIP_DEFLATED
        )
        archive.writestr(
            "Contents/section0.xml", "<section/>", compress_type=ZIP_DEFLATED
        )
    return buffer.getvalue()


HWPX = make_hwpx()


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("request.hwp", "application/x-hwp"),
        ("request.hwpx", "application/vnd.hancom.hwpx"),
        ("request.HWP", "application/x-hwp"),
        ("request.HWPX", "application/vnd.hancom.hwpx"),
    ],
)
def test_source_mime_type_selects_by_supported_extension(
    filename: str,
    expected: str,
) -> None:
    assert MODULE._source_mime_type(Path(filename)) == expected


@pytest.mark.parametrize("filename", ["request.pdf", "request.hwp.exe", "request"])
def test_source_mime_type_rejects_unsupported_extension(filename: str) -> None:
    with pytest.raises(MODULE.E2EFailure, match="must be an HWP or HWPX file"):
        MODULE._source_mime_type(Path(filename))


def test_external_mode_requires_a_deployed_api_url() -> None:
    with pytest.raises(
        MODULE.E2EFailure,
        match="external worker mode requires a deployed --api-base-url",
    ):
        asyncio.run(
            MODULE._run(
                Path("never-read.hwpx"),
                worker_mode="external",
                api_base_url=None,
            )
        )


def test_inline_mode_rejects_a_deployed_api_url() -> None:
    with pytest.raises(
        MODULE.E2EFailure,
        match="--api-base-url requires --worker-mode external",
    ):
        asyncio.run(
            MODULE._run(
                Path("never-read.hwpx"),
                worker_mode="inline",
                api_base_url="https://api.example.test",
            )
        )


@pytest.mark.parametrize("raw", ("0", "-1", "nan", "inf", "not-a-number"))
def test_poll_timeout_rejects_non_positive_or_non_finite_values(raw: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        MODULE._positive_timeout_seconds(raw)


def test_poll_timeout_accepts_a_positive_value() -> None:
    assert MODULE._positive_timeout_seconds("123.5") == 123.5


def test_main_forwards_the_report_poll_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "request.hwpx"
    source.write_bytes(HWPX)
    captured: dict[str, object] = {}

    async def run(path: Path, **kwargs: object) -> dict[str, str]:
        captured["path"] = path
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(MODULE, "_configure_environment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(MODULE, "_run", run)
    monkeypatch.setattr(
        MODULE.sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--file",
            str(source),
            "--report-poll-timeout-seconds",
            "17.5",
        ],
    )

    assert MODULE.main() == 0
    assert captured["path"] == source.resolve()
    assert captured["report_poll_timeout_seconds"] == 17.5
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}


def test_configure_environment_carries_ml_settings_and_preserves_shell_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "\n".join(
            (
                "OPENAI_API_KEY=test-openai-key",
                "OPENAI_REQUEST_PROFILE_MODEL=from-backend-env-terra",
                "PREREVIEW_ML_ROOT=/from/backend-env/ml",
                "PREREVIEW_MODEL1_SERVING_DIR=/from/backend-env/model1",
                "PREREVIEW_ML_PYTHON_EXECUTABLE=/from/backend-env/python",
                "PREREVIEW_ML_TIMEOUT_SECONDS=180",
            )
        ),
        encoding="utf-8",
    )
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "\n".join(
            (
                "POSTGRES_PASSWORD=test-password",
                "POOLER_TENANT_ID=test-tenant",
                "ANON_KEY=test-anon-key",
                "SERVICE_ROLE_KEY=test-service-key",
            )
        ),
        encoding="utf-8",
    )
    environment = {
        "PREREVIEW_ML_TIMEOUT_SECONDS": "240",
        "OPENAI_REQUEST_PROFILE_MODEL": "from-shell-terra",
    }
    monkeypatch.setattr(MODULE.os, "environ", environment)

    MODULE._configure_environment(backend_env, supabase_env)

    assert environment["PREREVIEW_ML_ROOT"] == "/from/backend-env/ml"
    assert environment["PREREVIEW_MODEL1_SERVING_DIR"] == "/from/backend-env/model1"
    assert (
        environment["PREREVIEW_ML_PYTHON_EXECUTABLE"]
        == "/from/backend-env/python"
    )
    assert environment["PREREVIEW_ML_TIMEOUT_SECONDS"] == "240"
    assert environment["OPENAI_REQUEST_PROFILE_MODEL"] == "from-shell-terra"


def test_configure_environment_defaults_request_profile_model_to_terra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("OPENAI_API_KEY=test-openai-key\n", encoding="utf-8")
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "\n".join(
            (
                "POSTGRES_PASSWORD=test-password",
                "POOLER_TENANT_ID=test-tenant",
                "ANON_KEY=test-anon-key",
                "SERVICE_ROLE_KEY=test-service-key",
            )
        ),
        encoding="utf-8",
    )
    environment: dict[str, str] = {}
    monkeypatch.setattr(MODULE.os, "environ", environment)

    MODULE._configure_environment(backend_env, supabase_env)

    assert environment["OPENAI_LLM_MODEL"] == "gpt-5.6-terra"
    assert environment["OPENAI_REQUEST_PROFILE_MODEL"] == "gpt-5.6-terra"


def test_inline_configure_environment_loads_selected_vllm_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "OPENAI_API_KEY=embedding-key\n"
        "OPENAI_EMBEDDING_MODEL=embedding-model\n"
        "PREREVIEW_LLM_PROVIDER=vllm\n"
        "VLLM_BASE_URL=https://gpu.internal/v1\n"
        "VLLM_API_KEY=vllm-key\n"
        "VLLM_LLM_MODEL=served-model\n"
        "VLLM_TIMEOUT_SECONDS=42\n"
        "VLLM_MAX_OUTPUT_TOKENS=1234\n"
        "VLLM_MAX_RESPONSE_BYTES=2048\n",
        encoding="utf-8",
    )
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "POSTGRES_PASSWORD=test-password\n"
        "POOLER_TENANT_ID=test-tenant\n"
        "ANON_KEY=test-anon-key\n"
        "SERVICE_ROLE_KEY=test-service-key\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}
    monkeypatch.setattr(MODULE.os, "environ", environment)

    MODULE._configure_environment(backend_env, supabase_env)

    assert environment["PREREVIEW_LLM_PROVIDER"] == "vllm"
    assert environment["VLLM_BASE_URL"] == "https://gpu.internal/v1"
    assert environment["VLLM_API_KEY"] == "vllm-key"
    assert environment["VLLM_LLM_MODEL"] == "served-model"
    assert environment["VLLM_TIMEOUT_SECONDS"] == "42"
    assert environment["VLLM_MAX_OUTPUT_TOKENS"] == "1234"
    assert environment["VLLM_MAX_RESPONSE_BYTES"] == "2048"


def test_configure_environment_preserves_explicit_blank_stage_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "OPENAI_API_KEY=test-openai-key\n"
        "OPENAI_REQUEST_PROFILE_MODEL=from-backend-env-terra\n"
        "OPENAI_CHAT_MODEL=from-backend-env-chat\n",
        encoding="utf-8",
    )
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "POSTGRES_PASSWORD=test-password\n"
        "POOLER_TENANT_ID=test-tenant\n"
        "ANON_KEY=test-anon-key\n"
        "SERVICE_ROLE_KEY=test-service-key\n",
        encoding="utf-8",
    )
    environment = {
        "OPENAI_REQUEST_PROFILE_MODEL": "",
        "OPENAI_CHAT_MODEL": "",
    }
    monkeypatch.setattr(MODULE.os, "environ", environment)

    MODULE._configure_environment(backend_env, supabase_env)

    assert environment["OPENAI_REQUEST_PROFILE_MODEL"] == ""
    assert environment["OPENAI_CHAT_MODEL"] == ""


def test_inline_configure_environment_loads_cursor_secret_from_backend_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "OPENAI_API_KEY=test-openai-key\n"
        "PREREVIEW_CURSOR_SIGNING_SECRET=backend-only-cursor-secret\n",
        encoding="utf-8",
    )
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "POSTGRES_PASSWORD=test-password\n"
        "POOLER_TENANT_ID=test-tenant\n"
        "ANON_KEY=test-anon-key\n"
        "SERVICE_ROLE_KEY=test-service-key\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}
    monkeypatch.setattr(MODULE.os, "environ", environment)

    MODULE._configure_environment(
        backend_env,
        supabase_env,
        require_cursor_signing_secret=True,
    )

    assert environment["PREREVIEW_CURSOR_SIGNING_SECRET"] == "backend-only-cursor-secret"


def test_inline_configure_environment_prefers_existing_cursor_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "OPENAI_API_KEY=test-openai-key\n"
        "PREREVIEW_CURSOR_SIGNING_SECRET=backend-cursor-secret\n",
        encoding="utf-8",
    )
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "POSTGRES_PASSWORD=test-password\n"
        "POOLER_TENANT_ID=test-tenant\n"
        "ANON_KEY=test-anon-key\n"
        "SERVICE_ROLE_KEY=test-service-key\n",
        encoding="utf-8",
    )
    environment = {"PREREVIEW_CURSOR_SIGNING_SECRET": "shell-cursor-secret"}
    monkeypatch.setattr(MODULE.os, "environ", environment)

    MODULE._configure_environment(
        backend_env,
        supabase_env,
        require_cursor_signing_secret=True,
    )

    assert environment["PREREVIEW_CURSOR_SIGNING_SECRET"] == "shell-cursor-secret"


def test_inline_configure_environment_requires_cursor_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("OPENAI_API_KEY=test-openai-key\n", encoding="utf-8")
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "POSTGRES_PASSWORD=test-password\n"
        "POOLER_TENANT_ID=test-tenant\n"
        "ANON_KEY=test-anon-key\n"
        "SERVICE_ROLE_KEY=test-service-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE.os, "environ", {})

    with pytest.raises(MODULE.E2EFailure, match="PREREVIEW_CURSOR_SIGNING_SECRET"):
        MODULE._configure_environment(
            backend_env,
            supabase_env,
            require_cursor_signing_secret=True,
        )


def test_configure_environment_does_not_override_deployed_api_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("OPENAI_API_KEY=test-openai-key\n", encoding="utf-8")
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "POSTGRES_PASSWORD=test-password\n"
        "POOLER_TENANT_ID=test-tenant\n"
        "ANON_KEY=test-anon-key\n"
        "SERVICE_ROLE_KEY=test-service-key\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}
    monkeypatch.setattr(MODULE.os, "environ", environment)

    MODULE._configure_environment(
        backend_env,
        supabase_env,
        auth_allowed_origins=None,
    )

    assert "PREREVIEW_AUTH_ALLOWED_ORIGINS" not in environment


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:8001", "http://127.0.0.1:8001"),
        ("http://[::1]:8001/", "http://[::1]:8001"),
        ("https://api.example.test/", "https://api.example.test"),
    ],
)
def test_api_base_url_accepts_https_or_loopback_http(
    value: str,
    expected: str,
) -> None:
    assert MODULE._validated_api_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.test",
        "ftp://127.0.0.1:8001",
        "http://user:password@127.0.0.1:8001",
        "http://127.0.0.1:8001/api",
        "http://127.0.0.1:8001?next=https://evil.example",
    ],
)
def test_api_base_url_rejects_unsafe_or_non_origin_values(value: str) -> None:
    with pytest.raises(MODULE.E2EFailure):
        MODULE._validated_api_base_url(value)


def test_external_request_origin_prefers_the_explicit_safe_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "PREREVIEW_AUTH_ALLOWED_ORIGINS=https://ignored.example\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE.os, "environ", {})

    origin = MODULE._external_request_origin(
        explicit_origin="https://frontend.example.test/",
        backend_env=backend_env,
    )

    assert origin == "https://frontend.example.test"


def test_external_request_origin_uses_first_backend_allowed_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "PREREVIEW_AUTH_ALLOWED_ORIGINS=https://first.example,https://second.example\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE.os, "environ", {})

    origin = MODULE._external_request_origin(
        explicit_origin=None,
        backend_env=backend_env,
    )

    assert origin == "https://first.example"


def test_external_request_origin_requires_a_configured_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("# no allowed origin\n", encoding="utf-8")
    monkeypatch.setattr(MODULE.os, "environ", {})

    with pytest.raises(MODULE.E2EFailure, match="--api-base-url requires"):
        MODULE._external_request_origin(
            explicit_origin=None,
            backend_env=backend_env,
        )


def test_live_e2e_hardens_provider_logging_before_external_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger_names = ("openai", "openai._base_client", "httpx", "httpcore")
    loggers = [logging.getLogger(name) for name in logger_names]
    original_levels = [logger.level for logger in loggers]
    root_logger = logging.getLogger()
    original_root_level = root_logger.level

    class StopBeforeExternalWork(RuntimeError):
        pass

    async def stop_after_logging_is_hardened() -> None:
        assert all(logger.getEffectiveLevel() == logging.WARNING for logger in loggers)
        raise StopBeforeExternalWork

    monkeypatch.setenv("OPENAI_LOG", "debug")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DATABASE_URL", "not-used")
    monkeypatch.setattr(MODULE, "_assert_queues_quiescent", lambda _url: None)
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    monkeypatch.setattr(
        MODULE,
        "_create_confirmed_test_user",
        stop_after_logging_is_hardened,
    )
    try:
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        with pytest.raises(StopBeforeExternalWork):
            asyncio.run(
                MODULE._run(
                    Path("never-read.hwp"),
                    source_content=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture",
                    source_mime_type="application/x-hwp",
                )
            )
    finally:
        root_logger.setLevel(original_root_level)
        for logger, original_level in zip(loggers, original_levels, strict=True):
            logger.setLevel(original_level)


def test_external_audit_failure_is_not_hidden_by_missing_worker_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self, status_code: int, payload: dict[str, str]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, str]:
            return self._payload

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, path: str, **_kwargs: object) -> Response:
            if path == "/api/v1/auth/sign-in":
                return Response(200, {"status": "ok"})
            assert path == "/api/v1/analysis-runs"
            return Response(
                202,
                {"analysis_run_id": "run-id", "status": "queued"},
            )

    async def create_user() -> tuple[str, str, str]:
        return "user-id", "user@example.test", "password"

    async def api_build_identity(
        _client: object, *, expected_build_id: str
    ) -> str:
        return expected_build_id

    async def poll_analysis(**_kwargs: object) -> tuple[dict[str, str], int]:
        return {"status": "succeeded"}, 1

    audit_failure = MODULE.E2EFailure("durable analysis audit failed")

    def reject_audit(*_args: object, **_kwargs: object) -> None:
        raise audit_failure

    manifest_worker_ids: list[str | None] = []

    def partial_manifest(**kwargs: object) -> dict[str, object]:
        worker_id = kwargs["analysis_worker_id"]
        assert worker_id is None or isinstance(worker_id, str)
        manifest_worker_ids.append(worker_id)
        return {"analysis_worker_id": worker_id}

    captured_manifests: list[object] = []

    async def capture_trace(**kwargs: object) -> bool:
        captured_manifests.append(kwargs["execution_manifest"])
        return True

    monkeypatch.setenv("DATABASE_URL", "not-used")
    monkeypatch.setitem(
        sys.modules,
        "worker.main",
        SimpleNamespace(configure_runtime_logging=lambda **_kwargs: None),
    )
    # Replace only the script module references.  Mutating the process-wide
    # asyncio/httpx modules makes this test order-dependent: worker.main imports
    # OpenAI, which must still be able to subclass the real httpx.AsyncClient.
    monkeypatch.setattr(
        MODULE,
        "asyncio",
        SimpleNamespace(to_thread=_inline_to_thread),
    )
    monkeypatch.setattr(
        MODULE,
        "_require_clean_checkout_build_context_digest",
        lambda: "a" * 64,
    )
    monkeypatch.setattr(MODULE, "_assert_queues_quiescent", lambda _url: None)
    monkeypatch.setattr(MODULE, "_create_confirmed_test_user", create_user)
    monkeypatch.setattr(
        MODULE,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **_kwargs: Client()),
    )
    monkeypatch.setattr(MODULE, "_external_api_build_identity", api_build_identity)
    monkeypatch.setattr(
        MODULE,
        "_poll_external_analysis_until_terminal",
        poll_analysis,
    )
    monkeypatch.setattr(
        MODULE,
        "_require_external_analysis_worker_audit",
        reject_audit,
    )
    monkeypatch.setattr(MODULE, "_partial_execution_manifest", partial_manifest)
    monkeypatch.setattr(MODULE, "_capture_analysis_failure_trace", capture_trace)

    with pytest.raises(MODULE.E2EFailure, match="durable analysis audit failed") as error:
        asyncio.run(
            MODULE._run(
                Path("request.hwpx"),
                worker_mode="external",
                api_base_url="https://api.example.test",
                source_content=HWPX,
                source_mime_type="application/vnd.hancom.hwpx",
            )
        )

    assert error.value is audit_failure
    assert manifest_worker_ids == [None]
    assert captured_manifests == [{"analysis_worker_id": None}]


class _DelegateRepository:
    def __init__(self, job_pk: str | None) -> None:
        self.job_pk = job_pk
        self.claim_calls = 0

    def claim(self, *, worker_id: str, lease_seconds: int) -> object | None:
        assert worker_id == "worker-test"
        assert lease_seconds == 120
        self.claim_calls += 1
        if self.job_pk is None:
            return None
        return SimpleNamespace(job_pk=self.job_pk)

    def heartbeat(self, **kwargs: object) -> bool:
        return True

    def complete(self, **kwargs: object) -> bool:
        return True

    def fail(self, **kwargs: object) -> bool:
        return True


def test_target_repository_claims_only_the_requested_run() -> None:
    delegate = _DelegateRepository("target-run")
    repository = MODULE._TargetRunRepository(
        delegate,
        target_run_id="target-run",
        database_url="not-used",
        claim_target=delegate.claim,
    )

    job = repository.claim(worker_id="worker-test", lease_seconds=120)

    assert job is not None
    assert job.job_pk == "target-run"
    assert repository.target_claims == 1
    assert repository.unexpected_claim is False


def test_target_repository_fails_closed_on_an_unexpected_claim() -> None:
    delegate = _DelegateRepository("another-run")
    repository = MODULE._TargetRunRepository(
        delegate,
        target_run_id="target-run",
        database_url="not-used",
        claim_target=delegate.claim,
    )

    with pytest.raises(MODULE.E2EFailure, match="unexpected analysis run"):
        repository.claim(worker_id="worker-test", lease_seconds=120)

    assert repository.target_claims == 0
    assert repository.unexpected_claim is True


def test_target_repository_retains_safe_guard_failure() -> None:
    def unavailable_claim(**_kwargs: object) -> None:
        raise MODULE.E2EFailure("Local E2E queue isolation is unavailable")

    repository = MODULE._TargetRunRepository(
        _DelegateRepository("target-run"),
        target_run_id="target-run",
        database_url="not-used",
        claim_target=unavailable_claim,
    )

    with pytest.raises(MODULE.E2EFailure, match="queue isolation is unavailable"):
        repository.claim(worker_id="worker-test", lease_seconds=120)

    assert repository.claim_error is not None


def test_target_chat_repository_claims_only_the_created_assistant_message() -> None:
    target = "8c5ce7e8-6be4-4d86-bc4c-7c2af01db4d1"
    delegate = _DelegateRepository(target)
    repository = MODULE._TargetChatRepository(
        delegate,
        target_assistant_message_id=target,
        database_url="not-used",
        claim_target=delegate.claim,
    )

    job = repository.claim(worker_id="worker-test", lease_seconds=120)

    assert job is not None
    assert job.job_pk == target
    assert repository.target_claims == 1
    assert repository.unexpected_claim is False


def test_target_chat_repository_fails_closed_on_an_unexpected_claim() -> None:
    delegate = _DelegateRepository("another-message")
    repository = MODULE._TargetChatRepository(
        delegate,
        target_assistant_message_id="target-message",
        database_url="not-used",
        claim_target=delegate.claim,
    )

    with pytest.raises(MODULE.E2EFailure, match="unexpected chat message"):
        repository.claim(worker_id="worker-test", lease_seconds=120)

    assert repository.target_claims == 0
    assert repository.unexpected_claim is True


def test_target_chat_repository_retains_safe_guard_failure() -> None:
    def unavailable_claim(**_kwargs: object) -> None:
        raise MODULE.E2EFailure("Local E2E chat queue isolation is unavailable")

    repository = MODULE._TargetChatRepository(
        _DelegateRepository("target-message"),
        target_assistant_message_id="target-message",
        database_url="not-used",
        claim_target=unavailable_claim,
    )

    with pytest.raises(MODULE.E2EFailure, match="chat queue isolation is unavailable"):
        repository.claim(worker_id="worker-test", lease_seconds=120)

    assert repository.claim_error is not None


def test_target_chat_claim_is_verified_before_database_transaction_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object | None]] = []
    exits: list[type[BaseException] | None] = []
    assistant_id = "8c5ce7e8-6be4-4d86-bc4c-7c2af01db4d1"
    row = {
        "assistant_message_id": assistant_id,
        "analysis_case_id": "64e71208-51ea-4fb6-a1b7-1490c15da1e2",
        "analysis_session_id": "df4cfc7c-a0b2-4aa9-a425-17bc633a73ec",
        "user_message_id": "eef28123-aa10-4717-883a-01b5ae8c093a",
        "owner_id": "bbf2cbe8-95ef-4c83-b7c8-9072a2c6d53c",
        "question": "분석 결과를 요약해 주세요.",
        "result_payload": {"ml": {}},
        "conversation": [],
        "processing_run_pk": "7f4b5f49-f24f-4cdb-9271-8d5340068ec7",
        "attempt_count": 1,
        "lease_expires_at": "later",
        "heartbeat_interval_seconds": 30,
    }

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: object | None = None) -> None:
            calls.append((" ".join(query.split()), params))

        def fetchall(self) -> list[object]:
            return []

        def fetchone(self) -> dict[str, object]:
            return dict(row)

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: object,
            _traceback: object,
        ) -> None:
            exits.append(exc_type)
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(
        MODULE.psycopg,
        "connect",
        lambda *_args, **_kwargs: Connection(),
    )

    job = MODULE._claim_target_chat_message(
        "postgresql://test",
        assistant_id,
        worker_id="chat-worker-test",
        lease_seconds=120,
    )

    assert str(job.job_pk) == assistant_id
    assert calls[0] == ("SET LOCAL lock_timeout = '5s'", None)
    assert calls[1] == ("SET LOCAL statement_timeout = '15s'", None)
    assert "pg_advisory_xact_lock" in calls[2][0]
    assert "workspace.claim_next_conversation_message_v2" in calls[3][0]
    assert "workspace.claim_next_conversation_message(" not in calls[3][0]
    assert calls[3][1] == ("chat-worker-test", 120)
    assert exits == [None]


def test_chat_create_request_uses_a_uuid_idempotency_header() -> None:
    class Response:
        pass

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

        async def post(
            self,
            path: str,
            *,
            headers: dict[str, str],
            json: dict[str, str],
        ) -> Response:
            self.calls.append((path, headers, json))
            return Response()

    client = Client()

    response = asyncio.run(
        MODULE._post_chat_message(
            client,
            case_id="8c5ce7e8-6be4-4d86-bc4c-7c2af01db4d1",
            request_origin="https://e2e.example.test",
        )
    )

    assert isinstance(response, Response)
    assert len(client.calls) == 1
    path, headers, payload = client.calls[0]
    assert path == "/api/v1/analysis-cases/8c5ce7e8-6be4-4d86-bc4c-7c2af01db4d1/messages"
    assert headers["Origin"] == "https://e2e.example.test"
    assert str(MODULE.UUID(headers["Idempotency-Key"])) == headers["Idempotency-Key"]
    assert payload == {"content": MODULE.E2E_CHAT_QUESTION}


def test_target_claim_is_verified_before_database_transaction_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object | None]] = []
    exits: list[type[BaseException] | None] = []
    row = {
        "analysis_run_pk": "target-run",
        "source_bucket": "request-temp",
        "source_object_key": "run/source/hash.hwp",
        "source_content_sha256": "a" * 64,
        "processing_run_pk": "processing-run",
        "attempt_count": 1,
        "lease_expires_at": "later",
        "heartbeat_interval_seconds": 30,
    }

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: object | None = None) -> None:
            calls.append((" ".join(query.split()), params))

        def fetchall(self) -> list[object]:
            return []

        def fetchone(self) -> dict[str, object]:
            return dict(row)

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: object,
            _traceback: object,
        ) -> None:
            exits.append(exc_type)
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    def connect(database_url: str, **kwargs: object) -> Connection:
        assert database_url == "postgresql://test"
        assert kwargs == {"connect_timeout": 10, "row_factory": MODULE.dict_row}
        return Connection()

    monkeypatch.setattr(MODULE.psycopg, "connect", connect)

    job = MODULE._claim_target_analysis_run(
        "postgresql://test",
        "target-run",
        worker_id="worker-test",
        lease_seconds=120,
    )

    assert job.job_pk == "target-run"
    assert job.processing_run_pk == "processing-run"
    assert calls[0] == ("SET LOCAL lock_timeout = '5s'", None)
    assert calls[1] == ("SET LOCAL statement_timeout = '15s'", None)
    assert "pg_advisory_xact_lock" in calls[2][0]
    assert "workspace.claim_next_analysis_run" in calls[3][0]
    assert calls[3][1] == ("worker-test", 120)
    assert exits == [None]


def test_target_claim_maps_only_guard_acquisition_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[type[BaseException] | None] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, _params: object | None = None) -> None:
            if "pg_advisory_xact_lock" in query:
                raise MODULE.psycopg.OperationalError("advisory lock unavailable")

        def fetchall(self) -> list[object]:
            return []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: object,
            _traceback: object,
        ) -> None:
            exits.append(exc_type)
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(
        MODULE.psycopg,
        "connect",
        lambda *_args, **_kwargs: Connection(),
    )

    with pytest.raises(MODULE.E2EFailure, match="queue isolation is unavailable"):
        MODULE._claim_target_analysis_run(
            "postgresql://test",
            "target-run",
            worker_id="worker-test",
            lease_seconds=120,
        )

    assert exits == [MODULE.E2EFailure]


def test_target_claim_database_error_after_guard_is_safely_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[type[BaseException] | None] = []
    expected_error = MODULE.psycopg.OperationalError(
        "private DSN and claim query must not be logged"
    )

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, _params: object | None = None) -> None:
            if "workspace.claim_next_analysis_run" in query:
                raise expected_error

        def fetchall(self) -> list[object]:
            return []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: object,
            _traceback: object,
        ) -> None:
            exits.append(exc_type)
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(
        MODULE.psycopg,
        "connect",
        lambda *_args, **_kwargs: Connection(),
    )

    with pytest.raises(MODULE.E2EFailure, match="queue claim failed") as raised:
        MODULE._claim_target_analysis_run(
            "postgresql://test",
            "target-run",
            worker_id="worker-test",
            lease_seconds=120,
        )

    assert "private DSN" not in str(raised.value)
    assert exits == [MODULE.psycopg.OperationalError]


def test_target_claim_commit_error_is_safely_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: object | None = None) -> None:
            return None

        def fetchall(self) -> list[object]:
            return []

        def fetchone(self) -> None:
            return None

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: object,
            _traceback: object,
        ) -> None:
            assert exc_type is None
            raise MODULE.psycopg.OperationalError(
                "private commit details must not be logged"
            )

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(
        MODULE.psycopg,
        "connect",
        lambda *_args, **_kwargs: Connection(),
    )

    with pytest.raises(MODULE.E2EFailure, match="queue claim failed") as raised:
        MODULE._claim_target_analysis_run(
            "postgresql://test",
            "target-run",
            worker_id="worker-test",
            lease_seconds=120,
        )

    assert "private commit" not in str(raised.value)


def test_unexpected_database_claim_rolls_back_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[type[BaseException] | None] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: object | None = None) -> None:
            return None

        def fetchall(self) -> list[object]:
            return []

        def fetchone(self) -> dict[str, object]:
            return {
                "analysis_run_pk": "another-run",
                "source_bucket": "request-temp",
                "source_object_key": "other/source/hash.hwp",
                "source_content_sha256": "b" * 64,
                "processing_run_pk": "processing-run",
                "attempt_count": 1,
                "lease_expires_at": "later",
                "heartbeat_interval_seconds": 30,
            }

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: object,
            _traceback: object,
        ) -> None:
            exits.append(exc_type)
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(
        MODULE.psycopg,
        "connect",
        lambda *_args, **_kwargs: Connection(),
    )

    with pytest.raises(MODULE.E2EFailure, match="unexpected analysis run"):
        MODULE._claim_target_analysis_run(
            "postgresql://test",
            "target-run",
            worker_id="worker-test",
            lease_seconds=120,
        )

    assert exits == [MODULE._UnexpectedClaim]


def test_delegate_database_errors_are_not_relabelled_as_queue_isolation() -> None:
    expected_error = MODULE.psycopg.OperationalError("completion failed")

    class FailingDelegate:
        def heartbeat(self, **_kwargs: object) -> bool:
            raise expected_error

        def complete(self, **_kwargs: object) -> bool:
            raise expected_error

        def fail(self, **_kwargs: object) -> bool:
            raise expected_error

    repository = MODULE._TargetRunRepository(
        FailingDelegate(),
        target_run_id="target-run",
        database_url="not-used",
    )

    with pytest.raises(MODULE.psycopg.OperationalError) as raised:
        repository.complete(job_pk="target-run")

    assert raised.value is expected_error


class _StateResponse:
    status_code = 200

    def __init__(self, run_id: str, status: str) -> None:
        self._payload = {
            "analysis_run_id": run_id,
            "status": status,
            "analysis_case_id": "case-id" if status == "succeeded" else None,
        }

    def json(self) -> dict[str, object]:
        return dict(self._payload)


class _StateClient:
    def __init__(self, run_id: str, statuses: list[str]) -> None:
        self.run_id = run_id
        self.statuses = list(statuses)
        self.calls = 0

    async def get(self, path: str) -> _StateResponse:
        assert path == f"/api/v1/analysis-runs/{self.run_id}"
        self.calls += 1
        return _StateResponse(self.run_id, self.statuses.pop(0))


class _AttemptRuntime:
    def __init__(self, repository: object, outcomes: list[str]) -> None:
        self.repository = repository
        self.outcomes = list(outcomes)
        self.calls = 0

    def run_once(self) -> object:
        self.calls += 1
        self.repository.target_claims += 1
        return SimpleNamespace(value=self.outcomes.pop(0))


async def _inline_to_thread(
    function: Callable[..., object], *args: object, **kwargs: object
) -> object:
    return function(*args, **kwargs)


def test_retryable_first_failure_runs_the_same_target_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    run_id = "target-run"
    repository = SimpleNamespace(target_claims=0, unexpected_claim=False)
    runtime = _AttemptRuntime(repository, ["failed", "completed"])
    client = _StateClient(run_id, ["queued", "succeeded"])

    state, outcome, attempts = asyncio.run(
        MODULE._run_target_until_terminal(
            runtime=runtime,
            repository=repository,
            client=client,
            run_id=run_id,
        )
    )

    assert state["status"] == "succeeded"
    assert outcome == "completed"
    assert attempts == 2
    assert runtime.calls == 2
    assert client.calls == 2


def test_succeeded_analysis_is_rejected_when_this_worker_was_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    run_id = "target-run"
    repository = SimpleNamespace(target_claims=0, unexpected_claim=False)
    runtime = _AttemptRuntime(repository, ["fenced"])
    client = _StateClient(run_id, ["succeeded"])

    with pytest.raises(MODULE.E2EFailure, match="unexpected state"):
        asyncio.run(
            MODULE._run_target_until_terminal(
                runtime=runtime,
                repository=repository,
                client=client,
                run_id=run_id,
            )
        )

    assert runtime.calls == 1
    assert client.calls == 1


def test_retry_loop_is_bounded_by_the_database_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    run_id = "target-run"
    repository = SimpleNamespace(target_claims=0, unexpected_claim=False)
    runtime = _AttemptRuntime(repository, ["failed", "failed"])
    client = _StateClient(run_id, ["queued", "queued"])

    with pytest.raises(MODULE.E2EFailure, match="retry budget was exhausted"):
        asyncio.run(
            MODULE._run_target_until_terminal(
                runtime=runtime,
                repository=repository,
                client=client,
                run_id=run_id,
            )
        )

    assert runtime.calls == MODULE.MAX_WORKER_ATTEMPTS
    assert client.calls == MODULE.MAX_WORKER_ATTEMPTS


def test_terminal_failure_stops_without_an_extra_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    run_id = "target-run"
    repository = SimpleNamespace(target_claims=0, unexpected_claim=False)
    runtime = _AttemptRuntime(repository, ["failed", "failed"])
    client = _StateClient(run_id, ["queued", "failed"])

    with pytest.raises(MODULE.E2EFailure, match="terminal failure after 2 attempt"):
        asyncio.run(
            MODULE._run_target_until_terminal(
                runtime=runtime,
                repository=repository,
                client=client,
                run_id=run_id,
            )
        )

    assert runtime.calls == 2
    assert client.calls == 2


def test_retry_loop_surfaces_repository_claim_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    failure = MODULE.E2EFailure("Local E2E queue isolation is unavailable")
    repository = SimpleNamespace(
        target_claims=0,
        unexpected_claim=False,
        claim_error=failure,
    )
    runtime = _AttemptRuntime(repository, ["unavailable"])
    client = _StateClient("target-run", ["queued"])

    with pytest.raises(MODULE.E2EFailure, match="queue isolation is unavailable"):
        asyncio.run(
            MODULE._run_target_until_terminal(
                runtime=runtime,
                repository=repository,
                client=client,
                run_id="target-run",
            )
        )

    assert client.calls == 0


def test_external_analysis_polling_waits_for_the_public_terminal_success() -> None:
    run_id = "target-run"
    client = _StateClient(run_id, ["queued", "running", "succeeded"])

    state, polls = asyncio.run(
        MODULE._poll_external_analysis_until_terminal(
            client=client,
            run_id=run_id,
        )
    )

    assert state["status"] == "succeeded"
    assert polls == 3
    assert client.calls == 3


def test_external_analysis_polling_rejects_a_terminal_failure() -> None:
    client = _StateClient("target-run", ["failed"])

    with pytest.raises(MODULE.E2EFailure, match="external analysis worker reached"):
        asyncio.run(
            MODULE._poll_external_analysis_until_terminal(
                client=client,
                run_id="target-run",
            )
        )


def test_external_analysis_polling_is_bounded(
) -> None:
    client = _StateClient("target-run", ["queued"])

    with pytest.raises(MODULE.E2EFailure, match="external analysis worker polling timed out"):
        asyncio.run(
            MODULE._poll_external_analysis_until_terminal(
                client=client,
                run_id="target-run",
                timeout_seconds=0,
            )
        )

    assert client.calls == 1


class _SingleRowCursor:
    def __init__(self, row: object, calls: list[tuple[str, object | None]]) -> None:
        self._row = row
        self._calls = calls

    def __enter__(self) -> _SingleRowCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object | None = None) -> None:
        self._calls.append((" ".join(query.split()), params))

    def fetchone(self) -> object:
        return self._row


class _SingleRowConnection:
    def __init__(self, row: object, calls: list[tuple[str, object | None]]) -> None:
        self._row = row
        self._calls = calls

    def __enter__(self) -> _SingleRowConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _SingleRowCursor:
        return _SingleRowCursor(self._row, self._calls)


def _single_row_database(
    monkeypatch: pytest.MonkeyPatch,
    row: object,
) -> list[tuple[str, object | None]]:
    calls: list[tuple[str, object | None]] = []

    def connect(database_url: str, **kwargs: object) -> _SingleRowConnection:
        assert database_url == "postgresql://test"
        assert kwargs == {"connect_timeout": 10, "row_factory": MODULE.dict_row}
        return _SingleRowConnection(row, calls)

    monkeypatch.setattr(MODULE.psycopg, "connect", connect)
    return calls


class _RowsCursor:
    def __init__(
        self,
        rows: list[object],
        calls: list[tuple[str, object | None]],
    ) -> None:
        self._rows = rows
        self._calls = calls

    def __enter__(self) -> _RowsCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object | None = None) -> None:
        self._calls.append((" ".join(query.split()), params))

    def fetchall(self) -> list[object]:
        return list(self._rows)


class _RowsConnection:
    def __init__(
        self,
        rows: list[object],
        calls: list[tuple[str, object | None]],
    ) -> None:
        self._rows = rows
        self._calls = calls

    def __enter__(self) -> _RowsConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _RowsCursor:
        return _RowsCursor(self._rows, self._calls)


def _rows_database(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[object],
) -> list[tuple[str, object | None]]:
    calls: list[tuple[str, object | None]] = []

    def connect(database_url: str, **kwargs: object) -> _RowsConnection:
        assert database_url == "postgresql://test"
        assert kwargs == {"connect_timeout": 10, "row_factory": MODULE.dict_row}
        return _RowsConnection(rows, calls)

    monkeypatch.setattr(MODULE.psycopg, "connect", connect)
    return calls


class _ChatMessageResponse:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _ChatMessageClient:
    def __init__(self, case_id: str, assistant_id: str, payload: object) -> None:
        self._case_id = case_id
        self._assistant_id = assistant_id
        self._payload = payload
        self.calls = 0

    async def get(self, path: str) -> _ChatMessageResponse:
        assert path == (
            f"/api/v1/analysis-cases/{self._case_id}/messages/"
            f"{self._assistant_id}"
        )
        self.calls += 1
        return _ChatMessageResponse(self._payload)


def test_assistant_message_state_returns_only_a_valid_matching_assistant() -> None:
    case_id = "case-id"
    assistant_id = "assistant-id"
    client = _ChatMessageClient(
        case_id,
        assistant_id,
        {
            "message_id": assistant_id,
            "analysis_case_id": case_id,
            "role": "assistant",
            "status": "generating",
        },
    )

    state = asyncio.run(
        MODULE._assistant_message_state(client, case_id, assistant_id)
    )

    assert state["message_id"] == assistant_id
    assert client.calls == 1


@pytest.mark.parametrize(
    "message",
    [
        {
            "message_id": "assistant-id",
            "analysis_case_id": "other-case",
            "role": "assistant",
            "status": "completed",
        },
        {
            "message_id": "assistant-id",
            "analysis_case_id": "case-id",
            "role": "user",
            "status": "completed",
        },
        {
            "message_id": "assistant-id",
            "analysis_case_id": "case-id",
            "role": "assistant",
            "status": "unexpected",
        },
    ],
)
def test_assistant_message_state_rejects_an_invalid_matching_message(
    message: dict[str, str],
) -> None:
    client = _ChatMessageClient("case-id", "assistant-id", message)

    with pytest.raises(MODULE.E2EFailure, match="chat polling response is invalid"):
        asyncio.run(MODULE._assistant_message_state(client, "case-id", "assistant-id"))


class _ReportResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: object = None,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = (
            {
                "content-type": "application/json",
                "cache-control": "private, no-store",
                "vary": "Cookie",
            }
            if headers is None
            else headers
        )
        self.content = content

    def json(self) -> object:
        return self._payload


class _ReportClient:
    def __init__(self, responses: list[_ReportResponse]) -> None:
        self._responses = list(responses)
        self.paths: list[str] = []

    async def get(self, path: str) -> _ReportResponse:
        self.paths.append(path)
        return self._responses.pop(0)


def _report_payload(
    status: str,
    *,
    can_download: bool = False,
    retry_count: int = 0,
) -> dict[str, object]:
    return {
        "status": status,
        "can_download": can_download,
        "can_regenerate": False,
        "retry_count": retry_count,
    }


def test_report_polling_waits_for_ready_and_requires_downloadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ReportClient(
        [
            _ReportResponse(payload=_report_payload("generating")),
            _ReportResponse(
                payload=_report_payload(
                    "ready", can_download=True, retry_count=1
                )
            ),
        ]
    )
    monkeypatch.setattr(MODULE, "REPORT_POLL_INTERVAL_SECONDS", 0)

    report, polls = asyncio.run(
        MODULE._poll_report_until_ready(client=client, case_id="case-id")
    )

    assert report == _report_payload("ready", can_download=True, retry_count=1)
    assert polls == 2
    assert client.paths == [
        "/api/v1/analysis-cases/case-id/report/status",
        "/api/v1/analysis-cases/case-id/report/status",
    ]


def test_report_polling_rejects_terminal_failure() -> None:
    client = _ReportClient(
        [_ReportResponse(payload=_report_payload("failed", retry_count=2))]
    )

    with pytest.raises(MODULE.E2EFailure, match="terminal failure"):
        asyncio.run(
            MODULE._poll_report_until_ready(client=client, case_id="case-id")
        )


def test_report_polling_rejects_ready_without_download() -> None:
    client = _ReportClient(
        [_ReportResponse(payload=_report_payload("ready", can_download=False))]
    )

    with pytest.raises(MODULE.E2EFailure, match="not downloadable"):
        asyncio.run(
            MODULE._poll_report_until_ready(client=client, case_id="case-id")
        )


@pytest.mark.parametrize(
    "payload",
    [
        {**_report_payload("generating"), "unexpected": "field"},
        _report_payload("unexpected"),
        _report_payload("generating", can_download=True),
        {**_report_payload("ready", can_download=True), "can_regenerate": True},
        {**_report_payload("ready", can_download=True), "retry_count": True},
        {**_report_payload("ready", can_download=True), "retry_count": 3},
    ],
)
def test_report_status_rejects_invalid_contract(payload: object) -> None:
    client = _ReportClient([_ReportResponse(payload=payload)])

    with pytest.raises(MODULE.E2EFailure, match="polling response is invalid"):
        asyncio.run(MODULE._report_status(client, "case-id"))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            "content-type": "text/plain",
            "cache-control": "private, no-store",
            "vary": "Cookie",
        },
        {
            "content-type": "application/json",
            "cache-control": "private",
            "vary": "Cookie",
        },
        {
            "content-type": "application/json",
            "cache-control": "private, no-store",
            "vary": "Origin",
        },
    ],
)
def test_report_status_requires_private_json_headers(
    headers: dict[str, str],
) -> None:
    client = _ReportClient(
        [_ReportResponse(payload=_report_payload("generating"), headers=headers)]
    )

    with pytest.raises(MODULE.E2EFailure, match="report status"):
        asyncio.run(MODULE._report_status(client, "case-id"))


def test_report_polling_is_bounded() -> None:
    client = _ReportClient(
        [_ReportResponse(payload=_report_payload("generating"))]
    )

    with pytest.raises(MODULE.E2EFailure, match="report polling timed out"):
        asyncio.run(
            MODULE._poll_report_until_ready(
                client=client,
                case_id="case-id",
                timeout_seconds=0,
            )
        )

    assert client.paths == []


def _valid_pdf_bytes(subject: str = "FixtureProgram") -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    buffer = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 12 Tf 72 720 Td ({subject}) Tj ET".encode("ascii")
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(buffer)
    return buffer.getvalue()


def test_report_download_requires_the_safe_pdf_response_contract() -> None:
    pdf = _valid_pdf_bytes()
    client = _ReportClient(
        [
            _ReportResponse(
                headers={
                    "content-type": "application/pdf",
                    "content-disposition": (
                        'attachment; filename="pre-review-report.pdf"'
                    ),
                    "cache-control": "private, no-store",
                    "vary": "Origin, Cookie",
                    "x-content-type-options": "nosniff",
                    "content-length": str(len(pdf)),
                },
                content=pdf,
            )
        ]
    )

    size = asyncio.run(
        MODULE._download_ready_report(
            client=client,
            case_id="case-id",
            expected_subject="Fixture Program",
        )
    )

    assert size == len(pdf)
    assert client.paths == ["/api/v1/analysis-cases/case-id/report.pdf"]


def test_report_download_requires_http_200() -> None:
    client = _ReportClient([_ReportResponse(status_code=404)])

    with pytest.raises(MODULE.E2EFailure, match="failed with HTTP 404"):
        asyncio.run(
            MODULE._download_ready_report(
                client=client,
                case_id="case-id",
                expected_subject="FixtureProgram",
            )
        )


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({}, b"%PDF-1.7\nfixture"),
        (
            {
                "content-type": "text/plain",
                "content-disposition": 'attachment; filename="pre-review-report.pdf"',
                "cache-control": "private, no-store",
                "vary": "Cookie",
                "x-content-type-options": "nosniff",
            },
            b"%PDF-1.7\nfixture",
        ),
        (
            {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="unsafe.pdf"',
                "cache-control": "private, no-store",
                "vary": "Cookie",
                "x-content-type-options": "nosniff",
            },
            b"%PDF-1.7\nfixture",
        ),
        (
            {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="pre-review-report.pdf"',
                "cache-control": "private",
                "vary": "Cookie",
                "x-content-type-options": "nosniff",
            },
            b"%PDF-1.7\nfixture",
        ),
        (
            {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="pre-review-report.pdf"',
                "cache-control": "private, no-store",
                "vary": "Cookie",
                "x-content-type-options": "",
            },
            b"%PDF-1.7\nfixture",
        ),
        (
            {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="pre-review-report.pdf"',
                "cache-control": "private, no-store",
                "vary": "Cookie",
                "x-content-type-options": "nosniff",
            },
            _valid_pdf_bytes("WrongProgram"),
        ),
        (
            {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="pre-review-report.pdf"',
                "cache-control": "private, no-store",
                "vary": "Cookie",
                "x-content-type-options": "nosniff",
            },
            b"not-a-pdf",
        ),
        (
            {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="pre-review-report.pdf"',
                "cache-control": "private, no-store",
                "vary": "Cookie",
                "x-content-type-options": "nosniff",
            },
            b"%PDF-1.7\nstructurally-invalid",
        ),
        (
            {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="pre-review-report.pdf"',
                "cache-control": "private, no-store",
                "vary": "Origin",
                "x-content-type-options": "nosniff",
            },
            b"%PDF-1.7\nfixture",
        ),
        (
            {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="pre-review-report.pdf"',
                "cache-control": "private, no-store",
                "vary": "Cookie",
                "x-content-type-options": "nosniff",
                "content-length": "999",
            },
            b"%PDF-1.7\nfixture",
        ),
    ],
)
def test_report_download_rejects_an_invalid_response(
    headers: dict[str, str],
    content: bytes,
) -> None:
    client = _ReportClient([_ReportResponse(headers=headers, content=content)])

    with pytest.raises(MODULE.E2EFailure, match="report download response"):
        asyncio.run(
            MODULE._download_ready_report(
                client=client,
                case_id="case-id",
                expected_subject="FixtureProgram",
            )
        )


def test_external_chat_polling_waits_for_a_completed_public_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        {"status": "generating"},
        {"status": "completed", "content": "완료된 안전한 응답"},
    ]

    async def state(*_args: object, **_kwargs: object) -> dict[str, object]:
        return messages.pop(0)

    monkeypatch.setattr(MODULE, "_assistant_message_state", state)
    message, polls = asyncio.run(
        MODULE._poll_external_chat_until_terminal(
            client=object(),
            case_id="case-id",
            assistant_message_id="assistant-id",
        )
    )

    assert message["status"] == "completed"
    assert polls == 2
    assert not messages


def test_external_chat_polling_rejects_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def state(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "failed"}

    monkeypatch.setattr(MODULE, "_assistant_message_state", state)

    with pytest.raises(MODULE.E2EFailure, match="external chat worker reached"):
        asyncio.run(
            MODULE._poll_external_chat_until_terminal(
                client=object(),
                case_id="case-id",
                assistant_message_id="assistant-id",
            )
        )


def test_external_chat_polling_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def state(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "generating"}

    monkeypatch.setattr(MODULE, "_assistant_message_state", state)

    with pytest.raises(MODULE.E2EFailure, match="external chat worker polling timed out"):
        asyncio.run(
            MODULE._poll_external_chat_until_terminal(
                client=object(),
                case_id="case-id",
                assistant_message_id="assistant-id",
                timeout_seconds=0,
            )
        )


class _ChatAttemptRuntime:
    def __init__(self, repository: object, outcomes: list[str]) -> None:
        self.repository = repository
        self.outcomes = list(outcomes)
        self.calls = 0

    def run_once(self) -> object:
        self.calls += 1
        self.repository.target_claims += 1
        return SimpleNamespace(value=self.outcomes.pop(0))


def _chat_poll_responses(
    messages: list[dict[str, object]],
) -> Callable[..., object]:
    async def poll(**kwargs: object) -> dict[str, object]:
        worker_task = kwargs["worker_task"]
        assert isinstance(worker_task, asyncio.Task)
        await worker_task
        return messages.pop(0)

    return poll


def test_chat_poll_rereads_public_state_after_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {"status": "generating"},
        {"status": "completed", "content": "완료"},
    ]

    async def state(*_args: object, **_kwargs: object) -> dict[str, object]:
        return responses.pop(0)

    async def scenario() -> dict[str, object]:
        task = asyncio.create_task(asyncio.sleep(0, result="completed"))
        await task
        return await MODULE._poll_chat_message_while_worker_runs(
            client=object(),
            case_id="case-id",
            assistant_message_id="assistant-id",
            worker_task=task,
        )

    monkeypatch.setattr(MODULE, "_assistant_message_state", state)

    assert asyncio.run(scenario())["status"] == "completed"
    assert not responses


def test_chat_poll_soft_deadline_accepts_worker_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate: asyncio.Event
    responses = [
        {"status": "generating"},
        {"status": "completed", "content": "늦게 완료"},
    ]

    async def state(*_args: object, **_kwargs: object) -> dict[str, object]:
        gate.set()
        return responses.pop(0)

    async def worker() -> SimpleNamespace:
        await gate.wait()
        return SimpleNamespace(value="completed")

    async def scenario() -> dict[str, object]:
        nonlocal gate
        gate = asyncio.Event()
        task = asyncio.create_task(worker())
        return await MODULE._poll_chat_message_while_worker_runs(
            client=object(),
            case_id="case-id",
            assistant_message_id="assistant-id",
            worker_task=task,
        )

    monkeypatch.setattr(MODULE, "CHAT_POLL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(MODULE, "_assistant_message_state", state)

    assert asyncio.run(scenario())["status"] == "completed"
    assert not responses


def test_chat_first_completed_attempt_returns_valid_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    repository = SimpleNamespace(
        target_claims=0,
        unexpected_claim=False,
        claim_error=None,
    )
    runtime = _ChatAttemptRuntime(repository, ["completed"])
    monkeypatch.setattr(
        MODULE,
        "_poll_chat_message_while_worker_runs",
        _chat_poll_responses([{"status": "completed", "content": "요약입니다."}]),
    )

    message, outcome, attempts = asyncio.run(
        MODULE._run_target_chat_until_terminal(
            runtime=runtime,
            repository=repository,
            client=object(),
            case_id="case-id",
            assistant_message_id="assistant-id",
        )
    )

    assert message["status"] == "completed"
    assert outcome == "completed"
    assert attempts == 1
    assert runtime.calls == 1


def test_completed_chat_is_rejected_when_this_worker_was_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    repository = SimpleNamespace(
        target_claims=0,
        unexpected_claim=False,
        claim_error=None,
    )
    runtime = _ChatAttemptRuntime(repository, ["fenced"])
    monkeypatch.setattr(
        MODULE,
        "_poll_chat_message_while_worker_runs",
        _chat_poll_responses([{"status": "completed", "content": "요약입니다."}]),
    )

    with pytest.raises(MODULE.E2EFailure, match="unexpected state"):
        asyncio.run(
            MODULE._run_target_chat_until_terminal(
                runtime=runtime,
                repository=repository,
                client=object(),
                case_id="case-id",
                assistant_message_id="assistant-id",
            )
        )

    assert runtime.calls == 1


def test_chat_retryable_first_failure_retries_then_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    repository = SimpleNamespace(
        target_claims=0,
        unexpected_claim=False,
        claim_error=None,
    )
    runtime = _ChatAttemptRuntime(repository, ["failed", "completed"])
    monkeypatch.setattr(
        MODULE,
        "_poll_chat_message_while_worker_runs",
        _chat_poll_responses(
            [
                {"status": "generating"},
                {"status": "completed", "content": "재시도 후 요약입니다."},
            ]
        ),
    )

    _message, outcome, attempts = asyncio.run(
        MODULE._run_target_chat_until_terminal(
            runtime=runtime,
            repository=repository,
            client=object(),
            case_id="case-id",
            assistant_message_id="assistant-id",
        )
    )

    assert outcome == "completed"
    assert attempts == 2
    assert runtime.calls == 2


def test_chat_terminal_failure_stops_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    repository = SimpleNamespace(
        target_claims=0,
        unexpected_claim=False,
        claim_error=None,
    )
    runtime = _ChatAttemptRuntime(repository, ["failed"])
    monkeypatch.setattr(
        MODULE,
        "_poll_chat_message_while_worker_runs",
        _chat_poll_responses([{"status": "failed"}]),
    )

    with pytest.raises(MODULE.E2EFailure, match="terminal failure after 1 attempt"):
        asyncio.run(
            MODULE._run_target_chat_until_terminal(
                runtime=runtime,
                repository=repository,
                client=object(),
                case_id="case-id",
                assistant_message_id="assistant-id",
            )
        )

    assert runtime.calls == 1


def test_chat_retry_budget_failure_stops_after_two_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE.asyncio, "to_thread", _inline_to_thread)
    repository = SimpleNamespace(
        target_claims=0,
        unexpected_claim=False,
        claim_error=None,
    )
    runtime = _ChatAttemptRuntime(repository, ["failed", "failed"])
    monkeypatch.setattr(
        MODULE,
        "_poll_chat_message_while_worker_runs",
        _chat_poll_responses([{"status": "generating"}, {"status": "generating"}]),
    )

    with pytest.raises(MODULE.E2EFailure, match="retry budget was exhausted"):
        asyncio.run(
            MODULE._run_target_chat_until_terminal(
                runtime=runtime,
                repository=repository,
                client=object(),
                case_id="case-id",
                assistant_message_id="assistant-id",
            )
        )

    assert runtime.calls == MODULE.MAX_WORKER_ATTEMPTS


def test_external_analysis_worker_audit_reports_exact_durable_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rows_database(
        monkeypatch,
        [
            {
                "attempt_count": 2,
                "status": "failed",
                "run_metadata": {"attempt_no": 1, "worker_id": "worker-a:10"},
            },
            {
                "attempt_count": 2,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 2, "worker_id": "worker-b:20"},
            },
        ],
    )

    audit = MODULE._require_external_analysis_worker_audit(
        "postgresql://test",
        run_id="run-id",
    )

    assert audit.final_worker_id == "worker-b:20"
    assert audit.attempt_count == 2
    assert audit.attempt_worker_ids == ("worker-a:10", "worker-b:20")
    assert "ops.processing_run" in calls[0][0]
    assert "claim_next" not in calls[0][0]
    assert calls[0][1] == ("run-id",)


@pytest.mark.parametrize(
    "rows",
    [
        [
            {
                "attempt_count": 2,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 1, "worker_id": "worker-a"},
            }
        ],
        [
            {
                "attempt_count": 2,
                "status": "failed",
                "run_metadata": {"attempt_no": 1, "worker_id": "worker-a"},
            },
            {
                "attempt_count": 2,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 1, "worker_id": "worker-b"},
            },
        ],
    ],
)
def test_external_analysis_worker_audit_rejects_inexact_history(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> None:
    _rows_database(monkeypatch, rows)

    with pytest.raises(MODULE.E2EFailure, match="analysis worker audit is invalid"):
        MODULE._require_external_analysis_worker_audit(
            "postgresql://test",
            run_id="run-id",
        )


def test_report_worker_audit_reports_bounded_durable_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rows_database(
        monkeypatch,
        [
            {
                "attempt_count": 3,
                "retry_count": 2,
                "status": "failed",
                "run_metadata": {"attempt_no": 1, "worker_id": "report-a:10"},
            },
            {
                "attempt_count": 3,
                "retry_count": 2,
                "status": "failed",
                "run_metadata": {"attempt_no": 2, "worker_id": "report-b:20"},
            },
            {
                "attempt_count": 3,
                "retry_count": 2,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 3, "worker_id": "report-c:30"},
            },
        ],
    )

    audit = MODULE._require_report_worker_audit(
        "postgresql://test",
        case_id="case-id",
        run_id="run-id",
    )

    assert audit.final_worker_id == "report-c:30"
    assert audit.attempt_count == 3
    assert audit.attempt_worker_ids == (
        "report-a:10",
        "report-b:20",
        "report-c:30",
    )
    assert "ops.processing_run" in calls[0][0]
    assert "processing.run_type = 'report_pdf'" in calls[0][0]
    assert "claim_next" not in calls[0][0]
    assert calls[0][1] == ("case-id", "run-id")


@pytest.mark.parametrize(
    "rows",
    [
        [
            {
                "attempt_count": 4,
                "retry_count": 3,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 4, "worker_id": "report-a"},
            }
        ],
        [
            {
                "attempt_count": 1,
                "retry_count": 1,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 1, "worker_id": "report-a"},
            }
        ],
        [
            {
                "attempt_count": 2,
                "retry_count": 1,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 1, "worker_id": "report-a"},
            },
            {
                "attempt_count": 2,
                "retry_count": 1,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 2, "worker_id": "report-b"},
            },
        ],
    ],
)
def test_report_worker_audit_rejects_invalid_history(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> None:
    _rows_database(monkeypatch, rows)

    with pytest.raises(MODULE.E2EFailure, match="report worker audit is invalid"):
        MODULE._require_report_worker_audit(
            "postgresql://test",
            case_id="case-id",
            run_id="run-id",
        )


def test_external_chat_worker_audit_reports_exact_reset_cycle_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rows_database(
        monkeypatch,
        [
            {
                "current_cycle_attempt_count": 1,
                "auto_retry_count": 1,
                "manual_retry_count": 0,
                "status": "failed",
                "run_metadata": {"attempt_no": 1, "worker_id": "chat-a:30"},
            },
            {
                "current_cycle_attempt_count": 1,
                "auto_retry_count": 1,
                "manual_retry_count": 0,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 1, "worker_id": "chat-b:40"},
            },
        ],
    )

    audit = MODULE._require_external_chat_worker_audit(
        "postgresql://test",
        assistant_message_id="assistant-id",
        case_id="case-id",
    )

    assert audit.final_worker_id == "chat-b:40"
    assert audit.attempt_count == 2
    assert audit.attempt_worker_ids == ("chat-a:30", "chat-b:40")
    assert "ops.processing_run" in calls[0][0]
    assert "claim_next" not in calls[0][0]
    assert calls[0][1] == ("assistant-id", "case-id")


@pytest.mark.parametrize(
    "rows",
    [
        [
            {
                "current_cycle_attempt_count": 1,
                "auto_retry_count": 0,
                "manual_retry_count": 1,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 1, "worker_id": "chat-a"},
            }
        ],
        [
            {
                "current_cycle_attempt_count": 1,
                "auto_retry_count": 0,
                "manual_retry_count": 0,
                "status": "failed",
                "run_metadata": {"attempt_no": 1, "worker_id": "chat-a"},
            },
            {
                "current_cycle_attempt_count": 1,
                "auto_retry_count": 0,
                "manual_retry_count": 0,
                "status": "succeeded",
                "run_metadata": {"attempt_no": 1, "worker_id": "chat-b"},
            },
        ],
    ],
)
def test_external_chat_worker_audit_rejects_out_of_scope_or_unbounded_history(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> None:
    _rows_database(monkeypatch, rows)

    with pytest.raises(MODULE.E2EFailure, match="chat worker audit is invalid"):
        MODULE._require_external_chat_worker_audit(
            "postgresql://test",
            assistant_message_id="assistant-id",
            case_id="case-id",
        )


@pytest.mark.parametrize(
    ("row", "expected_count"),
    [
        ({"reference_count": 2, "valid_reference_count": 2}, 2),
        ({"reference_count": 0, "valid_reference_count": 0}, None),
        ({"reference_count": 2, "valid_reference_count": 1}, None),
    ],
)
def test_chat_reference_count_requires_nonzero_valid_references(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, int],
    expected_count: int | None,
) -> None:
    calls = _single_row_database(monkeypatch, row)

    if expected_count is None:
        with pytest.raises(MODULE.E2EFailure, match="chat references are invalid"):
            MODULE._chat_reference_count(
                "postgresql://test",
                assistant_message_id="assistant-id",
                case_id="case-id",
            )
    else:
        assert (
            MODULE._chat_reference_count(
                "postgresql://test",
                assistant_message_id="assistant-id",
                case_id="case-id",
            )
            == expected_count
        )

    assert "result.conversation_reference" in calls[0][0]
    assert calls[0][1] == ("case-id", "assistant-id")


@pytest.mark.parametrize(
    ("row", "error"),
    [
        (
            {
                "active_analysis_runs": 0,
                "active_chat_messages": 0,
                "active_report_jobs": 0,
            },
            None,
        ),
        (
            {
                "active_analysis_runs": 1,
                "active_chat_messages": 0,
                "active_report_jobs": 0,
            },
            "analysis=1, chat=0, report=0",
        ),
        (
            {
                "active_analysis_runs": 0,
                "active_chat_messages": 2,
                "active_report_jobs": 0,
            },
            "analysis=0, chat=2, report=0",
        ),
        (
            {
                "active_analysis_runs": 0,
                "active_chat_messages": 0,
                "active_report_jobs": 1,
            },
            "analysis=0, chat=0, report=1",
        ),
    ],
)
def test_queue_preflight_allows_only_quiescent_queues(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, int],
    error: str | None,
) -> None:
    calls = _single_row_database(monkeypatch, row)

    if error is None:
        MODULE._assert_queues_quiescent("postgresql://test")
    else:
        with pytest.raises(MODULE.E2EFailure, match=error):
            MODULE._assert_queues_quiescent("postgresql://test")

    assert "workspace.analysis_run" in calls[0][0]
    assert "result.conversation_message" in calls[0][0]
    assert "workspace.report_pdf_dispatch" in calls[0][0]


def test_live_ml_results_require_all_models_to_finish_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _single_row_database(
        monkeypatch,
        {
            "ml_result": {
                "model_1": {"status": "OK"},
                "model_2": {"status": "OK"},
                "model_3": {"status": "OK"},
            }
        },
    )

    assert MODULE._require_live_ml_results(
        "postgresql://test", case_id="case-id"
    ) == {"model_1": "OK", "model_2": "OK", "model_3": "OK"}
    assert "result.analysis_case" in calls[0][0]
    assert calls[0][1] == ("case-id",)


def test_live_ml_results_reject_unavailable_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _single_row_database(
        monkeypatch,
        {
            "ml_result": {
                "model_1": {"status": "OK"},
                "model_2": {"status": "UNAVAILABLE"},
                "model_3": {"status": "OK"},
            }
        },
    )

    with pytest.raises(
        MODULE.E2EFailure,
        match="live ML execution did not complete: model_2=UNAVAILABLE",
    ):
        MODULE._require_live_ml_results("postgresql://test", case_id="case-id")


def test_public_ml_projection_requires_all_three_nonempty_messages() -> None:
    MODULE._require_public_ml_projection(
        {
            "ml": {
                "model_1": {"message": "지원 유형 결과"},
                "model_2": {"message": "금액 예측 결과"},
                "model_3": {"message": "이상치 결과"},
            }
        }
    )


@pytest.mark.parametrize(
    "ml",
    [
        None,
        {},
        {
            "model_1": {"message": "지원 유형 결과"},
            "model_2": {"message": ""},
            "model_3": {"message": "이상치 결과"},
        },
    ],
)
def test_public_ml_projection_rejects_missing_or_blank_messages(ml: object) -> None:
    with pytest.raises(MODULE.E2EFailure, match="result ML response is invalid"):
        MODULE._require_public_ml_projection({"ml": ml})


def test_trace_writes_actual_stage_layout(tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace"
    result = {"cpl": {"items": []}, "fit": {"items": []}, "sim": {}, "ml": {}}

    MODULE._write_trace(
        trace_dir,
        upload={"analysis_run_id": "run-1"},
        common_ir={"schema_version": "common_ir_v1"},
        structured_profile={"profile_id": "request:run-1"},
        result=result,
    )

    assert [path.name for path in sorted(trace_dir.iterdir())] == [
        "00_upload.json",
        "01_common_ir.json",
        "02_structured_profile.json",
        "03_cpl.json",
        "04_fit.json",
        "05_sim.json",
        "06_ml.json",
        "07_result.json",
        "08_run_state.json",
        "09_execution_manifest.json",
        "cpl_diagnostics.json",
    ]
    assert json.loads((trace_dir / "02_structured_profile.json").read_text("utf-8"))["profile_id"] == "request:run-1"
    assert json.loads((trace_dir / "03_cpl.json").read_text("utf-8")) == {"items": []}


def test_embedding_provenance_query_survives_terminal_dispatch_cleanup() -> None:
    query = MODULE._ANALYSIS_EMBEDDING_CONFIGURATION_SQL

    assert "FROM ops.processing_run AS processing" in query
    assert "processing.source_analysis_run_id = %s::uuid" in query
    assert "processing.status = 'succeeded'" in query
    assert "analysis_run_dispatch" not in query


def test_execution_manifest_records_reproducible_non_secret_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(MODULE, "_git_source_state", lambda _revision: (False, "d" * 64))
    monkeypatch.setattr(
        MODULE,
        "_model_configuration_manifest",
        lambda: {"provider": "openai", "request_profile_model": "terra"},
    )
    monkeypatch.setattr(
        MODULE,
        "_analysis_embedding_configuration",
        lambda _url, _run_id: {"configuration_id": "embedding-v2"},
    )

    manifest = MODULE._execution_manifest(
        source_content=b"fixture",
        worker_mode="inline",
        analysis_worker_id="analysis-worker",
        chat_worker_id="chat-worker",
        report_worker_id="report-worker",
        database_url="not-used",
        analysis_run_id="run-id",
    )

    assert manifest["input_sha256"] == (
        "f16d05ec6b29248d2c61adb1e9263f78e4f7bace1b955014a2d17872cfe4064d"
    )
    assert manifest["git_commit"] == "a" * 40
    assert manifest["git_dirty"] is False
    assert manifest["git_source_state_sha256"] == "d" * 64
    assert manifest["api"] == {"kind": "in-process", "build_id": "a" * 40}
    assert manifest["analysis_worker"]["kind"] == "host-python"
    assert manifest["chat_worker"]["worker_id"] == "chat-worker"
    assert manifest["report_worker"] == {
        "kind": "database-audited-external",
        "worker_id": "report-worker",
    }
    assert manifest["models"]["provider"] == "openai"
    assert manifest["embedding_configuration"] == {
        "configuration_id": "embedding-v2"
    }


def test_external_manifest_resolves_all_immutable_worker_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_git_commit", lambda: "b" * 40)
    monkeypatch.setattr(MODULE, "_git_source_state", lambda _revision: (False, "e" * 64))
    monkeypatch.setattr(
        MODULE,
        "_require_clean_checkout_build_context_digest",
        lambda: "b" * 64,
    )
    monkeypatch.setattr(
        MODULE,
        "_analysis_embedding_configuration",
        lambda _url, _run_id: {},
    )
    seen: list[str] = []

    def image_id(worker_id: str) -> str:
        seen.append(worker_id)
        return "sha256:" + ("c" * 64)

    monkeypatch.setattr(MODULE, "_docker_image_identity", image_id)
    model_worker_ids: list[str] = []

    def model_environment(worker_id: str) -> dict[str, str]:
        model_worker_ids.append(worker_id)
        return {
            "OPENAI_LLM_MODEL": f"cpl-{worker_id[0]}",
            "OPENAI_EMBEDDING_MODEL": f"embedding-{worker_id[0]}",
            "OPENAI_CHAT_MODEL": f"chat-{worker_id[0]}",
            "OPENAI_MAX_REPAIRS": "3" if worker_id[0] == "a" else "4",
        }

    monkeypatch.setattr(MODULE, "_external_worker_model_environment", model_environment)
    manifest = MODULE._execution_manifest(
        source_content=b"fixture",
        worker_mode="external",
        analysis_worker_id="a" * 12 + ":1:x",
        chat_worker_id="b" * 12 + ":2:y",
        report_worker_id="c" * 12 + ":3:z",
        database_url="not-used",
        analysis_run_id="run-id",
        deployed_api_build_id="b" * 64,
    )

    assert seen == [
        "a" * 12 + ":1:x",
        "b" * 12 + ":2:y",
        "c" * 12 + ":3:z",
    ]
    assert model_worker_ids == ["a" * 12 + ":1:x", "b" * 12 + ":2:y"]
    assert manifest["analysis_worker"]["image_id"].startswith("sha256:")
    assert manifest["chat_worker"]["kind"] == "docker"
    assert manifest["report_worker"]["image_id"].startswith("sha256:")
    assert manifest["git_dirty"] is False
    assert manifest["api"] == {"kind": "deployed-http", "build_id": "b" * 64}
    assert manifest["models"] == {
        "analysis_worker": {
            "provider": "openai",
            "request_profile_model": "cpl-a",
            "cpl_model": "cpl-a",
            "fit_model": "cpl-a",
            "sim_model": "cpl-a",
            "chat_model": "chat-a",
            "embedding_model": "embedding-a",
            "max_repairs": 3,
        },
        "chat_worker": {
            "provider": "openai",
            "request_profile_model": "cpl-b",
            "cpl_model": "cpl-b",
            "fit_model": "cpl-b",
            "sim_model": "cpl-b",
            "chat_model": "chat-b",
            "embedding_model": None,
            "max_repairs": 4,
        },
    }


def test_external_manifest_rejects_a_non_container_report_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_git_commit", lambda: "b" * 40)
    monkeypatch.setattr(
        MODULE, "_git_source_state", lambda _revision: (False, "e" * 64)
    )
    monkeypatch.setattr(
        MODULE,
        "_require_clean_checkout_build_context_digest",
        lambda: "b" * 64,
    )

    def image_id(worker_id: str) -> str:
        if worker_id == "host-report-worker":
            raise MODULE.E2EFailure(
                "External worker is not a running Docker container"
            )
        return "sha256:" + ("c" * 64)

    monkeypatch.setattr(MODULE, "_docker_image_identity", image_id)

    with pytest.raises(MODULE.E2EFailure, match="not a running Docker container"):
        MODULE._execution_manifest(
            source_content=b"fixture",
            worker_mode="external",
            analysis_worker_id="a" * 12 + ":1:x",
            chat_worker_id="b" * 12 + ":2:y",
            report_worker_id="host-report-worker",
            database_url="not-used",
            analysis_run_id="run-id",
            deployed_api_build_id="b" * 64,
        )


def test_external_execution_manifest_explicitly_rejects_vllm_until_runtime_contract_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_git_commit", lambda: "b" * 40)
    monkeypatch.setattr(
        MODULE, "_git_source_state", lambda _revision: (False, "e" * 64)
    )
    monkeypatch.setattr(
        MODULE,
        "_require_clean_checkout_build_context_digest",
        lambda: "b" * 64,
    )
    monkeypatch.setattr(
        MODULE,
        "_docker_image_identity",
        lambda _worker_id: "sha256:" + ("c" * 64),
    )
    monkeypatch.setattr(
        MODULE,
        "_external_worker_model_environment",
        lambda _worker_id: {
            "PREREVIEW_LLM_PROVIDER": "vllm",
            "VLLM_LLM_MODEL": "gemma",
            "OPENAI_EMBEDDING_MODEL": "embedding",
        },
    )

    with pytest.raises(MODULE.E2EFailure, match="does not support vLLM"):
        MODULE._execution_manifest(
            source_content=b"fixture",
            worker_mode="external",
            analysis_worker_id="a" * 12 + ":1:x",
            chat_worker_id="b" * 12 + ":2:y",
            report_worker_id="c" * 12 + ":3:z",
            database_url="not-used",
            analysis_run_id="run-id",
            deployed_api_build_id="b" * 64,
        )


def test_external_worker_model_environment_uses_allowlisted_docker_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "PREREVIEW_LLM_PROVIDER=openai\n"
                "PREREVIEW_EMBEDDING_PROVIDER=openai\n"
                "OPENAI_LLM_MODEL=analysis-cpl\n"
                "OPENAI_EMBEDDING_MODEL=analysis-embedding\n"
                "OPENAI_REQUEST_PROFILE_MODEL=analysis-profile\n"
                "OPENAI_CPL_MODEL=\n"
                "OPENAI_FIT_MODEL=\n"
                "OPENAI_SIM_MODEL=analysis-sim\n"
                "OPENAI_CHAT_MODEL=analysis-chat\n"
                "OPENAI_MAX_REPAIRS=7\n"
                "OPENAI_TIMEOUT_SECONDS=120\n"
                "VLLM_LLM_MODEL=\n"
                "VLLM_REQUEST_PROFILE_MODEL=\n"
                "VLLM_CPL_MODEL=\n"
                "VLLM_FIT_MODEL=\n"
                "VLLM_SIM_MODEL=\n"
                "VLLM_CHAT_MODEL=\n"
                "VLLM_MAX_REPAIRS=\n"
                "VLLM_TIMEOUT_SECONDS=120\n"
                "VLLM_MAX_OUTPUT_TOKENS=16384\n"
                "VLLM_MAX_RESPONSE_BYTES=1048576\n"
            ),
        )

    monkeypatch.setattr(MODULE.subprocess, "run", run)

    environment = MODULE._external_worker_model_environment("a" * 12 + ":worker")

    assert environment == {
        "PREREVIEW_LLM_PROVIDER": "openai",
        "PREREVIEW_EMBEDDING_PROVIDER": "openai",
        "OPENAI_LLM_MODEL": "analysis-cpl",
        "OPENAI_EMBEDDING_MODEL": "analysis-embedding",
        "OPENAI_REQUEST_PROFILE_MODEL": "analysis-profile",
        "OPENAI_CPL_MODEL": "",
        "OPENAI_FIT_MODEL": "",
        "OPENAI_SIM_MODEL": "analysis-sim",
        "OPENAI_CHAT_MODEL": "analysis-chat",
        "OPENAI_MAX_REPAIRS": "7",
        "OPENAI_TIMEOUT_SECONDS": "120",
        "VLLM_LLM_MODEL": "",
        "VLLM_REQUEST_PROFILE_MODEL": "",
        "VLLM_CPL_MODEL": "",
        "VLLM_FIT_MODEL": "",
        "VLLM_SIM_MODEL": "",
        "VLLM_CHAT_MODEL": "",
        "VLLM_MAX_REPAIRS": "",
        "VLLM_TIMEOUT_SECONDS": "120",
        "VLLM_MAX_OUTPUT_TOKENS": "16384",
        "VLLM_MAX_RESPONSE_BYTES": "1048576",
    }
    assert len(calls) == 1
    command = calls[0]
    assert command[:4] == ["docker", "exec", "a" * 12, "/bin/sh"]
    assert "inspect" not in command
    assert "OPENAI_API_KEY" not in command[-1]
    for name in MODULE.MODEL_CONFIGURATION_ENVIRONMENT_KEYS:
        assert name in command[-1]


def test_external_api_build_identity_requires_matching_valid_health_values() -> None:
    class Response:
        status_code = 200
        headers = {"X-PreReview-Build-Id": "a" * 64}

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "ready", "build_id": "a" * 64}

    class Client:
        @staticmethod
        async def get(path: str) -> Response:
            assert path == "/health/ready"
            return Response()

    assert asyncio.run(
        MODULE._external_api_build_identity(Client(), expected_build_id="a" * 64)
    ) == "a" * 64


def test_external_api_build_identity_rejects_missing_or_mismatched_health_values() -> None:
    class Response:
        status_code = 200
        headers = {"X-PreReview-Build-Id": "a" * 64}

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "ready", "build_id": "b" * 64}

    class Client:
        @staticmethod
        async def get(_path: str) -> Response:
            return Response()

    with pytest.raises(MODULE.E2EFailure, match="build identity"):
        asyncio.run(
            MODULE._external_api_build_identity(Client(), expected_build_id="a" * 64)
        )


def test_external_api_build_identity_rejects_a_valid_but_stale_deployment() -> None:
    class Response:
        status_code = 200
        headers = {"X-PreReview-Build-Id": "a" * 64}

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "ready", "build_id": "a" * 64}

    class Client:
        @staticmethod
        async def get(_path: str) -> Response:
            return Response()

    with pytest.raises(MODULE.E2EFailure, match="does not match the clean checkout"):
        asyncio.run(
            MODULE._external_api_build_identity(Client(), expected_build_id="b" * 64)
        )


def test_external_manifest_rejects_a_dirty_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(MODULE, "_git_source_state", lambda _revision: (True, "d" * 64))

    with pytest.raises(MODULE.E2EFailure, match="requires a clean Git checkout"):
        MODULE._execution_manifest(
            source_content=b"fixture",
            worker_mode="external",
            analysis_worker_id="analysis-worker",
            chat_worker_id="chat-worker",
            report_worker_id="report-worker",
            database_url="not-used",
            analysis_run_id="run-id",
            deployed_api_build_id="a" * 64,
        )


def test_partial_execution_manifest_marks_unknown_chat_identity_as_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(MODULE, "_git_source_state", lambda _revision: (True, "b" * 64))
    monkeypatch.setattr(
        MODULE,
        "_model_configuration_manifest",
        lambda: {"provider": "openai"},
    )
    monkeypatch.setattr(
        MODULE,
        "_analysis_embedding_configuration",
        lambda _url, _run_id: {"selected": False, "configuration_id": None},
    )

    manifest = MODULE._partial_execution_manifest(
        source_content=b"fixture",
        worker_mode="inline",
        analysis_worker_id="analysis-worker",
        database_url="not-used",
        analysis_run_id="run-id",
        deployed_api_build_id=None,
    )

    assert manifest["partial"] is True
    assert manifest["chat_worker"] is None
    assert manifest["analysis_worker"] == {
        "kind": "host-python",
        "worker_id": "analysis-worker",
    }
    assert manifest["embedding_configuration"] == {
        "selected": False,
        "configuration_id": None,
    }


def test_external_model_manifest_uses_worker_max_repairs_default_and_rejects_invalid_value() -> None:
    environment = {
        "OPENAI_LLM_MODEL": "cpl",
        "OPENAI_EMBEDDING_MODEL": "embedding",
    }

    assert MODULE._model_configuration_manifest(environment)["max_repairs"] == 2

    environment["OPENAI_MAX_REPAIRS"] = "not-an-integer"
    with pytest.raises(MODULE.E2EFailure, match="model configuration is unavailable"):
        MODULE._model_configuration_manifest(environment)

    environment["OPENAI_MAX_REPAIRS"] = "-1"
    with pytest.raises(MODULE.E2EFailure, match="model configuration is unavailable"):
        MODULE._model_configuration_manifest(environment)


def test_inline_model_manifest_supports_vllm_without_openai_llm_settings() -> None:
    environment = {
        "PREREVIEW_LLM_PROVIDER": "vllm",
        "VLLM_LLM_MODEL": "gemma-default",
        "VLLM_REQUEST_PROFILE_MODEL": "gemma-request",
        "VLLM_CHAT_MODEL": "gemma-chat",
        "VLLM_MAX_REPAIRS": "3",
        "OPENAI_EMBEDDING_MODEL": "embedding",
    }

    assert MODULE._model_configuration_manifest(environment) == {
        "provider": "vllm",
        "request_profile_model": "gemma-request",
        "cpl_model": "gemma-default",
        "fit_model": "gemma-default",
        "sim_model": "gemma-default",
        "chat_model": "gemma-chat",
        "embedding_model": "embedding",
        "max_repairs": 3,
        "timeout_seconds": 120.0,
        "max_output_tokens": 16384,
        "max_response_bytes": 1048576,
    }


def test_external_release_model_manifest_rejects_vllm_without_runtime_bound_contract() -> None:
    environment = {
        "PREREVIEW_LLM_PROVIDER": "vllm",
        "VLLM_LLM_MODEL": "gemma-default",
        "OPENAI_EMBEDDING_MODEL": "embedding",
    }
    with pytest.raises(MODULE.E2EFailure, match="does not support vLLM"):
        MODULE._model_configuration_manifest(
            environment, allow_vllm=False
        )


def test_inline_chat_manifest_does_not_require_embedding_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worker import providers

    class Provider:
        name = "openai"
        max_repairs = 2

        @staticmethod
        def model_id_for(profile: str) -> str:
            return f"model-{profile}"

    monkeypatch.setattr(providers, "build_llm_provider", lambda **_kwargs: Provider())

    manifest = MODULE._model_configuration_manifest(embedding_required=False)

    assert manifest["embedding_model"] is None


def test_git_source_state_is_deterministic_and_does_not_expose_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "untracked.txt"
    source.write_text("private source content", encoding="utf-8")
    monkeypatch.setattr(MODULE, "REPOSITORY_ROOT", tmp_path)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        arguments = command[1:]
        outputs = {
            ("status", "--porcelain=v1", "-z"): b"?? untracked.txt\0",
            ("diff", "--no-ext-diff", "--binary", "--full-index", "HEAD"): b"",
            ("ls-files", "--others", "--exclude-standard", "-z"): b"untracked.txt\0",
        }
        return SimpleNamespace(returncode=0, stdout=outputs[tuple(arguments)])

    monkeypatch.setattr(MODULE.subprocess, "run", run)

    dirty, first = MODULE._git_source_state("a" * 40)
    repeated_dirty, second = MODULE._git_source_state("a" * 40)

    assert dirty is repeated_dirty is True
    assert first == second
    assert len(first) == 64
    assert "private source content" not in first

    source.write_text("changed private source content", encoding="utf-8")
    _, changed = MODULE._git_source_state("a" * 40)

    assert changed != first


def test_failure_trace_keeps_persisted_artifacts_diagnostics_and_run_state(
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "trace"

    class Handler:
        _store = SimpleNamespace(
            cached_request_profile=lambda **_kwargs: SimpleNamespace(
                common_ir="common-ir", structured_profile="structured-profile"
            )
        )

        @staticmethod
        def _load_json_artifact(value: str) -> object:
            return {"artifact": value}

    written = MODULE._write_analysis_trace_safely(
        trace_dir,
        upload={"analysis_run_id": "run-1"},
        handler=Handler(),
        run_id="run-1",
        run_state={"analysis_run_id": "run-1", "status": "failed"},
        cpl_diagnostics=[{"stage": "structured_profile", "reason_code": "BAD"}],
    )

    assert written is True
    assert json.loads((trace_dir / "01_common_ir.json").read_text("utf-8")) == {
        "artifact": "common-ir"
    }
    assert json.loads((trace_dir / "02_structured_profile.json").read_text("utf-8")) == {
        "artifact": "structured-profile"
    }
    assert json.loads((trace_dir / "08_run_state.json").read_text("utf-8")) == {
        "analysis_run_id": "run-1",
        "status": "failed",
    }
    diagnostics = json.loads((trace_dir / "cpl_diagnostics.json").read_text("utf-8"))
    assert diagnostics["diagnostics"] == [
        {"stage": "structured_profile", "reason_code": "BAD"}
    ]


def test_ml_acceptance_failure_writes_persisted_trace_without_masking_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_dir = tmp_path / "trace"
    result = {
        "cpl": {"items": []},
        "fit": {"items": []},
        "sim": {"candidates": []},
        "ml": {"model_3": {"status": "UNAVAILABLE"}},
    }

    class Handler:
        _store = SimpleNamespace(
            cached_request_profile=lambda **_kwargs: SimpleNamespace(
                common_ir="common-ir", structured_profile="structured-profile"
            )
        )

        @staticmethod
        def _load_json_artifact(value: str) -> object:
            return {"artifact": value}

    def reject_ml_acceptance(_database_url: str, *, case_id: str) -> dict[str, str]:
        assert case_id == "case-id"
        raise MODULE.E2EFailure(
            "live ML execution did not complete: model_3=UNAVAILABLE"
        )

    async def persisted_run_state(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"analysis_run_id": "run-id", "status": "succeeded"}

    monkeypatch.setattr(MODULE, "_require_live_ml_results", reject_ml_acceptance)
    monkeypatch.setattr(MODULE, "_analysis_state", persisted_run_state)

    with pytest.raises(MODULE.E2EFailure, match="model_3=UNAVAILABLE"):
        asyncio.run(
            MODULE._require_live_ml_results_with_failure_trace(
                client=object(),
                trace_dir=trace_dir,
                upload={"analysis_run_id": "run-id"},
                handler=Handler(),
                run_id="run-id",
                cpl_diagnostics=[{"stage": "ml", "reason_code": "UNAVAILABLE"}],
                result=result,
                database_url="postgresql://test",
                case_id="case-id",
                execution_manifest={"partial": True, "chat_worker": None},
            )
        )

    assert json.loads((trace_dir / "01_common_ir.json").read_text("utf-8")) == {
        "artifact": "common-ir"
    }
    assert json.loads((trace_dir / "02_structured_profile.json").read_text("utf-8")) == {
        "artifact": "structured-profile"
    }
    assert json.loads((trace_dir / "07_result.json").read_text("utf-8")) == result
    assert json.loads((trace_dir / "08_run_state.json").read_text("utf-8")) == {
        "analysis_run_id": "run-id",
        "status": "succeeded",
    }
    assert json.loads((trace_dir / "09_execution_manifest.json").read_text("utf-8")) == {
        "partial": True,
        "chat_worker": None,
    }


def test_failure_trace_write_error_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_to_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(MODULE, "_write_trace", fail_to_write)

    assert (
        MODULE._write_analysis_trace_safely(
            tmp_path / "trace",
            upload={},
            handler=None,
            run_id="run-1",
            run_state={"status": "failed"},
            cpl_diagnostics=[],
        )
        is False
    )


def test_inline_stage_progress_uses_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    MODULE._report_analysis_stage("cpl", "failed", "contract error")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[cpl] failed — contract error\n"


def test_trace_never_overwrites_existing_directory(tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    with pytest.raises(MODULE.E2EFailure, match="must not already exist"):
        MODULE._write_trace(
            trace_dir,
            upload={},
            common_ir={},
            structured_profile={},
            result={},
        )


@pytest.mark.parametrize(
    ("filename", "content", "mime_type"),
    [
        ("request.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture", "application/x-hwp"),
        ("request.hwpx", HWPX, "application/vnd.hancom.hwpx"),
    ],
)
def test_validated_source_accepts_hwp_and_hwpx(
    tmp_path: Path, filename: str, content: bytes, mime_type: str
) -> None:
    source = tmp_path / filename
    source.write_bytes(content)

    uploaded, detected_mime_type = MODULE._validated_source(source)

    assert uploaded == content
    assert detected_mime_type == mime_type


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("request.hwp", b"PK\x03\x04not-ole"),
        ("request.hwpx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1not-zip"),
        ("request.pdf", b"%PDF-1.7"),
    ],
)
def test_validated_source_rejects_wrong_magic_or_extension(
    tmp_path: Path, filename: str, content: bytes
) -> None:
    source = tmp_path / filename
    source.write_bytes(content)

    with pytest.raises(MODULE.E2EFailure, match="format is invalid"):
        MODULE._validated_source(source)


@pytest.mark.parametrize("top_k", [0, -1, 6])
def test_configure_environment_rejects_out_of_range_top_k(
    tmp_path: Path, top_k: int
) -> None:
    with pytest.raises(MODULE.E2EFailure, match="between 1 and 5"):
        MODULE._configure_environment(
            tmp_path / "backend.env", tmp_path / "supabase.env", top_k=top_k
        )


def test_top_k_defaults_to_the_stored_trace_baseline() -> None:
    assert MODULE.DEFAULT_TOP_K == 5


def test_windows_uses_the_direct_postgres_listener() -> None:
    assert MODULE._database_endpoint("acme", "win32") == ("postgres", 55432)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_keep_the_supabase_pooler(platform: str) -> None:
    assert MODULE._database_endpoint("acme", platform) == ("postgres.acme", 5432)


def test_pooler_username_is_url_quoted() -> None:
    username, port = MODULE._database_endpoint("a/c me", "linux")
    assert username == "postgres.a%2Fc%20me"
    assert port == 5432


@pytest.fixture
def isolated_environ(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    copy = dict(os.environ)
    monkeypatch.setattr(MODULE.os, "environ", copy)
    return copy


def _env_files(tmp_path: Path, model1_line: str) -> tuple[Path, Path]:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "OPENAI_API_KEY=sk-test-not-a-real-key\n" + model1_line, encoding="utf-8"
    )
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "POSTGRES_PASSWORD=pw\nPOOLER_TENANT_ID=acme\n"
        "JWT_SECRET=s\nANON_KEY=a\nSERVICE_ROLE_KEY=r\n",
        encoding="utf-8",
    )
    return backend_env, supabase_env


def test_model1_serving_dir_reaches_the_worker_environment(
    tmp_path: Path, isolated_environ: dict[str, str]
) -> None:
    served = r"C:\models\model1\serving"
    backend_env, supabase_env = _env_files(
        tmp_path, f"PREREVIEW_MODEL1_SERVING_DIR={served}\n"
    )
    isolated_environ.pop("PREREVIEW_MODEL1_SERVING_DIR", None)

    MODULE._configure_environment(backend_env, supabase_env)

    assert isolated_environ["PREREVIEW_MODEL1_SERVING_DIR"] == served


@pytest.mark.parametrize("line", ["", "PREREVIEW_MODEL1_SERVING_DIR=   \n"])
def test_a_blank_model1_serving_dir_never_overwrites_the_caller(
    tmp_path: Path, isolated_environ: dict[str, str], line: str
) -> None:
    backend_env, supabase_env = _env_files(tmp_path, line)
    isolated_environ["PREREVIEW_MODEL1_SERVING_DIR"] = "/already/exported"

    MODULE._configure_environment(backend_env, supabase_env)

    assert isolated_environ["PREREVIEW_MODEL1_SERVING_DIR"] == "/already/exported"


def test_configure_environment_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], isolated_environ: dict[str, str]
) -> None:
    backend_env, supabase_env = _env_files(
        tmp_path, "PREREVIEW_MODEL1_SERVING_DIR=/models/model1/serving\n"
    )

    MODULE._configure_environment(backend_env, supabase_env)

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
