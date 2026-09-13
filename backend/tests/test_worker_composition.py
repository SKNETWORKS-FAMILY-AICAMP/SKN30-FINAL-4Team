"""Offline contracts for the deployable PostgreSQL polling worker composition."""

from __future__ import annotations

import logging

import pytest

from worker.analysis_job import AnalysisJobHandler
from worker.main import (
    WorkerConfigurationError,
    WorkerSettings,
    build_worker,
    configure_runtime_logging,
)
from worker.postgres_repository import PostgresJobRepository


def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://worker:never-print@db.internal:5432/postgres")
    monkeypatch.setenv("SUPABASE_URL", "http://supabase.internal:8000/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "never-print-service-role")
    monkeypatch.setenv("OPENAI_API_KEY", "never-print-openai")
    monkeypatch.setenv("OPENAI_LLM_MODEL", "configured-llm")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "configured-embedding")


def test_worker_settings_hold_queue_defaults_and_accept_legacy_dsn_name() -> None:
    settings = WorkerSettings.from_env(
        {
            "SUPABASE_DB_URL": "postgresql://worker@db/postgres",
            "SUPABASE_URL": "http://supabase:8000/",
            "SUPABASE_SECRET_KEY": "service-role",
        }
    )

    assert settings.database_url == "postgresql://worker@db/postgres"
    assert settings.supabase_url == "http://supabase:8000"
    assert settings.heartbeat_seconds == 30.0
    assert settings.lease_seconds == 120
    assert settings.top_k == 5
    assert settings.parse_timeout_seconds == 120.0
    assert settings.model1_serving_dir is None
    assert settings.ml_python_executable is None
    assert settings.ml_timeout_seconds == 180.0


def test_worker_settings_read_external_ml_boundaries() -> None:
    settings = WorkerSettings.from_env(
        {
            "DATABASE_URL": "postgresql://worker@db/postgres",
            "SUPABASE_URL": "http://supabase:8000",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role",
            "PREREVIEW_ML_ROOT": r"C:\external\ml",
            "PREREVIEW_MODEL1_SERVING_DIR": r"C:\external\serving\model1",
            "PREREVIEW_ML_PYTHON_EXECUTABLE": r"C:\venvs\ml\python.exe",
            "PREREVIEW_ML_TIMEOUT_SECONDS": "240",
        }
    )

    assert str(settings.ml_root).endswith(r"external\ml")
    assert str(settings.model1_serving_dir).endswith(r"external\serving\model1")
    assert settings.ml_python_executable.endswith(r"venvs\ml\python.exe")
    assert settings.ml_timeout_seconds == 240.0


