from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "developer", "user", "assistant"]
    content: str


class LLMUnavailableError(RuntimeError):
    pass


class LLMTimeoutError(RuntimeError):
    pass


class LLMInvalidResponseError(RuntimeError):
    pass


class LLMClient(Protocol):
    async def generate_structured(
        self,
        *,
        task_name: str,
        messages: list[Message],
        response_schema: type[BaseModel],
        model_profile: str,
    ) -> BaseModel: ...
