import asyncio
import logging
import math
import time
from typing import Any

import httpx

from app.ports.embedding_client import (
    EmbeddingBatch,
    EmbeddingClient,
    EmbeddingInvalidResponseError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)


logger = logging.getLogger(__name__)


class OpenAIEmbeddingClient(EmbeddingClient):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("Embedding input must contain non-blank text")
        started_at = time.perf_counter()
        response = await self._post(
            {
                "model": self._model_name,
                "input": texts,
                "encoding_format": "float",
            }
        )
        try:
            body: Any = response.json()
            model_name = body["model"]
            data = body["data"]
            usage = body.get("usage", {})
            if not isinstance(model_name, str) or not model_name.strip():
                raise ValueError
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError
            ordered: list[list[float] | None] = [None] * len(texts)
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError
                index = item.get("index")
                vector = item.get("embedding")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(texts)
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
                    raise ValueError
                ordered[index] = [float(value) for value in vector]
            vectors = [vector for vector in ordered if vector is not None]
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise EmbeddingInvalidResponseError(
                "Embedding provider returned an invalid response"
            ) from None
        logger.info(
            "Embedding request completed model=%s inputs=%s prompt_tokens=%s "
            "total_tokens=%s duration_ms=%s",
            model_name,
            len(texts),
            usage.get("prompt_tokens") if isinstance(usage, dict) else None,
            usage.get("total_tokens") if isinstance(usage, dict) else None,
            round((time.perf_counter() - started_at) * 1000),
        )
        return EmbeddingBatch(model_name=model_name, vectors=vectors)

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                for attempt in range(2):
                    response = await client.post(
                        f"{self._base_url}/embeddings",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if response.status_code != 429 and response.status_code < 500:
                        break
                    if attempt == 0:
                        await asyncio.sleep(0.25)
        except httpx.TimeoutException:
            raise EmbeddingTimeoutError("Embedding request timed out") from None
        except httpx.RequestError:
            raise EmbeddingUnavailableError(
                "Embedding service is unavailable"
            ) from None
        if response.is_error:
            logger.warning(
                "Embedding provider HTTP error status=%s retryable=%s",
                response.status_code,
                response.status_code == 429 or response.status_code >= 500,
            )
            raise EmbeddingUnavailableError(
                "Embedding service rejected the request"
            )
        return response
