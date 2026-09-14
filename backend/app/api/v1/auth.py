"""Supabase Auth proxy endpoints with an HttpOnly-cookie session boundary."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ..auth import (
    ACCESS_COOKIE,
    LEGACY_REFRESH_COOKIE_PATH,
    REFRESH_COOKIE,
    REFRESH_COOKIE_PATH,
    AuthCookieConfig,
    InvalidAuthSession,
    SupabaseClientDep,
    access_cookie_scheme,
    clear_session_cookies,
    display_name_from_metadata,
    normalize_display_name,
    refresh_cookie_scheme,
    require_trusted_origin,
)
from .openapi_models import error_responses


router = APIRouter(prefix="/auth", tags=["Authentication"])


class CredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(
        min_length=3,
        max_length=254,
        description="로그인에 사용할 이메일 주소",
        examples=["user@example.com"],
    )
    password: SecretStr = Field(
        min_length=1,
        max_length=1024,
        description="로그인 비밀번호. 응답이나 로그에 포함되지 않는다.",
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("A valid email address is required")
        return normalized


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(
        min_length=3,
        max_length=254,
        description="비밀번호 재설정 안내를 받을 이메일 주소",
        examples=["user@example.com"],
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return CredentialsRequest.normalize_email(value)


class UpdatePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(
        min_length=8,
        max_length=1024,
        description="변경할 새 비밀번호. 응답이나 로그에 포함되지 않는다.",
    )


class SignUpRequest(BaseModel):
    """Distinct from :class:`CredentialsRequest`: sign-up always names the user.

    ``display_name`` is required so every new account gets a validated
    ``user_metadata.display_name`` at creation time instead of relying on the
    email-local-part fallback from day one.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(
        min_length=3,
        max_length=254,
        description="가입할 이메일 주소",
        examples=["user@example.com"],
    )
    password: SecretStr = Field(
        min_length=8,
        max_length=1024,
        description="가입에 사용할 비밀번호. 응답이나 로그에 포함되지 않는다.",
    )
    display_name: str = Field(
        min_length=1,
        max_length=100,
        description="화면에 표시할 사용자 이름. Supabase user_metadata에 저장한다.",
        examples=["홍길동"],
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return CredentialsRequest.normalize_email(value)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return normalize_display_name(value)


class AuthUserResponse(BaseModel):
    """The only identity shape returned across the browser Auth boundary."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Supabase Auth 사용자 UUID")
    email: str = Field(description="로그인 이메일", examples=["user@example.com"])
    display_name: str = Field(
        description=(
            "화면 표시 이름. Supabase user_metadata.display_name을 사용하고, 기존 "
            "사용자에게 값이 없으면 이메일 @ 앞부분을 반환한다."
        ),
        examples=["홍길동"],
    )


class AuthUserEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: AuthUserResponse = Field(description="인증된 사용자 공개 정보")


class SignUpResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_confirmation_required: bool = Field(
        description="true이면 이메일 확인 후 별도로 로그인해야 한다."
    )
    # The provider can require confirmation and omit a session/user payload.
    user: AuthUserResponse | None = Field(
        default=None,
        description="가입된 사용자. 이메일 확인 정책에 따라 null일 수 있다.",
    )


class PasswordResetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        description="계정 존재 여부를 드러내지 않는 고정 비밀번호 재설정 안내 문구"
    )


def _public_user(payload: object) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    user = payload.get("user", payload)
    if not isinstance(user, dict):
        return None
    user_id = user.get("id")
    email = user.get("email")
    if not isinstance(user_id, str) or not user_id or not isinstance(email, str) or not email:
        return None
    display_name = display_name_from_metadata(user.get("user_metadata"), email=email)
    return {"id": user_id, "email": email, "display_name": display_name}


def _session(payload: object) -> tuple[str, str, int | None]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid session")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid session")
    expires_in = payload.get("expires_in")
    # Access cookies naturally become session cookies if the provider omits an
    # expiry. Never accept a non-positive or unreasonably large provider value.
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not 0 < expires_in <= 60 * 60 * 24 * 7:
        expires_in = None
    return access, refresh, expires_in


def _set_session_cookies(response: Response, request: Request, payload: object) -> None:
    access, refresh, access_max_age = _session(payload)
    config = AuthCookieConfig.from_request(request)
    response.set_cookie(ACCESS_COOKIE, access, **config.attributes(max_age=access_max_age))
    # v0.1 used Path=/ for this same name.  Expire it whenever a v0.2 session
    # is issued so Starlette cannot select a stale duplicate on the next
    # refresh request.  Security and Domain attributes intentionally come from
    # the same validated configuration as the replacement cookie.
    response.delete_cookie(
        REFRESH_COOKIE,
        **config.attributes(path=LEGACY_REFRESH_COOKIE_PATH),
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        **config.attributes(max_age=config.refresh_max_age, path=REFRESH_COOKIE_PATH),
    )


def _cleared_auth_error(
    request: Request, *, status_code: int, code: str, message: str
) -> JSONResponse:
    """Build an error response that also deletes both session cookies.

    Reserved for an invalid/expired credential: the only outcome where the
    stored session itself is known to be dead. Rate limits, transport
    failures, and malformed upstream payloads are transient and must leave
    the caller's cookies untouched so a still-valid session is not destroyed.
    """

    response = JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
    )
    clear_session_cookies(response, request)
    return response


def _provider_failure(response_status: int, *, invalid_status: int = status.HTTP_401_UNAUTHORIZED) -> None:
    if response_status >= 500:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Supabase authentication is unavailable")
    if response_status in {400, 401, 403}:
        raise HTTPException(status_code=invalid_status, detail="Authentication request was rejected")
    if response_status == 429:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Authentication request was rate limited")
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an unexpected response")


async def trusted_origin(request: Request) -> None:
    require_trusted_origin(request)


TrustedOriginDep = Annotated[None, Depends(trusted_origin)]


@router.post(
    "/sign-in",
    response_model=AuthUserEnvelope,
    summary="로그인",
    description=(
        "Supabase Auth로 이메일과 비밀번호를 검증하고 HttpOnly 세션 Cookie 두 개를 "
        "설정한다. 응답의 user.display_name을 화면 사용자 이름으로 사용한다."
    ),
    responses=error_responses(401, 403, 422, 429, 500, 502, 503),
)
async def sign_in(request: Request, body: CredentialsRequest, _: TrustedOriginDep, supabase: SupabaseClientDep) -> JSONResponse:
    response = await supabase.request(
        "POST",
        "/token",
        params={"grant_type": "password"},
        json={"email": body.email, "password": body.password.get_secret_value()},
    )
    if response.status_code != status.HTTP_200_OK:
        _provider_failure(response.status_code)
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid response") from exc
    user = _public_user(payload)
    if user is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid user")
    result = JSONResponse(status_code=status.HTTP_200_OK, content={"user": user})
    _set_session_cookies(result, request, payload)
    return result


@router.post(
    "/sign-up",
    response_model=SignUpResponse,
    status_code=status.HTTP_201_CREATED,
    summary="회원가입",
    description=(
        "display_name을 Supabase user_metadata에 함께 저장한다. 이메일 확인 정책이 "
        "켜져 있으면 user가 null이고 email_confirmation_required가 true일 수 있다."
    ),
    responses=error_responses(400, 403, 422, 429, 500, 502, 503),
)
async def sign_up(request: Request, body: SignUpRequest, _: TrustedOriginDep, supabase: SupabaseClientDep) -> JSONResponse:
    response = await supabase.request(
        "POST",
        "/signup",
        json={
            "email": body.email,
            "password": body.password.get_secret_value(),
            # Supabase writes the ``data`` object into user_metadata verbatim,
            # which is the DB source of truth (auth.users.raw_user_meta_data).
            "data": {"display_name": body.display_name},
        },
    )
    if response.status_code not in {status.HTTP_200_OK, status.HTTP_201_CREATED}:
        _provider_failure(response.status_code, invalid_status=status.HTTP_400_BAD_REQUEST)
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid response") from exc
    result_payload: dict[str, object] = {"email_confirmation_required": not (isinstance(payload, dict) and payload.get("access_token") and payload.get("refresh_token"))}
    user = _public_user(payload)
    if user:
        result_payload["user"] = user
    result = JSONResponse(status_code=status.HTTP_201_CREATED, content=result_payload)
    if not result_payload["email_confirmation_required"]:
        _set_session_cookies(result, request, payload)
    return result


@router.post(
    "/refresh",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="로그인 세션 갱신",
    description="refresh HttpOnly Cookie로 Supabase 세션을 갱신하고 Cookie를 다시 설정한다.",
    dependencies=[Security(refresh_cookie_scheme)],
    responses=error_responses(401, 403, 422, 429, 500, 502, 503),
)
async def refresh(request: Request, _: TrustedOriginDep, supabase: SupabaseClientDep) -> Response:
    refresh_token = request.cookies.get(REFRESH_COOKIE, "")
    if not refresh_token:
        return _cleared_auth_error(
            request,
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="UNAUTHORIZED",
            message="Refresh cookie is required",
        )
    response = await supabase.request("POST", "/token", params={"grant_type": "refresh_token"}, json={"refresh_token": refresh_token})
    if response.status_code != status.HTTP_200_OK:
        if response.status_code in {400, 401, 403}:
            # Only an invalid/expired credential proves the session is dead.
            return _cleared_auth_error(
                request,
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="UNAUTHORIZED",
                message="Authentication request was rejected",
            )
        # Rate limits, transport failures, and 5xx are transient. Raising
        # instead of clearing cookies preserves a still-valid refresh cookie
        # so the browser can retry rather than being forced to sign in again.
        _provider_failure(response.status_code)
    try:
        payload: Any = response.json()
    except ValueError as exc:
        # A malformed 200 payload is also transient upstream noise; leave the
        # existing cookies alone rather than treating it like an invalid grant.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid response") from exc
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookies(result, request, payload)
    return result


@router.post(
    "/sign-out",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="로그아웃",
    description="Supabase 세션 종료를 요청하고 브라우저의 인증 Cookie를 삭제한다.",
    # Sign-out is intentionally idempotent when the cookie is already absent,
    # so do not declare it as a required security scheme.
    responses=error_responses(403, 422, 500, 503),
)
async def sign_out(request: Request, _: TrustedOriginDep, supabase: SupabaseClientDep) -> Response:
    # Cookie deletion is authoritative locally, including for an already-expired
    # Supabase session. Do not reveal whether a remote session existed.
    access_token = request.cookies.get(ACCESS_COOKIE, "")
    if access_token:
        try:
            response = await supabase.request("POST", "/logout", token=access_token)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
                raise
            # Local logout is authoritative even when the provider cannot be
            # reached.  Return the dependency failure, but attach deletion
            # cookies so a stale browser session is not retained.
            return _cleared_auth_error(
                request,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
                message="Supabase authentication is unavailable",
            )
        if response.status_code >= 500:
            return _cleared_auth_error(
                request,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
                message="Supabase authentication is unavailable",
            )
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(result, request)
    return result


@router.post(
    "/password-reset",
    response_model=PasswordResetResponse,
    summary="비밀번호 재설정 메일 요청",
    responses=error_responses(403, 422, 429, 500, 503),
)
async def password_reset(request: Request, body: PasswordResetRequest, _: TrustedOriginDep, supabase: SupabaseClientDep) -> PasswordResetResponse:
    payload = {"email": body.email}
    redirect_to = str(getattr(request.app.state, "auth_password_reset_redirect_to", "") or "").strip()
    params = {"redirect_to": redirect_to} if redirect_to else None
    provider_response = await supabase.request(
        "POST", "/recover", json=payload, params=params
    )
    if provider_response.status_code >= 500:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Supabase authentication is unavailable")
    if provider_response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        _provider_failure(provider_response.status_code)
    # Supabase normally returns 200 for unknown accounts. Treat every provider
    # account-related 4xx identically to preserve that non-enumeration boundary.
    return PasswordResetResponse(
        message="If an account exists, password reset instructions have been sent."
    )


@router.post(
    "/update-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="로그인 사용자의 비밀번호 변경",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 422, 429, 500, 502, 503),
)
async def update_password(request: Request, body: UpdatePasswordRequest, _: TrustedOriginDep, supabase: SupabaseClientDep) -> Response:
    access_token = request.cookies.get(ACCESS_COOKIE, "")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication cookie is required")
    response = await supabase.request("PUT", "/user", token=access_token, json={"password": body.password.get_secret_value()})
    if response.status_code in {400, 401, 403}:
        raise InvalidAuthSession("Invalid or expired authentication cookie")
    if response.status_code != status.HTTP_200_OK:
        _provider_failure(response.status_code)
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    # Some Supabase configurations rotate sessions after a password change.
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("access_token") and payload.get("refresh_token"):
        _set_session_cookies(result, request, payload)
    return result


@router.get(
    "/me",
    response_model=AuthUserEnvelope,
    summary="현재 로그인 사용자 조회",
    description=(
        "HttpOnly access Cookie를 검증하고 id, email, display_name을 반환한다. "
        "프론트는 사용자 이름을 별도 DB에서 조회하지 않는다."
    ),
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 429, 500, 502, 503),
)
async def me(request: Request, supabase: SupabaseClientDep) -> AuthUserEnvelope:
    access_token = request.cookies.get(ACCESS_COOKIE, "")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication cookie is required")
    provider_response = await supabase.request("GET", "/user", token=access_token)
    if provider_response.status_code in {400, 401, 403}:
        raise InvalidAuthSession("Invalid or expired authentication cookie")
    if provider_response.status_code != status.HTTP_200_OK:
        _provider_failure(provider_response.status_code)
    try:
        user = _public_user(provider_response.json())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid response") from exc
    if user is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid user")
    return AuthUserEnvelope(user=AuthUserResponse.model_validate(user))
