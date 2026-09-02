from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.deps import CurrentUser
from app.api.v1.responses import (
    BAD_GATEWAY,
    describe,
    CONFLICT,
    NOT_FOUND,
    SERVICE_UNAVAILABLE,
    UNAUTHORIZED,
)
from app.schemas.chat import (
    ChatMessageRequest,
    ChatMessagesResponse,
    ChatTurnResponse,
)
from app.services.chat import (
    ChatGenerationError,
    ChatNotFoundError,
    ChatNotReadyError,
    answer_chat,
    get_chat_history,
)


router = APIRouter(prefix="/api/v1/cases", tags=["분석"], responses=UNAUTHORIZED)


@router.get(
    "/{case_id}/messages",
    response_model=ChatMessagesResponse,
    summary="이전 대화 불러오기",
    description=(
        "화면을 열 때는 분석 상세 조회가 최근 대화 20개를 함께 준다. "
        "이 API 는 **위로 스크롤해 그보다 앞선 대화**를 볼 때만 쓴다. "
        "응답의 `next_cursor` 를 그대로 `cursor` 에 넣어 다시 호출한다. "
        "메시지는 시간순으로 정렬돼 있고, 이력 목록과 방향이 반대다."
    ),
    responses=describe({**UNAUTHORIZED, **NOT_FOUND, **CONFLICT}),
)
def chat_messages(
    case_id: int,
    request: Request,
    user: CurrentUser,
    cursor: Annotated[
        int | None,
        Query(description="이전 응답의 next_cursor 를 그대로 넣는다"),
    ] = None,
) -> ChatMessagesResponse:
    """이전 대화를 더 불러온다.

    화면을 열 때는 GET /cases/{case_id} 가 최근 대화를 함께 준다. 이 API 는
    위로 스크롤해 그보다 앞선 대화를 볼 때만 쓴다.
    """
    try:
        return get_chat_history(
            request.app.state.database_engine,
            user.id,
            case_id,
            cursor=cursor,
        )
    except ChatNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ChatNotReadyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None


@router.post(
    "/{case_id}/messages",
    response_model=ChatTurnResponse,
    summary="AI 에게 질문",
    description=(
        "분석 결과에 대해 묻고 답을 받는다. 보낸 질문과 받은 답을 함께 돌려주므로 "
        "화면은 두 말풍선을 바로 붙이면 된다. "
        "AI 호출이 실패하면 `502` 또는 `503` 이 나가는데, 사용자에게는 둘 다 "
        "답을 받지 못했다는 뜻이라 화면에서 구분할 필요가 없다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **NOT_FOUND, **CONFLICT, **BAD_GATEWAY, **SERVICE_UNAVAILABLE},
        _409="아직 분석이 끝나지 않았다",
        _502="AI 가 지정한 형식을 지키지 않았다",
        _503="AI 서비스를 쓸 수 없거나 시간이 초과됐다",
    ),
)
async def send_chat_message(
    case_id: int,
    payload: ChatMessageRequest,
    request: Request,
    user: CurrentUser,
) -> ChatTurnResponse:
    try:
        return await answer_chat(
            request.app.state.database_engine,
            request.app.state.llm_client,
            request.app.state.settings,
            owner_user_id=user.id,
            case_id=case_id,
            question=payload.content,
        )
    except ChatNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ChatNotReadyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except ChatGenerationError as error:
        error_status = (
            status.HTTP_502_BAD_GATEWAY
            if error.code == "LLM_INVALID_RESPONSE"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(
            status_code=error_status,
            detail={
                "code": error.code,
                "message": str(error),
            },
        ) from None
