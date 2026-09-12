"""Small public contract for a persisted chat answer."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ChatIntent(StrEnum):
    """Supported question scopes understood by the result chatbot."""

    DOCUMENT = "DOCUMENT"
    MODEL_1 = "MODEL_1"
    MODEL_2 = "MODEL_2"
    MODEL_3 = "MODEL_3"
    REPORT = "REPORT"
    UNKNOWN = "UNKNOWN"


class ChatReference(BaseModel):
    """Internal pointer to a persisted result or evidence item."""

    model_config = ConfigDict(extra="forbid")

    section: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    evidence_id: str | None


class ChatAnswer(BaseModel):
    """The only payload a chat worker needs to persist."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=12_000)
    intent: ChatIntent
    warnings: list[str] = Field(max_length=20)
    references: list[ChatReference] = Field(max_length=30)
