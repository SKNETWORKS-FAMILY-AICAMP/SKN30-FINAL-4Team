"""Offline contracts for selectable worker LLM provider composition."""

from __future__ import annotations

import pytest

from worker.adapters.openai_embedding_client import OpenAIEmbeddingClient
from worker.adapters.openai_llm_client import OpenAILLMClient
from worker.adapters.vllm_llm_client import VllmLLMClient
from worker.config import MissingConfigError
from worker.providers import (
    ProviderConfigurationError,
    build_embedding_client,
    build_llm_provider,
)


def test_openai_is_the_default_llm_provider_with_stage_fallbacks() -> None:
    provider = build_llm_provider(
        profiles=("request_profile", "cpl", "fit", "sim", "chat"),
        env={
            "OPENAI_API_KEY": "not-a-real-secret",
            "OPENAI_LLM_MODEL": "openai-default",
            "OPENAI_REQUEST_PROFILE_MODEL": "openai-request",
            "OPENAI_CPL_MODEL": "openai-cpl",
            "OPENAI_CHAT_MODEL": "openai-chat",
        },
    )

    assert provider.name == "openai"
    assert isinstance(provider.client, OpenAILLMClient)
    assert provider.model_profiles == {
        "request_profile": "openai-request",
        "cpl": "openai-cpl",
        "fit": "openai-default",
        "sim": "openai-default",
        "chat": "openai-chat",
    }
    assert provider.model_id_for("request_profile") == "openai-request"


def test_vllm_llm_does_not_require_openai_llm_settings_and_honors_overrides() -> None:
    provider = build_llm_provider(
        profiles=("request_profile", "cpl", "fit", "sim", "chat"),
        env={
            "PREREVIEW_LLM_PROVIDER": "vllm",
            "VLLM_BASE_URL": "https://gpu.internal/v1/",
            "VLLM_API_KEY": "not-a-real-secret",
            "VLLM_LLM_MODEL": "gemma-default",
            "VLLM_REQUEST_PROFILE_MODEL": "gemma-request",
            "VLLM_CPL_MODEL": "gemma-cpl",
            "VLLM_FIT_MODEL": "gemma-fit",
            "VLLM_SIM_MODEL": "gemma-sim",
            "VLLM_CHAT_MODEL": "gemma-chat",
            "VLLM_TIMEOUT_SECONDS": "42.5",
            "VLLM_MAX_REPAIRS": "3",
            "VLLM_MAX_OUTPUT_TOKENS": "1234",
            "VLLM_MAX_RESPONSE_BYTES": "2048",
        },
    )

    assert provider.name == "vllm"
    assert isinstance(provider.client, VllmLLMClient)
    assert provider.model_profiles == {
        "request_profile": "gemma-request",
        "cpl": "gemma-cpl",
        "fit": "gemma-fit",
        "sim": "gemma-sim",
        "chat": "gemma-chat",
    }
    assert provider.max_repairs == 3
    assert provider.client._base_url == "https://gpu.internal/v1"  # type: ignore[attr-defined]
    assert provider.client._timeout.connect == 42.5  # type: ignore[attr-defined]
    assert provider.client._max_output_tokens == 1234  # type: ignore[attr-defined]
    assert provider.client._max_response_bytes == 2048  # type: ignore[attr-defined]


