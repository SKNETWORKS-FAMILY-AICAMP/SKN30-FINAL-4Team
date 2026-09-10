from __future__ import annotations

import asyncio
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
    assert "FOR UPDATE OF ar, dispatch" in calls[3][0]
    assert calls[3][1] == ("target-run",)
    assert "workspace.claim_next_analysis_run" in calls[4][0]
    assert calls[4][1] == ("worker-test", 120)
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
