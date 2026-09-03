import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.envelope import error_body
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
from app.services.document_parsing import fail_interrupted_analyses


logger = logging.getLogger(__name__)

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

        # 분석은 이 프로세스 안의 태스크로만 돈다. 새로 뜨는 순간 이전에
        # 진행 중이던 건은 유실된 상태이므로, 영원히 분석 중으로 남지 않게
        # 실패로 정리한다.
        #
        # 기동을 막지 않고 뒤에서 돌린다. DB 가 없어도 서버는 떠야 하고
        # /health/live 는 응답해야 한다. 실패하면 다음 기동에 다시 시도된다.
        async def sweep_interrupted() -> None:
            try:
                interrupted = await asyncio.to_thread(
                    fail_interrupted_analyses, engine
                )
            except Exception as error:
                logger.warning(
                    "끊긴 분석 정리를 건너뜁니다: %s", type(error).__name__
                )
            else:
                if interrupted:
                    logger.warning(
                        "서버 재시작으로 끊긴 분석 %s건을 실패로 정리했습니다",
                        interrupted,
                    )

        sweep_task: asyncio.Task[None] | None = None
        if runtime_settings.sweep_interrupted_analyses_on_startup:
            sweep_task = asyncio.create_task(sweep_interrupted())
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
        if active_mail_sender is None or not runtime_settings.password_reset_url:
            # 사용자에게는 알리지 않기로 했으므로 운영자가 볼 곳은 여기뿐이다.
            logger.warning(
                "SMTP 설정이 없어 비밀번호 재설정 메일이 발송되지 않습니다"
            )
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
            if sweep_task is not None:
                sweep_task.cancel()
            engine.dispose()

    application = FastAPI(
        title="Pre-review API",
        version="1.0.0",
        summary="사전협의 요청서 AI 사전검토 서비스의 백엔드 API",
        description=(
            "이 문서가 프론트와의 계약이다. **어떤 상황에 어떤 상태 코드가 나가는지**가 "
            "계약의 핵심이며, 화면에 띄울 문구는 프론트가 상태 코드를 보고 정한다. "
            "응답의 `message` 는 폴백이자 여기 설명용이다. "
            "성공 응답은 공통 래퍼 없이 데이터만 담고, 실패 응답은 문구 하나만 담는다. "
            "로그인과 비밀번호 재설정을 뺀 모든 API 는 `Authorization: Bearer <토큰>` 이 필요하다."
        ),
        openapi_tags=[
            {
                "name": "인증",
                "description": (
                    "로그인·세션 연장·비밀번호 변경과 재설정. "
                    "세션은 1시간이고 자동으로 늘어나지 않는다. 연장 시점은 화면이 정한다. "
                    "로그아웃 API 는 없다. 저장한 토큰을 지우는 것으로 끝나며 그 토큰은 "
                    "만료까지 유효하다."
                ),
            },
            {
                "name": "분석",
                "description": (
                    "요청서 업로드부터 결과 조회·PDF·AI 질의응답까지. "
                    "업로드가 곧 분석 시작이며 따로 시작하는 API 는 없다. "
                    "진행 상태는 롱폴링으로 확인한다."
                ),
            },
        ],
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
    return application


app = create_app()
