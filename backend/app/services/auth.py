from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, text
from sqlalchemy.engine import RowMapping

from app.core.password_policy import validate_new_password
from app.core.security import hash_password, verify_password


_DUMMY_PASSWORD_HASH = hash_password("sims-dummy-password-never-valid")


class InvalidCredentialsError(Exception):
    pass


class PasswordUnchangedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AppUser:
    id: int
    email: str
    password_changed_at: datetime
    is_active: bool


_SELECT_USER_BY_EMAIL = text(
    """
    SELECT id, email::text AS email, password_hash, password_changed_at, is_active
    FROM sims.app_user
    WHERE email = :email
    """
)

_SELECT_USER_BY_ID = text(
    """
    SELECT id, email::text AS email, password_hash, password_changed_at, is_active
    FROM sims.app_user
    WHERE id = :user_id
    """
)

_SELECT_USER_BY_SUPABASE_ID = text(
    """
    SELECT id, email::text AS email, password_hash, password_changed_at, is_active
    FROM sims.app_user
    WHERE supabase_user_id = CAST(:supabase_user_id AS uuid)
    """
)

# 아직 연결되지 않은 행에만 붙인다. 이미 다른 Supabase 계정을 가진 행은
# WHERE 절에서 빠져 조용히 넘어간다(빈 결과 = 로그인 실패).
_LINK_USER_BY_EMAIL = text(
    """
    UPDATE sims.app_user
       SET supabase_user_id = CAST(:supabase_user_id AS uuid)
     WHERE email = :email
       AND supabase_user_id IS NULL
    RETURNING id, email::text AS email, password_hash, password_changed_at, is_active
    """
)

_SELECT_USER_BY_ID_FOR_UPDATE = text(
    """
    SELECT id, email::text AS email, password_hash, password_changed_at, is_active
    FROM sims.app_user
    WHERE id = :user_id
    FOR UPDATE
    """
)


def login(engine: Engine, email: str, password: str) -> AppUser:
    """이메일로 계정을 찾는다.

    로그인 식별자와 비밀번호 재설정 대상이 같은 컬럼을 보게 해서, 한쪽으로
    로그인하고 다른 쪽으로 재설정 메일이 가는 상황을 만들지 않는다.
    email 은 citext 라 대소문자를 구분하지 않는다.
    """
    with engine.begin() as connection:
        row = (
            connection.execute(
                _SELECT_USER_BY_EMAIL,
                {"email": email},
            )
            .mappings()
            .one_or_none()
        )

        if row is None:
            verify_password(password, _DUMMY_PASSWORD_HASH)
            raise InvalidCredentialsError

        user = _to_user(row)
        password_matches = verify_password(password, row["password_hash"])
        if not password_matches or not user.is_active:
            raise InvalidCredentialsError

        connection.execute(
            text(
                """
                UPDATE sims.app_user
                SET last_login_at = statement_timestamp()
                WHERE id = :user_id
                """
            ),
            {"user_id": user.id},
        )

    return user


def get_user_by_id(engine: Engine, user_id: int) -> AppUser | None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                _SELECT_USER_BY_ID,
                {"user_id": user_id},
            )
            .mappings()
            .one_or_none()
        )

    return _to_user(row) if row is not None else None


def get_user_by_supabase_id(
    engine: Engine,
    supabase_user_id: str,
    *,
    email: str | None = None,
    link_by_email: bool = False,
) -> AppUser | None:
    """Supabase 사용자 → 내부 사용자.

    `supabase_user_id` 로 먼저 찾는다. 없고 `link_by_email` 이 켜져 있으면, 같은
    이메일을 가진 **아직 연결되지 않은** 사용자에게 한 번만 이어 붙인다.

    왜 이메일 연결이 필요한가 — 기존 사용자에게는 supabase_user_id 가 없다.
    이것 없이는 Supabase 로그인으로 전환하는 순간 모든 사용자가 자기 분석 이력을
    잃는다. Supabase 가 가입 때 이메일 소유를 확인하고 두 명부가 같은 조직의
    것이라는 전제 아래 켠다. 이미 다른 Supabase 계정에 연결된 행은
    `supabase_user_id IS NULL` 조건에서 걸러진다 — 계정 탈취 경로가 되지 않게
    한 사람당 한 번뿐이다.
    """
    with engine.begin() as connection:
        row = (
            connection.execute(_SELECT_USER_BY_SUPABASE_ID, {"supabase_user_id": supabase_user_id})
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return _to_user(row)
        if not link_by_email or not email:
            return None

        row = (
            connection.execute(
                _LINK_USER_BY_EMAIL,
                {"supabase_user_id": supabase_user_id, "email": email},
            )
            .mappings()
            .one_or_none()
        )
    return _to_user(row) if row is not None else None


def change_password(
    engine: Engine,
    user_id: int,
    current_password: str,
    new_password: str,
) -> datetime:
    """변경에 성공하면 DB 가 찍은 password_changed_at 을 돌려준다.

    호출자가 이 시각을 기준으로 새 토큰을 발급해야 앱·DB 시계 차이 때문에
    방금 발급한 토큰이 거부되지 않는다.
    """
    validate_new_password(new_password)

    with engine.begin() as connection:
        row = (
            connection.execute(
                _SELECT_USER_BY_ID_FOR_UPDATE,
                {"user_id": user_id},
            )
            .mappings()
            .one_or_none()
        )

        if row is None:
            verify_password(current_password, _DUMMY_PASSWORD_HASH)
            raise InvalidCredentialsError

        user = _to_user(row)
        password_matches = verify_password(current_password, row["password_hash"])
        if not password_matches or not user.is_active:
            raise InvalidCredentialsError

        if current_password == new_password:
            raise PasswordUnchangedError

        changed_at = connection.scalar(
            text(
                """
                UPDATE sims.app_user
                SET password_hash = :password_hash
                WHERE id = :user_id
                RETURNING password_changed_at
                """
            ),
            {
                "password_hash": hash_password(new_password),
                "user_id": user.id,
            },
        )
        if changed_at is None:
            raise InvalidCredentialsError
        return changed_at


def _to_user(row: RowMapping) -> AppUser:
    return AppUser(
        id=row["id"],
        email=row["email"],
        password_changed_at=row["password_changed_at"],
        is_active=row["is_active"],
    )
