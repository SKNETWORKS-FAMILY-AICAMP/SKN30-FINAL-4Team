"""Offline contracts for the deployable PostgreSQL polling worker composition."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from worker.analysis_job import AnalysisJobHandler
from worker.adapters.vllm_llm_client import VllmLLMClient
from worker import main as worker_main
from worker.main import (
    WorkerConfigurationError,
    WorkerSettings,
    _strict_ml_runtime_preflight_enabled,
    _validate_strict_ml_process_identity,
    build_worker,
    configure_runtime_logging,
)
from worker.postgres_repository import PostgresJobRepository
from worker.ml_runtime_preflight import MlRuntimePreflightError


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
    assert settings.request_native_exact_candidate_mode == "off"
    assert settings.model1_serving_dir is None
    assert settings.ml_python_executable is None
    assert settings.ml_timeout_seconds == 180.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "off"),
        ("OFF", "off"),
        (" lines ", "lines"),
        ("lines+continuations", "lines+continuations"),
    ],
)
def test_worker_settings_validate_request_native_candidate_mode(
    raw: str, expected: str,
) -> None:
    settings = WorkerSettings.from_env(
        {
            "DATABASE_URL": "postgresql://worker@db/postgres",
            "SUPABASE_URL": "http://supabase:8000",
            "SUPABASE_SERVICE_ROLE_KEY": "role",
            "PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE": raw,
        }
    )

    assert settings.request_native_exact_candidate_mode == expected


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
                "PREREVIEW_WORKER_TOP_K": "6",
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
        (
            {
                "DATABASE_URL": "postgresql://worker@db/postgres",
                "SUPABASE_URL": "http://supabase:8000",
                "SUPABASE_SERVICE_ROLE_KEY": "role",
                "PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE": "typo",
            },
            "PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE",
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
    monkeypatch.setenv("PREREVIEW_WORKER_TOP_K", "3")
    monkeypatch.setenv("PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE", "lines")

    composition = build_worker()

    assert isinstance(composition.repository, PostgresJobRepository)
    assert isinstance(composition.handler, AnalysisJobHandler)
    assert composition.settings.top_k == 3
    assert composition.settings.heartbeat_seconds == 30.0
    assert composition.settings.lease_seconds == 120
    assert composition.settings.request_native_exact_candidate_mode == "lines"
    assert composition.handler._producer._native_exact_candidate_mode == "lines"  # type: ignore[attr-defined]
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
    assert composition.handler._producer._llm._delegate._model_profiles == {  # type: ignore[attr-defined]
        "request_profile": "configured-terra",
        "fit": "configured-llm",
        "sim": "configured-llm",
        "cpl": "configured-llm",
    }


def test_build_worker_selects_vllm_for_llm_and_keeps_openai_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("PREREVIEW_LLM_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", "https://gpu.internal/v1")
    monkeypatch.setenv("VLLM_API_KEY", "never-print-vllm")
    monkeypatch.setenv("VLLM_LLM_MODEL", "gemma-default")
    monkeypatch.setenv("VLLM_REQUEST_PROFILE_MODEL", "gemma-request")

    composition = build_worker()

    delegate = composition.handler._producer._llm._delegate  # type: ignore[attr-defined]
    assert isinstance(delegate, VllmLLMClient)
    assert delegate._model_profiles == {  # type: ignore[attr-defined]
        "request_profile": "gemma-request",
        "cpl": "gemma-default",
        "fit": "gemma-default",
        "sim": "gemma-default",
    }
    assert composition.handler._producer._model_id == "gemma-request"  # type: ignore[attr-defined]


def test_main_refuses_to_poll_when_ml_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PREREVIEW_STRICT_ML_RUNTIME_PREFLIGHT", "true")
    monkeypatch.setattr(worker_main.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(worker_main.os, "getegid", lambda: 1000)
    settings = SimpleNamespace(
        ml_root=Path("/app/ml"), model1_serving_dir=Path("/opt/prereview/model1"),
        heartbeat_seconds=30.0,
        lease_seconds=120,
        idle_poll_seconds=1.0,
        top_k=5,
    )
    monkeypatch.setattr(
        worker_main, "build_worker", lambda: SimpleNamespace(settings=settings)
    )

    def fail_preflight(**_kwargs: object) -> None:
        raise MlRuntimePreflightError("test mismatch")

    monkeypatch.setattr(worker_main, "verify_ml_runtime", fail_preflight)
    monkeypatch.setattr(
        worker_main,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("queue polling must not start"),
    )

    assert worker_main.main() == 2


@pytest.mark.parametrize("value", ("1", "true", "TRUE", "yes", "on"))
def test_strict_ml_preflight_accepts_explicit_true_values(value: str) -> None:
    assert _strict_ml_runtime_preflight_enabled(value) is True


@pytest.mark.parametrize("value", (None, "", "0", "false", "FALSE", "no", "off"))
def test_strict_ml_preflight_accepts_explicit_false_values(value: str | None) -> None:
    assert _strict_ml_runtime_preflight_enabled(value) is False


@pytest.mark.parametrize("value", ("ture", "enabled", "2"))
def test_strict_ml_preflight_rejects_unknown_values(value: str) -> None:
    with pytest.raises(WorkerConfigurationError, match="must be one of"):
        _strict_ml_runtime_preflight_enabled(value)


def test_strict_ml_process_identity_is_not_checked_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_main.os, "geteuid", lambda: 0)
    monkeypatch.setattr(worker_main.os, "getegid", lambda: 0)

    _validate_strict_ml_process_identity(enabled=False)


@pytest.mark.parametrize("effective_uid,effective_gid", ((0, 1000), (1000, 0)))
def test_strict_ml_process_identity_rejects_root_user_or_group(
    monkeypatch: pytest.MonkeyPatch,
    effective_uid: int,
    effective_gid: int,
) -> None:
    monkeypatch.setattr(worker_main.os, "geteuid", lambda: effective_uid)
    monkeypatch.setattr(worker_main.os, "getegid", lambda: effective_gid)

    with pytest.raises(WorkerConfigurationError, match="non-zero"):
        _validate_strict_ml_process_identity(enabled=True)


def test_strict_ml_process_identity_accepts_non_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_main.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(worker_main.os, "getegid", lambda: 1000)

    _validate_strict_ml_process_identity(enabled=True)


def test_main_refuses_to_build_or_poll_for_an_invalid_preflight_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PREREVIEW_STRICT_ML_RUNTIME_PREFLIGHT", "ture")
    monkeypatch.setattr(
        worker_main,
        "build_worker",
        lambda: pytest.fail("worker composition must not be built"),
    )
    monkeypatch.setattr(
        worker_main,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("queue polling must not start"),
    )

    assert worker_main.main() == 2


def test_main_refuses_to_poll_before_claim_for_an_invalid_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch)
    monkeypatch.setenv("PREREVIEW_LLM_PROVIDER", "not-a-provider")
    monkeypatch.setattr(
        worker_main,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("queue polling must not start"),
    )

    assert worker_main.main() == 2


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


def test_compose_gives_only_analysis_worker_the_dedicated_ml_runtime() -> None:
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
        "PREREVIEW_STRICT_ML_RUNTIME_PREFLIGHT",
    ):
        assert name not in api_section
        assert name not in chat_worker_section

    for name in (
        "PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE",
        "PREREVIEW_EXISTING_NATIVE_EXACT_CANDIDATE_MODE",
    ):
        assert name not in api_section
        assert name not in chat_worker_section

    assert 'PREREVIEW_ML_ROOT: "/app/ml"' in worker_section
    assert 'PREREVIEW_MODEL1_SERVING_DIR: "/opt/prereview/model1"' in worker_section
    assert (
        'PREREVIEW_ML_PYTHON_EXECUTABLE: "/opt/prereview-ml-venv/bin/python"'
        in worker_section
    )
    assert 'PREREVIEW_ML_TIMEOUT_SECONDS: "${PREREVIEW_ML_TIMEOUT_SECONDS:-180}"' in worker_section
    assert 'OPENAI_TIMEOUT_SECONDS: "${OPENAI_TIMEOUT_SECONDS:-120}"' in worker_section
    assert 'OPENAI_MAX_REPAIRS: "${OPENAI_MAX_REPAIRS:-2}"' in worker_section
    assert (
        'PREREVIEW_EXISTING_COMPOSITE_CANDIDATE_MODE: '
        '"${PREREVIEW_EXISTING_COMPOSITE_CANDIDATE_MODE:-off}"'
        in worker_section
    )
    assert (
        'PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE: '
        '"${PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE:-off}"'
        in worker_section
    )
    assert (
        'PREREVIEW_EXISTING_NATIVE_EXACT_CANDIDATE_MODE: '
        '"${PREREVIEW_EXISTING_NATIVE_EXACT_CANDIDATE_MODE:-off}"'
        in worker_section
    )
    assert (
        'OPENAI_REQUEST_PROFILE_MODEL: '
        '"${OPENAI_REQUEST_PROFILE_MODEL:-gpt-5.6-terra}"'
        in worker_section
    )
    assert 'PREREVIEW_STRICT_ML_RUNTIME_PREFLIGHT: "true"' in worker_section
    assert "PREREVIEW_MODEL1_SERVING_HOST_DIR:?" in worker_section
    assert "PREREVIEW_MODEL1_RUNTIME_UID:?" in worker_section
    assert "PREREVIEW_MODEL1_RUNTIME_GID:?" in worker_section
    assert "target: /opt/prereview/model1" in worker_section
    assert "read_only: true" in worker_section
    assert "create_host_path: false" in worker_section
    assert 'HOME: "/tmp"' in worker_section
    assert 'XDG_CACHE_HOME: "/tmp/.cache"' in worker_section


def test_compose_keeps_api_and_chat_on_the_lightweight_image() -> None:
    compose = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "compose.yaml"
    ).read_text(encoding="utf-8")
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker_section = compose.split("\n  worker:\n", 1)[1].split(
        "\n  chat-worker:\n", 1
    )[0]
    chat_worker_section = compose.split("\n  chat-worker:\n", 1)[1]

    assert "context: ." in api_section
    assert "context: ." in chat_worker_section
    assert "context: .." in worker_section
    assert "dockerfile: backend/Dockerfile.ml-worker" in worker_section


def test_api_deployment_caps_total_body_and_asgi_concurrency() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    compose = (backend_root / "compose.yaml").read_text(encoding="utf-8")
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    dockerfile = (backend_root / "Dockerfile").read_text(encoding="utf-8")

    assert "PREREVIEW_HTTP_MAX_BODY_BYTES" in api_section
    assert "PREREVIEW_UPLOAD_CONCURRENCY" in api_section
    assert "PREREVIEW_GLOBAL_QUEUE_MAX" in api_section
    assert "PREREVIEW_API_LIMIT_CONCURRENCY" in api_section
    assert "--limit-concurrency" in api_section
    assert "--limit-concurrency" in dockerfile


def test_ml_worker_dockerfile_copies_tracked_ml_and_installs_native_runtime() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    dockerfile = (backend_root / "Dockerfile.ml-worker").read_text(encoding="utf-8")
    buildkit_dockerignore_path = backend_root / "Dockerfile.ml-worker.dockerignore"
    classic_dockerignore_path = backend_root.parent / ".dockerignore"
    dockerignore = buildkit_dockerignore_path.read_text(encoding="utf-8")

    assert "VIRTUAL_ENV=/opt/prereview-ml-venv" in dockerfile
    assert "PREREVIEW_ML_PYTHON_EXECUTABLE=/opt/prereview-ml-venv/bin/python" in dockerfile
    assert "COPY ml /app/ml" not in dockerfile
    assert "COPY backend /app/backend" not in dockerfile
    assert "COPY backend/app /app/backend/app" in dockerfile
    assert "COPY backend/config/prompts /app/backend/config/prompts" in dockerfile
    assert "requirements.runtime.txt" in dockerfile
    assert "download.pytorch.org/whl/cpu" in dockerfile
    assert "pip install xgboost-cpu==3.4.1" in dockerfile
    assert "import joblib, numpy, pandas, pyarrow, scipy" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "joblib.load('/app/ml/models/model2_canonical/model2_p3_bundle.joblib')" in dockerfile
    assert "business_taxonomy.parquet" in dockerfile
    assert "/app/ml/data/processed" in dockerfile
    assert "/app/ml/reports/model2/core" in dockerfile
    assert "libgomp1" in dockerfile
    assert "PREREVIEW_MODEL1_SERVING_DIR=/opt/prereview/model1" in dockerfile
    # BuildKit reads the Dockerfile-specific ignore file. The classic builder
    # reads only the context-root file, so both security boundaries must stay
    # exactly synchronized.
    assert classic_dockerignore_path.read_bytes() == buildkit_dockerignore_path.read_bytes()

    rules = [
        line
        for raw_line in dockerignore.splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]
    assert rules[0] == "*"
    assert "!backend/**" not in rules
    assert "!backend/vendor/**" not in rules
    assert "!backend/vendor/common_ir_pipeline/src/**" in rules
    assert "!backend/config/" in rules
    assert "!backend/config/prompts/" in rules
    assert "!backend/config/prompts/cpl-purpose-axis-v0.3.txt" in rules
    assert "!backend/config/prompts/cpl-recheck-v0.4.txt" in rules
    assert (
        "!backend/vendor/portable_existing_request_profiles_20260831/"
        "semantic_structuring/*.py"
    ) in rules
    # New CPL runtime dependencies stay within the deliberately narrow
    # allowlist: prompts are copied by name; the two vendor modules remain in
    # the Python-only semantic_structuring closure; and the subprocess adapter
    # stays in the active worker tree.
    assert "!backend/worker/**" in rules
    assert not {
        "backend/config/prompts/cpl-purpose-axis-v0.3.txt",
        "backend/config/prompts/cpl-recheck-v0.4.txt",
        "backend/vendor/portable_existing_request_profiles_20260831/"
        "semantic_structuring/field_regions.py",
        "backend/vendor/portable_existing_request_profiles_20260831/"
        "semantic_structuring/table_relations.py",
        "backend/worker/adapters/ml_subprocess.py",
        "backend/worker/ml_reference.py",
    } & set(rules)
    assert not any("semantic_structuring/**" in rule for rule in rules)
    assert not any("exploratory_study" in rule for rule in rules if rule.startswith("!"))
    assert not any("/examples" in rule for rule in rules if rule.startswith("!"))
    assert "!ml/serving/model1/" not in rules
    assert "!ml/research/" not in rules
    assert "!ml/data/raw/" not in rules
    assert "!ml/reports/" not in rules
    assert "!ml/figures/" not in rules
    assert "!ml/models/model2_canonical/model2_p3_bundle.joblib" in rules
    assert "!ml/data/processed/business_taxonomy.parquet" in rules

    # Directory allowlists intentionally admit the active source trees. These
    # last-match exclusions prevent local credentials and caches from being
    # sent by either builder.
    broad_worker_rule = rules.index("!backend/worker/**")
    hygiene_rules = {
        "**/.git/**",
        "**/.pytest_cache/**",
        "**/__pycache__/**",
        "**/*.py[cod]",
        "**/.venv/**",
        "**/venv/**",
        "**/*.egg-info/**",
        "**/.env",
        "**/.env.*",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/*.pfx",
        "**/id_rsa",
        "**/id_rsa.*",
        "**/handover/**",
    }
    active_worker_paths = {
        "backend/worker/main.py",
        "backend/worker/analysis_job.py",
        "backend/worker/runtime.py",
        "backend/worker/ml_reference.py",
        "backend/worker/adapters/ml_subprocess.py",
    }
    assert hygiene_rules <= set(rules)
    assert all(
        rules.index(rule) > broad_worker_rule
        for rule in hygiene_rules
    )
    assert active_worker_paths.isdisjoint(rules)
    assert all((backend_root.parent / path).is_file() for path in active_worker_paths)


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


def test_runtime_context_excludes_secrets_and_caches_but_keeps_active_worker_modules() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    rules = {
        line.strip()
        for line in (backend_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".pytest_cache",
        "__pycache__",
        "*.py[cod]",
        ".venv",
        "venv",
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        "handover",
    } <= rules

    active_worker_paths = {
        "worker/main.py",
        "worker/analysis_job.py",
        "worker/runtime.py",
        "worker/ml_reference.py",
        "worker/adapters/ml_subprocess.py",
    }
    assert active_worker_paths.isdisjoint(rules)
    assert all((backend_root / path).is_file() for path in active_worker_paths)
