"""Slice 6: sims 사용자와 팀원 스키마 사이의 신원 다리.

이것은 인증 교체가 아니라 신원 다리다. 실제 Supabase Auth 를 연결하면
auth.users 를 그쪽이 소유하고 이 동기화는 사라진다.

``sims.app_user`` 는 bigint PK 와 ``password_hash`` 를 그대로 들고 있고 JWT 도
거기서 나온다. 팀원 스키마는 사용자를 ``auth.users(id)`` UUID 로만 가리키므로
둘을 잇는 값 하나가 필요하다. 그 값이 ``sims.app_user.external_uuid`` 이고,
이 모듈이 하는 일은 그 UUID 로 ``auth.users`` 와 ``app.user_profile`` 행이
있게 만드는 것뿐이다. 비밀번호도, 세션도, 권한도 옮기지 않는다.

트리거가 아니라 명시적 함수다. 트리거로 만들면 팀원 스키마가 없는 DB 에서
``INSERT INTO sims.app_user`` 자체가 실패하고, 어느 쓰기가 auth 스키마를
건드리는지 호출부에서 보이지 않는다.

전환기에는 두 모양이 공존한다. 팀원 스키마가 설치되지 않은 DB 에서는
``to_regclass`` 로 확인하고 조용히 아무것도 하지 않는다 — 오류가 아니다.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

__all__ = ["ensure_identity", "teammate_schema_installed"]


_EXTERNAL_UUID = text(
    """
    SELECT external_uuid, display_name, email
      FROM sims.app_user
     WHERE id = :app_user_id
    """
)

# auth.users 는 표면이 id 하나뿐이다 (000_auth_stub.sql). 이미 있으면 그대로 둔다.
_AUTH_USER = text(
    """
    INSERT INTO auth.users (id) VALUES (:external_uuid)
    ON CONFLICT (id) DO NOTHING
    """
)

# display_name 은 우리 쪽이 주인이므로 갱신한다. 없으면 이메일 앞부분을 쓴다 —
# 로그인 화면과 같은 규칙이다.
_USER_PROFILE = text(
    """
    INSERT INTO app.user_profile (user_id, display_name)
    VALUES (:external_uuid, :display_name)
    ON CONFLICT (user_id) DO UPDATE
       SET display_name = EXCLUDED.display_name,
           updated_at   = now()
    """
)


def teammate_schema_installed(connection: Connection) -> bool:
    """팀원 Supabase 스키마가 이 DB 에 올라와 있는지 본다."""
    return (
        connection.scalar(text("SELECT to_regclass('app.user_profile')")) is not None
    )


def ensure_identity(connection: Connection, app_user_id: int) -> UUID:
    """사용자의 ``external_uuid`` 를 돌려주고, 팀원 쪽 신원 행을 맞춘다.

    사용자 행이 만들어질 때(또는 팀원 스키마 테이블을 처음 쓰기 직전에) 부른다.
    여러 번 불러도 같다. 팀원 스키마가 없으면 UUID 만 돌려주고 끝낸다.

    트랜잭션은 호출자 것이다.
    """
    row = connection.execute(_EXTERNAL_UUID, {"app_user_id": app_user_id}).one_or_none()
    if row is None:
        raise LookupError(f"sims.app_user {app_user_id} does not exist")
    external_uuid, display_name, email = row

    if not teammate_schema_installed(connection):
        return external_uuid

    connection.execute(_AUTH_USER, {"external_uuid": external_uuid})
    connection.execute(
        _USER_PROFILE,
        {
            "external_uuid": external_uuid,
            "display_name": display_name or str(email).partition("@")[0],
        },
    )
    return external_uuid
