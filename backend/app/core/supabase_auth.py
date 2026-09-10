"""Supabase Access Token 검증.

로그인은 Supabase Auth 가 맡고, React 가 받은 토큰을 그대로 FastAPI 로 보낸다.
FastAPI 는 그 토큰을 검증해 내부 사용자를 찾는다.

서명 방식을 하드코딩하지 않는다
------------------------------
Supabase 프로젝트마다 JWT signing 설정이 다르다. 예전 프로젝트는 공유 비밀
(HS256)이고, 최근에는 비대칭 키(ES256/RS256) + JWKS 가 기본이다. 둘 중 하나로
못박으면 다른 설정의 프로젝트에서 그냥 401 이 난다. 그래서 **토큰 헤더의 alg 를
보고** 검증 경로를 고른다.

    alg = HS256          -> SUPABASE_JWT_SECRET (공유 비밀)
    alg = ES256/RS256/…  -> JWKS (SUPABASE_JWKS_URL 또는 SUPABASE_URL 에서 유도)

둘 다 설정돼 있지 않으면 Supabase 인증을 끈 것으로 본다 — 그때는 호출부가 기존
자체 발급 JWT 로 넘어간다.

이 모듈은 DB 를 모른다. 토큰을 claim 으로 바꾸는 것까지가 전부다.
"""
import logging
from typing import Any

import jwt
from jwt import PyJWKClient


logger = logging.getLogger(__name__)

# Supabase 가 access token 에 넣는 audience. 로그인한 사용자는 authenticated 다.
DEFAULT_AUDIENCE = "authenticated"

_SYMMETRIC_ALGORITHMS = ("HS256", "HS384", "HS512")
_ASYMMETRIC_ALGORITHMS = ("ES256", "RS256", "RS384", "RS512", "EdDSA")


class SupabaseTokenError(ValueError):
    """Supabase 토큰으로 인정할 수 없다. 사유는 로그로만 남긴다."""


class SupabaseTokenVerifier:
    """토큰 하나 → claim dict. 검증에 실패하면 SupabaseTokenError."""

    def __init__(
        self,
        *,
        jwt_secret: str | None,
        jwks_url: str | None,
        issuer: str | None,
        audience: str | None = DEFAULT_AUDIENCE,
    ) -> None:
        self._secret = jwt_secret or None
        self._issuer = issuer or None
        self._audience = audience or None
        # JWKS 는 원격 조회다. PyJWKClient 가 kid 별로 캐시하고, 모르는 kid 가
        # 오면 그때만 다시 받아 온다. 키 회전이 있어도 재시작이 필요 없다.
        self._jwks_client = PyJWKClient(jwks_url, cache_keys=True) if jwks_url else None

    @property
    def enabled(self) -> bool:
        return self._secret is not None or self._jwks_client is not None

    def verify(self, token: str) -> dict[str, Any]:
        if not self.enabled:
            raise SupabaseTokenError("Supabase authentication is not configured")

        try:
            algorithm = jwt.get_unverified_header(token).get("alg")
        except jwt.PyJWTError:
            raise SupabaseTokenError("Malformed token header") from None

        if algorithm in _SYMMETRIC_ALGORITHMS:
            if self._secret is None:
                raise SupabaseTokenError(f"No shared secret configured for {algorithm}")
            key: Any = self._secret
            algorithms = [algorithm]
        elif algorithm in _ASYMMETRIC_ALGORITHMS:
            if self._jwks_client is None:
                raise SupabaseTokenError(f"No JWKS endpoint configured for {algorithm}")
            try:
                key = self._jwks_client.get_signing_key_from_jwt(token).key
            except jwt.PyJWTError as error:
                # 네트워크 실패와 모르는 kid 가 같은 예외로 온다. 둘 다
                # 사용자에게는 "인증 실패" 지만 운영자는 구분해야 해서 로그에
                # 사유를 남긴다.
                logger.warning("Supabase JWKS lookup failed: %s", error)
                raise SupabaseTokenError("Signing key is unavailable") from None
            algorithms = [algorithm]
        else:
            # alg=none 을 포함해 모르는 알고리즘은 전부 거절한다.
            raise SupabaseTokenError(f"Unsupported token algorithm: {algorithm!r}")

        try:
            return jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["sub", "exp"],
                    "verify_aud": self._audience is not None,
                    "verify_iss": self._issuer is not None,
                },
            )
        except jwt.PyJWTError as error:
            logger.info("Supabase token rejected: %s", error)
            raise SupabaseTokenError("Token is not valid") from None


def looks_like_supabase_token(token: str) -> bool:
    """자체 발급 토큰과 Supabase 토큰을 구분한다.

    검증이 아니라 **어느 검증기로 보낼지** 고르는 판단이다. 서명은 그 뒤에
    각자 확인하므로, 여기서 iss 를 위조해 봐야 통과할 수 있는 것은 없다.

    자체 발급 토큰에는 iss 가 없고 pwd 클레임이 있다. Supabase 토큰에는 항상
    `.../auth/v1` 형태의 iss 가 있다.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return False
    issuer = claims.get("iss")
    return isinstance(issuer, str) and "/auth/v" in issuer
