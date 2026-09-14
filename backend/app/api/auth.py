"""Authentication primitives shared by the public API routes.

The application never mints, persists, logs, or returns Supabase session
tokens. In production they arrive only in the two HttpOnly cookies managed by
``app.api.v1.auth``. The explicit development-header path remains so offline
pipeline contract tests do not need an identity provider.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.responses import Response
from fastapi.security import APIKeyCookie


ACCESS_COOKIE = "pre_review_access"
REFRESH_COOKIE = "pre_review_refresh"
RECOVERY_VERIFIER_COOKIE = "pre_review_recovery_verifier"
RECOVERY_COOKIE_PATH = "/api/v1/auth/password-recovery"
DEV_USER_HEADER = "X-PreReview-Dev-User"
DEV_ROLE_HEADER = "X-PreReview-Dev-Role"
COOKIE_SAME_SITE = {"lax", "strict", "none"}
# The refresh cookie is only ever read by ``POST /api/v1/auth/refresh``. Scoping
# its ``Path`` this narrowly means browsers never attach it to business API
# calls, matching the "refresh cookie는 auth 경로로 scope를 좁히고 업무 API에는
# 전송하지 않는다" contract. The access cookie stays at "/" because every
# business route needs it.
REFRESH_COOKIE_PATH = "/api/v1/auth"
# v0.1 issued the same refresh-cookie name at the site root.  A browser can
# retain that credential across the v0.2 path migration, producing two Cookie
# header entries on auth routes.  Keep the old path explicit until every
# session-setting and session-clearing response has expired it.
LEGACY_REFRESH_COOKIE_PATH = "/"

# These schemes are documentation dependencies.  ``auto_error=False`` keeps
# the existing runtime boundary intact: online requests are validated by
# ``supabase_principal`` and offline contract tests use explicit dev headers.
# They nevertheless tell OpenAPI generators that production browser requests
# carry HttpOnly cookies, never a bearer token.
access_cookie_scheme = APIKeyCookie(
    name=ACCESS_COOKIE,
    scheme_name="PreReviewAccessCookie",
    description=(
        "HttpOnly access-token cookie set by POST /api/v1/auth/sign-in or "
        "POST /api/v1/auth/refresh. Browser clients must use credentials: include."
    ),
    auto_error=False,
)
refresh_cookie_scheme = APIKeyCookie(
    name=REFRESH_COOKIE,
    scheme_name="PreReviewRefreshCookie",
    description=(
        "HttpOnly refresh-token cookie set with the access cookie. Used only "
        "by POST /api/v1/auth/refresh."
    ),
    auto_error=False,
)


recovery_cookie_scheme = APIKeyCookie(
    name=RECOVERY_VERIFIER_COOKIE,
    scheme_name="PreReviewRecoveryVerifierCookie",
    description=(
        "Short-lived HttpOnly PKCE verifier cookie set by POST "
        "/api/v1/auth/password-reset and consumed by POST "
        "/api/v1/auth/password-recovery/exchange."
    ),
    auto_error=False,
)

DISPLAY_NAME_MAX_LENGTH = 100


class InvalidAuthSession(RuntimeError):
    """The provider proved that the browser's stored session is invalid."""


def normalize_display_name(value: str) -> str:
    """Apply the shared NFC/trim/length/control-character contract.

    Raises ``ValueError`` when ``value`` cannot become a valid display name.
    Request-side validators (sign-up) should let that error surface as a 422.
    The Auth response boundary instead falls back to the email local-part;
    see :func:`display_name_from_metadata`.
    """

    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > DISPLAY_NAME_MAX_LENGTH:
        raise ValueError("display_name must be 1-100 characters after trimming")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("display_name must not contain control characters")
    return normalized


def email_local_part(email: str) -> str:
    """Fallback identity label used when no valid display_name is stored."""

    local_part = email.split("@", 1)[0].strip()
    return local_part or email


