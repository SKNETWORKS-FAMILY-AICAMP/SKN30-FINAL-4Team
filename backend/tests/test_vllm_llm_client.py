"""Offline HTTP contract tests for the selectable vLLM adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from openai.lib._parsing import type_to_response_format_param

import httpx
from pydantic import BaseModel
import pytest

from worker.adapters.vllm_llm_client import VllmLLMClient
from worker.ports.llm import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)


class _Result(BaseModel):
    value: str


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    api_key: str = "vllm-test-secret",
) -> VllmLLMClient:
    return VllmLLMClient(
        api_key=api_key,
        base_url="https://gpu.internal/v1",
        model_profiles={"chat": "served-model"},
        timeout_seconds=3,
        transport=transport,
    )


def _generate(client: VllmLLMClient, prompt: str = "untrusted document") -> BaseModel:
    return asyncio.run(
        client.generate_structured(
            task_name="chat_result",
            messages=[Message(role="user", content=prompt)],
            response_schema=_Result,
            model_profile="chat",
        )
    )


def _completion(
    content: str = '{"value":"grounded"}',
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": content, "refusal": refusal},
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "total_tokens": 7,
        },
    }


def test_vllm_chat_completions_request_and_response_contract() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion())

    result = _generate(_client(httpx.MockTransport(handler)))

    assert result == _Result(value="grounded")
    assert len(seen) == 1
    request = seen[0]
    assert request.url == httpx.URL("https://gpu.internal/v1/chat/completions")
    assert request.headers["authorization"] == "Bearer vllm-test-secret"
    payload = json.loads(request.content)
    assert payload["model"] == "served-model"
    assert payload["temperature"] == 0
    assert payload["seed"] == 0
    assert payload["top_p"] == 1
    assert payload["max_tokens"] == 16384
    assert payload["messages"] == [
        {"role": "user", "content": "untrusted document"}
    ]
    assert payload["response_format"] == type_to_response_format_param(_Result)


@pytest.mark.parametrize(
    ("body", "expected_raw"),
    [
        ("not-json", None),
        (json.dumps(_completion("{}")), {}),
    ],
)
def test_vllm_invalid_content_is_safe_and_preserves_only_schema_raw(
    body: str,
    expected_raw: dict[str, object] | None,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        if body == "not-json":
            return httpx.Response(200, text=body)
        return httpx.Response(200, content=body)

    with pytest.raises(LLMInvalidResponseError) as error:
        _generate(_client(httpx.MockTransport(handler)))

    assert error.value.raw == expected_raw


@pytest.mark.parametrize(
    "response",
    [
        _completion(finish_reason="length"),
        {"choices": [{"message": {"content": '{\"value\":\"x\"}'}}]},
        _completion(finish_reason=None),  # type: ignore[arg-type]
        _completion(refusal="refused"),
    ],
)
def test_vllm_refusal_and_incomplete_completion_are_invalid(
    response: dict[str, object],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=response)

    with pytest.raises(LLMInvalidResponseError):
        _generate(_client(httpx.MockTransport(handler)))


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_vllm_retryable_http_status_is_mapped_without_hidden_retry(
    status_code: int,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx.Response(status_code, json={"error": "provider detail"})

    with pytest.raises(LLMUnavailableError, match="rejected"):
        _generate(_client(httpx.MockTransport(handler)))

    assert calls == 1


@pytest.mark.parametrize(
    ("provider_error", "expected_error"),
    [
        (httpx.ReadTimeout("slow"), LLMTimeoutError),
        (httpx.ConnectError("offline"), LLMUnavailableError),
        (httpx.InvalidURL("invalid"), LLMUnavailableError),
    ],
)
def test_vllm_transport_errors_are_normalized(
    provider_error: Exception,
    expected_error: type[Exception],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise provider_error

    with pytest.raises(expected_error):
        _generate(_client(httpx.MockTransport(handler)))


def test_vllm_errors_and_logs_do_not_expose_prompt_or_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prompt = "private-document-marker"
    token = "private-token-marker"

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, text="provider detail")

    caplog.set_level(logging.WARNING)
    with pytest.raises(LLMUnavailableError) as error:
        _generate(_client(httpx.MockTransport(handler), api_key=token), prompt)

    output = caplog.text + str(error.value)
    assert prompt not in output
    assert token not in output
    assert "provider detail" not in output


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "7"},
        [],
    ],
)
def test_vllm_malformed_usage_is_safely_sanitized(
    caplog: pytest.LogCaptureFixture, usage: object
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        response = _completion()
        response["usage"] = usage
        return httpx.Response(200, json=response)

    caplog.set_level(logging.INFO)
    assert _generate(_client(httpx.MockTransport(handler))) == _Result(value="grounded")
    assert "prompt_tokens=None" in caplog.text
    assert "completion_tokens=None" in caplog.text
    assert "total_tokens=None" in caplog.text


@pytest.mark.parametrize("headers,body", [
    ({"content-length": "1048577"}, b"{}"),
    ({"content-length": "not-a-number"}, b"{}"),
    ({}, b"x" * 1_048_577),
])
def test_vllm_response_body_cap_is_safe(
    headers: dict[str, str], body: bytes
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=body)

    with pytest.raises(LLMInvalidResponseError):
        _generate(_client(httpx.MockTransport(handler)))


def test_vllm_recursive_json_is_an_invalid_response_not_an_adapter_crash() -> None:
    # json.loads can raise RecursionError before the response envelope can be
    # inspected; a remote service must not crash a queue worker that way.
    body = '{"choices":' + ("[" * 1100) + ("0" + ("]" * 1100)) + "}"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(LLMInvalidResponseError):
        _generate(_client(httpx.MockTransport(handler)))


def test_vllm_whole_call_deadline_is_not_only_httpx_inactivity_timeout() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.03)
        return httpx.Response(200, json=_completion())

    client = VllmLLMClient(
        api_key="key",
        base_url="https://gpu.internal/v1",
        model_profiles={"chat": "served-model"},
        timeout_seconds=0.001,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMTimeoutError):
        _generate(client)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": "", "base_url": "https://gpu.internal/v1", "timeout": 3},
        {"api_key": "key", "base_url": "ftp://gpu.internal/v1", "timeout": 3},
        {"api_key": "key", "base_url": "https://user@gpu.internal/v1", "timeout": 3},
        {"api_key": "key", "base_url": "https://gpu.internal/v1?q=1", "timeout": 3},
        {"api_key": "key", "base_url": "https://gpu.internal/v1#fragment", "timeout": 3},
        {"api_key": "key", "base_url": "https://gpu.internal:bad/v1", "timeout": 3},
        {"api_key": "key", "base_url": "https://gpu.internal/v1", "timeout": 0},
        {"api_key": "key", "base_url": "http://gpu.internal/v1", "timeout": 3},
        {"api_key": "key", "base_url": "http://10.0.0.8/v1", "timeout": 3},
        {"api_key": "key", "base_url": "http://[::2]/v1", "timeout": 3},
    ],
)
def test_vllm_constructor_fails_closed(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VllmLLMClient(
            api_key=str(kwargs["api_key"]),
            base_url=str(kwargs["base_url"]),
            model_profiles={"chat": "served-model"},
            timeout_seconds=float(kwargs["timeout"]),
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://127.255.255.255:8000/v1",
        "http://[::1]:8000/v1",
    ],
)
def test_vllm_constructor_allows_cleartext_only_for_literal_loopback(
    base_url: str,
) -> None:
    client = VllmLLMClient(
        api_key="key",
        base_url=base_url,
        model_profiles={"chat": "served-model"},
        timeout_seconds=3,
    )

    assert client._base_url == base_url  # type: ignore[attr-defined]
