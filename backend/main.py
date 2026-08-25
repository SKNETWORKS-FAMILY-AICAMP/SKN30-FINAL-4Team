from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.request_body_limit import RequestBodyLimitMiddleware
from app.api.router import router
from app.core.config import Settings
from app.core.upload_limits import MAX_MULTIPART_BODY_BYTES
from app.db.session import create_database_engine
from app.infrastructure.local_object_storage import LocalObjectStorage


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_settings = settings or Settings()
        engine = create_database_engine(
            str(runtime_settings.database_url),
            runtime_settings.database_connect_timeout_seconds,
        )
        application.state.database_engine = engine
        application.state.settings = runtime_settings
        application.state.object_storage = LocalObjectStorage(
            runtime_settings.local_storage_root
        )
        try:
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title="SIMS Pre-review API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=MAX_MULTIPART_BODY_BYTES,
    )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        safe_errors = [
            {key: value for key, value in item.items() if key != "input"}
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(safe_errors)},
        )

    application.include_router(router)
    return application


app = create_app()
