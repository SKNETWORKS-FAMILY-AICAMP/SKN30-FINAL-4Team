from __future__ import annotations

import math

import pytest

from app.retrieval.embedding_inputs import (
    EMBEDDING_DIMENSIONS,
    MAX_INPUT_TOKENS,
    assemble_embedding_inputs,
    mean_pool_embeddings,
)


def _fact(
    fact_id: str,
    field: str,
    value: str,
    *,
    status: str = "identified",
    block: str = "hwp:b1",
    role: str | None = None,
) -> dict:
    return {
        "fact_id": fact_id,
        "field_name": field,
        "value_raw": value,
        "status": status,
        "semantic_role": role,
        "value_source": {"source_block_id": block, "start_char": 0},
    }


def _profile() -> dict:
    return {
        "comparison_profile": {
            "purpose_goal": [_fact("p", "purpose_goal", "지역 기업의 수출 확대")],
            "applicant_eligibility": [
                _fact(
                    "t2",
                    "applicant_eligibility",
                    "비영리기관",
                    block="hwp:b10",
                    role="주관연구개발기관",
                ),
                _fact(
                    "t1",
                    "applicant_eligibility",
                    "중소기업",
                    block="hwp:b2",
                ),
                _fact(
                    "skip",
                    "applicant_eligibility",
                    "미확정 값",
                    status="unresolved",
                ),
            ],
            "support_methods": [_fact("c", "support_methods", "사업화 자금 지원")],
        },
        "support_components": [],
    }


def test_assembles_a_and_b_without_generated_summary() -> None:
    result = assemble_embedding_inputs(_profile())

    assert list(result) == ["purpose", "target", "support", "combined"]
    assert result["purpose"].text == "지역 기업의 수출 확대"
    assert "중소기업\n주관연구개발기관: 비영리기관" in result["target"].text
    assert "미확정 값" not in result["target"].text
    assert "[사업목적]" in result["combined"].text
    assert "[지원내용]" in result["combined"].text
    assert len(result["combined"].input_sha256) == 64
    assert EMBEDDING_DIMENSIONS == 1536


def test_8192_is_hard_limit_and_chunking_never_truncates() -> None:
    result = assemble_embedding_inputs(_profile(), max_input_tokens=8)
    combined = result["combined"]

    assert len(combined.chunks) > 1
    assert all(chunk.token_count <= 8 for chunk in combined.chunks)
    assert "".join(chunk.text for chunk in combined.chunks) == combined.text
    assert MAX_INPUT_TOKENS == 8192


def test_rejects_limit_above_provider_contract() -> None:
    with pytest.raises(ValueError, match="between 1 and 8192"):
        assemble_embedding_inputs(_profile(), max_input_tokens=8193)


def test_chunk_vectors_are_token_weighted_and_normalised() -> None:
    pooled = mean_pool_embeddings([[1.0, 0.0], [0.0, 1.0]], [3, 1])

    assert pooled[0] > pooled[1]
    assert math.sqrt(sum(value * value for value in pooled)) == pytest.approx(1.0)
