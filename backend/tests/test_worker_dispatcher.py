"""Slice 6: DB 큐 디스패처와 claim→run→finish 루프를 실제 PostgreSQL 로 본다.

sims 스키마와 팀원 Supabase 스키마가 **한 DB 에 같이** 있어야 한다. 신원
다리는 sims 쪽 bigint 사용자를 팀원 쪽 UUID 로 잇는 것이고, 큐 행은 그 UUID
를 참조하며, 워커는 다시 sims 쪽 검사 건으로 돌아온다. 한쪽만 있는 DB 로는
이 왕복 중 어느 것도 검증되지 않는다.

가장 중요한 것은 펜싱이다. 임대를 뺏긴 워커가 ``result.analysis_case`` 에
행을 남기면 안 된다. 그것을 확인하려면 트랜잭션 경계가 진짜여야 하므로
인메모리 흉내가 아니라 실제 DB 를 쓴다.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.db.identity_bridge import ensure_identity, teammate_schema_installed
from app.schemas.cpl import CplFieldCode
from worker.contracts.cpl_result import CplItem, CplResult
from worker.dispatcher import QueueJobDispatcher, enqueue, run_once, submission_key
from worker.jobs import claim_next
from worker.persistence import AnalysisResults

_DISPATCHER_DATABASE = "sims_dispatcher_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"
# 이미 만료된 임대. 다음 트랜잭션의 now() 를 기다리지 않고 결정적으로 만료시킨다.
_EXPIRED = -1


def _valid_cpl_result() -> CplResult:
    return CplResult(
        items=[
            CplItem(
                field_code=field_code,
                representative_status="needs_confirmation",
                status_reason=None,
            )
            for field_code in CplFieldCode
        ],
        profile_id="test-profile",
        common_ir_document_id=None,
        common_ir_source_sha256=None,
        candidate_pack_id=None,
        pipeline_version="test",
        structured_schema_version="test",
        model_id="test",
        prompt_version="test",
    )


@pytest.fixture(scope="session")
def engine() -> Engine:
    """sims 와 팀원 스키마를 **한 DB 에** 올린 이 파일 전용 DB 를 만든다.

    공용 ``sims_test`` 에는 팀원 스키마를 올리지 않는다. 거기 올리면 보류
    상태인 tests/test_worker_queue.py 6 개가 skip 에서 실패로 바뀐다 —
    그 파일 머리말이 예고한 그대로이고, 그것은 이 작업과 별개의 일이다.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for queue dispatcher tests")

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{_DISPATCHER_DATABASE}" WITH (FORCE)'
            )
            connection.exec_driver_sql(f'CREATE DATABASE "{_DISPATCHER_DATABASE}"')
    finally:
        admin.dispose()

    engine = create_engine(url.set(database=_DISPATCHER_DATABASE))
    with engine.connect() as connection:
        # 팀원 파일은 `format('... %I', t)` 를 쓴다. psycopg 는 파라미터를 넘기면
        # `%I` 를 플레이스홀더로 읽고 실패하므로 드라이버 커넥션에 원문 그대로
        # 넘긴다. 파일을 고치는 것은 벤더링 규율 위반이다.
        raw = connection.connection.driver_connection
        raw.execute(_SCHEMA.read_text(encoding="utf-8"))
        for path in sorted((_MIGRATIONS / "supabase").glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
    with engine.connect() as connection:
        assert teammate_schema_installed(connection)
    yield engine
    engine.dispose()
    # DB 는 지우지 않는다. 다음 실행에서 어차피 DROP 하고, 실패했을 때 안을 볼 수 있다.


@pytest.fixture
def case(engine: Engine):
    """사용자 하나와 검사 건 하나. 끝나면 자기가 만든 행만 지운다."""
    with engine.begin() as connection:
        user_id = connection.scalar(
            text(
                """
                INSERT INTO sims.app_user (login_id, email, password_hash, display_name)
                VALUES (:login_id, :email, 'x', '큐 사용자')
                RETURNING id
                """
            ),
            {
                "login_id": f"queue-{os.urandom(6).hex()}",
                "email": f"queue-{os.urandom(6).hex()}@example.test",
            },
        )
        case_id = connection.scalar(
            text(
                "INSERT INTO sims.inspection_case (owner_user_id, status)"
                " VALUES (:user_id, 'PARSING') RETURNING id"
            ),
            {"user_id": user_id},
        )
    yield case_id, user_id
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM result.analysis_case WHERE user_id ="
                " (SELECT external_uuid FROM sims.app_user WHERE id = :user_id)"
            ),
            {"user_id": user_id},
        )
        connection.execute(
            text("DELETE FROM sims.inspection_case WHERE id = :case_id"),
            {"case_id": case_id},
        )
        connection.execute(
            text(
                "DELETE FROM workspace.analysis_run WHERE user_id ="
                " (SELECT external_uuid FROM sims.app_user WHERE id = :user_id)"
            ),
            {"user_id": user_id},
        )
        connection.execute(
            text(
                "DELETE FROM app.user_profile WHERE user_id ="
                " (SELECT external_uuid FROM sims.app_user WHERE id = :user_id)"
            ),
            {"user_id": user_id},
        )
        connection.execute(
            text("DELETE FROM sims.app_user WHERE id = :user_id"),
            {"user_id": user_id},
        )


