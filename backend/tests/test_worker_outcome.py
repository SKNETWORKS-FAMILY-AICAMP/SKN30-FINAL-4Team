"""RUN-04 Realtime 이 읽을 세 칸을 워커가 제대로 채우는지 본다.

프론트는 ``workspace.analysis_run`` 의 UPDATE 만 구독한다. Realtime 은 바뀐
행 자체를 보내고 조인해 주지 않으므로, 세 값이 그 행에 없으면 화면은 결과를
찾지도 못하고 실패 사유도 못 보여준다.

  - ``analysis_case_pk``  succeeded 이벤트에서 결과를 여는 유일한 열쇠
  - ``error_code``        운영 집계용, 단계별로 구분된다
  - ``error_message``     화면에 그대로 보이는 문구

그리고 이 파일의 진짜 목적은 마지막 항목이다: **예외 원문이 error_message 로
새지 않는다.** 실패 메시지에는 요청서 조각·저장소 경로·접속 문자열이 섞여
들어온다. 그것이 사용자 화면에 도달하는 경로가 없어야 한다.
"""

from __future__ import annotations

import os
import pathlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.db.identity_bridge import ensure_identity
from worker.jobs import claim_next, complete, fail
from worker.outcome import DEFAULT_ERROR_CODE, USER_ERROR_MESSAGES, user_outcome
from worker.profiles import StageError


_DATABASE = "sims_outcome_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"

# 사용자 화면에 절대 있으면 안 되는 것들의 표본.
_SECRET = "postgresql://postgres:simstest@10.0.0.4:5432/sims"
_REQUEST_TEXT = "지원대상은 서울시 소재 중소기업이며 대표자 홍길동"


class _FakeDiagnostic:
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        self.message = f"{_REQUEST_TEXT} / {_SECRET}"


# ------------------------------------------------------- 문구표 (DB 없이)


def test_known_reason_code_keeps_its_code_and_uses_a_fixed_message() -> None:
    error = StageError.__new__(StageError)
    error.diagnostic = _FakeDiagnostic("PARSE_FAILED")

    code, message = user_outcome(error)

    assert code == "PARSE_FAILED"
    assert message == USER_ERROR_MESSAGES["PARSE_FAILED"]


def test_unknown_reason_code_falls_back_instead_of_passing_it_through() -> None:
    """새 reason code 가 생겨도 원문이 새지 않는다."""
    error = StageError.__new__(StageError)
    error.diagnostic = _FakeDiagnostic("SOME_BRAND_NEW_CODE")

    code, message = user_outcome(error)

    assert code == DEFAULT_ERROR_CODE
    assert message == USER_ERROR_MESSAGES[DEFAULT_ERROR_CODE]


def test_plain_exception_message_never_becomes_the_user_message() -> None:
    code, message = user_outcome(RuntimeError(f"{_REQUEST_TEXT} / {_SECRET}"))

    assert code == DEFAULT_ERROR_CODE
    assert message == USER_ERROR_MESSAGES[DEFAULT_ERROR_CODE]
    assert _SECRET not in message
    assert "홍길동" not in message


def test_every_message_in_the_table_is_free_of_internals() -> None:
    """표 자체가 안전해야 한다. 나중에 항목을 더할 때 걸리는 그물이다."""
    for code, message in USER_ERROR_MESSAGES.items():
        assert message.strip()
        for leak in ("Error", "Exception", "Traceback", "postgres", "://", "sims."):
            assert leak not in message, code


# ------------------------------------------------------------ DB 픽스처


@pytest.fixture(scope="module")
def engine() -> Engine:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for outcome tests")

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


def _queued_run(engine: Engine, login_id: str) -> UUID:
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
                " VALUES (:user_id, 'queued') RETURNING analysis_run_pk"
            ),
            {"user_id": user_uuid},
        )


def _claim(connection, run_id: UUID, worker_id: str):
    """내 run 을 집었는지 확인한다.

    ``claim_next`` 는 큐 전체에서 가장 오래된 것을 집는다. 앞선 테스트가
    ``queued`` 행을 남겨 두면 엉뚱한 행을 고치고도 조용히 통과한다 — 실제로
    한 번 그렇게 통과했다.
    """
    claim = claim_next(connection, worker_id=worker_id, lease_seconds=30)
    assert claim is not None
    assert claim.analysis_run_pk == run_id, "다른 테스트가 남긴 run 을 집었다"
    return claim


