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


# ---------------------------------------------------------- 보고서 다운로드

# 브라우저가 `window.location.href = signed_url` 로 이동한다. 그 이동에는
# Authorization 헤더가 붙지 않는다 — 그래서 URL 자체가 자격증명이어야 하고,
# 그것이 Supabase 서명 URL 이 존재하는 이유다. 여기서는 같은 역할을 짧게 사는
# 토큰으로 한다. Supabase Storage 가 서면 이 토큰은 그쪽 서명 URL 로 바뀐다.
#
# 소유권은 **발급 시점에** 확인한다. 토큰에 사용자를 담지 않는 이유이자,
# 수명을 분 단위로 두는 이유다.
_DOWNLOAD_PURPOSE = "report-download"


def create_report_download_token(
    analysis_case_id: str, secret: str, expires_in_seconds: int
) -> str:
    if not secret:
        raise ValueError("JWT secret must not be empty")
    if expires_in_seconds <= 0:
        raise ValueError("Download-token lifetime must be positive")

    issued_at = time.time()
    return jwt.encode(
        {
            "case": str(analysis_case_id),
            # 접근 토큰과 클레임 집합이 겹치지 않게 한다. decode_access_token 은
            # sub/pwd 를 요구하므로 이 토큰은 그쪽으로 쓸 수 없고, 그 반대도 같다.
            "purpose": _DOWNLOAD_PURPOSE,
            "iat": issued_at,
            "exp": issued_at + expires_in_seconds,
        },
        secret,
        algorithm=_ALGORITHM,
    )


def decode_report_download_token(token: str, secret: str) -> str:
    """``analysis_case_id`` 를 돌려준다. 아니면 ``InvalidTokenError``."""
    if not secret:
        raise ValueError("JWT secret must not be empty")

    claims = jwt.decode(
        token,
        secret,
        algorithms=[_ALGORITHM],
        options={"require": ["case", "purpose", "iat", "exp"]},
    )
    if claims.get("purpose") != _DOWNLOAD_PURPOSE:
        raise jwt.InvalidTokenError("Token was not issued for report download")
    case_id = claims["case"]
    if not isinstance(case_id, str):
        raise jwt.InvalidTokenError("Invalid case identifier")
    return case_id
