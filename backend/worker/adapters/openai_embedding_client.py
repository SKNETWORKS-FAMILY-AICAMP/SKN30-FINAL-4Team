"""OpenAI-backed embedding adapter for the worker-owned embedding port."""

from __future__ import annotations

import logging
import math
import inspect
import time
from typing import Any

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI

from ..ports.embedding import (
    EmbeddingBatch,
    EmbeddingInvalidResponseError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)


logger = logging.getLogger(__name__)


class OpenAIEmbeddingClient:
    """Call OpenAI embeddings with deterministic response validation.

    The registered model ID is supplied by deployment configuration, rather
    than embedded in pipeline code.  Dimensions are deliberately validated by
    the caller/DB contract, not guessed in this adapter.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        timeout_seconds: float = 60.0,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenAI API key must not be blank")
        if not model_name.strip():
            raise ValueError("OpenAI embedding model must not be blank")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("OpenAI timeout must be a finite positive number")

        self._model_name = model_name.strip()
        self._timeout_seconds = float(timeout_seconds)
        # The worker invokes this async-shaped port through several short-lived
        # ``asyncio.run`` loops.  Keep the provider client loop-neutral.
        self._client = client or OpenAI(
            api_key=api_key,
            timeout=self._timeout_seconds,
            max_retries=0,
        )

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding input must contain non-blank text")

        started_at = time.perf_counter()
        try:
            response = self._client.embeddings.create(
                model=self._model_name,
                input=texts,
                timeout=self._timeout_seconds,
            )
            if inspect.isawaitable(response):  # bounded offline async fake
                response = await response
        except APITimeoutError:
            raise EmbeddingTimeoutError("OpenAI embedding request timed out") from None
        except (APIConnectionError, APIStatusError, APIError):
            raise EmbeddingUnavailableError("OpenAI embedding request failed") from None
        except TimeoutError:
            raise EmbeddingTimeoutError("OpenAI embedding request timed out") from None
        except Exception as error:
            raise EmbeddingUnavailableError(
                f"OpenAI embedding request failed: {type(error).__name__}"
            ) from None

        model_name, vectors = _embedding_rows(response, expected_count=len(texts))
        usage = getattr(response, "usage", None)
        logger.info(
            "OpenAI embedding request completed model=%s inputs=%s prompt_tokens=%s "
            "total_tokens=%s duration_ms=%s",
            model_name,
            len(texts),
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "total_tokens", None),
            round((time.perf_counter() - started_at) * 1000),
        )
        return EmbeddingBatch(model_name=model_name, vectors=vectors)

    async def probe_dimension(self) -> int:
        """Ask the provider for a single vector when validating a deployment."""

        return len((await self.embed(["차원 확인"])).vectors[0])


def _embedding_rows(response: Any, *, expected_count: int) -> tuple[str, list[list[float]]]:
    try:
        model_name = getattr(response, "model", None)
        data = getattr(response, "data", None)
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("missing model")
        if not isinstance(data, list) or len(data) != expected_count:
            raise ValueError("invalid data count")
        ordered: list[list[float] | None] = [None] * expected_count
        for row in data:
            index = getattr(row, "index", None)
            vector = getattr(row, "embedding", None)
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < expected_count
                or ordered[index] is not None
                or not isinstance(vector, list)
                or not vector
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in vector
                )
            ):
                raise ValueError("invalid vector row")
            ordered[index] = [float(value) for value in vector]
        vectors = [vector for vector in ordered if vector is not None]
        if len(vectors) != expected_count or len({len(vector) for vector in vectors}) != 1:
            raise ValueError("inconsistent vectors")
        return model_name, vectors
    except (AttributeError, TypeError, ValueError):
        raise EmbeddingInvalidResponseError(
            "OpenAI returned an invalid embedding response"
        ) from None
