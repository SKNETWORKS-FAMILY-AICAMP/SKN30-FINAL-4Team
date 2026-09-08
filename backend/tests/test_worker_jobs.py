"""Slice 6: worker.jobs 임대·펜싱을 실제 PostgreSQL 동시성으로 확인한다.

목 락이 아니라 커넥션 두 개로 진짜 경쟁을 만든다. ``FOR UPDATE SKIP LOCKED``
와 토큰 펜싱은 DB 안에서만 성립하므로 인메모리로 흉내 내면 아무것도 검증하지
못한다.

팀원 Supabase 스키마는 이 파일 전용 DB 에 올린다. ``sims_test`` 에는 손대지
않는다 — 기존 테스트가 거기에 의존한다.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import pathlib
import threading
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from worker.jobs import Claim, claim_next, complete, fail, heartbeat

_JOBS_DATABASE = "sims_jobs_test"
_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "app/db/migrations/supabase"

# 이미 만료된 임대. 다음 트랜잭션의 now() 를 기다리지 않고 결정적으로 만료시킨다.
_EXPIRED = -1


@pytest.fixture(scope="session")
def jobs_engine() -> Engine:
    """팀원 스키마 + 큐 마이그레이션을 올린 전용 DB 를 만든다."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL job lease tests")

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{_JOBS_DATABASE}" WITH (FORCE)'
            )
            connection.exec_driver_sql(f'CREATE DATABASE "{_JOBS_DATABASE}"')
    finally:
        admin.dispose()

    engine = create_engine(url.set(database=_JOBS_DATABASE))
    with engine.connect() as connection:
        # 팀원 파일은 `format('... %I', t)` 를 쓴다. psycopg 는 파라미터를 넘기면
        # `%I` 를 플레이스홀더로 읽고 실패하므로 드라이버 커넥션에 원문 그대로
        # 넘긴다. 파일을 고치는 것은 벤더링 규율 위반이다.
        raw = connection.connection.driver_connection
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
    yield engine
    engine.dispose()
    # DB 는 지우지 않는다. 다음 실행에서 어차피 DROP 하고, 실패했을 때 안을 볼 수 있다.


@pytest.fixture
def user_id(jobs_engine: Engine) -> UUID:
    """auth.users FK 를 채울 사용자 하나. 테스트마다 큐를 비운다."""
    with jobs_engine.begin() as connection:
        connection.execute(text("DELETE FROM workspace.analysis_run"))
        return connection.execute(
            text("INSERT INTO auth.users DEFAULT VALUES RETURNING id")
        ).scalar_one()


def _seed(engine: Engine, user_id: UUID, *, submission_sha256: str | None = None) -> UUID:
    with engine.begin() as connection:
        return connection.execute(
            text(
                """
                INSERT INTO workspace.analysis_run (user_id, status, submission_sha256)
                VALUES (:user_id, 'queued', :sha)
                RETURNING analysis_run_pk
                """
            ),
            {"user_id": user_id, "sha": submission_sha256},
        ).scalar_one()


def _claim(engine: Engine, worker: str, *, lease_seconds: int = 300) -> Claim | None:
    with engine.begin() as connection:
        return claim_next(connection, worker_id=worker, lease_seconds=lease_seconds)


def _row(engine: Engine, run_id: UUID):
    with engine.connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT status, claim_token, attempt_count, last_error,"
                    "       claimed_by, lease_expires_at"
                    "  FROM workspace.analysis_run WHERE analysis_run_pk = :run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )


def test_두_워커가_동시에_클레임하면_하나만_잡는다(jobs_engine: Engine, user_id: UUID):
    run_id = _seed(jobs_engine, user_id)
    barrier = threading.Barrier(2)

    def attempt(worker: str) -> Claim | None:
        # 커넥션은 스레드마다 새로 연다. 진짜 두 세션이 같은 행을 노린다.
        with jobs_engine.begin() as connection:
            barrier.wait(timeout=10)
            return claim_next(connection, worker_id=worker, lease_seconds=300)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, "w1"), pool.submit(attempt, "w2")]
        results = [future.result() for future in futures]

    winners = [claim for claim in results if claim is not None]
    assert len(winners) == 1
    assert winners[0].analysis_run_pk == run_id
    assert _row(jobs_engine, run_id)["status"] == "running"


def test_살아있는_임대는_다른_클레이머에게_보이지_않는다(
    jobs_engine: Engine, user_id: UUID
):
    _seed(jobs_engine, user_id)
    assert _claim(jobs_engine, "w1") is not None
    assert _claim(jobs_engine, "w2") is None
    assert _claim(jobs_engine, "w3") is None


