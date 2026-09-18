"""Minimal LLM port used by the deterministic worker pipeline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "developer", "user", "assistant"]
    content: str


class LLMUnavailableError(RuntimeError):
    """The provider could not be reached or rejected the request."""


class LLMTimeoutError(RuntimeError):
    """The provider did not answer before the configured request deadline."""


LLM_INVALID_RESPONSE_REASON_CODES = frozenset({
    "invalid_response",
    "response_shape_invalid",
    "missing_choices",
    "incomplete_completion",
    "model_refusal",
    "missing_content",
    "invalid_json",
    "structured_response_not_object",
    "schema_validation_failed",
})
LLM_VALIDATION_ISSUE_LIMIT = 16
LLM_VALIDATION_LOC_DEPTH_LIMIT = 8


class LLMInvalidResponseError(RuntimeError):
    """The provider response was not valid for the requested output contract.

    ``raw`` remains private to the call-site recovery logic.  It must not be
    logged because model output can contain the uploaded request document.
    ``reason_code`` is closed and ``validation_issues`` contains only bounded
    ``loc``/``type`` metadata; provider adapters must remove input, messages,
    and context before constructing this exception.
    """

    def __init__(
        self,
        message: str,
        *,
        raw: dict[str, Any] | None = None,
        reason_code: str = "invalid_response",
        validation_issues: Sequence[Mapping[str, object]] = (),
    ) -> None:
        if reason_code not in LLM_INVALID_RESPONSE_REASON_CODES:
            raise ValueError("unsupported LLM invalid-response reason code")
        if len(validation_issues) > LLM_VALIDATION_ISSUE_LIMIT:
            raise ValueError("too many LLM validation issues")
        safe_issues: list[dict[str, object]] = []
        for issue in validation_issues:
            loc = issue.get("loc")
            issue_type = issue.get("type")
            if (
                not isinstance(loc, (list, tuple))
                or len(loc) > LLM_VALIDATION_LOC_DEPTH_LIMIT
                or any(type(item) not in (str, int) for item in loc)
                or not isinstance(issue_type, str)
                or not issue_type
            ):
                raise ValueError("invalid LLM validation issue metadata")
            safe_issues.append({"loc": tuple(loc), "type": issue_type})
        super().__init__(message)
        self.raw = raw
        self.reason_code = reason_code
        self.validation_issues = tuple(safe_issues)


class LLMClient(Protocol):
    async def generate_structured(
        self,
        *,
        task_name: str,
        messages: list[Message],
        response_schema: type[BaseModel],
        model_profile: str,
    ) -> BaseModel: ...
