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


_USER_COLUMNS = """
    id,
    email::text AS email,
    password_hash,
    password_changed_at,
    is_active
"""


def login(engine: Engine, email: str, password: str) -> AppUser:
    """이메일로 계정을 찾는다.

    로그인 식별자와 비밀번호 재설정 대상이 같은 컬럼을 보게 해서, 한쪽으로
    로그인하고 다른 쪽으로 재설정 메일이 가는 상황을 만들지 않는다.
    email 은 citext 라 대소문자를 구분하지 않는다.
    """
    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    f"""
                    SELECT {_USER_COLUMNS}
                    FROM sims.app_user
                    WHERE email = :email
                    """
                ),
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
                text(
                    f"""
                    SELECT {_USER_COLUMNS}
                    FROM sims.app_user
                    WHERE id = :user_id
                    """
                ),
                {"user_id": user_id},
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
                text(
                    f"""
                    SELECT {_USER_COLUMNS}
                    FROM sims.app_user
                    WHERE id = :user_id
                    FOR UPDATE
                    """
                ),
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
