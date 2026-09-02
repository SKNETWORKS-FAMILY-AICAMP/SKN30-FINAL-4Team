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
    MAX_HISTORY_LIMIT,
    list_cases,
    start_analysis,
)


router = APIRouter(prefix="/api/v1/cases", tags=["분석"], responses=UNAUTHORIZED)


class CreateCaseResponse(BaseModel):
    """업로드가 끝나면 분석이 이미 시작된 상태다."""

    case_id: int
    started_at: datetime


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
        "완료된 분석만 최신순으로 한 쪽씩 준다. "
        "**진행 중이거나 실패한 건은 담기지 않는다.** 진행 중인 분석은 업로드할 때 받은 "
        "`case_id` 로 상태를 조회해 복구하고, 실패는 업로드 화면에서만 알린다. "
        "그래서 항목에 상태 필드가 없다. "
        "더보기는 응답의 `next_cursor` 를 그대로 `cursor` 에 넣어 다시 호출한다. "
        "`next_cursor` 가 `null` 이면 마지막 쪽이다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **BAD_REQUEST},
        _400="cursor 값이 서버가 만든 것이 아니다",
    ),
)
def list_case_history(
    request: Request,
    user: CurrentUser,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_HISTORY_LIMIT,
            description="한 번에 받을 개수. 화면은 5를 쓴다",
        ),
    ] = DEFAULT_HISTORY_LIMIT,
    cursor: Annotated[
        str | None,
        Query(description="이전 응답의 next_cursor 를 그대로 넣는다. 첫 쪽은 비운다"),
    ] = None,
) -> CaseListResponse:
    try:
        page = list_cases(
            request.app.state.database_engine,
            user.id,
            limit=limit,
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
    status_code=status.HTTP_202_ACCEPTED,
    summary="요청서 업로드 및 분석 시작",
    description=(
        "사전협의 요청서 한 건을 올린다. **검증을 통과하면 곧바로 분석이 시작된다.** "
        "분석을 따로 시작하는 API 는 없다. "
        "지원 형식은 HWP 와 HWPX 뿐이고 최대 50MB 다. "
        "받은 `case_id` 를 브라우저에 저장해 두면 새로고침해도 진행 상태를 다시 조회해 "
        "대기 화면을 복구할 수 있다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **BAD_REQUEST, **PAYLOAD_TOO_LARGE, **UNSUPPORTED_MEDIA_TYPE},
        _400="파일이 없거나 둘 이상이다",
        _413="50MB 를 넘는다",
        _415="HWP 또는 HWPX 가 아니다",
        _422="파일이 비었거나 확장자와 내용이 다르다",
    ),
)
async def create_case(
    request: Request,
    user: CurrentUser,
    files: Annotated[
        list[UploadFile],
        File(
            alias="file",
            description="Exactly one HWP or HWPX request document",
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
    return CreateCaseResponse(
        case_id=created.case_id,
        started_at=created.created_at,
    )
