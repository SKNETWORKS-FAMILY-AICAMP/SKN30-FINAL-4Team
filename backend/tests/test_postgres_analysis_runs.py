"""Offline transaction-order tests for the FastAPI analysis-run adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.pq import DiagnosticField

from app.infrastructure import postgres_analysis_runs
from app.infrastructure.postgres_analysis_runs import PostgresAnalysisRunRepository
from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    AnalysisRunFinalizationRejected,
    AnalysisRunFinalizationUncertain,
    SourceObject,
)


RUN_ID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = "22222222-2222-2222-2222-222222222222"
DATABASE_URL = "postgresql://api:do-not-log@database.example:5432/postgres"
NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)
SOURCE = SourceObject(
    bucket="request-temp",
    object_key=f"{RUN_ID}/source/{'a' * 64}.hwpx",
    content_sha256="a" * 64,
    filename="request.hwpx",
    mime_type="application/vnd.hancom.hwpx",
    declared_mime_type="application/zip",
    size_bytes=1234,
)


@dataclass(frozen=True)
class Step:
    name: str
    query_contains: str
    one: Mapping[str, Any] | None = None
    many: tuple[Mapping[str, Any], ...] = ()
    error: BaseException | None = None


@dataclass
class FakeCursor:
    steps: list[Step]
    events: list[str]
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    current: Step | None = None

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *_values: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[object, ...]) -> None:
        assert self.steps, f"unexpected SQL: {query}"
        step = self.steps.pop(0)
        assert step.query_contains in query
        self.current = step
        self.calls.append((query, params))
        self.events.append(step.name)
        if step.error is not None:
            raise step.error

    async def fetchone(self) -> Mapping[str, Any] | None:
        assert self.current is not None
        return self.current.one

    async def fetchall(self) -> list[Mapping[str, Any]]:
        assert self.current is not None
        return list(self.current.many)


@dataclass
class FakeConnection:
    steps: list[Step]
    events: list[str]
    commit_error: BaseException | None = None
    cursor_instance: FakeCursor = field(init=False)

    def __post_init__(self) -> None:
        self.cursor_instance = FakeCursor(self.steps, self.events)

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *_values: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    async def commit(self) -> None:
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error


@dataclass
class FakeConnect:
    outcomes: list[FakeConnection | BaseException]
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def connect(self, database_url: str, **kwargs: object) -> FakeConnection:
        self.calls.append((database_url, dict(kwargs)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _install_connect(
    monkeypatch: pytest.MonkeyPatch,
    *outcomes: FakeConnection | BaseException,
) -> FakeConnect:
    factory = FakeConnect(list(outcomes))

    class AsyncConnection:
        connect = staticmethod(factory.connect)

    monkeypatch.setattr(postgres_analysis_runs.psycopg, "AsyncConnection", AsyncConnection)
    return factory


def _repository() -> PostgresAnalysisRunRepository:
    return PostgresAnalysisRunRepository(DATABASE_URL, connect_timeout_seconds=7)


def _run_row(status: str) -> dict[str, Any]:
    return {
        "analysis_run_pk": RUN_ID,
        "status": status,
        "analysis_case_pk": None,
        "error_code": None,
        "error_message": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _finalization_row(
    status: str,
    *,
    source_artifact_matches: bool | None = None,
) -> dict[str, Any]:
    row = {
        **_run_row(status),
        "source_bucket": SOURCE.bucket,
        "source_object_key": SOURCE.object_key,
        "source_content_sha256": SOURCE.content_sha256,
    }
    if source_artifact_matches is not None:
        row["source_artifact_matches"] = source_artifact_matches
    return row


def _reservation_row(status: str = "uploading") -> dict[str, Any]:
    return {
        **_finalization_row(status),
        "original_filename": SOURCE.filename,
        "declared_mime_type": SOURCE.declared_mime_type,
        "declared_size_bytes": SOURCE.size_bytes,
    }


def _unique_violation(constraint_name: str) -> psycopg.errors.UniqueViolation:
    return psycopg.errors.UniqueViolation(
        info={DiagnosticField.CONSTRAINT_NAME: constraint_name.encode()}
    )


def test_reserve_claims_stale_cleanup_before_run_and_dispatch_then_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stale_id = str(uuid4())
    connection = FakeConnection(
        [
            Step(
                "reserve.stale_cleanup",
                "WITH candidate_runs AS MATERIALIZED",
                many=(
                    {
                        "analysis_run_pk": stale_id,
                        "source_bucket": "request-temp",
                        "source_object_key": f"{stale_id}/source/stale.hwpx",
                    },
                ),
            ),
            Step("reserve.run", "INSERT INTO workspace.analysis_run (", one=_run_row("uploading")),
            Step("reserve.dispatch", "INSERT INTO workspace.analysis_run_dispatch ("),
        ],
        events,
    )
    factory = _install_connect(monkeypatch, connection)

    reservation = asyncio.run(
        _repository().reserve_uploading(
            analysis_run_id=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert events == [
        "reserve.stale_cleanup",
        "reserve.run",
        "reserve.dispatch",
        "commit",
    ]
    assert reservation.record.status == "uploading"
    assert reservation.cleanup_objects[0].analysis_run_id == stale_id
    cleanup_query, _cleanup_params = connection.cursor_instance.calls[0]
    active_prefilter = cleanup_query.index("AND NOT EXISTS")
    batch_limit = cleanup_query.index("LIMIT %s")
    run_lock = cleanup_query.index("FOR UPDATE OF analysis_run SKIP LOCKED")
    dispatch_lock = cleanup_query.index("FOR UPDATE OF dispatch")
    assert active_prefilter < batch_limit
    assert run_lock < dispatch_lock
    assert "LEFT JOIN locked_dispatch" in cleanup_query
    assert "ELSE 'failed'" in cleanup_query
    assert factory.calls == [
        (
            DATABASE_URL,
            {"connect_timeout": 7, "row_factory": postgres_analysis_runs.dict_row},
        )
    ]
    assert connection.steps == []


@pytest.mark.parametrize(
    "constraint_name",
    [
        "analysis_run_pkey",
        "uq_workspace_analysis_run_one_active_per_user",
    ],
)
def test_reserve_unique_violation_resumes_exact_uploading_reservation(
    monkeypatch: pytest.MonkeyPatch,
    constraint_name: str,
) -> None:
    events: list[str] = []
    attempted = FakeConnection(
        [
            Step(
                "reserve.stale_cleanup",
                "WITH candidate_runs AS MATERIALIZED",
            ),
            Step(
                "reserve.run",
                "INSERT INTO workspace.analysis_run (",
                error=_unique_violation(constraint_name),
            ),
        ],
        events,
    )
    readback = FakeConnection(
        [
            Step(
                "reserve.readback",
                "analysis_run.original_filename",
                one=_reservation_row(),
            )
        ],
        events,
    )
    factory = _install_connect(monkeypatch, attempted, readback)

    reservation = asyncio.run(
        _repository().reserve_uploading(
            analysis_run_id=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert reservation.replayed is False
    assert reservation.record.analysis_run_id == RUN_ID
    assert reservation.record.status == "uploading"
    assert events == ["reserve.stale_cleanup", "reserve.run", "reserve.readback"]
    assert len(factory.calls) == 2
    _query, params = readback.cursor_instance.calls[0]
    assert params == (RUN_ID, OWNER_ID)


@pytest.mark.parametrize(
    "status",
    ["queued", "running", "succeeded", "failed", "cancelled", "cleanup_pending"],
)
def test_reserve_unique_violation_replays_exact_non_uploading_reservation(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    events: list[str] = []
    attempted = FakeConnection(
        [
            Step(
                "reserve.stale_cleanup",
                "WITH candidate_runs AS MATERIALIZED",
            ),
            Step(
                "reserve.run",
                "INSERT INTO workspace.analysis_run (",
                error=_unique_violation("analysis_run_pkey"),
            ),
        ],
        events,
    )
    readback = FakeConnection(
        [
            Step(
                "reserve.readback",
                "analysis_run.original_filename",
                one=_reservation_row(status),
            )
        ],
        events,
    )
    _install_connect(monkeypatch, attempted, readback)

    reservation = asyncio.run(
        _repository().reserve_uploading(
            analysis_run_id=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert reservation.replayed is True
    assert reservation.record.status == status


@pytest.mark.parametrize(
    "constraint_name",
    [
        "analysis_run_pkey",
        "uq_workspace_analysis_run_one_active_per_user",
    ],
)
@pytest.mark.parametrize(
    ("field", "different_value"),
    [
        ("source_bucket", "different-bucket"),
        ("source_object_key", "different/source.hwpx"),
        ("source_content_sha256", "b" * 64),
        ("original_filename", "different.hwpx"),
        ("declared_mime_type", "application/octet-stream"),
        ("declared_size_bytes", SOURCE.size_bytes + 1),
    ],
)
def test_reserve_unique_violation_rejects_immutable_metadata_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    constraint_name: str,
    field: str,
    different_value: object,
) -> None:
    events: list[str] = []
    attempted = FakeConnection(
        [
            Step(
                "reserve.stale_cleanup",
                "WITH candidate_runs AS MATERIALIZED",
            ),
            Step(
                "reserve.run",
                "INSERT INTO workspace.analysis_run (",
                error=_unique_violation(constraint_name),
            ),
        ],
        events,
    )
    mismatched = _reservation_row()
    mismatched[field] = different_value
    readback = FakeConnection(
        [
            Step(
                "reserve.readback",
                "analysis_run.original_filename",
                one=mismatched,
            )
        ],
        events,
    )
    _install_connect(monkeypatch, attempted, readback)

    with pytest.raises(ActiveAnalysisRunExists):
        asyncio.run(
            _repository().reserve_uploading(
                analysis_run_id=RUN_ID,
                owner_id=OWNER_ID,
                source=SOURCE,
            )
        )


def test_finalize_locks_then_registers_source_and_queues_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    connection = FakeConnection(
        [
            Step(
                "finalize.lock",
                "FOR UPDATE OF analysis_run, dispatch",
                one=_finalization_row("uploading"),
            ),
            Step(
                "finalize.source",
                "INSERT INTO workspace.source_artifact (",
                one={"artifact_pk": uuid4()},
            ),
            Step("finalize.queue", "SET status = 'queued'", one=_run_row("queued")),
        ],
        events,
    )
    _install_connect(monkeypatch, connection)

    record = asyncio.run(
        _repository().finalize_queued(
            analysis_run_id=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert record.status == "queued"
    assert events == ["finalize.lock", "finalize.source", "finalize.queue", "commit"]
    assert connection.steps == []


def test_finalize_commit_error_accepts_exact_durable_queued_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transaction = FakeConnection(
        [
            Step(
                "finalize.lock",
                "FOR UPDATE OF analysis_run, dispatch",
                one=_finalization_row("uploading"),
            ),
            Step(
                "finalize.source",
                "INSERT INTO workspace.source_artifact (",
                one={"artifact_pk": uuid4()},
            ),
            Step("finalize.queue", "SET status = 'queued'", one=_run_row("queued")),
        ],
        events,
        commit_error=psycopg.OperationalError("commit result lost"),
    )
    readback = FakeConnection(
        [
            Step(
                "finalize.readback",
                "AS source_artifact_matches",
                one=_finalization_row("queued", source_artifact_matches=True),
            )
        ],
        events,
    )
    _install_connect(monkeypatch, transaction, readback)

    record = asyncio.run(
        _repository().finalize_queued(
            analysis_run_id=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert record.status == "queued"
    assert events == [
        "finalize.lock",
        "finalize.source",
        "finalize.queue",
        "commit",
        "finalize.readback",
    ]


def test_finalize_commit_error_with_exact_uploading_readback_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transaction = FakeConnection(
        [
            Step(
                "finalize.lock",
                "FOR UPDATE OF analysis_run, dispatch",
                one=_finalization_row("uploading"),
            ),
            Step(
                "finalize.source",
                "INSERT INTO workspace.source_artifact (",
                one={"artifact_pk": uuid4()},
            ),
            Step("finalize.queue", "SET status = 'queued'", one=_run_row("queued")),
        ],
        events,
        commit_error=psycopg.OperationalError("commit rejected"),
    )
    readback = FakeConnection(
        [
            Step(
                "finalize.readback",
                "AS source_artifact_matches",
                one=_finalization_row("uploading", source_artifact_matches=False),
            )
        ],
        events,
    )
    _install_connect(monkeypatch, transaction, readback)

    with pytest.raises(AnalysisRunFinalizationRejected):
        asyncio.run(
            _repository().finalize_queued(
                analysis_run_id=RUN_ID,
                owner_id=OWNER_ID,
                source=SOURCE,
            )
        )


def test_finalize_commit_error_with_unavailable_readback_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transaction = FakeConnection(
        [
            Step(
                "finalize.lock",
                "FOR UPDATE OF analysis_run, dispatch",
                one=_finalization_row("uploading"),
            ),
            Step(
                "finalize.source",
                "INSERT INTO workspace.source_artifact (",
                one={"artifact_pk": uuid4()},
            ),
            Step("finalize.queue", "SET status = 'queued'", one=_run_row("queued")),
        ],
        events,
        commit_error=psycopg.OperationalError("commit result lost"),
    )
    _install_connect(
        monkeypatch,
        transaction,
        psycopg.OperationalError("readback unavailable"),
    )

    with pytest.raises(AnalysisRunFinalizationUncertain):
        asyncio.run(
            _repository().finalize_queued(
                analysis_run_id=RUN_ID,
                owner_id=OWNER_ID,
                source=SOURCE,
            )
        )


def test_cleanup_pending_transition_is_fenced_to_exact_unclaimed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    connection = FakeConnection(
        [
            Step(
                "cleanup.mark",
                "SET status = 'cleanup_pending'",
                one={
                    "analysis_run_pk": RUN_ID,
                    "source_bucket": SOURCE.bucket,
                    "source_object_key": SOURCE.object_key,
                },
            )
        ],
        events,
    )
    _install_connect(monkeypatch, connection)

    cleanup = asyncio.run(
        _repository().mark_upload_cleanup_pending(
            analysis_run_id=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
            error_code="UPLOAD_FINALIZE_FAILED",
            error_message="Upload finalization failed",
        )
    )

    assert cleanup is not None
    assert cleanup.analysis_run_id == RUN_ID
    assert events == ["cleanup.mark", "commit"]
    query, params = connection.cursor_instance.calls[0]
    assert "dispatch.source_object_key = %s" in query
    assert "lower(dispatch.source_content_sha256) = lower(%s)" in query
    assert "dispatch.processing_run_pk IS NULL" in query
    assert params == (
        "UPLOAD_FINALIZE_FAILED",
        "Upload finalization failed",
        RUN_ID,
        OWNER_ID,
        SOURCE.bucket,
        SOURCE.object_key,
        SOURCE.content_sha256,
    )
