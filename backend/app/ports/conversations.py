"""Application boundary for the asynchronous result-grounded chat API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


class ConversationError(RuntimeError):
    """Base class for expected chat persistence failures."""


class ConversationNotFound(ConversationError):
    """The case, session, or message is not visible to this owner."""


class ConversationConflict(ConversationError):
    """The requested chat transition is not valid for the current state."""


class ConversationRetryExhausted(ConversationConflict):
    """The bounded manual retry budget has been consumed."""


class ConversationRetryCooldown(ConversationConflict):
    """A manual retry was requested before the cooldown elapsed."""


class ConversationRepositoryUnavailable(ConversationError):
    """The trusted database is unavailable or not configured."""


@dataclass(frozen=True, slots=True)
class ConversationMessageRecord:
    message_id: str
    analysis_case_id: str
    role: str
    sequence_no: int
    content: str | None
    status: str
    reply_to_message_id: str | None
    retry_count: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationTurnRecord:
    user_message_id: str
    assistant_message_id: str
    analysis_session_id: str
    status: str
    retry_count: int = 0


@runtime_checkable
class ConversationRepository(Protocol):
    """Owner-scoped HTTP operations backed by service-role RPCs."""

    async def create_message(
        self, *, owner_id: str, analysis_case_id: str, content: str
    ) -> ConversationTurnRecord: ...

    async def list_messages(
        self,
        *,
        owner_id: str,
        analysis_case_id: str,
        limit: int = 50,
        updated_since: datetime | None = None,
    ) -> list[ConversationMessageRecord]: ...

    async def retry_message(
        self, *, owner_id: str, analysis_case_id: str, assistant_message_id: str
    ) -> ConversationTurnRecord: ...
