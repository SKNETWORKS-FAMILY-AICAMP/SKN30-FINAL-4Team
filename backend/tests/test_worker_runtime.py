"""Deterministic worker-runtime tests with no database or network."""

from __future__ import annotations

import signal
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from worker.runtime import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_LEASE_SECONDS,
    ClaimedJob,
    JobFailure,
    RunOutcome,
    WorkerRuntime,
)


@dataclass
class FakeRepository:
    jobs: list[ClaimedJob] = field(default_factory=list)
    heartbeat_result: bool = True
    complete_result: bool = True
    fail_result: bool = True
    calls: list[tuple[str, Mapping[str, object]]] = field(default_factory=list)
    heartbeat_seen: threading.Event = field(default_factory=threading.Event)

    def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        self.calls.append(
            ("claim", {"worker_id": worker_id, "lease_seconds": lease_seconds})
        )
        return self.jobs.pop(0) if self.jobs else None

    def heartbeat(self, **values: object) -> bool:
        self.calls.append(("heartbeat", values))
        self.heartbeat_seen.set()
        return self.heartbeat_result

    def complete(self, **values: object) -> bool:
        self.calls.append(("complete", values))
        return self.complete_result

    def fail(self, **values: object) -> bool:
        self.calls.append(("fail", values))
        return self.fail_result


class ReturnHandler:
    def __init__(self, result: object) -> None:
        self.result = result
        self.seen: list[ClaimedJob] = []

    def handle(self, job: ClaimedJob) -> object:
        self.seen.append(job)
        return self.result


def _job() -> ClaimedJob:
    return ClaimedJob(
        job_pk="analysis-run-1",
        processing_run_pk="processing-run-9",
        payload={"pipeline": "request"},
    )


def test_success_uses_worker_identity_defaults_and_fencing_token() -> None:
    repository = FakeRepository(jobs=[_job()])
    handler = ReturnHandler({"artifact": "result-1"})
    runtime = WorkerRuntime(repository, handler, worker_id="worker-a")

    assert runtime.run_once() is RunOutcome.COMPLETED
    assert handler.seen == [_job()]
    assert repository.calls == [
        (
            "claim",
            {"worker_id": "worker-a", "lease_seconds": DEFAULT_LEASE_SECONDS},
        ),
        (
            "complete",
            {
                "job_pk": "analysis-run-1",
                "processing_run_pk": "processing-run-9",
                "worker_id": "worker-a",
                "result": {"artifact": "result-1"},
            },
        ),
    ]
    assert DEFAULT_HEARTBEAT_SECONDS == 30.0
    assert DEFAULT_LEASE_SECONDS == 120


def test_handler_error_is_recorded_and_retry_policy_is_not_in_runtime() -> None:
    class BrokenHandler:
        def handle(self, _job: ClaimedJob) -> object:
            raise ValueError("bad\ninput")

    repository = FakeRepository(jobs=[_job()])
    runtime = WorkerRuntime(repository, BrokenHandler(), worker_id="worker-a")

    assert runtime.run_once() is RunOutcome.FAILED
    assert [name for name, _values in repository.calls] == ["claim", "fail"]
    failure_call = repository.calls[-1][1]
    assert failure_call["processing_run_pk"] == "processing-run-9"
    assert failure_call["failure"] == JobFailure(kind="ValueError", message="bad input")
    assert "max_attempts" not in repository.calls[0][1]


def test_repository_claim_failure_does_not_terminate_the_worker() -> None:
    class BrokenClaimRepository(FakeRepository):
        def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
            raise RuntimeError("database temporarily unavailable")

    runtime = WorkerRuntime(
        BrokenClaimRepository(), ReturnHandler(None), worker_id="worker-a"
    )

    assert runtime.run_once() is RunOutcome.UNAVAILABLE


