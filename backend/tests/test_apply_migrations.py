"""마이그레이션 러너가 자체 COMMIT 없는 파일을 잃지 않는지 본다.

이 함정은 **실제로 밟았다.** 파이썬으로 16개 파일을 넣었더니 전부 "성공" 으로
보였는데 ``sims.app_user.external_uuid`` 가 없었다. 팀원 파일은 자기 안에
``BEGIN; ... COMMIT;`` 이 있어 스스로 커밋하지만 우리 ``001``·``002`` 는 없어서,
커밋하지 않는 커넥션에서는 오류 한 줄 없이 통째로 롤백된다.

``sims_test`` 로는 절대 재현되지 않는다. 거기 ``schema.sql`` 은 이미
``external_uuid`` 를 갖고 있어 002 가 no-op 이기 때문이다. 그래서 이 테스트는
**컬럼을 일부러 떨어뜨려** 실제 앱 DB 와 같은 모양을 만든 뒤 러너를 돌린다.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db.migrations.supabase.verify_vendor_hashes import EXPECTED as _VENDORED
from app.db.scripts.apply_migrations import apply_migrations, migration_files


_DATABASE = "sims_migration_runner_test"
_AUTH_DATABASE = "sims_auth_stub_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app/db/schema.sql"


def test_migration_order_puts_teammate_schema_first() -> None:
    """002 는 팀원 스키마 위에 얹힌다. 순서가 뒤집히면 안 된다."""
    names = [path.name for path in migration_files()]

    assert names.index("000_auth_stub.sql") < names.index("103_analysis_run_upload.sql")
    assert names.index("103_analysis_run_upload.sql") < names.index(
        "001_announcement_profile.sql"
    )
    assert names.index("001_announcement_profile.sql") < names.index(
        "002_identity_bridge.sql"
    )


def test_runner_commits_files_that_have_no_commit_of_their_own() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for the migration runner test")

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

    target = url.set(database=_DATABASE)
    engine = create_engine(target)
    try:
        with engine.connect() as connection:
            connection.connection.driver_connection.execute(
                _SCHEMA.read_text(encoding="utf-8")
            )
        # 실제 앱 DB 와 같은 상태로 되돌린다: 002 가 no-op 이 아니게 만든다.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE sims.app_user DROP COLUMN external_uuid CASCADE"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE sims.inspection_case"
                    " DROP COLUMN analysis_run_id CASCADE"
                )
            )
        assert not _has_column(engine, "app_user", "external_uuid")

        apply_migrations(target.render_as_string(hide_password=False))

        # 팀원 스키마도, 자체 COMMIT 이 없는 우리 002 도 함께 살아남아야 한다.
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT to_regclass('workspace.analysis_run')"))
                is not None
            )
        assert _has_column(engine, "app_user", "external_uuid")
        assert _has_column(engine, "inspection_case", "analysis_run_id")

        # 멱등: 두 번째 적용도 조용히 성공한다.
        apply_migrations(target.render_as_string(hide_password=False))
        assert _has_column(engine, "app_user", "external_uuid")
    finally:
        engine.dispose()


def _has_column(engine, table: str, column: str) -> bool:
    with engine.connect() as connection:
        return (
            connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_schema = 'sims' AND table_name = :table"
                    "   AND column_name = :column"
                ),
                {"table": table, "column": column},
            )
            == 1
        )


def test_auth_stub_never_overwrites_an_existing_auth_uid() -> None:
    """진짜 Supabase 의 auth.uid() 를 덮어쓰면 그 프로젝트 RLS 가 전부 막힌다.

    Supabase 의 auth.uid() 는 지금 PostgREST 가 설정하는
    ``request.jwt.claims`` (JSON) 를 읽는다. 우리 스텁은 구버전의
    ``request.jwt.claim.sub`` 만 읽으므로, 덮어쓰면 모든 요청에서 NULL 이 되어
    ``USING (user_id = auth.uid())`` 형태의 정책이 전부 거짓이 된다.

    그래서 마이그레이션을 자체 호스팅 Supabase 에 돌려도 안전해야 한다.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for the auth stub test")

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{_AUTH_DATABASE}" WITH (FORCE)'
            )
            connection.exec_driver_sql(f'CREATE DATABASE "{_AUTH_DATABASE}"')
    finally:
        admin.dispose()

    target = url.set(database=_AUTH_DATABASE)
    engine = create_engine(target)
    try:
        # 진짜 Supabase 를 흉내낸다: auth.uid() 가 이미 있고 claims 를 읽는다.
        with engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA auth"))
            connection.execute(
                text(
                    "CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE"
                    " AS $stub$ SELECT (nullif(current_setting("
                    "'request.jwt.claims', true), '')::jsonb ->> 'sub')::uuid $stub$"
                )
            )
        with engine.connect() as connection:
            connection.connection.driver_connection.execute(
                _SCHEMA.read_text(encoding="utf-8")
            )

        apply_migrations(target.render_as_string(hide_password=False))

        with engine.connect() as connection:
            body = connection.scalar(
                text("SELECT prosrc FROM pg_proc p"
                     " JOIN pg_namespace n ON n.oid = p.pronamespace"
                     " WHERE n.nspname = 'auth' AND p.proname = 'uid'")
            )
        # 기존 구현이 그대로 살아 있어야 한다.
        assert "request.jwt.claims" in body
        assert "request.jwt.claim.sub" not in body
    finally:
        engine.dispose()


def test_supabase_mode_skips_what_the_teammate_already_installed() -> None:
    """팀원 Supabase 에는 그쪽 DDL 이 이미 있다. 다시 쓰지 않는다.

    특히 08_rls_policies.sql 은 DROP POLICY 후 CREATE 라, 다시 돌리면 팀원이
    고친 정책이 조용히 우리 버전으로 되돌아간다. auth 스텁도 진짜 Supabase 가
    주인이므로 돌릴 이유가 없다.
    """
    names = [path.name for path in migration_files(on_supabase=True)]

    # 팀원 것과 스텁은 빠진다.
    assert "000_auth_stub.sql" not in names
    assert not [name for name in names if name in _VENDORED]
    # 우리 추가분은 전부 남고 순서도 그대로다.
    assert names == [
        "100_analysis_run_queue.sql",
        "101_result_contract_snapshots.sql",
        "102_result_api.sql",
        "103_analysis_run_upload.sql",
        "104_analysis_run_outcome.sql",
        "105_session_api.sql",
        "001_announcement_profile.sql",
        "002_identity_bridge.sql",
    ]
    # 순수 PostgreSQL 대상에서는 하나도 빠지지 않는다.
    assert set(names) < {path.name for path in migration_files()}
