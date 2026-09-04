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
    password_changed_at: float,
) -> str:
    """비밀번호 변경 시각을 토큰에 함께 담는다.

    이 토큰이 어느 비밀번호를 기준으로 발급됐는지를 pwd 클레임에 적어 둔다.
    인증은 이 값이 DB 의 현재 값과 같은지만 본다. 비밀번호가 바뀌면 값이
    달라져 이전 토큰이 전부 무효가 된다.

    두 시각의 앞뒤를 비교하지 않는 이유는 password_changed_at 이 DB 시계로,
    iat 는 앱 시계로 찍히기 때문이다. 실측에서 DB 가 0.1~0.4초 앞서 있었고,
    그 차이를 iat 로 메우면 PyJWT 가 미래 토큰(약 0.39초 초과)으로 보고
    거부한다. 값이 같은지만 보면 시계 차이가 개입할 여지가 없다.
    """
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
            "pwd": password_changed_at,
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
        options={"require": ["sub", "iat", "exp", "pwd"]},
    )
