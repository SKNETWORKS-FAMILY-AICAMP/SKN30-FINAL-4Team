"""마이그레이션을 순서대로, autocommit 으로 적용한다.

    uv run python -m app.db.scripts.apply_migrations            # DATABASE_URL
    uv run python -m app.db.scripts.apply_migrations --dry-run  # 순서만 본다

**autocommit 이 이 스크립트의 존재 이유다.** 팀원 파일은 자기 안에
``BEGIN; ... COMMIT;`` 을 갖고 있어 스스로 커밋하지만, 우리 ``001``·``002`` 는
그렇지 않다. 커밋하지 않는 커넥션으로 돌리면 그 두 파일은 **오류 없이 조용히
사라진다** — 실제로 그렇게 당했고, ``sims_test`` 는 ``schema.sql`` 이 이미 같은
컬럼을 갖고 있어 002 가 no-op 이라 테스트로는 잡히지 않는다.

순서는 파일명 정렬이고 테스트 픽스처와 같다: ``supabase/*.sql`` 먼저(팀원
스키마), 그다음 ``*.sql``(우리 것). ``000`` < ``01`` < … < ``09`` < ``100`` <
``103`` 으로 사전순이 곧 적용순이다.

전부 멱등이라 여러 번 돌려도 같다. 첫 오류에서 멈춘다 (psql 의
``ON_ERROR_STOP=1`` 과 같다).

ponytail: 적용 이력 테이블을 두지 않는다. 파일이 전부 멱등이라 "무엇이
적용됐는가" 를 따로 기록할 필요가 없다. 파일이 멱등하지 않게 되는 날
``schema_migration`` 테이블을 만든다.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

from sqlalchemy import create_engine

from app.core.config import Settings
from app.db.migrations.supabase.verify_vendor_hashes import EXPECTED as _VENDORED

_DB = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS = _DB / "migrations"
_SCHEMA = _DB / "schema.sql"

# 진짜 Supabase 에는 이미 있는 것들. 팀원 파일 목록은 벤더 해시 매니페스트가
# 단일 출처다 — 목록을 여기 다시 적으면 둘이 어긋난다.
_ALREADY_ON_SUPABASE = frozenset(_VENDORED) | {"000_auth_stub.sql"}


def migration_files(*, on_supabase: bool = False) -> list[pathlib.Path]:
    """적용 순서대로. 팀원 스키마가 먼저다 — 우리 002 가 그 위에 얹힌다.

    ``on_supabase`` 는 팀원이 이미 자기 DDL 을 올려 둔 self-hosted Supabase 를
    대상으로 할 때 쓴다. 팀원 01~09 와 auth 스텁을 건너뛰고 우리 추가분(큐·
    결과 RPC·업로드·신원 다리)만 얹는다. 팀원 정책을 다시 쓰지 않는 것이
    목적이다 — 08 은 DROP POLICY 후 CREATE 라 그쪽 수정이 조용히 되돌려진다.
    """
    paths = sorted((_MIGRATIONS / "supabase").glob("*.sql")) + sorted(
        _MIGRATIONS.glob("*.sql")
    )
    if on_supabase:
        paths = [path for path in paths if path.name not in _ALREADY_ON_SUPABASE]
    return paths


def apply_migrations(
    database_url: str,
    *,
    dry_run: bool = False,
    on_supabase: bool = False,
    with_schema: bool = False,
) -> int:
    paths = migration_files(on_supabase=on_supabase)
    if with_schema:
        # 새 대상에는 우리 sims 스키마가 없다. 팀원 스키마는 임베딩 테이블을
        # 일부러 뺐으므로(02 헤더) 벡터 쪽은 이쪽이 주인이다.
        paths = [_SCHEMA] + paths
    if dry_run:
        for path in paths:
            print(f"      {path.relative_to(_DB)}")
        return len(paths)

    # AUTOCOMMIT 이 아니면 자체 COMMIT 이 없는 파일이 조용히 롤백된다.
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            # 팀원 파일의 `format('... %I', t)` 를 SQLAlchemy 가 바인드 파라미터로
            # 오해하지 않게 드라이버 커넥션에 원문을 그대로 넘긴다.
            raw = connection.connection.driver_connection
            for path in paths:
                started = time.perf_counter()
                raw.execute(path.read_text(encoding="utf-8"))
                elapsed = time.perf_counter() - started
                print(f"{elapsed:6.2f}s  {path.name}")
    finally:
        engine.dispose()
    return len(paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="생략하면 DATABASE_URL 설정을 쓴다.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="적용하지 않고 순서만 출력한다.",
    )
    parser.add_argument(
        "--on-supabase",
        action="store_true",
        help="팀원 DDL 이 이미 올라간 self-hosted Supabase 대상. 01~09 와 auth "
        "스텁을 건너뛰고 우리 추가분만 적용한다.",
    )
    parser.add_argument(
        "--with-schema",
        action="store_true",
        help="우리 sims 스키마(schema.sql)를 먼저 적용한다. 새 DB 를 만들 때 쓴다.",
    )
    arguments = parser.parse_args()

    database_url = arguments.database_url or str(Settings().database_url)
    # 어느 DB 를 건드리는지 비밀번호 없이 보여준다.
    target = database_url.rsplit("@", 1)[-1]
    print(f"target: {target}", file=sys.stderr)

    started = time.perf_counter()
    count = apply_migrations(
        database_url,
        dry_run=arguments.dry_run,
        on_supabase=arguments.on_supabase,
        with_schema=arguments.with_schema,
    )
    if not arguments.dry_run:
        print(f"\n{count} files in {time.perf_counter() - started:.2f}s")


if __name__ == "__main__":
    main()