def _row(engine: Engine, run_id: UUID) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, analysis_case_pk, error_code, error_message,"
                    "       last_error"
                    "  FROM workspace.analysis_run WHERE analysis_run_pk = :run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )


# ------------------------------------------------------------------ 실제 DB


def test_complete_records_the_case_the_screen_must_open(engine: Engine) -> None:
    run_id = _queued_run(engine, "outcome-ok")
    case_pk = uuid4()

    with engine.begin() as connection:
        claim = _claim(connection, run_id, "w1")
        assert complete(
            connection,
            run_id=run_id,
            claim_token=claim.claim_token,
            analysis_case_pk=case_pk,
        )

    row = _row(engine, run_id)
    assert row["status"] == "succeeded"
    assert row["analysis_case_pk"] == case_pk
    # 성공한 run 에 오류 표시가 남아 있으면 화면이 둘 다 본다.
    assert row["error_code"] is None
    assert row["error_message"] is None


def test_fail_keeps_the_raw_cause_out_of_the_user_message(engine: Engine) -> None:
    run_id = _queued_run(engine, "outcome-fail")
    raw = f"ValueError: {_REQUEST_TEXT} / {_SECRET}"

    with engine.begin() as connection:
        claim = _claim(connection, run_id, "w1")
        code, message = user_outcome(RuntimeError(raw))
        assert fail(
            connection,
            run_id=run_id,
            claim_token=claim.claim_token,
            last_error=raw,
            error_code=code,
            error_message=message,
        )

    row = _row(engine, run_id)
    assert row["status"] == "failed"
    assert row["error_code"] == DEFAULT_ERROR_CODE
    assert row["error_message"] == USER_ERROR_MESSAGES[DEFAULT_ERROR_CODE]
    # 원문은 운영용 칸에만 있다.
    assert _SECRET in row["last_error"]
    assert _SECRET not in row["error_message"]
    assert "홍길동" not in row["error_message"]


def test_error_code_and_message_must_live_and_die_together(engine: Engine) -> None:
    """CHECK 이 반쪽 상태를 막는다. 코드만 있으면 화면이 빈 Alert 를 띄운다."""
    run_id = _queued_run(engine, "outcome-pairing")

    with pytest.raises(Exception) as raised:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace.analysis_run SET error_code = 'X'"
                    " WHERE analysis_run_pk = :run_id"
                ),
                {"run_id": run_id},
            )
    assert "ck_workspace_analysis_run_error_pairing" in str(raised.value)

    # 이 테스트는 claim 하지 않는다. queued 로 두면 다음 테스트가 집어 간다.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace.analysis_run SET status = 'cancelled'"
                " WHERE analysis_run_pk = :run_id"
            ),
            {"run_id": run_id},
        )


def test_a_retry_that_succeeds_clears_the_previous_failure(engine: Engine) -> None:
    run_id = _queued_run(engine, "outcome-retry")
    with engine.begin() as connection:
        claim = _claim(connection, run_id, "w1")
        fail(
            connection,
            run_id=run_id,
            claim_token=claim.claim_token,
            last_error="boom",
            error_code="PARSE_FAILED",
            error_message=USER_ERROR_MESSAGES["PARSE_FAILED"],
        )
    assert _row(engine, run_id)["error_code"] == "PARSE_FAILED"

    # 같은 run 을 다시 큐에 넣고 이번엔 성공시킨다.
    case_pk = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace.analysis_run SET status = 'queued',"
                " completed_at = NULL WHERE analysis_run_pk = :run_id"
            ),
            {"run_id": run_id},
        )
    with engine.begin() as connection:
        claim = _claim(connection, run_id, "w2")
        assert complete(
            connection,
            run_id=run_id,
            claim_token=claim.claim_token,
            analysis_case_pk=case_pk,
        )

    row = _row(engine, run_id)
    assert row["analysis_case_pk"] == case_pk
    assert row["error_code"] is None
    assert row["error_message"] is None
