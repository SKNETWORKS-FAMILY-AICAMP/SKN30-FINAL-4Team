"""Typed public API errors.

HTTP status and the machine-readable ``code`` are independent axes of the
error contract (v0.2 spec section 4). A bare ``fastapi.HTTPException`` still
works and falls back to a single generic code per status in
``main.py``'s exception handler, but every route that can fail for more than
one *reason* at the same status code (most notably every 409) must raise
:class:`ApiError` with an explicit ``code`` so the global handler never has
to guess — and never collapses distinct conflicts into one opaque mapping.
"""

from __future__ import annotations

from fastapi import HTTPException


class ApiError(HTTPException):
    """An ``HTTPException`` that carries its own stable domain error code."""

    def __init__(self, status_code: int, *, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code


def unauthorized(message: str = "Authentication is required") -> ApiError:
    return ApiError(401, code="UNAUTHORIZED", message=message)


def not_found(message: str = "The requested resource was not found") -> ApiError:
    # Deliberately identical whether the row is missing or owned by someone
    # else: existence must not be distinguishable across owners.
    return ApiError(404, code="NOT_FOUND", message=message)


def analysis_run_active(message: str = "An active analysis run already exists") -> ApiError:
    return ApiError(409, code="ANALYSIS_RUN_ACTIVE", message=message)


def active_result_session(message: str = "An active result session must be closed first") -> ApiError:
    return ApiError(409, code="ACTIVE_RESULT_SESSION", message=message)


def idempotency_key_conflict(message: str = "Idempotency-Key was reused with different input") -> ApiError:
    return ApiError(409, code="IDEMPOTENCY_KEY_CONFLICT", message=message)


def chat_conflict(message: str = "The chat message cannot be processed in its current state") -> ApiError:
    return ApiError(409, code="CHAT_CONFLICT", message=message)


def chat_retry_exhausted(message: str = "The retry budget for this message is exhausted") -> ApiError:
    return ApiError(409, code="CHAT_RETRY_EXHAUSTED", message=message)


def validation_error(message: str = "Request validation failed") -> ApiError:
    return ApiError(422, code="VALIDATION_ERROR", message=message)


def service_unavailable(message: str = "A required backend dependency is unavailable") -> ApiError:
    return ApiError(503, code="SERVICE_UNAVAILABLE", message=message)


__all__ = [
    "ApiError",
    "active_result_session",
    "analysis_run_active",
    "chat_conflict",
    "chat_retry_exhausted",
    "idempotency_key_conflict",
    "not_found",
    "service_unavailable",
    "unauthorized",
    "validation_error",
]
