"""동기 워커에서 ``EmbeddingClient`` 포트를 호출하는 작은 경계.

검색·적재 코드는 임베딩 공급자의 HTTP 구현을 알지 않는다. 이 모듈에서만
비동기 포트를 동기 워커 호출로 바꾸고, 응답의 개수·차원·유한값·모델
identity를 검증한다. 테스트는 같은 경계에 FakeEmbeddingClient를 주입한다.
"""

from __future__ import annotations

import asyncio
import math

from app.ports.embedding_client import (
    EmbeddingBatch,
    EmbeddingClient,
    EmbeddingInvalidResponseError,
    EmbeddingUnavailableError,
    EmbeddingTimeoutError,
)


PORT_ERRORS: tuple[type[Exception], ...] = (
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
    EmbeddingInvalidResponseError,
)


def embed(
    client: EmbeddingClient | None,
    texts: list[str],
    *,
    expected_dimension: int,
    expected_model_name: str,
) -> EmbeddingBatch:
    """Call an injected async embedding client from a synchronous worker.

    ``None`` is deliberately an error rather than a fallback to a fake vector:
    production code must make the provider boundary explicit, while tests can
    inject a deterministic FakeEmbeddingClient.
    """

    if client is None:
        raise EmbeddingUnavailableError("Embedding client is not configured")
    if not texts or any(not isinstance(value, str) or not value.strip() for value in texts):
        raise ValueError("Embedding input must contain non-blank text")
    if expected_dimension < 1:
        raise ValueError("Embedding dimension must be positive")
    if not expected_model_name.strip():
        raise ValueError("Embedding model identity must not be blank")
    try:
        batch = asyncio.run(client.embed(texts))
    except PORT_ERRORS:
        raise
    except Exception as error:
        raise EmbeddingUnavailableError(
            f"Embedding port exception: {type(error).__name__}: {error}"
        ) from error
    _validate_batch(
        batch,
        expected_count=len(texts),
        expected_dimension=expected_dimension,
        expected_model_name=expected_model_name,
    )
    return batch


def _validate_batch(
    batch: EmbeddingBatch,
    *,
    expected_count: int,
    expected_dimension: int,
    expected_model_name: str,
) -> None:
    if not isinstance(batch, EmbeddingBatch):
        raise EmbeddingInvalidResponseError("Embedding response is not an EmbeddingBatch")
    if not isinstance(batch.model_name, str) or batch.model_name != expected_model_name:
        raise EmbeddingInvalidResponseError(
            "Embedding response model does not match the registered profile"
        )
    if (
        not isinstance(batch.vectors, list)
        or len(batch.vectors) != expected_count
        or not batch.vectors
    ):
        raise EmbeddingInvalidResponseError("Embedding response count does not match inputs")
    for vector in batch.vectors:
        if (
            not isinstance(vector, list)
            or len(vector) != expected_dimension
            or not vector
        ):
            raise EmbeddingInvalidResponseError(
                "Embedding response dimension does not match the registered profile"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise EmbeddingInvalidResponseError(
                "Embedding response contains a non-finite value"
            )
