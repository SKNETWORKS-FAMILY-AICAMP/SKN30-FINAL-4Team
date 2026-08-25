from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, text
from sqlalchemy.engine import RowMapping

from app.core.security import hash_password, verify_password


_DUMMY_PASSWORD_HASH = hash_password("sims-dummy-password-never-valid")


class InvalidCredentialsError(Exception):
    pass


class PasswordUnchangedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AppUser:
    id: int
    login_id: str
    password_changed_at: datetime
    is_active: bool


_USER_COLUMNS = """
    id,
    login_id::text AS login_id,
    password_hash,
    password_changed_at,
    is_active
"""


def login(engine: Engine, login_id: str, password: str) -> AppUser:
    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    f"""
                    SELECT {_USER_COLUMNS}
                    FROM sims.app_user
                    WHERE login_id = :login_id
                    """
                ),
                {"login_id": login_id},
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
) -> None:
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

        connection.execute(
            text(
                """
                UPDATE sims.app_user
                SET password_hash = :password_hash
                WHERE id = :user_id
                """
            ),
            {
                "password_hash": hash_password(new_password),
                "user_id": user.id,
            },
        )


def _to_user(row: RowMapping) -> AppUser:
    return AppUser(
        id=row["id"],
        login_id=row["login_id"],
        password_changed_at=row["password_changed_at"],
        is_active=row["is_active"],
    )
