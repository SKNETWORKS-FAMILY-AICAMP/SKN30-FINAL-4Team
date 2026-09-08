# =============================================================================
# 보류 — worker/queue.py 와 함께 중단된 작업이다.
#
# 팀원 스키마를 실제로 설치하면 6 개 전부 실패한다(analysis_run.run_metadata
# 없음). 지금은 스키마 미설치로 skip 되어 실패가 드러나지 않는다.
# 동작하는 큐의 테스트는 tests/test_worker_jobs.py 다.
# =============================================================================

"""PostgreSQL integration tests for the Slice 6a worker lease."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from worker.queue import (
    AnalysisRunQueue,
    LEASE_EXPIRED_ERROR_CODE,
    MAX_ATTEMPTS_ERROR_CODE,
)


_REQUIRED_TABLES = (
    "workspace.analysis_run",
    "ops.processing_run",
)


@pytest.fixture(scope="session")
def queue_engine() -> Engine:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL queue tests")
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        missing = [
            table
            for table in _REQUIRED_TABLES
            if connection.scalar(text("SELECT to_regclass(:table)"), {"table": table})
            is None
        ]
        if missing:
            engine.dispose()
            pytest.skip(f"team Supabase tables are not installed: {', '.join(missing)}")
        if not connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        ):
            engine.dispose()
            pytest.skip("TEST_DATABASE_URL must point to PostgreSQL with pgvector")
    return engine


@pytest.fixture
def seeded_runs(queue_engine: Engine):
    with queue_engine.connect() as connection:
        user_id = connection.scalar(text("SELECT id FROM auth.users LIMIT 1"))
    if user_id is None:
        pytest.skip("auth.users must contain a test user")
    created: list[UUID] = []

    def seed(*, status: str = "queued", created_at: datetime | None = None) -> UUID:
        analysis_run_pk = uuid4()
        created.append(analysis_run_pk)
        timestamp = created_at or datetime.now(timezone.utc)
        with queue_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO workspace.analysis_run (
                        analysis_run_pk, user_id, status, created_at, updated_at,
                        run_metadata
                    )
                    VALUES (
                        :analysis_run_pk, :user_id, :status, :created_at,
                        :updated_at, CAST(:run_metadata AS jsonb)
                    )
                    """
                ),
                {
                    "analysis_run_pk": analysis_run_pk,
                    "user_id": user_id,
                    "status": status,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "run_metadata": "{}",
                },
            )
        return analysis_run_pk

    yield seed
    with queue_engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM ops.processing_run "
                "WHERE source_analysis_run_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [str(value) for value in created]},
        )
        connection.execute(
            text(
                "DELETE FROM workspace.analysis_run "
                "WHERE analysis_run_pk = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [str(value) for value in created]},
        )


def _state(engine: Engine, analysis_run_pk: UUID) -> tuple[str, dict]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, run_metadata FROM workspace.analysis_run "
                "WHERE analysis_run_pk = :analysis_run_pk"
            ),
            {"analysis_run_pk": analysis_run_pk},
        ).mappings().one()
    return row["status"], row["run_metadata"] or {}


def _processing_state(engine: Engine, processing_run_pk: UUID) -> tuple[str, str | None]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, error_code FROM ops.processing_run "
                "WHERE processing_run_pk = :processing_run_pk"
            ),
            {"processing_run_pk": processing_run_pk},
        ).one()
    return row[0], row[1]


