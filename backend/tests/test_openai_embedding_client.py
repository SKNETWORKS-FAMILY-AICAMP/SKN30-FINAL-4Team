"""Security and error-mapping contracts for the OpenAI embedding adapter."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Callable

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError
import pytest

from worker.adapters.openai_embedding_client import OpenAIEmbeddingClient
from worker.ports.embedding import EmbeddingTimeoutError, EmbeddingUnavailableError


_LOGGER = "worker.adapters.openai_embedding_client"
_API_KEY = "private-api-key-marker"
_INPUT_MARKER = "private-input-marker"
_PROVIDER_MARKER = "private-provider-marker"


class _RaisingEmbeddings:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def create(self, **_values: object) -> object:
        raise self._error


def _request() -> httpx.Request:
    return httpx.Request(
        "POST",
        f"https://api.openai.invalid/v1/embeddings?secret={_PROVIDER_MARKER}",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )


def _status_error(status_code: int) -> APIStatusError:
    response = httpx.Response(
        status_code,
        request=_request(),
        text=f"provider body {_PROVIDER_MARKER}",
    )
    return APIStatusError(
        f"provider message {_PROVIDER_MARKER}",
        response=response,
        body={"detail": _PROVIDER_MARKER},
    )


def _embed(error: Exception) -> None:
    client = OpenAIEmbeddingClient(
        api_key=_API_KEY,
        model_name="text-embedding-test",
        client=SimpleNamespace(embeddings=_RaisingEmbeddings(error)),
    )
    asyncio.run(client.embed([_INPUT_MARKER, "가나다"]))


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(400, False), (429, True), (503, True)],
)
def test_status_error_warning_is_diagnostic_but_redacted(
    caplog: pytest.LogCaptureFixture,
    status_code: int,
    retryable: bool,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    with pytest.raises(
        EmbeddingUnavailableError,
        match="^OpenAI embedding request failed$",
    ) as raised:
        _embed(_status_error(status_code))

    assert len(caplog.records) == 1
    record = caplog.records[0]
    output = record.getMessage() + str(raised.value)
    assert record.levelno == logging.WARNING
    assert "error_class=APIStatusError" in output
    assert f"status_code={status_code}" in output
    assert "model=text-embedding-test" in output
    assert "input_count=2" in output
    assert f"input_bytes_total={len(_INPUT_MARKER.encode('utf-8')) + 9}" in output
    assert f"input_bytes_max={len(_INPUT_MARKER.encode('utf-8'))}" in output
    assert "duration_ms=" in output
    assert f"retryable={retryable}" in output
    assert not any(isinstance(value, BaseException) for value in record.args)
    assert _API_KEY not in output
    assert _INPUT_MARKER not in output
    assert _PROVIDER_MARKER not in output


@pytest.mark.parametrize(
    ("error_factory", "error_class", "public_error", "public_message", "retryable"),
    [
        (
            lambda: APITimeoutError(request=_request()),
            "APITimeoutError",
            EmbeddingTimeoutError,
            "OpenAI embedding request timed out",
            True,
        ),
        (
            lambda: APIConnectionError(
                message=f"connection {_PROVIDER_MARKER}", request=_request()
            ),
            "APIConnectionError",
            EmbeddingUnavailableError,
            "OpenAI embedding request failed",
            True,
        ),
        (
            lambda: APIError(
                f"api {_PROVIDER_MARKER}",
                _request(),
                body={"detail": _PROVIDER_MARKER},
            ),
            "APIError",
            EmbeddingUnavailableError,
            "OpenAI embedding request failed",
            False,
        ),
        (
            lambda: TimeoutError(f"timeout {_PROVIDER_MARKER}"),
            "TimeoutError",
            EmbeddingTimeoutError,
            "OpenAI embedding request timed out",
            True,
        ),
        (
            lambda: RuntimeError(f"runtime {_PROVIDER_MARKER}"),
            "RuntimeError",
            EmbeddingUnavailableError,
            "OpenAI embedding request failed: RuntimeError",
            False,
        ),
    ],
)
def test_non_status_failure_warning_and_public_contract_are_redacted(
    caplog: pytest.LogCaptureFixture,
    error_factory: Callable[[], Exception],
    error_class: str,
    public_error: type[Exception],
    public_message: str,
    retryable: bool,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    with pytest.raises(public_error) as raised:
        _embed(error_factory())

    assert str(raised.value) == public_message
    assert len(caplog.records) == 1
    record = caplog.records[0]
    output = record.getMessage() + str(raised.value)
    assert record.levelno == logging.WARNING
    assert f"error_class={error_class}" in output
    assert "status_code=None" in output
    assert "model=text-embedding-test" in output
    assert "input_count=2" in output
    assert "input_bytes_total=" in output
    assert "input_bytes_max=" in output
    assert "duration_ms=" in output
    assert f"retryable={retryable}" in output
    assert not any(isinstance(value, BaseException) for value in record.args)
    assert _API_KEY not in output
    assert _INPUT_MARKER not in output
    assert _PROVIDER_MARKER not in output
