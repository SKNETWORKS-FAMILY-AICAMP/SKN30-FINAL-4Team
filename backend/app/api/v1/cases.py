from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.envelope import EnvelopeRoute
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
from app.services.document_parsing import list_cases


router = APIRouter(prefix="/api/v1/cases", tags=["cases"], responses=UNAUTHORIZED, route_class=EnvelopeRoute)


class CreateCaseResponse(BaseModel):
    case_id: int
    status: Literal["UPLOADED"]


class CaseSummaryResponse(BaseModel):
    case_id: int
    title: str | None
    status: Literal["분석 중", "분석 완료", "분석 실패"]
    created_at: datetime


class CaseListResponse(BaseModel):
    cases: list[CaseSummaryResponse]


@router.get("", response_model=CaseListResponse)
def list_case_history(request: Request, user: CurrentUser) -> CaseListResponse:
    return CaseListResponse(
        cases=[
            CaseSummaryResponse(**summary.__dict__)
            for summary in list_cases(request.app.state.database_engine, user.id)
        ]
    )


@router.post(
    "",
    response_model=CreateCaseResponse,
    responses={**BAD_REQUEST, **PAYLOAD_TOO_LARGE, **UNSUPPORTED_MEDIA_TYPE},
)
async def create_case(
    request: Request,
    user: CurrentUser,
    files: Annotated[
        list[UploadFile],
        File(alias="file", description="Exactly one HWP or HWPX request document"),
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

    return CreateCaseResponse(case_id=created.case_id, status=created.status)
