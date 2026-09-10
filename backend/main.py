"""FastAPI entrypoint for the rebuilt PreReview backend."""

import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.auth import parse_allowed_origins
from app.api.router import router as api_router
from app.api.v1.openapi_models import HealthStatusResponse, error_responses
from app.infrastructure.postgres_conversations import PostgresConversationRepository
from app.infrastructure.postgres_analysis_runs import PostgresAnalysisRunRepository
from app.infrastructure.postgres_results import PostgresResultRepository
from app.infrastructure.supabase_storage import SupabasePrivateObjectStorage
from app.services.analysis_runs import AnalysisRunService


def create_app() -> FastAPI:
    app = FastAPI(title="PreReview API", version="0.1.0")
    app.state.offline_mode = os.getenv("PREREVIEW_OFFLINE_MODE", "true").lower() in {"1", "true", "yes"}
    app.state.upload_max_bytes = int(os.getenv("PREREVIEW_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
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

    app.state.analysis_run_service = None
    app.state.result_repository = None
    app.state.conversation_repository = None
    if not app.state.offline_mode and app.state.database_url:
        app.state.result_repository = PostgresResultRepository(app.state.database_url)
        app.state.conversation_repository = PostgresConversationRepository(
            app.state.database_url
        )
    if (
        not app.state.offline_mode
        and app.state.supabase_url
        and app.state.supabase_service_role_key
        and app.state.database_url
    ):
        app.state.analysis_run_service = AnalysisRunService(
            PostgresAnalysisRunRepository(app.state.database_url),
            SupabasePrivateObjectStorage(
                supabase_url=app.state.supabase_url,
                service_role_key=app.state.supabase_service_role_key,
            ),
        )

    # Cross-origin browser calls are allowed only for the same explicit list
    # used by the CSRF Origin gate. Credentials are required for HttpOnly
    # session cookies, so wildcard origins are intentionally unsupported.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(app.state.auth_allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-CSRF-Token"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "ANALYSIS_ALREADY_ACTIVE",
            413: "FILE_TOO_LARGE",
            415: "UNSUPPORTED_FILE_FORMAT",
            422: "VALIDATION_ERROR",
            503: "SERVICE_UNAVAILABLE",
        }
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": codes.get(exc.status_code, "HTTP_ERROR"),
                "message": message,
            },
        )

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
    async def live() -> HealthStatusResponse:
        return HealthStatusResponse(status="live")

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
            and app.state.analysis_run_service is not None
        )
        if configured:
            return JSONResponse(
                status_code=200,
                content={"status": "ready"},
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
