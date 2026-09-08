"""프론트 명세 SCR-004 의 두 Edge Function 을 HTTP 로 노출한다.

경로가 ``/functions/v1/<이름>`` 인 것은 ``supabase.functions.invoke(name)`` 이
그 경로로 POST 하기 때문이다. 프론트 코드를 바꾸지 않고 이 백엔드를 향하게만
해도 같은 호출이 그대로 닿는다.

**실제 Supabase Edge(Deno) 런타임은 아직 없다.** 여기 있는 것은 계약과 판정
로직이고, Edge 가 서면 그 함수는 이 로직을 부르거나 이 로직이 그쪽으로 옮겨
간다. 어느 쪽이든 요청·응답 모양은 이 파일이 고정한다.

인증은 현재 우리 JWT(``CurrentUser``)다. Supabase Auth 전환은 별도 작업이다.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.api.v1.responses import (
    BAD_REQUEST,
    CONFLICT,
    NOT_FOUND,
    PAYLOAD_TOO_LARGE,
    UNAUTHORIZED,
    UNSUPPORTED_MEDIA_TYPE,
    describe,
)
from app.services.analysis_run_upload import (
    AnalysisRunNotFoundError,
    AnalysisRunStateError,
    complete_analysis_upload,
    create_analysis_run,
)
from app.services.case_upload import (
    InvalidUploadError,
    UnsupportedDocumentError,
    UploadTooLargeError,
)


router = APIRouter(
    prefix="/functions/v1", tags=["분석"], responses=UNAUTHORIZED
)


class CreateAnalysisRunRequest(BaseModel):
    """RUN-01 요청. bucket·object_key 는 받지 않는다 — 서버가 정한다."""

    original_filename: str = Field(max_length=255)
    declared_mime_type: str | None = Field(default=None, max_length=255)
    declared_size_bytes: int


class CreateAnalysisRunResponse(BaseModel):
    analysis_run_id: UUID
    bucket: str
    object_key: str


class CompleteUploadRequest(BaseModel):
    """RUN-03 요청. 명세대로 run id 하나뿐이다."""

    analysis_run_id: UUID


class CompleteUploadResponse(BaseModel):
    analysis_run_id: UUID
    status: str


def _upload_http_error(error: InvalidUploadError) -> HTTPException:
    """입력 오류만 문구를 그대로 쓴다. 전부 우리가 쓴 고정 문자열이다."""
    if isinstance(error, UploadTooLargeError):
        return HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(error)
        )
    if isinstance(error, UnsupportedDocumentError):
        return HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(error)
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
    )


@router.post(
    "/edge-analysis-run-create",
    response_model=CreateAnalysisRunResponse,
    summary="분석 작업 생성 및 업로드 경로 발급 (RUN-01)",
    description=(
        "분석 작업을 만들고 요청자 전용 Storage 경로를 발급합니다. "
        "파일은 아직 받지 않으며, 신고한 파일명과 크기만 검사합니다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **BAD_REQUEST, **PAYLOAD_TOO_LARGE, **UNSUPPORTED_MEDIA_TYPE},
        _400="파일 입력을 확인해 주세요.",
        _413="파일이 50MB를 초과합니다.",
        _415="HWP 또는 HWPX 파일을 사용해 주세요.",
    ),
)
def edge_analysis_run_create(
    request: Request,
    user: CurrentUser,
    body: CreateAnalysisRunRequest,
) -> CreateAnalysisRunResponse:
    try:
        created = create_analysis_run(
            request.app.state.database_engine,
            user.id,
            original_filename=body.original_filename,
            declared_mime_type=body.declared_mime_type,
            declared_size_bytes=body.declared_size_bytes,
        )
    except InvalidUploadError as error:
        raise _upload_http_error(error) from None
    return CreateAnalysisRunResponse(
        analysis_run_id=created.analysis_run_id,
        bucket=created.bucket,
        object_key=created.object_key,
    )


@router.post(
    "/edge-analysis-run-complete-upload",
    response_model=CompleteUploadResponse,
    summary="업로드 완료 확인 및 분석 큐 등록 (RUN-03)",
    description=(
        "예약된 경로에 올라온 원본을 다시 읽어 형식과 크기를 검사한 뒤 분석 큐에 "
        "등록합니다. 같은 요청을 다시 보내도 작업은 하나만 등록됩니다."
    ),
    responses=describe(
        {
            **UNAUTHORIZED,
            **BAD_REQUEST,
            **NOT_FOUND,
            **CONFLICT,
            **PAYLOAD_TOO_LARGE,
            **UNSUPPORTED_MEDIA_TYPE,
        },
        _400="업로드된 파일이 신고한 내용과 다릅니다.",
        _404="분석 작업이 없거나 요청자의 것이 아닙니다.",
        _409="업로드된 파일이 없거나 이미 종료된 작업입니다.",
        _413="파일이 50MB를 초과합니다.",
        _415="HWP 또는 HWPX 파일을 사용해 주세요.",
    ),
)
async def edge_analysis_run_complete_upload(
    request: Request,
    user: CurrentUser,
    body: CompleteUploadRequest,
) -> CompleteUploadResponse:
    try:
        completed = await complete_analysis_upload(
            request.app.state.database_engine,
            request.app.state.object_storage,
            request.app.state.job_dispatcher,
            user.id,
            body.analysis_run_id,
        )
    except AnalysisRunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except AnalysisRunStateError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    except InvalidUploadError as error:
        raise _upload_http_error(error) from None
    return CompleteUploadResponse(
        analysis_run_id=completed.analysis_run_id, status=completed.status
    )
