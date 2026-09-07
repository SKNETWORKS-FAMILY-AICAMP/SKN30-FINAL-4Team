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
    """응답을 계약대로 읽지 못했다.

    ``raw`` 는 **JSON 으로는 읽혔지만 스키마를 어긴** 본문이다. 호출부가 행
    단위 부분 회수를 시도할 때만 쓰고, ``None`` 이면 본문 자체가 JSON 이
    아니었다는 뜻이라 최상위 실패다.

    ``raw`` 에는 요청서 원문이 실릴 수 있다. 메시지·로그·진단 문자열에는
    절대 넣지 않는다. 그래서 message 는 내용이 섞이지 않는 고정 문자열이다.
    """

    def __init__(self, message: str, *, raw: object | None = None) -> None:
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