def display_name_from_metadata(metadata: object, *, email: str) -> str:
    """Read Supabase ``user_metadata.display_name`` with a safe fallback.

    Any shape or content that cannot satisfy the display_name contract —
    missing, non-string, blank after trim, too long, or containing control
    characters — falls back to the email local-part instead of rejecting the
    response. Legacy or externally-edited provider metadata must not break
    the Auth read boundary.
    """

    candidate = metadata.get("display_name") if isinstance(metadata, dict) else None
    if not isinstance(candidate, str):
        return email_local_part(email)
    try:
        return normalize_display_name(candidate)
    except ValueError:
        return email_local_part(email)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True, slots=True)
class AuthCookieConfig:
    """Validated cookie settings, supplied through application state."""

    secure: bool
    samesite: Literal["lax", "strict", "none"]
    domain: str | None
    refresh_max_age: int

    @classmethod
    def from_request(cls, request: Request) -> "AuthCookieConfig":
        raw_samesite = str(getattr(request.app.state, "auth_cookie_samesite", "lax")).lower()
        if raw_samesite not in COOKIE_SAME_SITE:
            raise HTTPException(status_code=503, detail="Authentication cookie configuration is invalid")
        secure = bool(getattr(request.app.state, "auth_cookie_secure", True))
        # Modern browsers reject SameSite=None cookies without Secure. Refuse
        # to create a weaker or silently broken cookie configuration.
        if raw_samesite == "none" and not secure:
            raise HTTPException(status_code=503, detail="Authentication cookie configuration is invalid")
        raw_domain = str(getattr(request.app.state, "auth_cookie_domain", "") or "").strip()
        if raw_domain and any(character in raw_domain for character in "/:@\t\r\n"):
            raise HTTPException(status_code=503, detail="Authentication cookie configuration is invalid")
        refresh_max_age = int(getattr(request.app.state, "auth_refresh_cookie_max_age", 60 * 60 * 24 * 30))
        if refresh_max_age <= 0:
            raise HTTPException(status_code=503, detail="Authentication cookie configuration is invalid")
        return cls(secure=secure, samesite=raw_samesite, domain=raw_domain or None, refresh_max_age=refresh_max_age)

    def attributes(self, *, max_age: int | None = None, path: str = "/") -> dict[str, object]:
        attributes: dict[str, object] = {
            "httponly": True,
            "secure": self.secure,
            "samesite": self.samesite,
            "path": path,
        }
        if self.domain:
            attributes["domain"] = self.domain
        if max_age is not None:
            attributes["max_age"] = max_age
        return attributes


def parse_allowed_origins(value: str) -> frozenset[str]:
    """Parse a comma-separated origin allow-list without accepting paths."""

    parsed: set[str] = set()
    for candidate in value.split(","):
        origin = candidate.strip()
        if not origin:
            continue
        components = urlsplit(origin)
        if (
            components.scheme not in {"http", "https"}
            or not components.netloc
            or components.path not in {"", "/"}
            or components.query
            or components.fragment
            or components.username
            or components.password
        ):
            raise ValueError("PREREVIEW_AUTH_ALLOWED_ORIGINS must contain origins only")
        parsed.add(f"{components.scheme}://{components.netloc}".lower())
    return frozenset(parsed)


def require_trusted_origin(request: Request) -> None:
    """Reject state-changing auth requests without an explicit allow-listed Origin."""

    origin = request.headers.get("origin", "")
    components = urlsplit(origin)
    normalized = f"{components.scheme}://{components.netloc}".lower()
    allowed = getattr(request.app.state, "auth_allowed_origins", frozenset())
    if (
        not origin
        or components.scheme not in {"http", "https"}
        or not components.netloc
        or components.path not in {"", "/"}
        or components.query
        or components.fragment
        or normalized not in allowed
    ):
        # Missing Origin is intentionally not treated as same-origin; accepting
        # it would make cookie-authenticated endpoints CSRF-bypassable.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Untrusted request origin")


