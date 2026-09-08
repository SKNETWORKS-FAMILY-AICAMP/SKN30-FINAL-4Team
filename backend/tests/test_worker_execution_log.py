"""구조화 실행 한 건이 ``ops.*`` 에 남는지 본다 (초안 §9.5).

``analyse_case`` 는 성공하면 ``ProfileSnapshot.diagnostics`` 를 통째로 버린다.
그래서 지금까지는 "보완이 몇 번 돌았고 무엇이 걸렸나" 가 어디에도 남지 않았다.

그 공백은 관측 취향의 문제가 아니다. 재료화 실패를 묶음 단위로 격리해 정상
결과를 보존하려면 "무엇을 덜어냈는지" 를 적을 자리가 필요한데, 그 자리가
없었다. 이 파일은 그 자리가 생겼는지, 그리고 **성공 실행에서도** 남는지를
고정한다.

새 테이블은 만들지 않았다. 팀원 스키마의 ``ops.processing_run`` /
``ops.model_invocation`` 이 이미 이 용도다.
"""

from __future__ import annotations

import os
import pathlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.db.identity_bridge import ensure_identity
from worker.contracts.profile_snapshot import ProfileSnapshot, StageDiagnostic
from worker.execution_log import RUN_TYPE, record_profile_run


_DATABASE = "sims_execution_log_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"


@pytest.fixture(scope="module")
def engine() -> Engine:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for execution log tests")

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)'
            )
            connection.exec_driver_sql(f'CREATE DATABASE "{_DATABASE}"')
    finally:
        admin.dispose()

    value = create_engine(url.set(database=_DATABASE))
    with value.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute(_SCHEMA.read_text(encoding="utf-8"))
        for path in sorted((_MIGRATIONS / "supabase").glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
    yield value
    value.dispose()


def _snapshot(
    *,
    status: str = "OK",
    diagnostics: list[StageDiagnostic] | None = None,
    usage: list[dict] | None = None,
    attempts: int = 1,
) -> ProfileSnapshot:
    return ProfileSnapshot(
        profile_id="case:1",
        status=status,
        profile={"program_name": "테스트"} if status == "OK" else None,
        candidate_pack_id="hwpx:pack-v0.1.2",
        common_ir=None,
        model_id="gemma-12b",
        prompt_version="request-selection-v1",
        profile_contract_version="v0.1.2",
        selection_attempts=attempts,
        usage=usage if usage is not None else [{"repair": False}],
        diagnostics=diagnostics or [],
    )


def _run_id(engine: Engine, login_id: str) -> UUID:
    with engine.begin() as connection:
        user_id = connection.scalar(
            text(
                "INSERT INTO sims.app_user (login_id, email, password_hash)"
                " VALUES (:login_id, :email, 'x') RETURNING id"
            ),
            {"login_id": login_id, "email": f"{login_id}@example.com"},
        )
        user_uuid = ensure_identity(connection, user_id)
        return connection.scalar(
            text(
                "INSERT INTO workspace.analysis_run (user_id, status)"
                " VALUES (:user_id, 'running') RETURNING analysis_run_pk"
            ),
            {"user_id": user_uuid},
        )


def _rows(engine: Engine, processing_run_pk: UUID) -> tuple[dict, list[dict]]:
    with engine.connect() as connection:
        run = dict(
            connection.execute(
                text(
                    "SELECT source_analysis_run_id, run_type, status,"
                    "       pipeline_version, component_name, component_version,"
                    "       started_at, finished_at, run_metadata,"
                    "       error_code, error_message"
                    "  FROM ops.processing_run WHERE processing_run_pk = :pk"
                ),
                {"pk": processing_run_pk},
            )
            .mappings()
            .one()
        )
        calls = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT model_role, model_id, prompt_version, status,"
                    "       input_tokens, output_tokens, latency_ms"
                    "  FROM ops.model_invocation"
                    " WHERE processing_run_pk = :pk ORDER BY created_at"
                ),
                {"pk": processing_run_pk},
            ).mappings()
        ]
    return run, calls


def test_a_successful_run_is_recorded_not_discarded(engine: Engine) -> None:
    """성공 경로가 가장 중요하다. 지금까지 여기서 진단이 사라졌다."""
    run_id = _run_id(engine, "exec-ok")
    started = datetime.now(timezone.utc)

    with engine.begin() as connection:
        pk = record_profile_run(
            connection,
            _snapshot(),
            source_analysis_run_id=run_id,
            started_at=started,
            finished_at=started + timedelta(seconds=27),
        )

    assert pk is not None
    run, calls = _rows(engine, pk)
    assert run["source_analysis_run_id"] == run_id
    assert run["run_type"] == RUN_TYPE
    assert run["status"] == "succeeded"
    assert run["component_version"] == "request-selection-v1"
    assert run["error_code"] is None
    assert (run["finished_at"] - run["started_at"]).total_seconds() == 27
    assert len(calls) == 1
    assert calls[0]["model_role"].endswith(":initial")
    assert calls[0]["model_id"] == "gemma-12b"