def _runs(engine: Engine, case_id: int) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT analysis_run_pk, status, last_error"
                    "  FROM workspace.analysis_run"
                    " WHERE submission_sha256 = :sha"
                    " ORDER BY created_at"
                ),
                {"sha": submission_key(case_id)},
            ).mappings()
        ]


def _results(engine: Engine, run_id: UUID) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT user_id, case_status, analysis_completed_at"
                    "  FROM result.analysis_case"
                    " WHERE source_analysis_run_id = :run_id"
                ),
                {"run_id": run_id},
            ).mappings()
        ]


def _complete_case(engine: Engine, case_id: int) -> None:
    """분석이 성공한 것처럼 검사 건을 COMPLETED 로 만든다."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sims.inspection_case"
                "   SET status = 'COMPLETED', completed_at = now(),"
                "       result_frozen_at = now()"
                " WHERE id = :case_id"
            ),
            {"case_id": case_id},
        )


def test_제출은_run_하나를_만들고_두_번_넣어도_같은_run_이다(engine: Engine, case):
    case_id, user_id = case

    first = enqueue(engine, case_id)
    second = enqueue(engine, case_id)

    assert first == second
    rows = _runs(engine, case_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "queued"

    with engine.connect() as connection:
        linked = connection.scalar(
            text("SELECT analysis_run_id FROM sims.inspection_case WHERE id = :case_id"),
            {"case_id": case_id},
        )
        owner_uuid = connection.scalar(
            text("SELECT external_uuid FROM sims.app_user WHERE id = :user_id"),
            {"user_id": user_id},
        )
        run_user = connection.scalar(
            text(
                "SELECT user_id FROM workspace.analysis_run"
                " WHERE analysis_run_pk = :run_id"
            ),
            {"run_id": first},
        )
    assert linked == first
    assert run_user == owner_uuid


def test_집은_작업은_연결된_케이스를_돌리고_결과를_쓴다(engine: Engine, case):
    case_id, user_id = case
    run_id = enqueue(engine, case_id)
    seen: list[int] = []

    async def analyse(claimed_case_id: int) -> None:
        seen.append(claimed_case_id)
        _complete_case(engine, claimed_case_id)

    assert asyncio.run(run_once(engine, analyse, worker_id="w1")) == run_id
    assert seen == [case_id]

    rows = _runs(engine, case_id)
    assert [row["status"] for row in rows] == ["succeeded"]

    with engine.connect() as connection:
        owner_uuid = connection.scalar(
            text("SELECT external_uuid FROM sims.app_user WHERE id = :user_id"),
            {"user_id": user_id},
        )
    results = _results(engine, run_id)
    assert len(results) == 1
    assert results[0]["user_id"] == owner_uuid
    assert results[0]["case_status"] == "ready"
    assert results[0]["analysis_completed_at"] is not None


def test_임대를_뺏긴_워커는_결과를_한_행도_남기지_못한다(engine: Engine, case):
    case_id, _ = case
    run_id = enqueue(engine, case_id)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_analysis(claimed_case_id: int) -> None:
        started.set()
        await release.wait()
        _complete_case(engine, claimed_case_id)

    async def scenario() -> UUID | None:
        # A 가 집는다. 임대는 이미 만료된 상태로 준다.
        worker_a = asyncio.create_task(
            run_once(engine, slow_analysis, worker_id="A", lease_seconds=_EXPIRED)
        )
        await started.wait()

        # B 가 같은 행을 다시 빌린다. 이 순간 A 의 토큰은 썩는다.
        with engine.begin() as connection:
            stolen = claim_next(connection, worker_id="B", lease_seconds=300)
        assert stolen is not None and stolen.analysis_run_pk == run_id

        # 이제 A 가 뒤늦게 끝난다.
        release.set()
        return await worker_a

    assert asyncio.run(scenario()) == run_id

    # A 의 complete() 는 0행이었어야 하고 — 행은 여전히 B 의 running 이다 —
    assert [row["status"] for row in _runs(engine, case_id)] == ["running"]
    # A 가 쓴 결과 행도 남아 있으면 안 된다. 두 조건은 한 트랜잭션이었다.
    assert _results(engine, run_id) == []


def test_분석이_터지면_fail_로_사유를_남기고_결과는_없다(engine: Engine, case):
    case_id, _ = case
    run_id = enqueue(engine, case_id)

    async def exploding(_case_id: int) -> None:
        raise RuntimeError("파서가 죽었다")

    assert asyncio.run(run_once(engine, exploding, worker_id="w1")) == run_id

    rows = _runs(engine, case_id)
    assert [row["status"] for row in rows] == ["failed"]
    assert "파서가 죽었다" in rows[0]["last_error"]
    assert _results(engine, run_id) == []


def test_디스패처는_넣은_직후_같은_프로세스에서_한_건을_끝낸다(engine: Engine, case):
    """운영 배선 그대로다 — main.py 가 만드는 모양으로 제출 한 번을 끝까지 본다."""
    case_id, _ = case

    async def analyse(claimed_case_id: int) -> None:
        _complete_case(engine, claimed_case_id)

    async def submit() -> str:
        dispatcher = QueueJobDispatcher(engine, analyse, worker_id="main")
        job_id = await dispatcher.enqueue_analysis(case_id)
        await dispatcher.shutdown()
        return job_id

    job_id = asyncio.run(submit())

    rows = _runs(engine, case_id)
    assert [str(row["analysis_run_pk"]) for row in rows] == [job_id]
    assert rows[0]["status"] == "succeeded"
    assert _results(engine, UUID(job_id))[0]["case_status"] == "ready"


def test_디스패처는_큐_경로에서_레거시를_부르지_않고_조립_결과를_ready로_쓴다(
    engine: Engine, case
):
    case_id, _ = case
    calls: list[str] = []

    async def worker(_case_id: int) -> AnalysisResults:
        calls.append("worker")
        return AnalysisResults(
            cpl=_valid_cpl_result(),
            program_name="프로필 사업명",
            original_filename="요청서.hwpx",
        )

    async def legacy(_case_id: int) -> None:
        calls.append("legacy")

    async def submit() -> str:
        dispatcher = QueueJobDispatcher(
            engine,
            worker,
            legacy_run_analysis=legacy,
            worker_id="main-worker",
        )
        job_id = await dispatcher.enqueue_analysis(case_id)
        await dispatcher.shutdown()
        return job_id

    job_id = asyncio.run(submit())

    assert calls == ["worker"]
    assert [row["status"] for row in _runs(engine, case_id)] == ["succeeded"]
    result_rows = _results(engine, UUID(job_id))
    assert len(result_rows) == 1
    assert result_rows[0]["case_status"] == "ready"
    with engine.connect() as connection:
        snapshot = connection.execute(
            text(
                "SELECT program_name, original_filename, report_status "
                "FROM result.analysis_case "
                "WHERE source_analysis_run_id = :run_id"
            ),
            {"run_id": job_id},
        ).mappings().one()
        assert dict(snapshot) == {
            "program_name": "프로필 사업명",
            "original_filename": "요청서.hwpx",
            "report_status": "generating",
        }
        analysis_case_pk = connection.scalar(
            text(
                "SELECT analysis_case_pk FROM result.analysis_case "
                "WHERE source_analysis_run_id = :run_id"
            ),
            {"run_id": job_id},
        )
        assert connection.scalar(
            text(
                "SELECT count(*) FROM result.axis_result "
                "WHERE analysis_case_pk = :analysis_case_pk "
                "AND axis_type = 'CPL'"
            ),
            {"analysis_case_pk": analysis_case_pk},
        ) == 13
        session = connection.execute(
            text(
                "SELECT status, expires_at > now() + interval '29 minutes' AS fresh "
                "FROM result.analysis_session "
                "WHERE analysis_case_pk = :analysis_case_pk"
            ),
            {"analysis_case_pk": analysis_case_pk},
        ).mappings().one()
        assert session["status"] == "active"
        assert session["fresh"] is True
    # AnalysisResults 경로는 레거시 sims 상태를 결과 성공의 전제조건으로
    # 사용하지 않는다. fixture가 만든 PARSING 상태 그대로 남아 있어야 한다.
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT status FROM sims.inspection_case WHERE id = :case_id"),
            {"case_id": case_id},
        ) == "PARSING"


def test_디스패처는_엄격한_큐_경로의_빈_조립_결과를_실패로_처리한다(
    engine: Engine, case
):
    case_id, _ = case

    async def worker(_case_id: int) -> AnalysisResults:
        return AnalysisResults()

    async def legacy(_case_id: int) -> None:
        raise AssertionError("legacy callback must not run")

    async def submit() -> str:
        dispatcher = QueueJobDispatcher(
            engine,
            worker,
            legacy_run_analysis=legacy,
            worker_id="main-worker",
        )
        job_id = await dispatcher.enqueue_analysis(case_id)
        await dispatcher.shutdown()
        return job_id

    job_id = asyncio.run(submit())

    assert [row["status"] for row in _runs(engine, case_id)] == ["failed"]
    assert _results(engine, UUID(job_id)) == []
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM result.analysis_session s "
                "JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk "
                "WHERE c.source_analysis_run_id = :run_id"
            ),
            {"run_id": job_id},
        ) == 0


def test_디스패처_조립_콜백_실패는_큐를_failed로_남기고_결과를_만들지_않는다(
    engine: Engine, case
):
    case_id, _ = case
    calls: list[str] = []

    async def worker(_case_id: int) -> AnalysisResults:
        calls.append("worker")
        raise RuntimeError("profile assembly failed")

    async def legacy(_case_id: int) -> None:
        calls.append("legacy")

    async def submit() -> str:
        dispatcher = QueueJobDispatcher(
            engine,
            worker,
            legacy_run_analysis=legacy,
            worker_id="main-worker",
        )
        job_id = await dispatcher.enqueue_analysis(case_id)
        await dispatcher.shutdown()
        return job_id

    job_id = asyncio.run(submit())

    assert calls == ["worker"]
    rows = _runs(engine, case_id)
    assert [row["status"] for row in rows] == ["failed"]
    assert "profile assembly failed" in rows[0]["last_error"]
    assert _results(engine, UUID(job_id)) == []


def test_신원_다리는_팀원_스키마가_없는_DB_에서_아무_일도_하지_않는다():
    """전환기에는 두 모양이 공존한다. sims 만 있는 DB 에서 오류가 아니어야 한다.

    공용 ``sims_test`` 가 바로 그 모양이다 — sims 만 있고 팀원 스키마는 없다.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for identity bridge tests")

    sims_only = create_engine(database_url)
    try:
        with sims_only.begin() as connection:
            if teammate_schema_installed(connection):
                pytest.skip("shared test database is no longer sims-only")
            user_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.app_user (login_id, email, password_hash)
                    VALUES (:login_id, :email, 'x')
                    RETURNING id
                    """
                ),
                {
                    "login_id": f"bridge-{os.urandom(6).hex()}",
                    "email": f"bridge-{os.urandom(6).hex()}@example.test",
                },
            )
            # 팀원 테이블이 없어도 예외 없이 external_uuid 만 돌려준다.
            external_uuid = ensure_identity(connection, user_id)
            assert isinstance(external_uuid, UUID)
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )
    finally:
        sims_only.dispose()


def test_디스패처는_팀원_스키마가_없으면_레거시_fallback만_한번_호출한다():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for legacy fallback test")

    sims_only = create_engine(database_url)
    try:
        with sims_only.connect() as connection:
            if teammate_schema_installed(connection):
                pytest.skip("fallback is only observable on a sims-only database")

        case_id = 77
        calls: list[tuple[str, int]] = []

        async def worker(_case_id: int) -> None:
            calls.append(("worker", _case_id))

        async def legacy(_case_id: int) -> None:
            calls.append(("legacy", _case_id))

        async def submit() -> str:
            dispatcher = QueueJobDispatcher(
                sims_only,
                worker,
                legacy_run_analysis=legacy,
                worker_id="legacy-fallback",
            )
            job_id = await dispatcher.enqueue_analysis(case_id)
            await dispatcher.shutdown()
            return job_id

        job_id = asyncio.run(submit())

        assert job_id
        assert calls == [("legacy", case_id)]
    finally:
        sims_only.dispose()