def test_만료된_임대는_다시_잡히고_옛_토큰은_썩는다(jobs_engine: Engine, user_id: UUID):
    run_id = _seed(jobs_engine, user_id)
    first = _claim(jobs_engine, "w1", lease_seconds=_EXPIRED)
    assert first is not None

    second = _claim(jobs_engine, "w2", lease_seconds=300)
    assert second is not None
    assert second.analysis_run_pk == run_id
    assert second.claim_token != first.claim_token

    row = _row(jobs_engine, run_id)
    assert row["claim_token"] == second.claim_token
    assert row["claimed_by"] == "w2"

    with jobs_engine.begin() as connection:
        assert (
            heartbeat(connection, run_id=run_id, claim_token=first.claim_token) is False
        )


def test_펜싱된_워커의_complete_는_0행이고_남의_결과를_덮지_않는다(
    jobs_engine: Engine, user_id: UUID
):
    run_id = _seed(jobs_engine, user_id)
    stale = _claim(jobs_engine, "w1", lease_seconds=_EXPIRED)
    fresh = _claim(jobs_engine, "w2", lease_seconds=300)
    assert stale is not None and fresh is not None

    with jobs_engine.begin() as connection:
        assert complete(connection, run_id=run_id, claim_token=fresh.claim_token) is True

    # 되살아난 옛 워커. 예외를 던지지 않고 조용히 0행이어야 한다.
    with jobs_engine.begin() as connection:
        assert (
            complete(connection, run_id=run_id, claim_token=stale.claim_token) is False
        )
        assert (
            fail(
                connection,
                run_id=run_id,
                claim_token=stale.claim_token,
                last_error="stale worker crash",
            )
            is False
        )

    row = _row(jobs_engine, run_id)
    assert row["status"] == "succeeded"
    assert row["last_error"] is None
    assert row["claim_token"] is None


def test_heartbeat_는_토큰이_맞을_때만_임대를_연장한다(jobs_engine: Engine, user_id: UUID):
    run_id = _seed(jobs_engine, user_id)
    claim = _claim(jobs_engine, "w1", lease_seconds=60)
    assert claim is not None

    with jobs_engine.begin() as connection:
        assert (
            heartbeat(
                connection,
                run_id=run_id,
                claim_token=claim.claim_token,
                lease_seconds=600,
            )
            is True
        )
    assert _row(jobs_engine, run_id)["lease_expires_at"] > claim.lease_expires_at

    # 임대를 만료시키고 다른 워커가 뺏어가면 옛 토큰의 heartbeat 는 False 다.
    with jobs_engine.begin() as connection:
        heartbeat(
            connection,
            run_id=run_id,
            claim_token=claim.claim_token,
            lease_seconds=_EXPIRED,
        )
    assert _claim(jobs_engine, "w2") is not None
    with jobs_engine.begin() as connection:
        assert (
            heartbeat(connection, run_id=run_id, claim_token=claim.claim_token) is False
        )


def test_attempt_count_는_클레임마다_늘어난다(jobs_engine: Engine, user_id: UUID):
    run_id = _seed(jobs_engine, user_id)
    for expected in (1, 2, 3):
        claim = _claim(jobs_engine, f"w{expected}", lease_seconds=_EXPIRED)
        assert claim is not None
        assert claim.attempt_count == expected
    assert _row(jobs_engine, run_id)["attempt_count"] == 3


def test_같은_제출을_두_번_해도_run_은_하나다(jobs_engine: Engine, user_id: UUID):
    sha = "a" * 64

    def submit() -> UUID:
        # 100_analysis_run_queue.sql 헤더가 규정한 형태 그대로다.
        with jobs_engine.begin() as connection:
            inserted = connection.execute(
                text(
                    """
                    INSERT INTO workspace.analysis_run (
                        user_id, status, submission_sha256
                    )
                    VALUES (:user_id, 'queued', :sha)
                    ON CONFLICT (user_id, submission_sha256)
                        WHERE submission_sha256 IS NOT NULL
                          AND status IN ('queued', 'running')
                    DO NOTHING
                    RETURNING analysis_run_pk
                    """
                ),
                {"user_id": user_id, "sha": sha},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            return connection.execute(
                text(
                    """
                    SELECT analysis_run_pk
                      FROM workspace.analysis_run
                     WHERE user_id = :user_id
                       AND submission_sha256 = :sha
                       AND status IN ('queued', 'running')
                    """
                ),
                {"user_id": user_id, "sha": sha},
            ).scalar_one()

    assert submit() == submit()

    with jobs_engine.connect() as connection:
        total = connection.execute(
            text(
                "SELECT count(*) FROM workspace.analysis_run"
                " WHERE user_id = :user_id AND submission_sha256 = :sha"
            ),
            {"user_id": user_id, "sha": sha},
        ).scalar_one()
    assert total == 1
