"""Shared OpenAPI-only transport contracts.

The exception handlers in :mod:`main` deliberately return a small, stable
error envelope instead of FastAPI's default ``HTTPValidationError`` shape.
Keeping that model here makes the generated client contract match the payload
that browsers actually receive.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    """Safe error payload returned by every public HTTP endpoint."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Stable, machine-readable application error code.")
    message: str = Field(description="Safe message suitable for display or logging.")
    errors: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional validation details. Secret request values are never included.",
    )


class HealthStatusResponse(BaseModel):
    """Liveness/readiness success payload."""

    model_config = ConfigDict(extra="forbid")

    status: str


_ERROR_DESCRIPTIONS = {
    400: "The authentication or request input was rejected.",
    401: "A required or valid authentication cookie was not supplied.",
    403: "The request origin or caller role is not allowed.",
    404: "The requested resource is not visible to the current user.",
    409: "An active analysis run already exists for this user.",
    413: "The uploaded file exceeds the configured size limit.",
    415: "The uploaded file type or file signature is unsupported.",
    422: "The request failed validation.",
    429: "The upstream authentication provider rate-limited the request.",
    500: "The server could not complete the request.",
    502: "The upstream authentication provider returned an invalid response.",
    503: "A required backend dependency is unavailable or not configured.",
}


def error_responses(*status_codes: int) -> dict[int, dict[str, object]]:
    """Build route response metadata using the single public error schema."""

    return {
        status_code: {
            "model": ErrorResponse,
            "description": _ERROR_DESCRIPTIONS[status_code],
        }
        for status_code in status_codes
    }
