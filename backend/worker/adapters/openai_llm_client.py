"""OpenAI-backed structured-output adapter for the worker LLM port.

The caller supplies model-profile names and their model IDs.  Credentials are
never read from or written to a payload or log; production construction uses
the normal ``OPENAI_API_KEY`` environment variable through ``OpenAIConfig``.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    OpenAI,
)
from pydantic import BaseModel, ValidationError

from ..ports.llm import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)


logger = logging.getLogger(__name__)


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
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenAI API key must not be blank")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("OpenAI timeout must be a finite positive number")
        profiles = {
            str(profile): str(model).strip()
            for profile, model in model_profiles.items()
            if str(profile).strip() and str(model).strip()
        }
        if not profiles:
            raise ValueError("At least one OpenAI LLM model profile is required")

        self._model_profiles = profiles
        self._timeout_seconds = float(timeout_seconds)
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

        started_at = time.perf_counter()
        try:
            # Let the SDK turn the Pydantic model into OpenAI's strict JSON
            # Schema dialect.  Passing ``model_json_schema()`` directly with
            # ``strict=True`` is not equivalent: optional/defaulted Pydantic
            # fields are not automatically added to the strict ``required``
            # lists and the API can reject the schema before inference.
            completion = self._client.chat.completions.parse(
                model=model,
                messages=[
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                response_format=response_schema,
                timeout=self._timeout_seconds,
            )
            if inspect.isawaitable(completion):  # bounded offline async fake
                completion = await completion
        except (ValidationError, LengthFinishReasonError, ContentFilterFinishReasonError):
            raise LLMInvalidResponseError(
                "OpenAI returned an invalid structured response"
            ) from None
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

        result = _structured_result(completion, response_schema)

        usage = getattr(completion, "usage", None)
        logger.info(
            "OpenAI structured request completed task=%s model=%s prompt_tokens=%s "
            "completion_tokens=%s total_tokens=%s duration_ms=%s",
            task_name,
            model,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            getattr(usage, "total_tokens", None),
            round((time.perf_counter() - started_at) * 1000),
        )
        return result


def _structured_result(completion: Any, response_schema: type[BaseModel]) -> BaseModel:
    """Read one parsed Chat Completions response without leaking its content."""

    try:
        choices = completion.choices
        if not isinstance(choices, list) or not choices:
            raise ValueError("missing choices")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason not in (None, "stop"):
            raise ValueError("incomplete completion")
        message = choice.message
        if getattr(message, "refusal", None):
            raise ValueError("model refused")

        parsed = getattr(message, "parsed", None)
        if isinstance(parsed, response_schema):
            return parsed
        if parsed is not None:
            try:
                return response_schema.model_validate(parsed)
            except ValidationError:
                raise LLMInvalidResponseError(
                    "OpenAI returned an invalid structured response"
                ) from None

        # Keep this fallback for bounded fakes and defensive compatibility
        # with SDK transports that expose content but no ``parsed`` value.
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
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        raise LLMInvalidResponseError("OpenAI returned an invalid structured response") from None
