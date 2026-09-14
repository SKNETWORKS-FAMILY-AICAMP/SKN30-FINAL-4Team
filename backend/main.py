"""FastAPI entrypoint for the rebuilt PreReview backend."""

import asyncio
import os
import re
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.auth import InvalidAuthSession, clear_session_cookies, parse_allowed_origins
from app.api.router import router as api_router
from app.api.v1.openapi_models import HealthStatusResponse, error_responses
from app.infrastructure.postgres_conversations import PostgresConversationRepository
from app.infrastructure.postgres_analysis_runs import PostgresAnalysisRunRepository
from app.infrastructure.postgres_results import PostgresResultRepository
from app.infrastructure.supabase_storage import SupabasePrivateObjectStorage
from app.middleware.request_body_limit import RequestBodyLimitMiddleware
from app.services.analysis_runs import AnalysisRunService


DEFAULT_GLOBAL_QUEUE_MAX = 25
MAX_GLOBAL_QUEUE_MAX = 10_000
DEFAULT_UPLOAD_CONCURRENCY = 2
MAX_UPLOAD_CONCURRENCY = 32
_BUILD_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
_BUILD_ID_FILE = Path(__file__).resolve().with_name(".prereview-build-id")


def _validated_build_id(value: str, *, source: str) -> str:
    if _BUILD_ID_PATTERN.fullmatch(value) is None:
        raise RuntimeError(f"{source} must be a 64 character lowercase SHA-256")
    return value


def _build_id_from_artifact() -> str | None:
    """Read the baked image identity without any runtime override.

    Docker builds always create ``.prereview-build-id`` from the deterministic
    Docker context digest. Ordinary host ASGI development has no such file and
    deliberately reports no deployment identity.
    """

    try:
        baked_value = _BUILD_ID_FILE.read_text(encoding="ascii")
    except FileNotFoundError:
        baked_value = None
    except OSError as exc:
        raise RuntimeError("Baked API build identity is unreadable") from exc

    if baked_value is not None:
        # The Dockerfile writes exactly one newline-terminated SHA.  Reject a
        # malformed or unexpectedly edited image artifact rather than masking
        # it with the runtime environment.
        if not baked_value.endswith("\n") or baked_value.count("\n") != 1:
            raise RuntimeError("Baked API build identity is invalid")
        return _validated_build_id(
            baked_value.removesuffix("\n"), source="Baked API build identity"
        )

    return None


def _health_headers(build_id: str | None) -> dict[str, str]:
    return {"X-PreReview-Build-Id": build_id} if build_id is not None else {}


def _global_queue_max_from_environment() -> int:
    """Read the shared admission cap once and reject unsafe deployment input.

    API replicas must use the same value. PostgreSQL serializes admissions;
    this process-local setting is passed into each trusted SQL transaction.
    """

    raw_value = os.getenv("PREREVIEW_GLOBAL_QUEUE_MAX", str(DEFAULT_GLOBAL_QUEUE_MAX))
    try:
        limit = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("PREREVIEW_GLOBAL_QUEUE_MAX must be an integer") from exc
    if not 1 <= limit <= MAX_GLOBAL_QUEUE_MAX:
        raise RuntimeError(
            f"PREREVIEW_GLOBAL_QUEUE_MAX must be between 1 and {MAX_GLOBAL_QUEUE_MAX}"
        )
    return limit


def _upload_concurrency_from_environment() -> int:
    """Bound simultaneous in-memory file assembly and Storage writes."""

    raw_value = os.getenv(
        "PREREVIEW_UPLOAD_CONCURRENCY", str(DEFAULT_UPLOAD_CONCURRENCY)
    )
    try:
        limit = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("PREREVIEW_UPLOAD_CONCURRENCY must be an integer") from exc
    if not 1 <= limit <= MAX_UPLOAD_CONCURRENCY:
        raise RuntimeError(
            "PREREVIEW_UPLOAD_CONCURRENCY must be between "
            f"1 and {MAX_UPLOAD_CONCURRENCY}"
        )
    return limit


