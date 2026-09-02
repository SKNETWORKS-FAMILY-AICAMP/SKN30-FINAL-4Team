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


router = APIRouter(prefix="/api/v1/cases", tags=["cases"], responses=UNAUTHORIZED)


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


@router.get("", response_model=CaseListResponse, responses=BAD_REQUEST)
def list_case_history(
    request: Request,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_LIMIT)] = DEFAULT_HISTORY_LIMIT,
    cursor: str | None = None,
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
    responses={**BAD_REQUEST, **PAYLOAD_TOO_LARGE, **UNSUPPORTED_MEDIA_TYPE},
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
