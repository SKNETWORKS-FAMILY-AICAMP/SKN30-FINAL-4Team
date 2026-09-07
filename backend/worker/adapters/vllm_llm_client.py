"""vLLM(OpenAI 호환) Chat Completions 기반 LLM 클라이언트.

vLLM 은 Responses API 를 구현하지 않는다. 그래서
``app/infrastructure/openai_llm_client.py`` 의 형제 구현이되 엔드포인트만
``/chat/completions`` 이고 구조화 출력은 ``response_format`` 으로 건다.
"""

import asyncio
from collections.abc import Mapping
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)


logger = logging.getLogger(__name__)


class VllmLLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model_profiles: Mapping[str, str],
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model_profiles = dict(model_profiles)
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def generate_structured(
        self,
        *,
        task_name: str,
        messages: list[Message],
        response_schema: type[BaseModel],
        model_profile: str,
    ) -> BaseModel:
        started_at = time.perf_counter()
        try:
            model = self._model_profiles[model_profile]
        except KeyError:
            raise LLMUnavailableError("Unknown LLM model profile") from None

        payload = {
            "model": model,
            # 같은 문서에 같은 판정이 나와야 한다. 기본값에서는 동일 문서의
            # CPL 개수가 26회 실행 동안 6~9 로 흔들렸다.
            # ponytail: 상수 0. 분산을 의도적으로 재려면 그때 설정으로 뺀다.
            "temperature": 0,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": task_name,
                    "schema": response_schema.model_json_schema(),
                    "strict": True,
                },
            },
        }

        response = await self._request("POST", "/chat/completions", json=payload)
        try:
            response_data = response.json()
        except (TypeError, ValueError):
            raise LLMInvalidResponseError("LLM returned an invalid response") from None
        if not isinstance(response_data, dict):
            raise LLMInvalidResponseError("LLM returned an invalid response")
        if response_data.get("error") is not None:
            raise LLMUnavailableError("LLM service failed to generate a response")

        try:
            content = _read_message_content(response_data)
            result = response_schema.model_validate_json(content)
        except (TypeError, ValueError, ValidationError):
            raise LLMInvalidResponseError("LLM returned an invalid response") from None

        usage = response_data.get("usage", {})
        logger.info(
            "LLM request completed task=%s model=%s prompt_tokens=%s "
            "completion_tokens=%s total_tokens=%s duration_ms=%s",
            task_name,
            model,
            usage.get("prompt_tokens") if isinstance(usage, dict) else None,
            usage.get("completion_tokens") if isinstance(usage, dict) else None,
            usage.get("total_tokens") if isinstance(usage, dict) else None,
            round((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def list_models(self) -> list[str]:
        """서빙 중인 모델 id 목록. 배포된 Gemma 의 실제 id 는 이걸로 확인한다."""

        response = await self._request("GET", "/models")
        try:
            body = response.json()
            data = body["data"]
            if not isinstance(data, list):
                raise ValueError
            ids = [item["id"] for item in data]
            if any(not isinstance(model_id, str) for model_id in ids):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise LLMInvalidResponseError("LLM returned an invalid response") from None
        return ids

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                for attempt in range(2):
                    response = await client.request(
                        method,
                        f"{self._base_url}{path}",
                        headers=headers,
                        json=json,
                    )
                    if response.status_code != 429 and response.status_code < 500:
                        break
                    if attempt == 0:
                        await asyncio.sleep(0.25)
        except httpx.TimeoutException:
            raise LLMTimeoutError("LLM request timed out") from None
        except httpx.RequestError:
            raise LLMUnavailableError("LLM service is unavailable") from None

        if response.is_error:
            logger.warning(
                "LLM provider HTTP error status=%s retryable=%s",
                response.status_code,
                response.status_code == 429 or response.status_code >= 500,
            )
            raise LLMUnavailableError("LLM service rejected the request")
        return response


def _read_message_content(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response choices are missing")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ValueError("response message is missing")
    if message.get("refusal"):
        raise ValueError("LLM refused to generate a response")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("response content is missing")
    return content
