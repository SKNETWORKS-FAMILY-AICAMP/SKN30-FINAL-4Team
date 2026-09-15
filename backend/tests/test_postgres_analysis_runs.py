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

from app.infrastructure import postgres_analysis_runs
from app.infrastructure.postgres_analysis_runs import PostgresAnalysisRunRepository
from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    ActiveResultSessionExists,
    AnalysisRunFinalizationRejected,
    AnalysisRunFinalizationExpired,
    AnalysisRunFinalizationUncertain,
    AnalysisQueueCapacityExceeded,
    IdempotencyKeyConflict,
    SourceObject,
    UploadCleanupObject,
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


def test_reserve_calls_atomic_v2_rpc_and_returns_its_cleanup_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stale_id = str(uuid4())
    connection = FakeConnection(
        [
            Step(
                "reserve.run",
                "reserve_analysis_upload_v2",
                one={
                    **_run_row("uploading"),
                    "replayed": False,
                    "cleanup_objects": [
                        {
                        "analysis_run_pk": stale_id,
                        "source_bucket": "request-temp",
                        "source_object_key": f"{stale_id}/source/stale.hwpx",
                        }
                    ],
                },
            ),
        ],
        events,
    )
    factory = _install_connect(monkeypatch, connection)

    reservation = asyncio.run(
        _repository().reserve_uploading(
            idempotency_key=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert events == ["reserve.run", "commit"]
    assert reservation.record.status == "uploading"
    assert reservation.cleanup_objects[0].analysis_run_id == stale_id
    _reserve_query, reserve_params = connection.cursor_instance.calls[0]
    assert reserve_params == (
        OWNER_ID,
        RUN_ID,
        SOURCE.filename,
        SOURCE.declared_mime_type,
        SOURCE.size_bytes,
        SOURCE.bucket,
        SOURCE.object_key,
        SOURCE.content_sha256,
        900,
    )
    assert factory.calls == [
        (
            DATABASE_URL,
            {"connect_timeout": 7, "row_factory": postgres_analysis_runs.dict_row},
        )
    ]
    assert connection.steps == []


@pytest.mark.parametrize("status", ["uploading", "queued", "running", "succeeded"])
def test_reserve_honours_v2_rpc_replay_flag(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    events: list[str] = []
    connection = FakeConnection(
        [
            Step(
                "reserve.run",
                "reserve_analysis_upload_v2",
                one={
                    **_run_row(status),
                    "replayed": True,
                    "cleanup_objects": [],
                },
            ),
        ],
        events,
    )
    _install_connect(monkeypatch, connection)

    reservation = asyncio.run(
        _repository().reserve_uploading(
            idempotency_key=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert reservation.replayed is True
    assert reservation.record.analysis_run_id == RUN_ID
    assert reservation.record.status == status
    assert events == ["reserve.run", "commit"]


def test_reserve_sets_the_configured_global_cap_in_its_rpc_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    connection = FakeConnection(
        [
            Step("reserve.cap", "set_config", one={}),
            Step(
                "reserve.run",
                "reserve_analysis_upload_v2",
                one={**_run_row("uploading"), "replayed": False, "cleanup_objects": []},
            ),
        ],
        events,
    )
    _install_connect(monkeypatch, connection)

    reservation = asyncio.run(
        PostgresAnalysisRunRepository(
            DATABASE_URL, connect_timeout_seconds=7, global_queue_max=3
        ).reserve_uploading(
            idempotency_key=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert reservation.record.status == "uploading"
    assert events == ["reserve.cap", "reserve.run", "commit"]
    assert connection.cursor_instance.calls[0][1] == ("3",)


def test_reserve_unknown_database_outcome_reads_back_exact_owner_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    attempted = FakeConnection(
        [
            Step(
                "reserve.run",
                "reserve_analysis_upload_v2",
                error=psycopg.OperationalError("response lost"),
            ),
        ],
        events,
    )
    readback = FakeConnection(
        [
            Step(
                "reserve.readback",
                "analysis_run.original_filename",
                one=_reservation_row("uploading"),
            )
        ],
        events,
    )
    _install_connect(monkeypatch, attempted, readback)

    reservation = asyncio.run(
        _repository().reserve_uploading(
            idempotency_key=RUN_ID,
            owner_id=OWNER_ID,
            source=SOURCE,
        )
    )

    assert reservation.replayed is True
    assert reservation.record.status == "uploading"
    _query, params = readback.cursor_instance.calls[0]
    assert params == (OWNER_ID, RUN_ID)


@pytest.mark.parametrize(
    ("error_token", "expected_exception"),
    [
        ("IDEMPOTENCY_KEY_CONFLICT", IdempotencyKeyConflict),
        ("ANALYSIS_RUN_ACTIVE", ActiveAnalysisRunExists),
        ("ACTIVE_RESULT_SESSION", ActiveResultSessionExists),
        ("GLOBAL_QUEUE_CAPACITY_EXCEEDED", AnalysisQueueCapacityExceeded),
    ],
)
def test_reserve_maps_v2_domain_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_token: str,
    expected_exception: type[Exception],
) -> None:
    events: list[str] = []
    attempted = FakeConnection(
        [
            Step(
                "reserve.run",
                "reserve_analysis_upload_v2",
                error=psycopg.errors.UniqueViolation(error_token),
            ),
        ],
        events,
    )
    _install_connect(monkeypatch, attempted)

    with pytest.raises(expected_exception):
        asyncio.run(
            _repository().reserve_uploading(
                idempotency_key=RUN_ID,
                owner_id=OWNER_ID,
                source=SOURCE,
            )
        )


def test_finalize_uses_one_db_owned_atomic_rpc_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    connection = FakeConnection(
        [
            Step(
                "finalize.rpc",
                "finalize_analysis_upload_v2",
                one={**_run_row("queued"), "outcome": "queued", "cleanup_objects": []},
            )
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
    assert events == ["finalize.rpc", "commit"]
    query, params = connection.cursor_instance.calls[0]
    assert "workspace.analysis_run" not in query
    assert params == (
        RUN_ID, OWNER_ID, SOURCE.filename, SOURCE.declared_mime_type,
        SOURCE.size_bytes, SOURCE.bucket, SOURCE.object_key,
        SOURCE.content_sha256, SOURCE.mime_type, SOURCE.size_bytes, 3600,
    )
    assert connection.steps == []


def test_finalize_commit_error_accepts_exact_durable_queued_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transaction = FakeConnection(
        [
            Step(
                "finalize.rpc",
                "finalize_analysis_upload_v2",
                one={**_run_row("queued"), "outcome": "queued", "cleanup_objects": []},
            )
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
        "finalize.rpc",
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
                "finalize.rpc",
                "finalize_analysis_upload_v2",
                one={**_run_row("queued"), "outcome": "queued", "cleanup_objects": []},
            )
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
                "finalize.rpc",
                "finalize_analysis_upload_v2",
                one={**_run_row("queued"), "outcome": "queued", "cleanup_objects": []},
            )
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


def test_finalize_expired_capacity_commits_cleanup_fence_and_returns_typed_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    connection = FakeConnection(
        [
            Step("finalize.cap", "set_config", one={}),
            Step(
                "finalize.rpc",
                "finalize_analysis_upload_v2",
                one={
                    **_run_row("cleanup_pending"),
                    "error_code": "UPLOAD_RESERVATION_EXPIRED",
                    "outcome": "expired_capacity",
                    "cleanup_objects": [{
                        "analysis_run_pk": RUN_ID,
                        "source_bucket": SOURCE.bucket,
                        "source_object_key": SOURCE.object_key,
                    }],
                },
            )
        ],
        events,
    )
    _install_connect(monkeypatch, connection)

    with pytest.raises(AnalysisRunFinalizationExpired) as raised:
        asyncio.run(
            PostgresAnalysisRunRepository(
                DATABASE_URL, global_queue_max=1
            ).finalize_queued(
                analysis_run_id=RUN_ID, owner_id=OWNER_ID, source=SOURCE
            )
        )

    assert raised.value.cleanup_object == UploadCleanupObject(
        analysis_run_id=RUN_ID, bucket=SOURCE.bucket, object_key=SOURCE.object_key
    )
    assert events == ["finalize.cap", "finalize.rpc", "commit"]


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