def test_completion_failure_is_recorded_for_the_same_fenced_attempt() -> None:
    class BrokenCompleteRepository(FakeRepository):
        def complete(self, **values: object) -> bool:
            self.calls.append(("complete", values))
            raise ValueError("result contract rejected")

    repository = BrokenCompleteRepository(jobs=[_job()])
    runtime = WorkerRuntime(
        repository, ReturnHandler({"result": "invalid"}), worker_id="worker-a"
    )

    assert runtime.run_once() is RunOutcome.FAILED
    assert [name for name, _values in repository.calls] == [
        "claim",
        "complete",
        "fail",
    ]
    failure = repository.calls[-1][1]["failure"]
    assert failure == JobFailure(
        kind="ValueError", message="result contract rejected"
    )


def test_failure_recording_error_leaves_the_lease_for_database_recovery() -> None:
    class BrokenFailureRepository(FakeRepository):
        def fail(self, **values: object) -> bool:
            self.calls.append(("fail", values))
            raise RuntimeError("database temporarily unavailable")

    class BrokenHandler:
        def handle(self, _job: ClaimedJob) -> object:
            raise ValueError("bad input")

    repository = BrokenFailureRepository(jobs=[_job()])
    runtime = WorkerRuntime(repository, BrokenHandler(), worker_id="worker-a")

    assert runtime.run_once() is RunOutcome.UNAVAILABLE
    assert [name for name, _values in repository.calls] == ["claim", "fail"]


def test_heartbeat_runs_during_handler_and_a_stale_completion_is_fenced() -> None:
    repository = FakeRepository(
        jobs=[_job()], heartbeat_result=False, complete_result=False
    )

    class WaitForHeartbeatHandler:
        def handle(self, _job: ClaimedJob) -> str:
            assert repository.heartbeat_seen.wait(timeout=1)
            return "late-result"

    runtime = WorkerRuntime(
        repository,
        WaitForHeartbeatHandler(),
        worker_id="worker-a",
        heartbeat_seconds=0.01,
        lease_seconds=1,
    )

    assert runtime.run_once() is RunOutcome.FENCED
    names = [name for name, _values in repository.calls]
    assert names == ["claim", "heartbeat", "complete"]
    for name, values in repository.calls[1:]:
        assert values["processing_run_pk"] == "processing-run-9", name


def test_idle_poll_is_interruptible_instead_of_busy_looping() -> None:
    repository = FakeRepository()
    first_claim = threading.Event()
    original_claim = repository.claim

    def observed_claim(*, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        result = original_claim(worker_id=worker_id, lease_seconds=lease_seconds)
        first_claim.set()
        return result

    repository.claim = observed_claim  # type: ignore[method-assign]
    runtime = WorkerRuntime(
        repository,
        ReturnHandler(None),
        worker_id="worker-a",
        idle_poll_seconds=60,
    )
    thread = threading.Thread(target=runtime.run_forever)
    thread.start()

    assert first_claim.wait(timeout=1)
    runtime.request_stop()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert [name for name, _values in repository.calls] == ["claim"]


def test_signal_context_requests_stop_and_restores_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = WorkerRuntime(FakeRepository(), ReturnHandler(None), worker_id="worker-a")
    previous = {signal.SIGINT: object(), signal.SIGTERM: object()}
    current = dict(previous)

    monkeypatch.setattr(signal, "getsignal", lambda signum: current[signum])
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: current.__setitem__(signum, handler),
    )

    with runtime.install_signal_handlers():
        installed = current[signal.SIGTERM]
        assert callable(installed)
        installed(signal.SIGTERM, None)
        assert runtime.stop_requested

    assert current == previous


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"worker_id": ""}, "worker_id"),
        ({"heartbeat_seconds": 120}, "shorter"),
        ({"idle_poll_seconds": 0}, "idle_poll_seconds"),
    ],
)
def test_invalid_runtime_configuration_is_rejected(
    values: dict[str, object], message: str
) -> None:
    arguments: dict[str, object] = {"worker_id": "worker-a", **values}
    with pytest.raises(ValueError, match=message):
        WorkerRuntime(FakeRepository(), ReturnHandler(None), **arguments)  # type: ignore[arg-type]
