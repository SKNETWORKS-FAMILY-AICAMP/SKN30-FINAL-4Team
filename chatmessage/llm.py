"""LLM provider 경계. 챗봇 로직은 이 인터페이스만 안다.

backend 의 `OpenAILLMClient` 를 import 하지 않는다 — 이 패키지가 backend 를
알면 의존 방향이 거꾸로 선다. 대신 같은 계약(Responses API + strict json_schema)
을 여기서 얇게 다시 부른다. FastAPI 는 자기 클라이언트를 계속 쓰면 되고, 이건
`run_chat.py` 처럼 backend 없이 챗봇만 돌릴 때 쓴다.

temperature 는 항상 0 이다 — 같은 근거면 같은 답이어야 한다.
"""
import json
from typing import Any, Protocol

from .prompt import SYSTEM_PROMPT, build_user_prompt
from .schema import ChatAnswer

TEMPERATURE = 0
TASK_NAME = "result_grounded_chat"


class ChatLLMClient(Protocol):
    def answer(self, context: dict[str, Any]) -> ChatAnswer: ...


class LLMError(RuntimeError):
    """모델을 부르지 못했거나 계약에 맞지 않는 답이 왔다."""


class OpenAIChatClient:
    """OpenAI Responses API 를 strict json_schema 로 부른다.

    strict 모드는 스키마의 모든 필드가 required 여야 한다. `ChatAnswer` 가
    기본값을 두지 않는 이유가 그것이다 — 값이 없으면 `[]` 와 `null` 을 명시해서
    받는다.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise LLMError("OPENAI_API_KEY 가 없다")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def answer(self, context: dict[str, Any]) -> ChatAnswer:
        import httpx  # 지연 import — 이 모듈을 쓰지 않으면 필요 없다

        payload = {
            "model": self._model,
            "store": False,
            "temperature": TEMPERATURE,
            "input": [
                {"role": "developer", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(context)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": TASK_NAME,
                    "schema": ChatAnswer.model_json_schema(),
                    "strict": True,
                }
            },
        }
        try:
            response = httpx.post(
                f"{self._base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise LLMError("모델 응답이 시간 안에 오지 않았다") from None
        except httpx.RequestError as error:
            raise LLMError(f"모델을 부르지 못했다: {error}") from None

        if response.is_error:
            raise LLMError(
                f"모델이 요청을 거절했다 (HTTP {response.status_code}): "
                f"{response.text[:400]}"
            )

        try:
            body = response.json()
        except ValueError:
            raise LLMError("응답이 JSON 이 아니다") from None

        text = _output_text(body)
        try:
            return ChatAnswer.model_validate_json(text)
        except ValueError as error:
            raise LLMError(f"답이 계약에 맞지 않는다: {error}") from None


def _output_text(body: Any) -> str:
    if not isinstance(body, dict):
        raise LLMError("응답이 객체가 아니다")
    if body.get("error") is not None:
        raise LLMError(f"모델 오류: {json.dumps(body['error'], ensure_ascii=False)}")

    parts: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "refusal":
                raise LLMError(f"모델이 답변을 거부했다: {part.get('refusal')}")
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])
    if not parts:
        raise LLMError("응답에 본문이 없다")
    return "".join(parts)
