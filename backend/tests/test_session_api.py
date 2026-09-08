"""세션·이력 API 표면 네 개를 실제 PostgreSQL 로 고정한다 (SES-01/02, HISTORY-01).

프론트가 `auth.uid()` 로만 자기 것을 식별하므로, 이 파일이 확인하는 것의 절반은
**남의 것이 새지 않는가**이다. PostgREST 가 세우는 클레임을 직접 세워
그 경로를 그대로 흉내낸다.

나머지 절반은 프론트 호출 방식이 요구하는 모양이다.

  - `v_active_analysis_session` 은 `.maybeSingle()` 로 읽으므로 두 행이 오면 안 된다
  - `v_my_analysis_history` 는 `.order().range()` 를 프론트가 걸므로 뷰가 먼저
    자르면 안 된다
  - `touch` 는 남은 시간에 더하는 것이 아니라 클릭 시점부터 30분이다
  - `close` 는 두 번 눌러도 같은 응답이다
"""

from __future__ import annotations

import os
import pathlib
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.db.identity_bridge import ensure_identity


_DATABASE = "sims_session_api_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"


@pytest.fixture(scope="module")
def engine() -> Engine:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for session api tests")

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


@pytest.fixture(scope="module")
def owners(engine: Engine) -> dict[str, UUID]:
    """두 사용자. 하나는 남의 것을 못 보는지 확인하는 용도다."""
    uuids: dict[str, UUID] = {}
    with engine.begin() as connection:
        for login_id in ("owner", "stranger"):
            user_id = connection.scalar(
                text(
                    "INSERT INTO sims.app_user (login_id, email, password_hash)"
                    " VALUES (:login_id, :email, 'x') RETURNING id"
                ),
                {"login_id": login_id, "email": f"{login_id}@example.com"},
            )
            uuids[login_id] = ensure_identity(connection, user_id)
    return uuids


def _case(
    engine: Engine,
    owner: UUID,
    *,
    program_name: str,
    case_status: str = "ready",
    report_status: str = "ready",
    completed_offset_minutes: int = 0,
    retention_offset_hours: int | None = None,
    session_status: str | None = "active",
    session_expires_minutes: int = 30,
    last_activity_minutes: int = 0,
) -> UUID:
    with engine.begin() as connection:
        run_id = connection.scalar(
            text(
                "INSERT INTO workspace.analysis_run (user_id, status)"
                " VALUES (:user_id, 'succeeded') RETURNING analysis_run_pk"
            ),
            {"user_id": owner},
        )
        case_pk = connection.scalar(
            text(
                "INSERT INTO result.analysis_case ("
                "  source_analysis_run_id, user_id, case_status,"
                "  analysis_completed_at, program_name, original_filename,"
                "  report_status, retention_expires_at)"
                " VALUES (:run_id, :user_id, :case_status,"
                "         now() + make_interval(mins => :completed),"
                "         :program_name, '요청서.hwpx', :report_status,"
                "         CASE WHEN CAST(:retention AS int) IS NULL THEN NULL"
                "              ELSE now() + make_interval("
                "                       hours => CAST(:retention AS int)) END)"
                " RETURNING analysis_case_pk"
            ),
            {
                "run_id": run_id,
                "user_id": owner,
                "case_status": case_status,
                "completed": completed_offset_minutes,
                "program_name": program_name,
                "report_status": report_status,
                "retention": retention_offset_hours,
            },
        )
        if session_status is not None:
            connection.execute(
                text(
                    "INSERT INTO result.analysis_session ("
                    "  analysis_case_pk, status, expires_at, last_activity_at)"
                    " VALUES (:case_pk, :status,"
                    "         now() + make_interval(mins => :expires),"
                    "         now() + make_interval(mins => :activity))"
                ),
                {
                    "case_pk": case_pk,
                    "status": session_status,
                    "expires": session_expires_minutes,
                    "activity": last_activity_minutes,
                },
            )
    return case_pk


def _as(engine: Engine, owner: UUID, sql: str, params: dict | None = None):
    """PostgREST 가 세우는 클레임을 세우고 그 사용자로 읽는다."""
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('request.jwt.claim.sub', :sub, true)"),
            {"sub": str(owner)},
        )
        return connection.execute(text(sql), params or {}).mappings().all()


# ------------------------------------------------------------------ SES-01