def test_embedding_provider_is_independent_from_vllm_llm_selection() -> None:
    embedding = build_embedding_client(
        {
            "PREREVIEW_LLM_PROVIDER": "vllm",
            "PREREVIEW_EMBEDDING_PROVIDER": "openai",
            "OPENAI_API_KEY": "not-a-real-secret",
            "OPENAI_EMBEDDING_MODEL": "text-embedding-configured",
        }
    )

    assert isinstance(embedding, OpenAIEmbeddingClient)
    assert embedding._model_name == "text-embedding-configured"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"PREREVIEW_LLM_PROVIDER": "unknown"}, "PREREVIEW_LLM_PROVIDER"),
        (
            {
                "PREREVIEW_LLM_PROVIDER": "vllm",
                "VLLM_API_KEY": "not-a-real-secret",
                "VLLM_LLM_MODEL": "gemma",
            },
            "VLLM_BASE_URL",
        ),
        (
            {
                "PREREVIEW_LLM_PROVIDER": "vllm",
                "VLLM_BASE_URL": "ftp://gpu.internal/v1",
                "VLLM_API_KEY": "not-a-real-secret",
                "VLLM_LLM_MODEL": "gemma",
            },
            "VLLM_BASE_URL",
        ),
        (
            {
                "PREREVIEW_LLM_PROVIDER": "vllm",
                "VLLM_BASE_URL": "http://gpu.internal/v1",
                "VLLM_API_KEY": "not-a-real-secret",
                "VLLM_LLM_MODEL": "gemma",
            },
            "VLLM_BASE_URL",
        ),
        ({"PREREVIEW_EMBEDDING_PROVIDER": "vllm"}, "PREREVIEW_EMBEDDING_PROVIDER"),
    ],
)
def test_provider_configuration_fails_closed_without_echoing_values(
    env: dict[str, str], message: str
) -> None:
    builder = build_embedding_client if "PREREVIEW_EMBEDDING_PROVIDER" in env else lambda value: build_llm_provider(profiles=("chat",), env=value)

    with pytest.raises(ProviderConfigurationError, match=message) as error:
        builder(env)

    assert "not-a-real-secret" not in str(error.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://127.42.0.1:8000/v1",
        "http://[::1]:8000/v1",
    ],
)
def test_provider_factory_allows_cleartext_only_for_literal_loopback(
    base_url: str,
) -> None:
    provider = build_llm_provider(
        profiles=("chat",),
        env={
            "PREREVIEW_LLM_PROVIDER": "vllm",
            "VLLM_BASE_URL": base_url,
            "VLLM_API_KEY": "not-a-real-secret",
            "VLLM_LLM_MODEL": "gemma",
        },
    )

    assert provider.name == "vllm"
    assert provider.client._base_url == base_url  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "env",
    [
        {
            "OPENAI_API_KEY": "not-a-real-secret",
            "OPENAI_LLM_MODEL": "openai-default",
            "OPENAI_MAX_REPAIRS": "-1",
        },
        {
            "PREREVIEW_LLM_PROVIDER": "vllm",
            "VLLM_BASE_URL": "https://gpu.internal/v1",
            "VLLM_API_KEY": "not-a-real-secret",
            "VLLM_LLM_MODEL": "gemma",
            "VLLM_MAX_REPAIRS": "-1",
        },
    ],
)
def test_llm_provider_rejects_negative_repair_limits(
    env: dict[str, str],
) -> None:
    with pytest.raises(MissingConfigError, match="non-negative|between 0 and 8"):
        build_llm_provider(profiles=("chat",), env=env)


@pytest.mark.parametrize("name,value", [
    ("VLLM_MAX_REPAIRS", "9"),
    ("VLLM_MAX_OUTPUT_TOKENS", "0"),
    ("VLLM_MAX_OUTPUT_TOKENS", "32769"),
    ("VLLM_MAX_RESPONSE_BYTES", "1023"),
    ("VLLM_MAX_RESPONSE_BYTES", "4194305"),
])
def test_vllm_bounds_fail_closed(name: str, value: str) -> None:
    env = {
        "PREREVIEW_LLM_PROVIDER": "vllm",
        "VLLM_BASE_URL": "https://gpu.internal/v1",
        "VLLM_API_KEY": "not-a-real-secret",
        "VLLM_LLM_MODEL": "gemma",
        name: value,
    }
    with pytest.raises(ProviderConfigurationError, match=name):
        build_llm_provider(profiles=("chat",), env=env)
