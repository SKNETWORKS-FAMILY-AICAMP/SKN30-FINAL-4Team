"""Minimal embedding port used by the worker retrieval boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class EmbeddingUnavailableError(RuntimeError):
    """The embedding provider could not be reached or rejected the request."""


class EmbeddingTimeoutError(RuntimeError):
    """The embedding provider did not answer before its configured deadline."""


class EmbeddingInvalidResponseError(RuntimeError):
    """The embedding provider returned an unusable response."""


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    model_name: str
    vectors: list[list[float]]


class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> EmbeddingBatch: ...
