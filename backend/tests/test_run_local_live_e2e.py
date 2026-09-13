from __future__ import annotations

import asyncio
import argparse
from collections.abc import Callable
import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "run_local_live_e2e.py"
SPEC = importlib.util.spec_from_file_location("run_local_live_e2e", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


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


@pytest.mark.parametrize("raw", ("0", "-1", "nan", "inf", "not-a-number"))
def test_poll_timeout_rejects_non_positive_or_non_finite_values(raw: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        MODULE._positive_timeout_seconds(raw)


def test_poll_timeout_accepts_a_positive_value() -> None:
    assert MODULE._positive_timeout_seconds("123.5") == 123.5


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

    assert environment["OPENAI_LLM_MODEL"] == "gpt-5.6-luna"
    assert environment["OPENAI_REQUEST_PROFILE_MODEL"] == "gpt-5.6-terra"


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
            asyncio.run(MODULE._run(Path("never-read.hwp")))
    finally:
        root_logger.setLevel(original_root_level)
        for logger, original_level in zip(loggers, original_levels, strict=True):
            logger.setLevel(original_level)


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
    assert "workspace.claim_next_conversation_message" in calls[3][0]
    assert calls[3][1] == ("chat-worker-test", 120)
    assert exits == [None]


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
    def __init__(self, case_id: str, payload: object) -> None:
        self._case_id = case_id
        self._payload = payload
        self.calls = 0

    async def get(self, path: str) -> _ChatMessageResponse:
        assert path == f"/api/v1/analysis-cases/{self._case_id}/messages"
        self.calls += 1
        return _ChatMessageResponse(self._payload)


def test_assistant_message_state_returns_only_a_valid_matching_assistant() -> None:
    case_id = "case-id"
    assistant_id = "assistant-id"
    client = _ChatMessageClient(
        case_id,
        [
            {"message_id": "user-id", "role": "user", "status": "completed"},
            {
                "message_id": assistant_id,
                "analysis_case_id": case_id,
                "role": "assistant",
                "status": "generating",
            },
        ],
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
    client = _ChatMessageClient("case-id", [message])

    with pytest.raises(MODULE.E2EFailure, match="chat polling response is invalid"):
        asyncio.run(MODULE._assistant_message_state(client, "case-id", "assistant-id"))


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
        ({"active_analysis_runs": 0, "active_chat_messages": 0}, None),
        ({"active_analysis_runs": 1, "active_chat_messages": 0}, "analysis=1, chat=0"),
        ({"active_analysis_runs": 0, "active_chat_messages": 2}, "analysis=0, chat=2"),
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
