from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.api.v1.responses import NOT_FOUND, UNAUTHORIZED
from app.services.document_parsing import (
    CaseNotFoundError,
    UiStatus,
    wait_for_case_status,
)


router = APIRouter(prefix="/api/v1/cases", tags=["analysis"], responses=UNAUTHORIZED)


class CaseStatusResponse(BaseModel):
    """실패 사유는 담지 않는다. 화면은 실패 하나로만 다룬다."""

    case_id: int
    status: UiStatus


@router.get(
    "/{case_id}/status",
    response_model=CaseStatusResponse,
    responses=NOT_FOUND,
)
async def case_status(
    case_id: int,
    request: Request,
    user: CurrentUser,
) -> CaseStatusResponse:
    """상태가 바뀔 때까지 기다렸다가 바뀌는 즉시 응답한다(롱폴링)."""
    try:
        result = await wait_for_case_status(
            request.app.state.database_engine,
            user.id,
            case_id,
        )
    except CaseNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    return CaseStatusResponse(**result.__dict__)
