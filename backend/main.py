from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.envelope import apply_envelope_to_openapi, error_body
from app.api.request_body_limit import RequestBodyLimitMiddleware
from app.api.router import router
from app.core.config import Settings
from app.core.upload_limits import MAX_MULTIPART_BODY_BYTES
from app.db.session import create_database_engine
from app.infrastructure.in_process_job_dispatcher import InProcessJobDispatcher
from app.infrastructure.local_object_storage import LocalObjectStorage
from app.infrastructure.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.openai_llm_client import OpenAILLMClient
from app.infrastructure.reportlab_pdf_renderer import ReportLabPdfRenderer
from app.parsers.hwp_parser import RhwpDocumentParser
from app.ports.document_parser import DocumentParser
from app.ports.embedding_client import EmbeddingClient
from app.ports.llm_client import LLMClient
from app.ports.pdf_renderer import PdfRenderer
from app.ports.mail_sender import MailSender
from app.infrastructure.smtp_mail_sender import SmtpMailSender
from app.services.password_reset import ResetRateLimiter
from app.services.analysis_pipeline import run_analysis_pipeline


_LLM_FROM_SETTINGS = object()
_EMBEDDING_FROM_SETTINGS = object()


def create_app(
    settings: Settings | None = None,
    document_parser: DocumentParser | None = None,
    llm_client: LLMClient | None | object = _LLM_FROM_SETTINGS,
    embedding_client: EmbeddingClient | None | object = _EMBEDDING_FROM_SETTINGS,
    pdf_renderer: PdfRenderer | None = None,
    mail_sender: MailSender | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(
            str(runtime_settings.database_url),
            runtime_settings.database_connect_timeout_seconds,
        )
        application.state.database_engine = engine
        application.state.settings = runtime_settings
        active_mail_sender = mail_sender
        if active_mail_sender is None and all((
            runtime_settings.smtp_host, runtime_settings.smtp_username,
            runtime_settings.smtp_password, runtime_settings.smtp_from_email,
            runtime_settings.password_reset_url,
        )):
            active_mail_sender = SmtpMailSender(
                host=runtime_settings.smtp_host,
                port=runtime_settings.smtp_port,
                username=runtime_settings.smtp_username,
                password=runtime_settings.smtp_password.get_secret_value(),
                from_email=runtime_settings.smtp_from_email,
            )
        application.state.mail_sender = active_mail_sender
        application.state.password_reset_limiter = ResetRateLimiter()
        object_storage = LocalObjectStorage(
            runtime_settings.local_storage_root
        )
        active_parser = document_parser or RhwpDocumentParser()
        active_pdf_renderer = pdf_renderer or ReportLabPdfRenderer()
        if llm_client is _LLM_FROM_SETTINGS:
            active_llm_client: LLMClient | None = None
            if runtime_settings.openai_api_key is not None:
                active_llm_client = OpenAILLMClient(
                    api_key=runtime_settings.openai_api_key.get_secret_value(),
                    base_url=str(runtime_settings.openai_base_url),
                    model_profiles={
                        profile: profile
                        for profile in {
                        runtime_settings.cpl_model_profile,
                        runtime_settings.fit_model_profile,
                        runtime_settings.sim_model_profile,
                        runtime_settings.chat_model_profile,
                    }
                    },
                    timeout_seconds=runtime_settings.cpl_llm_timeout_seconds,
                )
        else:
            active_llm_client = cast(LLMClient | None, llm_client)
        if embedding_client is _EMBEDDING_FROM_SETTINGS:
            active_embedding_client: EmbeddingClient | None = None
            if runtime_settings.openai_api_key is not None:
                active_embedding_client = OpenAIEmbeddingClient(
                    api_key=runtime_settings.openai_api_key.get_secret_value(),
                    base_url=str(runtime_settings.openai_base_url),
                    model_name=runtime_settings.embedding_model_name,
                    timeout_seconds=runtime_settings.embedding_timeout_seconds,
                )
        else:
            active_embedding_client = cast(
                EmbeddingClient | None,
                embedding_client,
            )
        dispatcher = InProcessJobDispatcher(
            lambda case_id: run_analysis_pipeline(
                engine,
                object_storage,
                active_parser,
                active_llm_client,
                runtime_settings,
                case_id,
                pdf_renderer=active_pdf_renderer,
                embedding_client=active_embedding_client,
            )
        )
        application.state.object_storage = object_storage
        application.state.document_parser = active_parser
        application.state.llm_client = active_llm_client
        application.state.embedding_client = active_embedding_client
        application.state.pdf_renderer = active_pdf_renderer
        application.state.job_dispatcher = dispatcher
        try:
            yield
        finally:
            await dispatcher.shutdown()
            engine.dispose()

    application = FastAPI(
        title="Pre-review API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=MAX_MULTIPART_BODY_BYTES,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.cors_allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
        expose_headers=["Content-Disposition", "Retry-After"],
    )

    # 비밀번호 흐름의 응답은 캐시에 남기지 않는다.
    _NO_STORE_PATHS = {
        "/api/v1/auth/password-reset/request",
        "/api/v1/auth/password-reset/confirm",
    }

    def _no_store(request: Request) -> dict[str, str]:
        return (
            {"Cache-Control": "no-store"}
            if request.url.path in _NO_STORE_PATHS
            else {}
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        # 입력 원문(input)과 내부 문맥(ctx)은 오류에 다시 담지 않는다.
        safe_errors = [
            {
                key: value
                for key, value in item.items()
                if key not in ("input", "ctx")
            }
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(error_body(422, safe_errors)),
            headers=_no_store(request),
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        headers = dict(error.headers or {})
        headers.update(_no_store(request))
        return JSONResponse(
            status_code=error.status_code,
            content=jsonable_encoder(error_body(error.status_code, error.detail)),
            headers=headers,
        )

    application.include_router(router)
    # 응답을 라우터 바깥에서 감싸므로 OpenAPI 도 같은 모양을 말하게 맞춘다.
    apply_envelope_to_openapi(application)
    return application


app = create_app()
