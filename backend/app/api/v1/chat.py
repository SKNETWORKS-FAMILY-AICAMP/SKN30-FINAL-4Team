from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.deps import CurrentUser
from app.api.v1.responses import (
    BAD_REQUEST,
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
    description="이전 대화를 20개씩 조회합니다. `next_cursor`가 있으면 더 이전 대화를 조회할 수 있습니다.",
    responses=describe({**UNAUTHORIZED, **NOT_FOUND, **CONFLICT}),
)
def chat_messages(
    case_id: int,
    request: Request,
    user: CurrentUser,
    cursor: Annotated[
        int | None,
        Query(description="이전 응답의 next_cursor를 그대로 넣습니다."),
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
    description="분석 결과에 대해 질문하고 AI 답변을 받습니다.",
    responses=describe(
        {**UNAUTHORIZED, **BAD_REQUEST, **NOT_FOUND, **CONFLICT, **SERVICE_UNAVAILABLE},
        _400="질문 입력을 확인해 주세요.",
        _409="아직 분석이 완료되지 않았습니다.",
        _503="AI 서비스를 사용할 수 없습니다.",
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": error.code,
                "message": str(error),
            },
        ) from None
