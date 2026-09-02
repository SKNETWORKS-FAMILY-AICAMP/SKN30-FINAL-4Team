"""Password hashing and JWT access-token helpers."""

import time

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError


_ALGORITHM = "HS256"
_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hash.verify(password, password_hash)
    except UnknownHashError:
        return False


def create_access_token(
    user_id: int,
    secret: str,
    expires_in_seconds: int,
    issued_at: float | None = None,
) -> str:
    """issued_at 은 비밀번호 변경 직후처럼 DB 시각을 기준 삼아야 할 때만 넘긴다.

    인증은 password_changed_at 보다 먼저 발급된 토큰을 거부하는데, 그 값은
    DB 시계로 찍힌다. 앱 시계가 DB 보다 조금이라도 뒤지면 방금 발급한
    토큰이 곧바로 거부되므로, 그런 경우 DB 시각을 그대로 넘겨 받는다.
    """
    if not secret:
        raise ValueError("JWT secret must not be empty")
    if expires_in_seconds <= 0:
        raise ValueError("Access-token lifetime must be positive")

    issued_at = time.time() if issued_at is None else issued_at
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": issued_at,
            "exp": issued_at + expires_in_seconds,
        },
        secret,
        algorithm=_ALGORITHM,
    )


def decode_access_token(token: str, secret: str) -> dict[str, object]:
    if not secret:
        raise ValueError("JWT secret must not be empty")

    return jwt.decode(
        token,
        secret,
        algorithms=[_ALGORITHM],
        options={"require": ["sub", "iat", "exp"]},
    )