def test_repair_attempts_are_distinguishable_from_the_first_call(
    engine: Engine,
) -> None:
    """같은 진단이 보완에서 반복되는지는 이 구분으로만 보인다."""
    run_id = _run_id(engine, "exec-repair")
    started = datetime.now(timezone.utc)
    diagnostics = [
        StageDiagnostic(
            stage="structure_request_profile",
            unit="ValueError",
            reason_code="MATERIALIZATION_FAILED",
            message="paragraph delivery relation members must share its container",
            attempt=1,
        ),
        StageDiagnostic(
            stage="structure_request_profile",
            unit="ValueError",
            reason_code="MATERIALIZATION_FAILED",
            message="paragraph delivery relation members must share its container",
            attempt=2,
        ),
    ]

    with engine.begin() as connection:
        pk = record_profile_run(
            connection,
            _snapshot(
                status="FAILED",
                diagnostics=diagnostics,
                usage=[{"repair": False}, {"repair": True}],
                attempts=2,
            ),
            source_analysis_run_id=run_id,
            started_at=started,
            finished_at=started + timedelta(seconds=62),
        )

    run, calls = _rows(engine, pk)
    assert run["status"] == "failed"
    assert run["error_code"] == "MATERIALIZATION_FAILED"
    assert [call["model_role"].rsplit(":", 1)[-1] for call in calls] == [
        "initial",
        "repair",
    ]
    # 두 시도가 같은 진단을 반복했다는 사실이 남아야 한다.
    recorded = run["run_metadata"]["diagnostics"]
    assert [row["attempt"] for row in recorded] == [1, 2]
    assert recorded[0]["message"] == recorded[1]["message"]
    assert run["run_metadata"]["selection_attempts"] == 2


def test_tokens_stay_null_instead_of_being_invented(engine: Engine) -> None:
    """포트가 사용량을 돌려주지 않는다. 값을 지어내지 않는다."""
    run_id = _run_id(engine, "exec-tokens")
    started = datetime.now(timezone.utc)

    with engine.begin() as connection:
        pk = record_profile_run(
            connection,
            _snapshot(),
            source_analysis_run_id=run_id,
            started_at=started,
            finished_at=started,
        )

    _, calls = _rows(engine, pk)
    assert calls[0]["input_tokens"] is None
    assert calls[0]["output_tokens"] is None
    assert calls[0]["latency_ms"] is None


def test_metadata_carries_no_profile_body(engine: Engine) -> None:
    """집계 가능한 구조만 담는다. 프로필 본문은 담지 않는다."""
    run_id = _run_id(engine, "exec-body")
    started = datetime.now(timezone.utc)

    with engine.begin() as connection:
        pk = record_profile_run(
            connection,
            _snapshot(),
            source_analysis_run_id=run_id,
            started_at=started,
            finished_at=started,
        )

    run, _ = _rows(engine, pk)
    assert set(run["run_metadata"]) == {
        "profile_id",
        "candidate_pack_id",
        "selection_attempts",
        "diagnostics",
    }
    assert "테스트" not in str(run["run_metadata"])


def test_recording_failure_leaves_the_outer_transaction_usable(engine: Engine) -> None:
    """진단을 남기려다 결과를 잃는 것은 거꾸로다.

    파이썬 예외를 삼키는 것만으로는 부족하다. PostgreSQL 은 문장 하나가
    실패하면 트랜잭션 전체를 abort 로 만들어 그 뒤 모든 명령을 거부한다.
    예외만 잡으면 호출자의 다음 쿼리나 커밋이 대신 죽는다 — SAVEPOINT 를
    쓰기 전에 실제로 그렇게 동작하는 것을 확인했다.
    """
    started = datetime.now(timezone.utc)
    # model_id 는 ops.model_invocation 에서 NOT NULL 이다. 두 번째 INSERT 가
    # 실패해 트랜잭션을 오염시킬 수 있는 상황을 만든다.
    broken = _snapshot()
    object.__setattr__(broken, "model_id", None)
    assert broken.model_id is None, "사보타주가 먹지 않으면 이 테스트는 무의미하다"

    with engine.begin() as connection:
        assert (
            record_profile_run(
                connection,
                broken,
                source_analysis_run_id=None,
                started_at=started,
                finished_at=started,
            )
            is None
        )
        # 기록이 실패해도 바깥 트랜잭션은 멀쩡해야 한다.
        assert connection.scalar(text("SELECT 1")) == 1

    # 실패한 기록은 한 행도 남기지 않는다 (SAVEPOINT 로 되감긴다).
    with engine.connect() as connection:
        orphans = connection.scalar(
            text(
                "SELECT count(*) FROM ops.processing_run pr"
                " WHERE NOT EXISTS (SELECT 1 FROM ops.model_invocation mi"
                "                    WHERE mi.processing_run_pk ="
                "                          pr.processing_run_pk)"
            )
        )
    assert orphans == 0


def test_missing_teammate_schema_is_silent(tmp_path) -> None:
    """전환기 DB 에는 ops 스키마가 없다. 오류가 아니라 무동작이다."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")

    url = make_url(database_url)
    plain = "sims_execution_log_plain"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{plain}" WITH (FORCE)')
            connection.exec_driver_sql(f'CREATE DATABASE "{plain}"')
    finally:
        admin.dispose()

    value = create_engine(url.set(database=plain))
    try:
        with value.connect() as connection:
            connection.connection.driver_connection.execute(
                _SCHEMA.read_text(encoding="utf-8")
            )
        started = datetime.now(timezone.utc)
        with value.begin() as connection:
            assert (
                record_profile_run(
                    connection,
                    _snapshot(),
                    source_analysis_run_id=None,
                    started_at=started,
                    finished_at=started,
                )
                is None
            )
    finally:
        value.dispose()
