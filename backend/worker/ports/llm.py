"""Minimal LLM port used by the deterministic worker pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "developer", "user", "assistant"]
    content: str


class LLMUnavailableError(RuntimeError):
    """The provider could not be reached or rejected the request."""


class LLMTimeoutError(RuntimeError):
    """The provider did not answer before the configured request deadline."""


class LLMInvalidResponseError(RuntimeError):
    """The provider response was not valid for the requested output contract.

    ``raw`` remains private to the call-site recovery logic.  It must not be
    logged because model output can contain the uploaded request document.
    """

    def __init__(self, message: str, *, raw: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class LLMClient(Protocol):
    async def generate_structured(
        self,
        *,
        task_name: str,
        messages: list[Message],
        response_schema: type[BaseModel],
        model_profile: str,
    ) -> BaseModel: ...
