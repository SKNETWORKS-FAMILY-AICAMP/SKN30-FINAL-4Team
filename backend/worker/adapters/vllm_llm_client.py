"""vLLM(OpenAI 호환) Chat Completions 기반 LLM 클라이언트.

``worker.providers``가 ``PREREVIEW_LLM_PROVIDER=vllm``일 때 이 adapter를
생성한다. provider는 구조화 추론만 담당하며 DB·Storage credential을 받지 않는다.

vLLM 은 Responses API 를 구현하지 않는다. 그래서
``app/infrastructure/openai_llm_client.py`` 의 형제 구현이되 엔드포인트만
``/chat/completions`` 이고 구조화 출력은 ``response_format`` 으로 건다.
"""

from collections.abc import Mapping
import asyncio
import ipaddress
import json
import logging
import math
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai.lib._parsing import type_to_response_format_param
from pydantic import BaseModel, ValidationError

from ..ports.llm import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)


logger = logging.getLogger(__name__)

_MIN_MAX_OUTPUT_TOKENS = 1
_MAX_MAX_OUTPUT_TOKENS = 32_768
_MIN_MAX_RESPONSE_BYTES = 1_024
_MAX_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def validate_vllm_base_url(base_url: str) -> str:
    """Return a safe vLLM endpoint or reject it without exposing its value.

    The provider token is sent in an Authorization header.  HTTP is therefore
    intentionally limited to an explicitly local development endpoint; a
    remote endpoint must use HTTPS.  Do not resolve arbitrary host names here:
    a DNS answer can change after validation.  ``localhost`` and numeric
    loopback literals are the only cleartext exceptions.
    """

    normalized_base_url = base_url.strip().rstrip("/")
    parsed = urlsplit(normalized_base_url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(
            "vLLM base URL must use HTTPS or literal loopback HTTP"
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 0 < port < 65536
    ):
        raise ValueError("vLLM base URL must use HTTPS or literal loopback HTTP")
    if parsed.scheme == "http" and not _is_literal_loopback(parsed.hostname):
        raise ValueError("vLLM base URL must use HTTPS or literal loopback HTTP")
    return normalized_base_url


def _is_literal_loopback(hostname: str) -> bool:
    """Accept only ``localhost``, IPv4 127/8, or IPv6 ::1 for cleartext."""

    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (
        (isinstance(address, ipaddress.IPv4Address) and address.is_loopback)
        or (
            isinstance(address, ipaddress.IPv6Address)
            and address == ipaddress.IPv6Address("::1")
        )
    )


class VllmLLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model_profiles: Mapping[str, str],
        timeout_seconds: float,
        max_output_tokens: int = 16_384,
        max_response_bytes: int = 1_048_576,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("vLLM API key must not be blank")
        # Revalidate at the adapter boundary even though provider composition
        # validates it before a worker claims work.  Direct callers must not
        # be able to bypass the cleartext-token transport policy.
        normalized_base_url = validate_vllm_base_url(base_url)
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("vLLM timeout must be a finite positive number")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not _MIN_MAX_OUTPUT_TOKENS
            <= max_output_tokens
            <= _MAX_MAX_OUTPUT_TOKENS
        ):
            raise ValueError("vLLM max output tokens must be a bounded integer")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not _MIN_MAX_RESPONSE_BYTES
            <= max_response_bytes
            <= _MAX_MAX_RESPONSE_BYTES
        ):
            raise ValueError("vLLM max response bytes must be a bounded integer")
        profiles = {
            str(profile): str(model).strip()
            for profile, model in model_profiles.items()
            if str(profile).strip() and str(model).strip()
        }
        if not profiles:
            raise ValueError("At least one vLLM model profile is required")

        self._api_key = api_key
        self._base_url = normalized_base_url
        self._model_profiles = profiles
        self._timeout_seconds = float(timeout_seconds)
        self._timeout = httpx.Timeout(self._timeout_seconds)
        self._max_output_tokens = max_output_tokens
        self._max_response_bytes = max_response_bytes
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

        # This is deliberately the SDK conversion rather than a hand-written
        # model_json_schema() wrapper: it carries OpenAI's strict-schema
        # normalization (including additionalProperties) to compatible vLLM.
        response_format = type_to_response_format_param(response_schema)
        payload = {
            "model": model,
            # 같은 문서에 같은 판정이 나와야 한다. 기본값에서는 동일 문서의
            # CPL 개수가 26회 실행 동안 6~9 로 흔들렸다.
            # ponytail: 상수 0. 분산을 의도적으로 재려면 그때 설정으로 뺀다.
            "temperature": 0,
            # vLLM's OpenAI-compatible Chat Completions accepts these sampling
            # controls. Determinism is still best-effort across server/model versions.
            "seed": 0,
            "top_p": 1,
            "max_tokens": self._max_output_tokens,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "response_format": response_format,
        }

        response = await self._request("POST", "/chat/completions", json=payload)
        try:
            response_data = response.json()
        except (TypeError, ValueError, RecursionError):
            raise LLMInvalidResponseError("LLM returned an invalid response") from None
        if not isinstance(response_data, dict):
            raise LLMInvalidResponseError("LLM returned an invalid response")
        if response_data.get("error") is not None:
            raise LLMUnavailableError("LLM service failed to generate a response")

        # 파싱 실패와 스키마 위반을 구분한다. 배치 응답에서 행 하나가 계약을
        # 어긴 것과 본문 자체를 못 읽은 것은 격리 범위가 다르다: 전자는 그
        # 행만, 후자는 호출 범위 전체다. 구분을 잃으면 정상 행의 판정까지
        # 함께 내려간다.
        try:
            finish_reason = _read_finish_reason(response_data)
            if finish_reason != "stop":
                raise LLMInvalidResponseError("LLM returned an incomplete response")
            content = _read_message_content(response_data)
            raw = json.loads(content)
        except LLMInvalidResponseError:
            raise
        except (TypeError, ValueError, RecursionError):
            raise LLMInvalidResponseError("LLM returned an invalid response") from None
        try:
            result = response_schema.model_validate(raw)
        except ValidationError:
            raise LLMInvalidResponseError(
                "LLM returned an invalid response", raw=raw
            ) from None

        usage = response_data.get("usage", {})
        logger.info(
            "LLM request completed task=%s model=%s prompt_tokens=%s "
            "completion_tokens=%s total_tokens=%s duration_ms=%s",
            task_name,
            model,
            _safe_usage_int(usage, "prompt_tokens"),
            _safe_usage_int(usage, "completion_tokens"),
            _safe_usage_int(usage, "total_tokens"),
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
        except (KeyError, TypeError, ValueError, RecursionError):
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
            # httpx's timeout covers inactivity phases; asyncio.timeout also
            # bounds the entire connect/send/receive/read call.
            async with asyncio.timeout(self._timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    transport=self._transport,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        method,
                        f"{self._base_url}{path}",
                        headers=headers,
                        json=json,
                    ) as response:
                        declared_length = response.headers.get("content-length")
                        if declared_length is not None:
                            try:
                                if (
                                    int(declared_length) < 0
                                    or int(declared_length)
                                    > self._max_response_bytes
                                ):
                                    raise ValueError
                            except ValueError:
                                raise LLMInvalidResponseError(
                                    "LLM returned an invalid response"
                                ) from None
                        if response.is_error:
                            logger.warning(
                                "LLM provider HTTP error status=%s retryable=%s",
                                response.status_code,
                                response.status_code == 429 or response.status_code >= 500,
                            )
                            raise LLMUnavailableError("LLM service rejected the request")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > self._max_response_bytes:
                                raise LLMInvalidResponseError(
                                    "LLM returned an invalid response"
                                )
                            body.extend(chunk)
                        return httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            content=bytes(body),
                        )
        except (TimeoutError, httpx.TimeoutException):
            raise LLMTimeoutError("LLM request timed out") from None
        except (LLMInvalidResponseError, LLMUnavailableError):
            raise
        except (httpx.InvalidURL, httpx.RequestError):
            raise LLMUnavailableError("LLM service is unavailable") from None


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


def _read_finish_reason(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response choices are missing")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("response choice is missing")
    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str):
        raise ValueError("response finish_reason is invalid")
    return finish_reason


def _safe_usage_int(usage: object, key: str) -> int | None:
    """Return a non-negative native int only; logs must be total on bad JSON."""

    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
