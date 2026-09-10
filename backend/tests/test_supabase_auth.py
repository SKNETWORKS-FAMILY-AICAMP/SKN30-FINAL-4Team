"""Supabase Access Token 검증 회귀 검사. DB 를 붙이지 않는다.

여기서 보는 것은 **토큰 하나를 claim 으로 바꾸는 규칙**이다. 내부 사용자를 찾는
부분(supabase_user_id 조회·이메일 연결)은 DB 가 필요해 test_auth.py 가 본다.
"""
import time

import jwt
import pytest

from app.core.security import create_access_token
from app.core.supabase_auth import (
    SupabaseTokenError,
    SupabaseTokenVerifier,
    looks_like_supabase_token,
)


SECRET = "supabase-shared-secret-at-least-32-bytes"
ISSUER = "https://project.supabase.co/auth/v1"
SUBJECT = "6a1f0a3e-6c2f-4e7b-9c1e-2f3a4b5c6d7e"


def token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": SUBJECT,
        "aud": "authenticated",
        "iss": ISSUER,
        "iat": now,
        "exp": now + 3600,
        "email": "user@example.com",
        "role": "authenticated",
    }
    claims.update(overrides)
    algorithm = overrides.pop("_alg", "HS256")
    return jwt.encode(claims, SECRET, algorithm=algorithm)


def verifier(**overrides) -> SupabaseTokenVerifier:
    kwargs = {
        "jwt_secret": SECRET,
        "jwks_url": None,
        "issuer": ISSUER,
        "audience": "authenticated",
    }
    kwargs.update(overrides)
    return SupabaseTokenVerifier(**kwargs)


def test_valid_token_yields_claims() -> None:
    claims = verifier().verify(token())
    assert claims["sub"] == SUBJECT
    assert claims["email"] == "user@example.com"


def test_wrong_secret_is_rejected() -> None:
    other = jwt.encode(
        {
            "sub": SUBJECT,
            "aud": "authenticated",
            "iss": ISSUER,
            "exp": int(time.time()) + 3600,
        },
        "a-different-secret-that-is-long-enough",
        algorithm="HS256",
    )
    with pytest.raises(SupabaseTokenError):
        verifier().verify(other)


def test_expired_token_is_rejected() -> None:
    with pytest.raises(SupabaseTokenError):
        verifier().verify(token(exp=int(time.time()) - 10))


def test_wrong_issuer_and_audience_are_rejected() -> None:
    with pytest.raises(SupabaseTokenError):
        verifier().verify(token(iss="https://evil.example/auth/v1"))
    with pytest.raises(SupabaseTokenError):
        verifier().verify(token(aud="anon"))


def test_token_without_subject_is_rejected() -> None:
    now = int(time.time())
    no_sub = jwt.encode(
        {"aud": "authenticated", "iss": ISSUER, "exp": now + 3600},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(SupabaseTokenError):
        verifier().verify(no_sub)


def test_alg_none_is_rejected() -> None:
    """서명 없는 토큰을 통과시키면 누구나 아무 사용자가 될 수 있다."""
    unsigned = jwt.encode({"sub": SUBJECT}, key="", algorithm="none")
    with pytest.raises(SupabaseTokenError):
        verifier().verify(unsigned)


def test_asymmetric_token_without_jwks_is_rejected() -> None:
    """공유 비밀만 설정된 프로젝트에 비대칭 토큰이 오면 검증할 키가 없다.

    조용히 통과시키지 않고 거절한다.
    """
    value = verifier()
    header = jwt.api_jws.base64url_encode(b'{"alg":"ES256","typ":"JWT"}').decode()
    with pytest.raises(SupabaseTokenError):
        value.verify(f"{header}.eyJzdWIiOiJ4In0.c2ln")


def test_disabled_verifier_rejects_everything() -> None:
    """설정이 비어 있으면 Supabase 인증을 끈 것이다. 통과시키지 않는다."""
    value = SupabaseTokenVerifier(
        jwt_secret=None, jwks_url=None, issuer=None, audience=None
    )
    assert not value.enabled
    with pytest.raises(SupabaseTokenError):
        value.verify(token())


def test_issuer_is_not_checked_when_project_url_is_unknown() -> None:
    """검증할 issuer 값이 없으면 항목에서 뺀다.

    아무 issuer 나 통과시키는 것과 같지만, 통과 조건을 조용히 느슨하게 만드는
    쪽보다 설정이 비었다는 사실이 드러나는 편이 낫다.
    """
    claims = verifier(issuer=None).verify(token(iss="https://other.example/auth/v1"))
    assert claims["sub"] == SUBJECT


def test_token_kind_is_routed_by_issuer() -> None:
    """어느 검증기로 보낼지만 고른다. 서명은 그 뒤에 각자 확인한다."""
    assert looks_like_supabase_token(token())
    internal = create_access_token(
        user_id=1,
        secret="internal-secret-that-is-at-least-32-bytes",
        expires_in_seconds=60,
        password_changed_at=time.time(),
    )
    assert not looks_like_supabase_token(internal)
    assert not looks_like_supabase_token("not-a-token")
