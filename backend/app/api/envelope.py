"""Consistent response envelope used by every public API."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field


T = TypeVar("T")


class ApiEnvelope(BaseModel, Generic[T]):
    code: str = "OK"
    message: str = "success"
    data: T | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


def ok(data: T | None = None, *, message: str = "success") -> ApiEnvelope[T]:
    return ApiEnvelope(data=data, message=message)
