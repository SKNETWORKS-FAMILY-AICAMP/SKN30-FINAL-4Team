from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.api.v1.responses import NOT_FOUND, UNAUTHORIZED, describe
from app.services.document_parsing import (
    CaseNotFoundError,
    UiStatus,
    wait_for_case_status,
)


router = APIRouter(prefix="/api/v1/cases", tags=["분석"], responses=UNAUTHORIZED)


class CaseStatusResponse(BaseModel):
    """실패 사유는 담지 않는다. 화면은 실패 하나로만 다룬다."""

    case_id: int
    status: UiStatus


@router.get(
    "/{case_id}/status",
    response_model=CaseStatusResponse,
    summary="분석 진행 상태 (롱폴링)",
    description=(
        "**상태가 바뀔 때까지 응답을 붙잡고 있다가 바뀌는 즉시 돌려준다.** "
        "최대 25초 기다리며, 그 안에 변화가 없으면 현재 상태로 응답하므로 다시 호출하면 된다. "
        "짧은 주기로 반복 호출할 필요가 없다. "
        "실패 사유는 담지 않는다. 화면은 실패 하나로만 다룬다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **NOT_FOUND},
        _200="현재 상태. IN_PROGRESS 이면 아직 끝나지 않았으니 다시 호출한다",
    ),
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
