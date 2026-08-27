from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.api.v1.responses import CONFLICT, NOT_FOUND, UNAUTHORIZED
from app.services.document_parsing import (
    CaseNotFoundError,
    CaseStateConflictError,
    get_case_status,
    start_analysis,
)


router = APIRouter(prefix="/api/v1/cases", tags=["analysis"], responses=UNAUTHORIZED)


class StartAnalysisResponse(BaseModel):
    case_id: int
    job_id: str
    status: Literal["PARSING"]


class CaseStatusResponse(BaseModel):
    case_id: int
    status: Literal["분석 중", "분석 완료", "분석 실패"]
    failure_code: str | None
    failure_message: str | None


@router.post(
    "/{case_id}/analyze",
    response_model=StartAnalysisResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**NOT_FOUND, **CONFLICT},
)
async def analyze_case(
    case_id: int,
    request: Request,
    user: CurrentUser,
) -> StartAnalysisResponse:
    try:
        started = await start_analysis(
            request.app.state.database_engine,
            request.app.state.job_dispatcher,
            request.app.state.document_parser,
            user.id,
            case_id,
        )
    except CaseNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except CaseStateConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return StartAnalysisResponse(**started.__dict__)


@router.get(
    "/{case_id}/status",
    response_model=CaseStatusResponse,
    responses=NOT_FOUND,
)
def case_status(
    case_id: int,
    request: Request,
    user: CurrentUser,
) -> CaseStatusResponse:
    try:
        result = get_case_status(
            request.app.state.database_engine,
            user.id,
            case_id,
        )
    except CaseNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    return CaseStatusResponse(**result.__dict__)
