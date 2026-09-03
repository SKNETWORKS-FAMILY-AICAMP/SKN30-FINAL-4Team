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


@pytest.fixture(scope="session", autouse=True)
def keep_active_embedding_profile():
    """테스트가 공용 DB 의 활성 임베딩 프로파일을 꺼 둔 채 끝나지 않게 한다.

    말뭉치 임베딩은 프로파일 하나를 켜면서 같은 종류의 나머지를 전부 끈다.
    프로파일은 하나만 활성이어야 하므로 그 자체는 맞지만, 테스트가 자기
    프로파일을 켜면 운영 프로파일이 꺼진다. 테스트가 끝나며 자기 것도
    비활성으로 남으면 활성 프로파일이 하나도 없게 되고, 검색이 "정확히
    하나"를 요구하므로 실제 분석이 RETRIEVAL_NOT_READY 로 실패한다.

    테스트 전용 DB 를 쓰면 필요 없는 장치다. 지금은 테스트와 실제 사용이
    같은 Supabase 를 보고 있어 여기서 막는다.
    """

    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        yield
        return

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            active_ids = [row[0] for row in connection.execute(_ACTIVE_SUMMARY_PROFILES)]
        try:
            yield
        finally:
            with engine.begin() as connection:
                connection.execute(
                    _RESTORE_ACTIVE_SUMMARY_PROFILES, {"ids": active_ids}
                )
    finally:
        engine.dispose()