def test_two_workers_claim_distinct_rows(queue_engine: Engine, seeded_runs) -> None:
    first = seeded_runs(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = seeded_runs(created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    queue_a = AnalysisRunQueue(queue_engine)
    queue_b = AnalysisRunQueue(queue_engine)

    with ThreadPoolExecutor(max_workers=2) as workers:
        claimed = list(workers.map(lambda queue: queue.claim_next(), (queue_a, queue_b)))

    assert {job.analysis_run_pk for job in claimed if job is not None} == {first, second}
    assert len({job.processing_run_pk for job in claimed if job is not None}) == 2


def test_heartbeat_requires_the_live_fencing_token(
    queue_engine: Engine, seeded_runs
) -> None:
    analysis_run_pk = seeded_runs()
    queue = AnalysisRunQueue(queue_engine)
    claim = queue.claim_next()
    assert claim is not None

    assert queue.heartbeat(claim)
    status, metadata = _state(queue_engine, analysis_run_pk)
    assert status == "running"
    assert metadata["processing_run_pk"] == str(claim.processing_run_pk)
    assert "heartbeat_at" in metadata

    assert not queue.heartbeat(analysis_run_pk, uuid4())


def test_expired_lease_is_requeued_for_the_next_attempt(
    queue_engine: Engine, seeded_runs
) -> None:
    analysis_run_pk = seeded_runs()
    queue = AnalysisRunQueue(queue_engine, lease_timeout=timedelta(seconds=1))
    first = queue.claim_next()
    assert first is not None

    recovered = queue.recover_stale_leases(
        now=first.claimed_at + timedelta(seconds=2)
    )
    assert [(item.analysis_run_pk, item.status, item.error_code) for item in recovered] == [
        (analysis_run_pk, "queued", LEASE_EXPIRED_ERROR_CODE)
    ]
    status, metadata = _state(queue_engine, analysis_run_pk)
    assert status == "queued"
    assert metadata["attempt_no"] == 1
    assert "processing_run_pk" not in metadata
    op_status, error_code = _processing_state(queue_engine, first.processing_run_pk)
    assert (op_status, error_code) == ("failed", LEASE_EXPIRED_ERROR_CODE)

    second = queue.claim_next()
    assert second is not None
    assert second.analysis_run_pk == analysis_run_pk
    assert second.attempt_no == 2


def test_expired_lease_fails_after_max_attempts(
    queue_engine: Engine, seeded_runs
) -> None:
    analysis_run_pk = seeded_runs()
    queue = AnalysisRunQueue(
        queue_engine,
        lease_timeout=timedelta(seconds=1),
        max_attempts=1,
    )
    claim = queue.claim_next()
    assert claim is not None

    recovered = queue.recover_stale_leases(
        now=claim.claimed_at + timedelta(seconds=2)
    )
    assert [(item.analysis_run_pk, item.status, item.error_code) for item in recovered] == [
        (analysis_run_pk, "failed", MAX_ATTEMPTS_ERROR_CODE)
    ]
    assert _state(queue_engine, analysis_run_pk)[0] == "failed"
    assert _processing_state(queue_engine, claim.processing_run_pk) == (
        "failed",
        MAX_ATTEMPTS_ERROR_CODE,
    )


def test_stale_worker_cannot_complete_after_reclaim(
    queue_engine: Engine, seeded_runs
) -> None:
    analysis_run_pk = seeded_runs()
    queue = AnalysisRunQueue(
        queue_engine,
        lease_timeout=timedelta(seconds=1),
        max_attempts=2,
    )
    first = queue.claim_next()
    assert first is not None
    queue.recover_stale_leases(now=first.claimed_at + timedelta(seconds=2))
    second = queue.claim_next()
    assert second is not None
    assert second.attempt_no == 2

    assert not queue.complete(first)
    assert _state(queue_engine, analysis_run_pk)[0] == "running"
    assert queue.complete(second)
    assert _state(queue_engine, analysis_run_pk)[0] == "succeeded"
    assert _processing_state(queue_engine, second.processing_run_pk) == (
        "succeeded",
        None,
    )


def test_live_worker_completion_advances_both_rows(
    queue_engine: Engine, seeded_runs
) -> None:
    analysis_run_pk = seeded_runs()
    queue = AnalysisRunQueue(queue_engine)
    claim = queue.claim_next()
    assert claim is not None

    assert queue.complete(claim)
    status, metadata = _state(queue_engine, analysis_run_pk)
    assert status == "succeeded"
    assert "processing_run_pk" not in metadata
    assert _processing_state(queue_engine, claim.processing_run_pk) == (
        "succeeded",
        None,
    )
