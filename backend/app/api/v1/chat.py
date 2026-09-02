from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import CurrentUser
from app.api.v1.responses import (
    BAD_GATEWAY,
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


router = APIRouter(prefix="/api/v1/cases", tags=["chat"], responses=UNAUTHORIZED)


@router.get(
    "/{case_id}/chat/messages",
    response_model=ChatMessagesResponse,
    responses={**NOT_FOUND, **CONFLICT},
)
def chat_messages(
    case_id: int,
    request: Request,
    user: CurrentUser,
    cursor: int | None = None,
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
    "/{case_id}/chat/messages",
    response_model=ChatTurnResponse,
    responses={**NOT_FOUND, **CONFLICT, **BAD_GATEWAY, **SERVICE_UNAVAILABLE},
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
