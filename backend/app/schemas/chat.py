"""Chat API 가 주고받는 모양.

답변 계약(`ChatAnswer`·`ChatReference`)은 여기 없다. 챗봇 로직이라 `chatmessage`
패키지가 갖고, 여기서는 다시 내보내기만 한다 — 두 곳에 같은 계약을 두면 어느
쪽이 정답인지 아무도 모른다.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.chat_bridge import ChatAnswer, ChatReference, ChatReferenceAgent


__all__ = [
    "ChatAnswer",
    "ChatMessageRequest",
    "ChatMessageResponse",
    "ChatMessagesResponse",
    "ChatReference",
    "ChatReferenceAgent",
    "ChatTurnResponse",
]


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=4_000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Chat question must not be blank")
        return value


class ChatMessageResponse(BaseModel):
    """말풍선 하나.

    **POST 응답과 GET 이력이 같은 모양이어야 한다.** 새로 물었을 때만 근거가
    보이고 새로고침하면 사라지면, 같은 대화가 화면에서 다르게 그려진다. 그래서
    근거와 수정 제안을 DB 에 저장하고 두 API 가 같이 돌려준다.

    모델명·토큰 수는 내부 운영 정보라 담지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    role: Literal["USER", "ASSISTANT"]
    content: str = Field(min_length=1)
    # 답변에만 붙는다. 사용자 질문 행은 항상 빈 배열과 None 이다.
    references: list[ChatReference] = Field(default_factory=list)
    suggested_revision: str | None = None


class ChatMessagesResponse(BaseModel):
    """최근 대화부터 한 쪽씩 준다.

    화면은 시간순으로 그리고, 위로 스크롤하면 이전 대화를 더 불러온다.
    이력 목록과 방향이 반대다.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessageResponse]
    next_cursor: str | None = None


class ChatTurnResponse(BaseModel):
    """질문 한 번의 결과 — 말풍선 둘.

    말풍선 하나만 돌려주지 않는다. 사용자 질문 행의 id 가 있어야 화면이 낙관적
    으로 그려 둔 말풍선을 서버 행으로 바꿔 놓을 수 있다. 각 말풍선의 모양은
    GET 이력과 같다.
    """

    model_config = ConfigDict(extra="forbid")

    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
