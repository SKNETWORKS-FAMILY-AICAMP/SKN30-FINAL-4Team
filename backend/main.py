from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.request_body_limit import RequestBodyLimitMiddleware
from app.api.router import router
from app.core.config import Settings
from app.core.upload_limits import MAX_MULTIPART_BODY_BYTES
from app.db.session import create_database_engine
from app.infrastructure.in_process_job_dispatcher import InProcessJobDispatcher
from app.infrastructure.local_object_storage import LocalObjectStorage
from app.infrastructure.openai_llm_client import OpenAILLMClient
from app.parsers.hwp_parser import RhwpDocumentParser
from app.ports.document_parser import DocumentParser
from app.ports.llm_client import LLMClient
from app.services.analysis_pipeline import run_analysis_pipeline


_LLM_FROM_SETTINGS = object()


def create_app(
    settings: Settings | None = None,
    document_parser: DocumentParser | None = None,
    llm_client: LLMClient | None | object = _LLM_FROM_SETTINGS,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_settings = settings or Settings()
        engine = create_database_engine(
            str(runtime_settings.database_url),
            runtime_settings.database_connect_timeout_seconds,
        )
        application.state.database_engine = engine
        application.state.settings = runtime_settings
        object_storage = LocalObjectStorage(
            runtime_settings.local_storage_root
        )
        active_parser = document_parser or RhwpDocumentParser()
        if llm_client is _LLM_FROM_SETTINGS:
            active_llm_client: LLMClient | None = None
            if runtime_settings.openai_api_key is not None:
                active_llm_client = OpenAILLMClient(
                    api_key=runtime_settings.openai_api_key.get_secret_value(),
                    base_url=str(runtime_settings.openai_base_url),
                    model_profiles={
                        runtime_settings.cpl_model_profile: (
                            runtime_settings.cpl_model_profile
                        )
                    },
                    timeout_seconds=runtime_settings.cpl_llm_timeout_seconds,
                )
        else:
            active_llm_client = cast(LLMClient | None, llm_client)
        dispatcher = InProcessJobDispatcher(
            lambda case_id: run_analysis_pipeline(
                engine,
                object_storage,
                active_parser,
                active_llm_client,
                runtime_settings,
                case_id,
            )
        )
        application.state.object_storage = object_storage
        application.state.document_parser = active_parser
        application.state.llm_client = active_llm_client
        application.state.job_dispatcher = dispatcher
        try:
            yield
        finally:
            await dispatcher.shutdown()
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