class SupabaseAuthClient:
    """Small injectable HTTP boundary around Supabase Auth.

    ``app.state.supabase_auth_transport`` may hold an ``httpx.MockTransport``
    in tests. Production leaves it unset and uses normal network transport.
    """

    def __init__(self, request: Request) -> None:
        self._base_url = str(getattr(request.app.state, "supabase_url", "")).rstrip("/")
        self._anon_key = str(getattr(request.app.state, "supabase_anon_key", ""))
        self._transport = getattr(request.app.state, "supabase_auth_transport", None)

    async def request(self, method: str, path: str, *, token: str | None = None, json: dict[str, str] | None = None, params: dict[str, str] | None = None) -> httpx.Response:
        if not self._base_url or not self._anon_key:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Supabase authentication is not configured")
        headers = {"apikey": self._anon_key}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=10.0, follow_redirects=False) as client:
                return await client.request(method, f"{self._base_url}/auth/v1{path}", headers=headers, json=json, params=params)
        except httpx.HTTPError as exc:
            # Do not surface provider text: it can contain tokens or account data.
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Supabase authentication is unavailable") from exc


async def supabase_client(request: Request) -> SupabaseAuthClient:
    return SupabaseAuthClient(request)


SupabaseClientDep = Annotated[SupabaseAuthClient, Depends(supabase_client)]


def user_from_payload(payload: object) -> Principal:
    if not isinstance(payload, dict):
        raise ValueError("Supabase user identity is invalid")
    user_id = payload.get("id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("Supabase user identity is invalid")
    metadata = payload.get("app_metadata")
    role = "admin" if isinstance(metadata, dict) and metadata.get("role") == "admin" else "user"
    return Principal(user_id=user_id, role=role)


async def offline_principal(
    request: Request,
    user_id: Annotated[
        str | None,
        Header(alias=DEV_USER_HEADER, include_in_schema=False),
    ] = None,
    role: Annotated[
        str | None,
        Header(alias=DEV_ROLE_HEADER, include_in_schema=False),
    ] = None,
) -> Principal:
    """Use explicit headers only in offline mode; use the access cookie otherwise."""

    if not getattr(request.app.state, "offline_mode", False):
        return await supabase_principal(request)
    if not user_id or not user_id.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"{DEV_USER_HEADER} is required in offline development")
    normalized_role = (role or "user").strip().lower()
    if normalized_role not in {"user", "admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Development role must be user or admin")
    return Principal(user_id=user_id.strip(), role=normalized_role)


async def supabase_principal(request: Request) -> Principal:
    """Validate the HttpOnly access cookie against Supabase Auth.

    Bearer headers are deliberately ignored: browser-facing business APIs share
    the same cookie boundary as the authentication routes.
    """

    token = request.cookies.get(ACCESS_COOKIE, "")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication cookie is required")
    response = await SupabaseAuthClient(request).request("GET", "/user", token=token)
    if response.status_code in {400, 401, 403}:
        raise InvalidAuthSession("Invalid or expired authentication cookie")
    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Authentication validation was rate limited",
        )
    if response.status_code >= 500:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase authentication is unavailable",
        )
    if response.status_code != status.HTTP_200_OK:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Supabase authentication returned an unexpected response",
        )
    try:
        return user_from_payload(response.json())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Supabase authentication returned an invalid user",
        ) from exc


def clear_session_cookies(response: Response, request: Request) -> None:
    """Delete the access credential and every deployed refresh-cookie scope."""

    config = AuthCookieConfig.from_request(request)
    response.delete_cookie(ACCESS_COOKIE, **config.attributes())
    response.delete_cookie(
        REFRESH_COOKIE,
        **config.attributes(path=REFRESH_COOKIE_PATH),
    )
    response.delete_cookie(
        REFRESH_COOKIE,
        **config.attributes(path=LEGACY_REFRESH_COOKIE_PATH),
    )


async def require_admin(principal: Annotated[Principal, Depends(offline_principal)]) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role is required")
    return principal


PrincipalDep = Annotated[Principal, Depends(offline_principal)]
AdminDep = Annotated[Principal, Depends(require_admin)]
