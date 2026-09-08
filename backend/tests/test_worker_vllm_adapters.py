"""vLLM 어댑터 계약 테스트. 전부 MockTransport 로 오프라인 실행된다."""

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from app.ports.embedding_client import (
    EmbeddingInvalidResponseError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)
from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from worker.adapters.vllm_embedding_client import VllmEmbeddingClient
from worker.adapters.vllm_llm_client import VllmLLMClient


class _Answer(BaseModel):
    verdict: str
    score: int


def _llm(handler) -> VllmLLMClient:
    return VllmLLMClient(
        api_key="test-key",
        base_url="https://vllm.invalid/v1",
        model_profiles={"structuring": "served-gemma"},
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def _embedding(handler) -> VllmEmbeddingClient:
    return VllmEmbeddingClient(
        api_key="test-key",
        base_url="https://vllm.invalid/v1",
        model_name="served-embedding",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def _chat_body(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _generate(client: VllmLLMClient):
    return asyncio.run(
        client.generate_structured(
            task_name="request_source_selection_v012",
            messages=[Message(role="user", content="ping")],
            response_schema=_Answer,
            model_profile="structuring",
        )
    )


def test_llm_parses_structured_response_and_sends_deterministic_body():
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200, json=_chat_body(json.dumps({"verdict": "ok", "score": 3}))
        )

    result = _generate(_llm(handler))

    assert isinstance(result, _Answer)
    assert (result.verdict, result.score) == ("ok", 3)
    assert sent["model"] == "served-gemma"
    assert sent["temperature"] == 0
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert (
        sent["response_format"]["json_schema"]["name"]
        == "request_source_selection_v012"
    )
    assert sent["response_format"]["json_schema"]["schema"] == _Answer.model_json_schema()


def test_llm_timeout_maps_to_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMTimeoutError):
        _generate(_llm(handler))


def test_llm_service_unavailable_maps_to_unavailable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "overloaded"})

    with pytest.raises(LLMUnavailableError):
        _generate(_llm(handler))


def test_llm_non_json_body_maps_to_invalid_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    with pytest.raises(LLMInvalidResponseError):
        _generate(_llm(handler))


def test_llm_schema_violating_content_maps_to_invalid_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_chat_body(json.dumps({"verdict": "ok", "score": "삼"}))
        )

    with pytest.raises(LLMInvalidResponseError) as caught:
        _generate(_llm(handler))

    assert caught.value.raw == {"verdict": "ok", "score": "삼"}
    assert "삼" not in str(caught.value)
    assert "삼" not in repr(caught.value)


def test_llm_non_stop_finish_reason_is_incomplete_not_schema_invalid():
    def handler(request: httpx.Request) -> httpx.Response:
        body = _chat_body(json.dumps({"verdict": "ok", "score": 3}))
        body["choices"][0]["finish_reason"] = "length"
        return httpx.Response(
            200,
            json=body,
        )

    with pytest.raises(LLMInvalidResponseError, match="incomplete") as caught:
        _generate(_llm(handler))

    assert caught.value.raw is None


def test_list_models_returns_served_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "google/gemma-3-27b-it", "object": "model"},
                    {"id": "BAAI/bge-m3", "object": "model"},
                ],
            },
        )

    assert asyncio.run(_llm(handler).list_models()) == [
        "google/gemma-3-27b-it",
        "BAAI/bge-m3",
    ]


def test_embedding_orders_vectors_by_index_not_array_position():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(
            200,
            json={
                "model": "served-embedding",
                "data": [
                    {"index": 2, "embedding": [3.0, 3.0]},
                    {"index": 0, "embedding": [1.0, 1.0]},
                    {"index": 1, "embedding": [2.0, 2.0]},
                ],
            },
        )

    batch = asyncio.run(_embedding(handler).embed(["가", "나", "다"]))

    assert batch.model_name == "served-embedding"
    assert batch.vectors == [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]


def test_embedding_count_mismatch_maps_to_invalid_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "served-embedding",
                "data": [{"index": 0, "embedding": [1.0, 1.0]}],
            },
        )

    with pytest.raises(EmbeddingInvalidResponseError):
        asyncio.run(_embedding(handler).embed(["가", "나"]))


def test_probe_dimension_returns_vector_length():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "served-embedding",
                "data": [{"index": 0, "embedding": [0.1] * 1024}],
            },
        )

    assert asyncio.run(_embedding(handler).probe_dimension()) == 1024


def test_embedding_rejects_empty_input():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the network")

    with pytest.raises(ValueError):
        asyncio.run(_embedding(handler).embed([]))


def test_embedding_timeout_and_unavailable_map_to_their_errors():
    def timing_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "overloaded"})

    with pytest.raises(EmbeddingTimeoutError):
        asyncio.run(_embedding(timing_out).embed(["가"]))
    with pytest.raises(EmbeddingUnavailableError):
        asyncio.run(_embedding(refusing).embed(["가"]))
