"""OpenAI-backed structured-output adapter for the worker LLM port.

The caller supplies model-profile names and their model IDs.  Credentials are
never read from or written to a payload or log; production construction uses
the normal ``OPENAI_API_KEY`` environment variable through ``OpenAIConfig``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import inspect
import json
import logging
import math
import time
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)
from openai.lib._parsing import type_to_response_format_param
from pydantic import BaseModel, ValidationError

from ..ports.llm import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpenAICompletionTelemetry:
    """Safe provider-completion accounting with no request or response data.

    This records only deployment and numeric provider usage after OpenAI has
    returned a completion.  It deliberately has no message, raw JSON,
    response-id, credential, or request-hash member.  A locally invalid JSON
    Schema response still receives this event because the provider consumed
    tokens before the local validator rejects it.
    """

    task_name: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_ms: int
    outcome: str = "provider_completed"


def _usage_count(value: object) -> int | None:
    """Accept only provider-shaped non-negative integer usage counters."""

    return value if type(value) is int and value >= 0 else None


class OpenAILLMClient:
    """Adapt OpenAI Chat Completions JSON Schema output to ``LLMClient``.

    ``max_retries=0`` and the per-call timeout make the deadline owned by the
    worker.  Queue retry policy remains outside this adapter, so a provider
    retry cannot silently outlive a dispatch lease.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model_profiles: Mapping[str, str],
        timeout_seconds: float = 60.0,
        max_completion_tokens: int | None = None,
        reasoning_effort: str | None = None,
        client: Any | None = None,
        telemetry_callback: Callable[[OpenAICompletionTelemetry], None] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenAI API key must not be blank")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("OpenAI timeout must be a finite positive number")
        if max_completion_tokens is not None and (
            type(max_completion_tokens) is not int or max_completion_tokens <= 0
        ):
            raise ValueError("OpenAI max_completion_tokens must be a positive integer")
        if reasoning_effort is not None and reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("OpenAI reasoning_effort must be low, medium, or high")
        profiles = {
            str(profile): str(model).strip()
            for profile, model in model_profiles.items()
            if str(profile).strip() and str(model).strip()
        }
        if not profiles:
            raise ValueError("At least one OpenAI LLM model profile is required")

        self._model_profiles = profiles
        self._timeout_seconds = float(timeout_seconds)
        self._max_completion_tokens = max_completion_tokens
        self._reasoning_effort = reasoning_effort
        self._telemetry_callback = telemetry_callback
        # The vendored synchronous orchestration opens a short-lived event
        # loop for each structured call.  A long-lived AsyncOpenAI/httpx client
        # can become bound to the first such loop and fail on the next call.
        # Use the synchronous SDK underneath this async-shaped port; the worker
        # already runs the whole handler in its own synchronous process/thread.
        self._client = client or OpenAI(
            api_key=api_key,
            timeout=self._timeout_seconds,
            max_retries=0,
        )

    async def generate_structured(
        self,
        *,
        task_name: str,
        messages: list[Message],
        response_schema: type[BaseModel],
        model_profile: str,
    ) -> BaseModel:
        model = self._model_profiles.get(model_profile)
        if model is None:
            raise LLMUnavailableError("Unknown OpenAI LLM model profile")
        if not task_name.strip() or not messages:
            raise ValueError("Structured LLM task name and messages are required")

        # Keep deterministic schema-conversion failures out of the provider
        # outage/retry path.  ``response_schema`` is an internal programming
        # contract, not a transient OpenAI transport concern.
        response_format = type_to_response_format_param(response_schema)
        started_at = time.perf_counter()
        try:
            # Reuse the OpenAI 2.x SDK's strict-schema conversion, but ask for
            # an ordinary completion so cross-field Pydantic validation stays
            # under this adapter's control.  The SDK ``parse`` helper raises
            # before exposing content when a model validator fails, which
            # prevents the caller's bounded in-memory normalization/repair.
            request = {
                "model": model,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                "response_format": response_format,
                "n": 1,
                "store": False,
                "timeout": self._timeout_seconds,
            }
            if self._max_completion_tokens is not None:
                request["max_completion_tokens"] = self._max_completion_tokens
            if self._reasoning_effort is not None:
                request["reasoning_effort"] = self._reasoning_effort
            completion = self._client.chat.completions.create(
                **request,
            )
            if inspect.isawaitable(completion):  # bounded offline async fake
                completion = await completion
        except APITimeoutError:
            raise LLMTimeoutError("OpenAI request timed out") from None
        except (APIConnectionError, APIStatusError, APIError):
            raise LLMUnavailableError("OpenAI request failed") from None
        except TimeoutError:
            # Supports a bounded fake client without depending on SDK internals.
            raise LLMTimeoutError("OpenAI request timed out") from None
        except Exception as error:  # provider SDK/transport errors are not document errors
            raise LLMUnavailableError(
                f"OpenAI request failed: {type(error).__name__}"
            ) from None

        usage = getattr(completion, "usage", None)
        prompt_tokens = _usage_count(getattr(usage, "prompt_tokens", None))
        completion_tokens = _usage_count(
            getattr(usage, "completion_tokens", None)
        )
        total_tokens = _usage_count(getattr(usage, "total_tokens", None))
        duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))
        self._emit_completion_telemetry(
            OpenAICompletionTelemetry(
                task_name=task_name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms,
            )
        )
        logger.info(
            "OpenAI structured response received task=%s model=%s prompt_tokens=%s "
            "completion_tokens=%s total_tokens=%s duration_ms=%s",
            task_name,
            model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            duration_ms,
        )
        # Record provider-side usage before local schema validation.  An
        # invalid response still consumed tokens, but must never be described
        # as a successful structured result in operator logs.
        return _structured_result(completion, response_schema)

    def _emit_completion_telemetry(
        self, telemetry: OpenAICompletionTelemetry
    ) -> None:
        """Call an optional accounting sink without changing LLM semantics."""

        callback = self._telemetry_callback
        if callback is None:
            return
        try:
            callback(telemetry)
        except Exception:  # telemetry must not fail a completed LLM request
            return


def _structured_result(completion: Any, response_schema: type[BaseModel]) -> BaseModel:
    """Validate one Chat Completions JSON response without logging its content."""

    try:
        choices = completion.choices
        if not isinstance(choices, list) or not choices:
            raise ValueError("missing choices")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason != "stop":
            raise ValueError("incomplete completion")
        message = choice.message
        if getattr(message, "refusal", None):
            raise ValueError("model refused")

        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("missing content")
        raw = json.loads(content)
        if not isinstance(raw, dict):
            raise ValueError("structured response is not an object")
        try:
            return response_schema.model_validate(raw)
        except ValidationError:
            raise LLMInvalidResponseError(
                "OpenAI returned an invalid structured response", raw=raw
            ) from None
    except LLMInvalidResponseError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise LLMInvalidResponseError("OpenAI returned an invalid structured response") from None
