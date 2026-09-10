import asyncio
from collections.abc import Mapping
import json
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


class OpenAILLMClient:
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
            "store": False,
            # 사전검토 결과는 같은 문서에 같은 판정이 나와야 한다. 기본값(1.0)
            # 으로는 동일 문서의 CPL 확인율이 7/13~11/13 로 흔들려 프롬프트
            # 수정 효과와 표집 잡음을 구분할 수 없었다.
            # ponytail: 상수 0. 분산을 의도적으로 재려면 그때 설정으로 뺀다.
            "temperature": 0,
            "input": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": task_name,
                    "schema": _strict_schema(
                        response_schema.model_json_schema()
                    ),
                    "strict": True,
                }
            },
        }

        response = await self._post(payload)
        try:
            response_data = response.json()
        except (TypeError, ValueError):
            raise LLMInvalidResponseError("LLM returned an invalid response") from None
        if not isinstance(response_data, dict):
            raise LLMInvalidResponseError("LLM returned an invalid response")
        if (
            response_data.get("status") == "failed"
            or response_data.get("error") is not None
        ):
            raise LLMUnavailableError("LLM service failed to generate a response")
        if response_data.get("status") not in (None, "completed"):
            raise LLMInvalidResponseError("LLM returned an incomplete response")
        if _contains_refusal(response_data):
            raise LLMInvalidResponseError("LLM refused to generate a response")

        try:
            output_text = _read_output_text(response_data)
            raw = json.loads(output_text)
        except (TypeError, ValueError):
            raise LLMInvalidResponseError("LLM returned an invalid response") from None
        try:
            result = response_schema.model_validate(raw)
        except ValidationError:
            raise LLMInvalidResponseError(
                "LLM returned an invalid response", raw=raw
            ) from None
        usage = response_data.get("usage", {})
        logger.info(
            "LLM request completed task=%s model=%s input_tokens=%s "
            "output_tokens=%s total_tokens=%s duration_ms=%s",
            task_name,
            model,
            usage.get("input_tokens") if isinstance(usage, dict) else None,
            usage.get("output_tokens") if isinstance(usage, dict) else None,
            usage.get("total_tokens") if isinstance(usage, dict) else None,
            round((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
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
                    response = await client.post(
                        f"{self._base_url}/responses",
                        headers=headers,
                        json=payload,
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


def _strict_schema(node: Any) -> Any:
    """Pydantic 스키마를 Responses API 의 ``strict`` 규칙에 맞춘다.

    strict 는 모든 object 에 ``additionalProperties: false`` 와 "properties 의
    모든 키를 담은 required" 를 요구한다. 우리가 쓴 스키마들은 이미 그렇지만
    ``packages/**`` 의 벤더 스키마(``RequestSourceSelectionV012`` 등)는 vLLM
    guided decoding 을 겨냥해 쓰여 선택 필드를 그대로 둔다. 그대로 보내면
    호출이 400 ``invalid_json_schema`` 로 떨어져 LLM 에 닿지도 못한다.

    필드를 required 로 올려도 값이 사라지지 않는다: ``X | None`` 은 anyOf 에
    null 이 이미 있고, 기본값이 빈 목록인 필드는 ``[]`` 를 내면 된다. 그래서
    타입은 건드리지 않는다. 벤더 파일도 수정하지 않는다 — 나가는 사본만 고친다.
    """

    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
        for value in node.values():
            _strict_schema(value)
    elif isinstance(node, list):
        for value in node:
            _strict_schema(value)
    return node


def _read_output_text(response_data: Any) -> str:
    if not isinstance(response_data, dict):
        raise ValueError("response must be an object")
    output = response_data.get("output")
    if not isinstance(output, list):
        raise ValueError("response output is missing")

    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                text_parts.append(part["text"])

    if not text_parts:
        raise ValueError("response output text is missing")
    return "".join(text_parts)


def _contains_refusal(response_data: dict[str, Any]) -> bool:
    output = response_data.get("output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") == "refusal"
        for item in output
        if isinstance(item, dict) and isinstance(item.get("content"), list)
        for part in item["content"]
    )
