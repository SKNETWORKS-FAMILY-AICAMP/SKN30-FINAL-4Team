"""Contract tests for the migration-21/22 psycopg worker repository.

No test connects to PostgreSQL.  The small fake has psycopg's context-manager
shape so tests also prove each repository method owns and closes one connection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Barrier, Lock, Thread
from typing import Any
from unittest.mock import ANY
from uuid import UUID, uuid4

import pytest

from worker.postgres_repository import (
    PostgresJobRepository,
    WorkerDatabaseUnavailable,
    WorkerQueueContractError,
)
from worker.runtime import JobFailure


@dataclass
class FakeCursor:
    rows: list[Mapping[str, Any] | None]
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.calls.append((query, params))

    def fetchone(self) -> Mapping[str, Any] | None:
        return self.rows.pop(0)


@dataclass
class FakeConnection:
    rows: list[Mapping[str, Any] | None]
    cursor_instance: FakeCursor = field(init=False)
    entered: int = 0
    exited: int = 0

    def __post_init__(self) -> None:
        self.cursor_instance = FakeCursor(self.rows)

    def __enter__(self) -> "FakeConnection":
        self.entered += 1
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.exited += 1
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_instance


@dataclass
class FakeConnect:
    row_sets: list[list[Mapping[str, Any] | None]]
    connections: list[FakeConnection] = field(default_factory=list)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)

    def __call__(self, database_url: str, **kwargs: object) -> FakeConnection:
        with self.lock:
            self.calls.append((database_url, dict(kwargs)))
            connection = FakeConnection(self.row_sets.pop(0))
            self.connections.append(connection)
            return connection


def _repository(*row_sets: list[Mapping[str, Any] | None]) -> tuple[PostgresJobRepository, FakeConnect]:
    factory = FakeConnect(list(row_sets))
    return (
        PostgresJobRepository(
            "postgresql://worker:do-not-log@database.example:5432/postgres",
            connect_timeout_seconds=7,
            connect=factory,
        ),
        factory,
    )


def test_claim_maps_migration_queue_row_to_immutable_payload() -> None:
    run_id = uuid4()
    processing_run_id = uuid4()
    expires_at = datetime(2026, 9, 10, tzinfo=timezone.utc)
    repository, factory = _repository(
        [
            {
                "analysis_run_pk": run_id,
                "source_bucket": "request-temp",
                "source_object_key": "request-source/user/run/source.hwp",
                "source_content_sha256": "a" * 64,
                "processing_run_pk": processing_run_id,
                "attempt_count": 1,
                "lease_expires_at": expires_at,
                "heartbeat_interval_seconds": 30,
            }
        ]
    )

    job = repository.claim(worker_id="worker-a", lease_seconds=120)

    assert job is not None
    assert job.job_pk == run_id
    assert job.processing_run_pk == processing_run_id
    assert job.payload == {
        "source_bucket": "request-temp",
        "source_object_key": "request-source/user/run/source.hwp",
        "source_content_sha256": "a" * 64,
        "attempt_count": 1,
        "lease_expires_at": expires_at,
        "heartbeat_interval_seconds": 30,
    }
    assert factory.calls == [
        (
            "postgresql://worker:do-not-log@database.example:5432/postgres",
            {"connect_timeout": 7, "row_factory": ANY},
        )
    ]
    query, params = factory.connections[0].cursor_instance.calls[0]
    assert "workspace.claim_next_analysis_run" in query
    assert params == ("worker-a", 120)
    assert factory.connections[0].entered == factory.connections[0].exited == 1


def test_fenced_transitions_forward_processing_run_token_and_safe_failure() -> None:
    run_id = uuid4()
    processing_run_id = uuid4()
    repository, factory = _repository(
        [{"is_live": True}],
        [{"analysis_case_pk": None}],
        [{"is_failed": True}],
    )

    assert repository.heartbeat(
        job_pk=run_id,
        processing_run_pk=processing_run_id,
        worker_id="worker-a",
        lease_seconds=120,
    )
    assert not repository.complete(
        job_pk=run_id,
        processing_run_pk=processing_run_id,
        worker_id="worker-a",
        result={
            "program_name": "테스트 사업",
            "axes": [],
            "candidates": [],
            "evidences": [],
        },
    )
    assert repository.fail(
        job_pk=run_id,
        processing_run_pk=processing_run_id,
        worker_id="worker-a",
        failure=JobFailure(
            kind="RuntimeError",
            message=(
                "database postgresql://worker:very-secret@db.example/postgres "
                "api_key=also-secret"
            ),
        ),
    )

    heartbeat_query, heartbeat_params = factory.connections[0].cursor_instance.calls[0]
    complete_query, complete_params = factory.connections[1].cursor_instance.calls[0]
    fail_query, fail_params = factory.connections[2].cursor_instance.calls[0]
    assert "workspace.heartbeat_analysis_run" in heartbeat_query
    assert "workspace.persist_analysis_result_core" in complete_query
    assert "workspace.fail_analysis_run" in fail_query
    assert heartbeat_params[:2] == complete_params[:2] == fail_params[:2] == (
        run_id,
        processing_run_id,
    )
    # JSONB adaptation is deliberate: migration 22, not a client-generated
    # analysis case ID, owns materialisation and the terminal transition.
    assert type(complete_params[2]).__name__ == "Jsonb"
    assert fail_params[2:4] == (
        "ANALYSIS_FAILED",
        "분석에 실패했습니다. 잠시 후 다시 시도해 주세요.",
    )
    assert "very-secret" not in fail_params[4]
    assert "also-secret" not in fail_params[4]
    assert "RuntimeError" in fail_params[4]


def test_complete_is_true_only_when_fenced_result_function_returns_uuid() -> None:
    run_id = uuid4()
    processing_run_id = uuid4()
    repository, factory = _repository([{"analysis_case_pk": uuid4()}])

    assert repository.complete(
        job_pk=run_id,
        processing_run_pk=processing_run_id,
        worker_id="worker-a",
        result={"axes": []},
    )
    _query, params = factory.connections[0].cursor_instance.calls[0]
    assert params[:2] == (run_id, processing_run_id)


@pytest.mark.parametrize(
    "result",
    [
        ["not", "a", "mapping"],
        {"score": float("nan")},
        {"nested": {"score": float("inf")}},
        {"raw": b"not-json"},
    ],
)
def test_complete_rejects_non_json_results_before_any_database_call(result: object) -> None:
    repository, factory = _repository()

    with pytest.raises(WorkerQueueContractError, match="JSON"):
        repository.complete(
            job_pk=uuid4(),
            processing_run_pk=uuid4(),
            worker_id="worker-a",
            result=result,
        )

    assert factory.calls == []


def test_heartbeat_calls_use_distinct_connections_when_concurrent() -> None:
    run_id = uuid4()
    processing_run_id = uuid4()
    repository, factory = _repository([{"is_live": True}], [{"is_live": True}])
    barrier = Barrier(3)
    outcomes: list[bool] = []

    def heartbeat() -> None:
        barrier.wait()
        outcomes.append(
            repository.heartbeat(
                job_pk=run_id,
                processing_run_pk=processing_run_id,
                worker_id="worker-a",
                lease_seconds=120,
            )
        )

    first = Thread(target=heartbeat)
    second = Thread(target=heartbeat)
    first.start()
    second.start()
    barrier.wait()
    first.join(timeout=1)
    second.join(timeout=1)

    assert outcomes == [True, True]
    assert len(factory.connections) == 2
    assert factory.connections[0] is not factory.connections[1]
    assert all(connection.entered == connection.exited == 1 for connection in factory.connections)


def test_database_error_has_safe_text_and_never_includes_dsn() -> None:
    def broken_connect(*_args: object, **_kwargs: object) -> FakeConnection:
        raise OSError("could not connect postgresql://worker:secret@db.example/postgres")

    repository = PostgresJobRepository(
        "postgresql://worker:do-not-log@database.example:5432/postgres",
        connect=broken_connect,
    )

    with pytest.raises(WorkerDatabaseUnavailable) as error:
        repository.claim(worker_id="worker-a", lease_seconds=120)

    assert str(error.value) == "Worker database is unavailable"
    assert "postgresql" not in repr(error.value)
    assert "do-not-log" not in repr(repository)
