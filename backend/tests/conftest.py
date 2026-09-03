"""테스트가 공용 DB 의 운영 상태를 바꿔 놓은 채 끝나지 않게 한다.

테스트와 실제 사용이 같은 Supabase 를 본다. 테스트는 자기가 만든 행을
정리하지만, **원래 있던 행을 함께 바꾸는 문장**은 되돌리지 못한다. 두 곳이
그렇다.

- 말뭉치 임베딩은 프로파일 하나를 켜면서 같은 종류의 나머지를 전부 끈다.
  테스트가 자기 프로파일을 켜면 운영 프로파일이 꺼지고, 테스트가 끝나며
  자기 것도 사라져 활성 프로파일이 하나도 남지 않는다. 검색은 "정확히
  하나"를 요구하므로 이후 실제 분석이 RETRIEVAL_NOT_READY 로 실패한다.
- 공고 동기화는 접수 마감 스윕에서 ``period_end_date < sync_date`` 인 현재
  공고를 전부 CLOSED 로 닫는다. 테스트는 기존 공고와 부딪히지 않으려고
  2030 년 이후 날짜로 동기화하는데, 그 날짜에서는 접수 중인 진짜 공고도
  전부 마감으로 닫힌다. 검색 대상이 0 이 되어 유사 공고가 비게 된다.

테스트 전용 DB 를 쓰면 둘 다 필요 없는 장치다.
"""

import os

import pytest
from sqlalchemy import create_engine, text


_ACTIVE_SUMMARY_PROFILES = text(
    """
    SELECT id FROM sims.embedding_profile
     WHERE profile_kind = 'SUMMARY' AND is_active
    """
)

_RESTORE_ACTIVE_SUMMARY_PROFILES = text(
    """
    UPDATE sims.embedding_profile
       SET is_active = (id = ANY(:ids))
     WHERE profile_kind = 'SUMMARY'
    """
)

_CURRENT_ANNOUNCEMENT_STATUS = text(
    """
    SELECT id, search_status FROM sims.announcement_version WHERE is_current
    """
)

_RESTORE_ANNOUNCEMENT_STATUS = text(
    """
    UPDATE sims.announcement_version av
       SET search_status = snapshot.search_status
      FROM (
            SELECT unnest(CAST(:ids AS bigint[])) AS id,
                   unnest(CAST(:statuses AS text[])) AS search_status
           ) AS snapshot
     WHERE av.id = snapshot.id
       AND av.search_status <> snapshot.search_status
    """
)


@pytest.fixture(scope="session", autouse=True)
def keep_shared_database_operational():
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        yield
        return

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            profile_ids = [row[0] for row in connection.execute(_ACTIVE_SUMMARY_PROFILES)]
            announcements = list(connection.execute(_CURRENT_ANNOUNCEMENT_STATUS))
        try:
            yield
        finally:
            with engine.begin() as connection:
                connection.execute(
                    _RESTORE_ACTIVE_SUMMARY_PROFILES, {"ids": profile_ids}
                )
                if announcements:
                    connection.execute(
                        _RESTORE_ANNOUNCEMENT_STATUS,
                        {
                            "ids": [row[0] for row in announcements],
                            "statuses": [row[1] for row in announcements],
                        },
                    )
    finally:
        engine.dispose()