@pytest.mark.parametrize(
    "overrides, message",
    [
        (
            {
                "DATABASE_URL": "postgresql://worker@db/postgres",
                "SUPABASE_URL": "http://supabase:8000",
                "SUPABASE_SERVICE_ROLE_KEY": "role",
                "PREREVIEW_WORKER_HEARTBEAT_SECONDS": "120",
                "PREREVIEW_WORKER_LEASE_SECONDS": "120",
            },
            "shorter",
        ),
        (
            {
                "DATABASE_URL": "postgresql://worker@db/postgres",
                "SUPABASE_URL": "http://supabase:8000",
                "SUPABASE_SERVICE_ROLE_KEY": "role",
                "PREREVIEW_WORKER_TOP_K": "101",
            },
            "TOP_K",
        ),
        (
            {
                "DATABASE_URL": "postgresql://worker@db/postgres",
                "SUPABASE_URL": "http://supabase:8000",
                "SUPABASE_SERVICE_ROLE_KEY": "role",
                "PREREVIEW_WORKER_LEASE_SECONDS": "29",
            },
            "between 30 and 3600",
        ),
    ],
)
def test_worker_settings_fail_closed_on_unsafe_queue_values(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(WorkerConfigurationError, match=message):
        WorkerSettings.from_env(overrides)


def test_build_worker_connects_only_trusted_server_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("PREREVIEW_WORKER_TOP_K", "7")

    composition = build_worker()

    assert isinstance(composition.repository, PostgresJobRepository)
    assert isinstance(composition.handler, AnalysisJobHandler)
    assert composition.settings.top_k == 7
    assert composition.settings.heartbeat_seconds == 30.0
    assert composition.settings.lease_seconds == 120
    # Adapter debug representations are a useful operational boundary: they
    # must never accidentally expose either trusted server credential.
    rendered = repr(composition.repository) + repr(composition.handler)
    assert "never-print" not in rendered


def test_build_worker_uses_request_profile_override_without_changing_fit_or_sim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("OPENAI_REQUEST_PROFILE_MODEL", "configured-terra")

    composition = build_worker()

    assert composition.handler._producer._model_id == "configured-terra"  # type: ignore[attr-defined]
    assert composition.handler._producer._llm._model_profiles == {  # type: ignore[attr-defined]
        "request_profile": "configured-terra",
        "fit": "configured-llm",
        "sim": "configured-llm",
    }


def test_debug_mode_never_enables_provider_or_transport_request_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Document text must not enter logs through OpenAI/httpx DEBUG tracing."""

    logger_names = ("openai", "openai._base_client", "httpx", "httpcore")
    loggers = [logging.getLogger(name) for name in logger_names]
    original_levels = [logger.level for logger in loggers]
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    sentinel = "do-not-log-uploaded-notice-text"
    try:
        # Simulate a dependency that had previously enabled a child logger.
        for logger in loggers:
            logger.setLevel(logging.DEBUG)

        configure_runtime_logging(level="DEBUG")

        assert logging.getLogger("openai").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("openai._base_client").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING

        # pytest already installs a root handler, so basicConfig intentionally
        # leaves its level alone.  Simulate a production DEBUG root explicitly.
        root_logger.setLevel(logging.DEBUG)
        for logger in loggers:
            logger.debug("provider request payload=%s", sentinel)
        assert sentinel not in caplog.text
    finally:
        root_logger.setLevel(original_root_level)
        for logger, original_level in zip(loggers, original_levels, strict=True):
            logger.setLevel(original_level)


def test_compose_starts_worker_by_module_without_publishing_a_port() -> None:
    compose = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "compose.yaml"
    ).read_text(encoding="utf-8")
    worker_section = compose.split("\n  worker:\n", 1)[1]

    assert 'command: ["python", "-m", "worker.main"]' in worker_section
    assert 'profiles: ["worker"]' not in worker_section
    assert "stop_grace_period: 10m" in worker_section
    assert "ports:" not in worker_section
    assert "redis" not in worker_section.lower()


def test_compose_passes_external_ml_boundaries_only_to_analysis_worker() -> None:
    compose = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "compose.yaml"
    ).read_text(encoding="utf-8")
    worker_section = compose.split("\n  worker:\n", 1)[1].split(
        "\n  chat-worker:\n", 1
    )[0]
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    chat_worker_section = compose.split("\n  chat-worker:\n", 1)[1]

    for name in (
        "PREREVIEW_ML_ROOT",
        "PREREVIEW_MODEL1_SERVING_DIR",
        "PREREVIEW_ML_PYTHON_EXECUTABLE",
        "PREREVIEW_ML_TIMEOUT_SECONDS",
    ):
        assert f'{name}: "${{{name}:-' in worker_section
        assert name not in api_section
        assert name not in chat_worker_section


def test_compose_restarts_both_runtime_processes_unless_stopped() -> None:
    compose = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "compose.yaml"
    ).read_text(encoding="utf-8")
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker_section = compose.split("\n  worker:\n", 1)[1]

    assert "restart: unless-stopped" in api_section
    assert "restart: unless-stopped" in worker_section


def test_compose_never_defaults_to_offline_header_auth() -> None:
    compose = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "compose.yaml"
    ).read_text(encoding="utf-8")
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]

    assert 'PREREVIEW_OFFLINE_MODE: "${PREREVIEW_OFFLINE_MODE:-false}"' in api_section
    assert 'PREREVIEW_OFFLINE_MODE: "${PREREVIEW_OFFLINE_MODE:-true}"' not in api_section


def test_compose_defaults_to_loopback_and_secure_session_cookies() -> None:
    compose = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "compose.yaml"
    ).read_text(encoding="utf-8")
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]

    assert (
        '"${PREREVIEW_API_BIND_ADDRESS:-127.0.0.1}:'
        '${PREREVIEW_API_PORT:-8001}:8000"'
    ) in api_section
    assert (
        'PREREVIEW_AUTH_COOKIE_SECURE: '
        '"${PREREVIEW_AUTH_COOKIE_SECURE:-true}"'
    ) in api_section
    assert '${PREREVIEW_AUTH_COOKIE_SECURE:-false}' not in api_section


def test_runtime_image_excludes_unimportable_retired_worker_modules() -> None:
    dockerignore = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / ".dockerignore"
    ).read_text(encoding="utf-8").splitlines()

    assert {
        "worker/analysis.py",
        "worker/dispatcher.py",
        "worker/execution_log.py",
        "worker/jobs.py",
        "worker/kb_ingest.py",
        "worker/kb_store.py",
        "worker/persistence.py",
        "worker/queue.py",
        "worker/report_pdf.py",
    } <= set(dockerignore)

    # These are active subprocess-boundary modules, not the retired worker.
    assert "worker/ml_reference.py" not in dockerignore
    assert "worker/adapters/ml_subprocess.py" not in dockerignore

    # Existing KB의 지원 writer는 scripts/ingest_existing_profile.py다. 이
    # compatibility path가 supported worker image에 다시 들어오면 migration
    # activation의 writer boundary가 불명확해진다.
    main_source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "worker" / "main.py"
    ).read_text(encoding="utf-8")
    assert "kb_store" not in main_source
