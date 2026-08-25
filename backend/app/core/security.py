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
) -> str:
    if not secret:
        raise ValueError("JWT secret must not be empty")
    if expires_in_seconds <= 0:
        raise ValueError("Access-token lifetime must be positive")

    issued_at = time.time()
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
