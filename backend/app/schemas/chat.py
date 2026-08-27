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
    model_config = ConfigDict(extra="forbid")

    id: int
    sequence_no: int = Field(ge=1)
    role: Literal["USER", "ASSISTANT"]
    content: str = Field(min_length=1)
    model_name: str | None = None
    model_version: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    evidence_refs: list[str]
    created_at: datetime


class ChatMessagesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: int
    chat_session_id: int | None = None
    messages: list[ChatMessageResponse]


class ChatTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: int
    chat_session_id: int
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