def create_app() -> FastAPI:
    app = FastAPI(
        title="PreReview API",
        version="0.2.0",
        description=(
            "프론트엔드가 사용하는 공개 API의 구조·타입·상태값 기준입니다. 모든 업무 API는 "
            "Supabase Auth 기반 HttpOnly Cookie를 사용하므로 브라우저 요청에 "
            "credentials: 'include'를 설정해야 합니다. 별도 Bearer token은 보내지 않습니다."
        ),
    )
    app.state.build_id = _build_id_from_artifact()
    # A missing/unset PREREVIEW_OFFLINE_MODE must fail toward the safer,
    # cookie-only production boundary. Offline dev-header auth is opt-in only
    # (see app.api.auth.offline_principal); it is never the silent default.
    app.state.offline_mode = os.getenv("PREREVIEW_OFFLINE_MODE", "false").lower() in {"1", "true", "yes"}
    app.state.upload_max_bytes = int(os.getenv("PREREVIEW_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
    app.state.request_body_max_bytes = int(
        os.getenv(
            "PREREVIEW_HTTP_MAX_BODY_BYTES",
            str(app.state.upload_max_bytes + 1024 * 1024),
        )
    )
    app.state.upload_concurrency = _upload_concurrency_from_environment()
    app.state.upload_semaphore = asyncio.Semaphore(app.state.upload_concurrency)
    app.state.global_queue_max = _global_queue_max_from_environment()
    app.state.supabase_url = os.getenv("SUPABASE_URL", "")
    app.state.supabase_anon_key = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY", "")
    app.state.supabase_service_role_key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    app.state.database_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL", "")
    # Auth routes are fail-closed unless browser origins are configured.  The
    # auth HTTP transport is injectable so tests never need a network call.
    app.state.supabase_auth_transport = None
    app.state.auth_allowed_origins = parse_allowed_origins(os.getenv("PREREVIEW_AUTH_ALLOWED_ORIGINS", ""))
    app.state.auth_cookie_secure = os.getenv("PREREVIEW_AUTH_COOKIE_SECURE", "false" if app.state.offline_mode else "true").lower() in {"1", "true", "yes"}
    app.state.auth_cookie_samesite = os.getenv("PREREVIEW_AUTH_COOKIE_SAMESITE", "lax")
    app.state.auth_cookie_domain = os.getenv("PREREVIEW_AUTH_COOKIE_DOMAIN", "")
    app.state.auth_refresh_cookie_max_age = int(os.getenv("PREREVIEW_AUTH_REFRESH_COOKIE_MAX_AGE", str(60 * 60 * 24 * 30)))
    app.state.auth_password_reset_redirect_to = os.getenv("PREREVIEW_AUTH_PASSWORD_RESET_REDIRECT_TO", "")
    # HMAC key for opaque history/message pagination cursors (see
    # app.api.v1.cursor). Cursors are bound to owner+endpoint+version, but the
    # signature itself is only as strong as this secret; treat it like any
    # other credential and never let it default in production.
    app.state.cursor_signing_secret = os.getenv("PREREVIEW_CURSOR_SIGNING_SECRET", "")

    app.state.analysis_run_service = None
    app.state.result_repository = None
    app.state.conversation_repository = None
    if not app.state.offline_mode and app.state.database_url:
        app.state.result_repository = PostgresResultRepository(app.state.database_url)
        app.state.conversation_repository = PostgresConversationRepository(
            app.state.database_url,
            global_queue_max=app.state.global_queue_max,
        )
    if (
        not app.state.offline_mode
        and app.state.supabase_url
        and app.state.supabase_service_role_key
        and app.state.database_url
    ):
        app.state.analysis_run_service = AnalysisRunService(
            PostgresAnalysisRunRepository(
                app.state.database_url,
                global_queue_max=app.state.global_queue_max,
            ),
            SupabasePrivateObjectStorage(
                supabase_url=app.state.supabase_url,
                service_role_key=app.state.supabase_service_role_key,
            ),
        )

    # Cross-origin browser calls are allowed only for the same explicit list
    # used by the CSRF Origin gate. Credentials are required for HttpOnly
    # session cookies, so wildcard origins are intentionally unsupported.
    # This is added before CORS so Starlette's reverse wrapping order keeps
    # CORS outside the body limiter; browser clients still receive CORS
    # headers on a rejected upload.
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(app.state.auth_allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        # X-CSRF-Token is not read by any route; the CSRF boundary is the
        # trusted-Origin check (app.api.auth.require_trusted_origin) plus
        # HttpOnly, SameSite cookies. Advertising an unused header here would
        # only widen the browser preflight surface for no benefit.
        allow_headers=["Content-Type", "Idempotency-Key"],
    )

    # Every /api/v1 response is per-user and must never be cached or reused
    # across identities by a shared cache, proxy, or the browser's bfcache.
    # This applies uniformly to auth and business routes rather than each
    # route setting its own headers, so no new endpoint can forget it.
    class PrivateNoStoreMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response: Response = await call_next(request)
            if request.url.path.startswith("/api/v1/"):
                response.headers["Cache-Control"] = "private, no-store"
                existing = [
                    token.strip()
                    for token in response.headers.get("vary", "").split(",")
                    if token.strip()
                ]
                if "Cookie" not in existing:
                    existing.append("Cookie")
                response.headers["Vary"] = ", ".join(existing)
            return response

    app.add_middleware(PrivateNoStoreMiddleware)

    # Status codes with more than one possible cause (currently every 409)
    # must not collapse onto a single guessed code. Routes that can fail for
    # more than one domain reason raise app.api.errors.ApiError explicitly;
    # this handler only falls back to a generic per-status code for routes
    # that raise a bare HTTPException, where the status code is unambiguous.
    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            413: "FILE_TOO_LARGE",
            415: "UNSUPPORTED_FILE_FORMAT",
            422: "VALIDATION_ERROR",
            429: "RATE_LIMITED",
            502: "BAD_GATEWAY",
            503: "SERVICE_UNAVAILABLE",
        }
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        code = getattr(exc, "code", None) or codes.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": code,
                "message": message,
            },
        )

    @app.exception_handler(InvalidAuthSession)
    async def invalid_auth_session_handler(
        request: Request, exc: InvalidAuthSession
    ) -> JSONResponse:
        response = JSONResponse(
            status_code=401,
            content={"code": "UNAUTHORIZED", "message": str(exc)},
        )
        clear_session_cookies(response, request)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's raw errors include the rejected input by default. Never
        # reflect passwords, reset credentials, or other request secrets.
        safe_errors = [{key: value for key, value in item.items() if key not in {"input", "ctx"}} for item in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "errors": jsonable_encoder(safe_errors),
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, __: Exception) -> JSONResponse:
        # Do not leak parser, storage, or provider details across the API boundary.
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL_ERROR", "message": "Internal server error"},
        )

    @app.get(
        "/health/live",
        tags=["Health"],
        response_model=HealthStatusResponse,
        responses=error_responses(500),
    )
    async def live() -> JSONResponse:
        return JSONResponse(
            content={"status": "live", "build_id": app.state.build_id},
            headers=_health_headers(app.state.build_id),
        )

    @app.get(
        "/health/ready",
        tags=["Health"],
        response_model=HealthStatusResponse,
        responses=error_responses(500, 503),
    )
    async def ready() -> JSONResponse:
        configured = (
            not app.state.offline_mode
            and bool(app.state.supabase_url)
            and bool(app.state.supabase_anon_key)
            and bool(app.state.auth_allowed_origins)
            and bool(str(app.state.cursor_signing_secret).strip())
            and app.state.upload_max_bytes > 0
            and app.state.request_body_max_bytes > app.state.upload_max_bytes
            and app.state.upload_concurrency > 0
            and app.state.analysis_run_service is not None
        )
        if configured:
            return JSONResponse(
                status_code=200,
                content={"status": "ready", "build_id": app.state.build_id},
                headers=_health_headers(app.state.build_id),
            )
        return JSONResponse(
            status_code=503,
            content={
                "code": "NOT_READY",
                "message": "Backend dependencies are not configured",
            },
        )

    app.include_router(api_router)
    return app


app = create_app()
