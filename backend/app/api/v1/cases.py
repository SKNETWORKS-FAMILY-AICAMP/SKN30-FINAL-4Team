from datetime import datetime
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.deps import CurrentUser
from app.api.v1.responses import (
    BAD_REQUEST,
    describe,
    PAYLOAD_TOO_LARGE,
    UNAUTHORIZED,
    UNSUPPORTED_MEDIA_TYPE,
)
from app.services.case_upload import (
    InvalidUploadError,
    UnsupportedDocumentError,
    UploadTooLargeError,
    create_case_from_upload,
)
from app.services.document_parsing import (
    DEFAULT_HISTORY_LIMIT,
    InvalidCursorError,
    list_cases,
    start_analysis,
)


router = APIRouter(prefix="/api/v1/cases", tags=["분석"], responses=UNAUTHORIZED)


class CreateCaseResponse(BaseModel):
    """업로드가 끝나면 분석이 이미 시작된 상태다."""

    case_id: int


class CaseSummaryResponse(BaseModel):
    case_id: int
    title: str | None
    completed_at: datetime


class CaseListResponse(BaseModel):
    """완료된 분석만 최신순으로 담는다. 상태 필드가 없는 이유다."""

    items: list[CaseSummaryResponse]
    next_cursor: str | None


@router.get(
    "",
    response_model=CaseListResponse,
    summary="분석 이력 목록",
    description=(
        "완료된 분석 이력을 최신순으로 5건씩 조회합니다. "
        "`next_cursor`가 있으면 다음 목록을 조회할 수 있습니다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **BAD_REQUEST},
        _400="cursor 값을 확인해 주세요.",
    ),
)
def list_case_history(
    request: Request,
    user: CurrentUser,
    cursor: Annotated[
        str | None,
        Query(description="이전 응답의 next_cursor를 그대로 넣습니다. 첫 요청은 생략합니다."),
    ] = None,
) -> CaseListResponse:
    try:
        page = list_cases(
            request.app.state.database_engine,
            user.id,
            limit=DEFAULT_HISTORY_LIMIT,
            cursor=cursor,
        )
    except InvalidCursorError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from None
    return CaseListResponse(
        items=[CaseSummaryResponse(**item.__dict__) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.post(
    "",
    response_model=CreateCaseResponse,
    summary="요청서 업로드 및 분석 시작",
    description="HWP 또는 HWPX 요청서 한 건을 업로드하고 분석을 시작합니다. 파일은 최대 50MB까지 업로드할 수 있습니다.",
    responses=describe(
        {**UNAUTHORIZED, **BAD_REQUEST, **PAYLOAD_TOO_LARGE, **UNSUPPORTED_MEDIA_TYPE},
        _400="파일 입력을 확인해 주세요.",
        _413="파일이 50MB를 초과합니다.",
        _415="HWP 또는 HWPX 파일을 사용해 주세요.",
    ),
)
async def create_case(
    request: Request,
    user: CurrentUser,
    files: Annotated[
        list[UploadFile],
        File(
            alias="file",
            description="분석할 HWP 또는 HWPX 요청서 한 건입니다.",
            json_schema_extra={"items": {"type": "string", "format": "binary"}},
        ),
    ],
) -> CreateCaseResponse:
    form = await request.form()
    all_uploads = [
        value
        for _, value in form.multi_items()
        if isinstance(value, StarletteUploadFile)
    ]
    if len(files) != 1 or len(all_uploads) != 1:
        for upload in all_uploads:
            await upload.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exactly one request document is required",
        )

    file = files[0]
    try:
        created = await create_case_from_upload(
            request.app.state.database_engine,
            request.app.state.object_storage,
            user.id,
            file.filename,
            file.file,
        )
    except UploadTooLargeError as error:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(error))
    except UnsupportedDocumentError as error:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(error),
        )
    except InvalidUploadError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))
    finally:
        await file.close()

    # 검증을 통과하면 곧바로 분석을 시작한다. 별도의 분석 시작 API 를 두지 않는다.
    await start_analysis(
        request.app.state.database_engine,
        request.app.state.job_dispatcher,
        request.app.state.document_parser,
        user.id,
        created.case_id,
    )
    return CreateCaseResponse(case_id=created.case_id)
