"""Supabase Auth proxy endpoints with an HttpOnly-cookie session boundary."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ..auth import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    AuthCookieConfig,
    SupabaseClientDep,
    access_cookie_scheme,
    refresh_cookie_scheme,
    require_trusted_origin,
)
from .openapi_models import error_responses


router = APIRouter(prefix="/auth", tags=["Authentication"])


class CredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("A valid email address is required")
        return normalized


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return CredentialsRequest.normalize_email(value)


class UpdatePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=8, max_length=1024)


class AuthUserResponse(BaseModel):
    """The only identity shape returned across the browser Auth boundary."""

    model_config = ConfigDict(extra="forbid")

    id: str
    email: str


class AuthUserEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: AuthUserResponse


class SignUpResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_confirmation_required: bool
    # The provider can require confirmation and omit a session/user payload.
    user: AuthUserResponse | None = None


class PasswordResetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


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
    return {"id": user_id, "email": email}


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
    response.set_cookie(REFRESH_COOKIE, refresh, **config.attributes(max_age=config.refresh_max_age))


def _clear_session_cookies(response: Response, request: Request) -> None:
    config = AuthCookieConfig.from_request(request)
    response.delete_cookie(ACCESS_COOKIE, **config.attributes())
    response.delete_cookie(REFRESH_COOKIE, **config.attributes())


def _private(response: Response) -> Response:
    """Prevent browsers and intermediaries from caching session responses."""

    response.headers["Cache-Control"] = "no-store"
    return response


def _cleared_auth_error(
    request: Request, *, status_code: int, code: str, message: str
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
    )
    _clear_session_cookies(response, request)
    return _private(response)


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
    return _private(result)


@router.post(
    "/sign-up",
    response_model=SignUpResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 403, 422, 429, 500, 502, 503),
)
async def sign_up(request: Request, body: CredentialsRequest, _: TrustedOriginDep, supabase: SupabaseClientDep) -> JSONResponse:
    response = await supabase.request(
        "POST",
        "/signup",
        json={"email": body.email, "password": body.password.get_secret_value()},
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
    return _private(result)


@router.post(
    "/refresh",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Security(refresh_cookie_scheme)],
    responses=error_responses(401, 403, 422, 429, 500, 502, 503),
)
async def refresh(request: Request, _: TrustedOriginDep, supabase: SupabaseClientDep) -> Response:
    refresh_token = request.cookies.get(REFRESH_COOKIE, "")
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    if not refresh_token:
        return _cleared_auth_error(
            request,
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="UNAUTHORIZED",
            message="Refresh cookie is required",
        )
    response = await supabase.request("POST", "/token", params={"grant_type": "refresh_token"}, json={"refresh_token": refresh_token})
    if response.status_code != status.HTTP_200_OK:
        if response.status_code >= 500:
            return _cleared_auth_error(
                request,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
                message="Supabase authentication is unavailable",
            )
        if response.status_code == 429:
            return _cleared_auth_error(
                request,
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="HTTP_ERROR",
                message="Authentication request was rate limited",
            )
        return _cleared_auth_error(
            request,
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="UNAUTHORIZED",
            message="Authentication request was rejected",
        )
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid response") from exc
    _set_session_cookies(result, request, payload)
    return _private(result)


@router.post(
    "/sign-out",
    status_code=status.HTTP_204_NO_CONTENT,
    # Sign-out is intentionally idempotent when the cookie is already absent,
    # so do not declare it as a required security scheme.
    responses=error_responses(403, 422, 500, 503),
)
async def sign_out(request: Request, _: TrustedOriginDep, supabase: SupabaseClientDep) -> Response:
    # Cookie deletion is authoritative locally, including for an already-expired
    # Supabase session. Do not reveal whether a remote session existed.
    access_token = request.cookies.get(ACCESS_COOKIE, "")
    if access_token:
        response = await supabase.request("POST", "/logout", token=access_token)
        if response.status_code >= 500:
            return _cleared_auth_error(
                request,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
                message="Supabase authentication is unavailable",
            )
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookies(result, request)
    return _private(result)


@router.post(
    "/password-reset",
    response_model=PasswordResetResponse,
    responses=error_responses(403, 422, 500, 503),
)
async def password_reset(request: Request, response: Response, body: PasswordResetRequest, _: TrustedOriginDep, supabase: SupabaseClientDep) -> PasswordResetResponse:
    payload = {"email": body.email}
    redirect_to = str(getattr(request.app.state, "auth_password_reset_redirect_to", "") or "").strip()
    params = {"redirect_to": redirect_to} if redirect_to else None
    provider_response = await supabase.request(
        "POST", "/recover", json=payload, params=params
    )
    if provider_response.status_code >= 500:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Supabase authentication is unavailable")
    # Supabase normally returns 200 for unknown accounts. Treat every provider
    # 4xx identically to preserve that non-enumeration boundary.
    response.headers["Cache-Control"] = "no-store"
    return PasswordResetResponse(
        message="If an account exists, password reset instructions have been sent."
    )


@router.post(
    "/update-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 422, 429, 500, 502, 503),
)
async def update_password(request: Request, body: UpdatePasswordRequest, _: TrustedOriginDep, supabase: SupabaseClientDep) -> Response:
    access_token = request.cookies.get(ACCESS_COOKIE, "")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication cookie is required")
    response = await supabase.request("PUT", "/user", token=access_token, json={"password": body.password.get_secret_value()})
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
    return _private(result)


@router.get(
    "/me",
    response_model=AuthUserEnvelope,
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 429, 500, 502, 503),
)
async def me(request: Request, response: Response, supabase: SupabaseClientDep) -> AuthUserEnvelope:
    access_token = request.cookies.get(ACCESS_COOKIE, "")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication cookie is required")
    provider_response = await supabase.request("GET", "/user", token=access_token)
    if provider_response.status_code != status.HTTP_200_OK:
        _provider_failure(provider_response.status_code)
    try:
        user = _public_user(provider_response.json())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid response") from exc
    if user is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Supabase authentication returned an invalid user")
    response.headers["Cache-Control"] = "no-store"
    return AuthUserEnvelope(user=AuthUserResponse.model_validate(user))