def test_active_session_view_returns_one_row_for_maybe_single(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    """프론트가 maybeSingle 로 읽는다. 두 건이 열려 있어도 한 행이어야 한다."""
    _case(engine, owners["owner"], program_name="먼저 연 분석", last_activity_minutes=-10)
    recent = _case(engine, owners["owner"], program_name="최근 분석")

    rows = _as(engine, owners["owner"], "SELECT * FROM api.v_active_analysis_session")

    assert len(rows) == 1
    assert rows[0]["analysis_case_id"] == recent
    assert rows[0]["program_name"] == "최근 분석"
    assert rows[0]["original_filename"] == "요청서.hwpx"
    assert set(rows[0]) == {
        "analysis_session_id",
        "analysis_case_id",
        "program_name",
        "original_filename",
        "session_expires_at",
    }


def test_active_session_view_hides_other_users(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    rows = _as(engine, owners["stranger"], "SELECT * FROM api.v_active_analysis_session")
    assert rows == []


def test_expired_session_is_not_active_even_while_status_says_so(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    """상태 컬럼이 아니라 시간이 기준이다. 상태 정정은 touch 의 일이다."""
    _case(
        engine,
        owners["stranger"],
        program_name="시간 지난 분석",
        session_expires_minutes=-1,
    )

    rows = _as(engine, owners["stranger"], "SELECT * FROM api.v_active_analysis_session")

    assert rows == []


# --------------------------------------------------------------- HISTORY-01


def test_history_view_leaves_ordering_and_paging_to_the_client(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    """뷰가 먼저 자르면 프론트의 order/range 가 잘린 집합 위에서 다시 자른다."""
    rows = _as(engine, owners["owner"], "SELECT * FROM api.v_my_analysis_history")

    assert len(rows) >= 2
    assert set(rows[0]) == {
        "analysis_case_id",
        "program_name",
        "original_filename",
        "completed_at",
        "report_status",
    }
    # 프론트가 거는 정렬·페이지가 그대로 먹는다.
    paged = _as(
        engine,
        owners["owner"],
        "SELECT * FROM api.v_my_analysis_history"
        " ORDER BY completed_at DESC LIMIT 1 OFFSET 0",
    )
    assert paged[0]["program_name"] == "최근 분석"


def test_history_view_drops_results_past_their_retention(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    """명세: 보관 기간이 끝난 결과는 목록 View 가 반환하지 않는다."""
    expired = _case(
        engine,
        owners["stranger"],
        program_name="보관 만료",
        retention_offset_hours=-1,
        session_status=None,
    )
    kept = _case(
        engine,
        owners["stranger"],
        program_name="보관 중",
        retention_offset_hours=24,
        session_status=None,
    )

    ids = {
        row["analysis_case_id"]
        for row in _as(
            engine, owners["stranger"], "SELECT * FROM api.v_my_analysis_history"
        )
    }

    assert kept in ids
    assert expired not in ids


def test_history_view_excludes_failed_cases(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    failed = _case(
        engine,
        owners["stranger"],
        program_name="실패한 분석",
        case_status="failed",
        report_status="failed",
        session_status=None,
    )

    ids = {
        row["analysis_case_id"]
        for row in _as(
            engine, owners["stranger"], "SELECT * FROM api.v_my_analysis_history"
        )
    }

    assert failed not in ids


# ------------------------------------------------------------------ SES-02


def _rpc(engine: Engine, owner: UUID, name: str, case_pk) -> dict | None:
    rows = _as(
        engine,
        owner,
        f"SELECT api.{name}(CAST(:pk AS uuid)) AS payload",
        {"pk": str(case_pk)},
    )
    return rows[0]["payload"]


def test_touch_restarts_the_window_from_now(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    """남은 시간에 더하는 것이 아니라 클릭 시점부터 30분이다."""
    case_pk = _case(
        engine,
        owners["owner"],
        program_name="touch 대상",
        session_expires_minutes=5,
    )

    payload = _rpc(engine, owners["owner"], "rpc_touch_active_analysis_session", case_pk)

    assert payload["status"] == "active"
    with engine.connect() as connection:
        remaining = connection.scalar(
            text(
                "SELECT expires_at - now() FROM result.analysis_session"
                " WHERE analysis_case_pk = :pk"
            ),
            {"pk": case_pk},
        )
    # 5분이 아니라 30분에서 다시 시작한다.
    assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)


def test_touch_does_not_resurrect_an_expired_session(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    case_pk = _case(
        engine,
        owners["owner"],
        program_name="만료된 세션",
        session_expires_minutes=-1,
    )

    payload = _rpc(engine, owners["owner"], "rpc_touch_active_analysis_session", case_pk)

    assert payload["status"] == "expired"
    # 상태 컬럼도 정정된다. 결정적 전이라 규칙이 맡는다.
    with engine.connect() as connection:
        stored = connection.scalar(
            text(
                "SELECT status FROM result.analysis_session"
                " WHERE analysis_case_pk = :pk"
            ),
            {"pk": case_pk},
        )
    assert stored == "expired"


def test_close_is_idempotent(engine: Engine, owners: dict[str, UUID]) -> None:
    """[새 분석]을 두 번 눌러도 화면이 오류를 받지 않는다."""
    case_pk = _case(engine, owners["owner"], program_name="닫을 분석")

    first = _rpc(engine, owners["owner"], "rpc_close_active_analysis_session", case_pk)
    second = _rpc(engine, owners["owner"], "rpc_close_active_analysis_session", case_pk)

    assert first == second
    assert first["status"] == "closed"
    assert first["close_reason"] == "new_analysis"


def test_rpcs_hide_another_users_session(
    engine: Engine, owners: dict[str, UUID]
) -> None:
    """남의 UUID 를 찍어도 NULL 이다. 존재 여부조차 알려주지 않는다."""
    case_pk = _case(engine, owners["owner"], program_name="남의 세션")

    for name in (
        "rpc_touch_active_analysis_session",
        "rpc_close_active_analysis_session",
    ):
        assert _rpc(engine, owners["stranger"], name, case_pk) is None
        assert _rpc(engine, owners["stranger"], name, uuid4()) is None
