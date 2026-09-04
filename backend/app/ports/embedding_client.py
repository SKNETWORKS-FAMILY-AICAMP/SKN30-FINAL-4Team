from dataclasses import dataclass
from typing import Protocol


class EmbeddingUnavailableError(RuntimeError):
    pass


class EmbeddingTimeoutError(RuntimeError):
    pass


class EmbeddingInvalidResponseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    model_name: str
    vectors: list[list[float]]


class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> EmbeddingBatch: ...
