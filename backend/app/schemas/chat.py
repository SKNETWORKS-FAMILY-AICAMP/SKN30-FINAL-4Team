from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatAnswer(BaseModel):
    """The provider-independent, result-grounded answer contract."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=12_000)
    evidence_refs: list[str] = Field(max_length=30)


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=4_000)


class ChatMessageResponse(BaseModel):
    """말풍선 하나에 필요한 것만 담는다.

    모델명·토큰 수·근거 식별자는 내부 운영 정보라 화면으로 내보내지 않는다.
    evidence_refs 는 request:BUDGET:0 같은 내부 값이라 프론트가 해석할 수도
    없다. 답변의 근거를 화면에 보여주기로 하면 그때 되살린다.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    role: Literal["USER", "ASSISTANT"]
    content: str = Field(min_length=1)
    created_at: datetime


class ChatMessagesResponse(BaseModel):
    """최근 대화부터 한 쪽씩 준다.

    화면은 시간순으로 그리고, 위로 스크롤하면 이전 대화를 더 불러온다.
    이력 목록과 방향이 반대다.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessageResponse]
    next_cursor: str | None = None


class ChatTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
