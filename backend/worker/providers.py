"""Provider composition for worker LLM and embedding ports.

The LLM endpoint is a replaceable execution detail.  Retrieval embeddings are
configured separately because changing their provider/model changes persisted
vector provenance and requires an explicit backfill, not an LLM switch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os

from worker.adapters.openai_embedding_client import OpenAIEmbeddingClient
from worker.adapters.openai_llm_client import OpenAILLMClient
from worker.adapters.vllm_llm_client import VllmLLMClient, validate_vllm_base_url
from worker.config import MissingConfigError, OpenAIConfig, OpenAIEmbeddingConfig
from worker.ports.embedding import EmbeddingClient
from worker.ports.llm import LLMClient


_LLM_PROVIDERS = frozenset({"openai", "vllm"})
_EMBEDDING_PROVIDERS = frozenset({"openai"})


class ProviderConfigurationError(MissingConfigError):
    """A provider configuration error whose message never includes a value."""


@dataclass(frozen=True, slots=True)
class LlmProvider:
    """One selected LLM client and its deployment-owned model resolution."""

    client: LLMClient
    name: str
    model_profiles: Mapping[str, str]
    max_repairs: int

    def model_id_for(self, profile: str) -> str:
        try:
            return self.model_profiles[profile]
        except KeyError:
            raise ProviderConfigurationError(
                "unknown configured LLM model profile"
            ) from None


def build_llm_provider(
    *,
    profiles: tuple[str, ...],
    env: Mapping[str, str] | None = None,
) -> LlmProvider:
    """Select an LLM client before a worker can claim a queue item.

    ``PREREVIEW_LLM_PROVIDER`` defaults to OpenAI to preserve the deployed
    behavior.  The selected provider requires only its own LLM credentials;
    embedding configuration is intentionally assembled by
    :func:`build_embedding_client`.
    """

    values = os.environ if env is None else env
    provider = _provider_name(values, "PREREVIEW_LLM_PROVIDER", _LLM_PROVIDERS)
    if not profiles or any(not profile.strip() for profile in profiles):
        raise ValueError("at least one non-blank LLM model profile is required")

    if provider == "openai":
        config = OpenAIConfig.from_env(values)
        model_profiles = config.llm_model_profiles(*profiles)
        return LlmProvider(
            client=OpenAILLMClient(
                api_key=config.api_key,
                model_profiles=model_profiles,
                timeout_seconds=config.timeout_seconds,
            ),
            name=provider,
            model_profiles=model_profiles,
            max_repairs=config.max_repairs,
        )

    config = _VllmLlmSettings.from_env(values)
    model_profiles = config.model_profiles(*profiles)
    return LlmProvider(
        client=VllmLLMClient(
            api_key=config.api_key,
            base_url=config.base_url,
            model_profiles=model_profiles,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            max_response_bytes=config.max_response_bytes,
        ),
        name=provider,
        model_profiles=model_profiles,
        max_repairs=config.max_repairs,
    )


def build_embedding_client(env: Mapping[str, str] | None = None) -> EmbeddingClient:
    """Build the retrieval embedding client independently from ``LLM_PROVIDER``.

    Only OpenAI embeddings are supported today.  A different value fails at
    process startup rather than silently changing vector provenance mid-run.
    """

    values = os.environ if env is None else env
    _provider_name(values, "PREREVIEW_EMBEDDING_PROVIDER", _EMBEDDING_PROVIDERS)
    config = OpenAIEmbeddingConfig.from_env(values)
    return OpenAIEmbeddingClient(
        api_key=config.api_key,
        model_name=config.embedding_model,
        timeout_seconds=config.timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class _VllmLlmSettings:
    base_url: str
    api_key: str
    llm_model: str
    request_profile_model: str | None
    cpl_model: str | None
    fit_model: str | None
    sim_model: str | None
    chat_model: str | None
    timeout_seconds: float
    max_output_tokens: int
    max_response_bytes: int
    max_repairs: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> _VllmLlmSettings:
        return cls(
            base_url=_vllm_base_url(env, "VLLM_BASE_URL"),
            api_key=_required(env, "VLLM_API_KEY"),
            llm_model=_required(env, "VLLM_LLM_MODEL"),
            request_profile_model=_optional(env, "VLLM_REQUEST_PROFILE_MODEL"),
            cpl_model=_optional(env, "VLLM_CPL_MODEL"),
            fit_model=_optional(env, "VLLM_FIT_MODEL"),
            sim_model=_optional(env, "VLLM_SIM_MODEL"),
            chat_model=_optional(env, "VLLM_CHAT_MODEL"),
            timeout_seconds=_positive_float(env, "VLLM_TIMEOUT_SECONDS", 120.0),
            max_output_tokens=_bounded_int(
                env, "VLLM_MAX_OUTPUT_TOKENS", 16384, minimum=1, maximum=32768
            ),
            max_response_bytes=_bounded_int(
                env, "VLLM_MAX_RESPONSE_BYTES", 1048576,
                minimum=1024, maximum=4194304,
            ),
            max_repairs=_bounded_int(
                env, "VLLM_MAX_REPAIRS", 2, minimum=0, maximum=8
            ),
        )

    def model_profiles(self, *profiles: str) -> dict[str, str]:
        overrides = {
            "request_profile": self.request_profile_model,
            "cpl": self.cpl_model,
            "fit": self.fit_model,
            "sim": self.sim_model,
            "chat": self.chat_model,
        }
        return {profile: overrides.get(profile) or self.llm_model for profile in profiles}


def _provider_name(
    env: Mapping[str, str], name: str, supported: frozenset[str]
) -> str:
    value = env.get(name, "").strip().lower() or "openai"
    if value not in supported:
        allowed = ", ".join(sorted(supported))
        raise ProviderConfigurationError(f"{name} must be one of: {allowed}")
    return value


def _required(env: Mapping[str, str], name: str) -> str:
    value = _optional(env, name)
    if value is None:
        raise ProviderConfigurationError(f"required environment variable is not set: {name}")
    return value


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _vllm_base_url(env: Mapping[str, str], name: str) -> str:
    value = _required(env, name)
    try:
        return validate_vllm_base_url(value)
    except ValueError:
        raise ProviderConfigurationError(
            f"environment variable must use HTTPS or literal loopback HTTP: {name}"
        )


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ProviderConfigurationError(
            f"environment variable must be a number: {name}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ProviderConfigurationError(
            f"environment variable must be finite and positive: {name}"
        )
    return value


def _bounded_int(
    env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int
) -> int:
    raw = env.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ProviderConfigurationError(
            f"environment variable must be an integer: {name}"
        ) from None
    if not minimum <= value <= maximum:
        raise ProviderConfigurationError(
            f"environment variable must be between {minimum} and {maximum}: {name}"
        )
    return value
