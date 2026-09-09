"""Adversarial stress-test suite for Decoupled Model 1 & 2 async invocation.

Empirical Challenger 2 verification against Architecture v2.2 specifications:
1. Independent execution of Model 1 and Model 2 (no cross-triggering).
2. Failed Model 1 dependency guard in Model 2 (all failure envelopes return not_available).
3. FrozenInspectionContext edge cases (empty title, missing CPL fields, empty text, malformed data).
4. Concurrency & non-interference stress test.
5. End-to-end ML branch fault isolation in analysis_pipeline.
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.schemas.analysis_result import FrozenInspectionContext

# Add ml serving paths
_SHARED = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ml", "serving", "shared")
)
_MODEL1 = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ml", "serving", "model1")
)
for _p in (_SHARED, _MODEL1):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ml_orchestrator as ORCH
import result_envelope as RE
import runner

FIXTURE_PATH = os.path.join(_SHARED, "fixtures", "preconsultation_example.txt")


@pytest.fixture
def sample_document_text() -> str:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def sample_frozen_context(sample_document_text: str) -> FrozenInspectionContext:
    cpl_results = {
        "ruleset_version": "test-v1",
        "items": [
            {
                "field_code": "PURPOSE_GOAL",
                "status": "PRESENT",
                "occurrences": [
                    {
                        "raw_text": "ICT혁신기업의 기술개발 지원",
                        "block_id": "blk-01",
                        "extraction_method": "RULE",
                    }
                ],
            },
            {
                "field_code": "NEW_OR_CHANGED_CONTENT",
                "status": "PRESENT",
                "occurrences": [
                    {
                        "raw_text": "시장수요최적화 R&D 추진",
                        "block_id": "blk-02",
                        "extraction_method": "RULE",
                    }
                ],
            },
            {
                "field_code": "TARGET_AND_CONDITIONS",
                "status": "PRESENT",
                "occurrences": [
                    {
                        "raw_text": "ICT중소기업 및 벤처기업",
                        "block_id": "blk-03",
                        "extraction_method": "RULE",
                    }
                ],
            },
        ],
    }
    return FrozenInspectionContext(
        context_version="ctx-adv-test-202",
        case_id=202,
        title="ICT혁신기업 지원사업",
        document_title="ICT혁신기업 지원사업",
        document_text=sample_document_text,
        cpl_results=cpl_results,
    )


# ==============================================================================
# 1. Independent Execution: Model 1 does not trigger Model 2, and vice versa
# ==============================================================================


def test_independent_model_1_invocation_does_not_call_model_2(
    sample_frozen_context: FrozenInspectionContext,
) -> None:
    """Verify run_model_1_async executes independently without invoking Model 2."""
    mock_m1_engine = MagicMock()
    mock_m1_engine.predict.return_value = [
        {"confidence": 0.92, "support_type_pred": "연구개발", "status": "신뢰"}
    ]
    mock_m1_engine.HOLD_THRESHOLD = 0.20
    mock_m1_engine.TRUST_THRESHOLD = 0.35
    mock_m1_engine.MAX_LEN = 256
    mock_m1_engine._classes = ["연구개발"]

    with patch.object(ORCH, "run_model_2", wraps=ORCH.run_model_2) as spy_m2, \
         patch.object(runner, "_impl", return_value=mock_m1_engine):

        result = asyncio.run(ORCH.run_model_1_async(sample_frozen_context))

        assert result["status"] == "success"
        assert result["result"]["support_type"] == "연구개발"
        assert result["result"]["confidence"] == 0.92
        assert result["result"]["trust_grade"] == "trusted"
        # Model 2 must NOT have been called
        assert spy_m2.call_count == 0


def test_independent_model_2_invocation_does_not_call_model_1(
    sample_frozen_context: FrozenInspectionContext,
) -> None:
    """Verify run_model_2_async executes independently when provided with a Model 1 envelope."""
    with patch.object(ORCH, "run_model_1", wraps=ORCH.run_model_1) as spy_m1:
        mock_m1_envelope = {
            "status": "success",
            "result": {
                "support_type": "연구개발",
                "confidence": 0.89,
                "trust_grade": "trusted",
            },
        }
        result = asyncio.run(
            ORCH.run_model_2_async(
                mock_m1_envelope, sample_frozen_context, cohort="taxonomy"
            )
        )

        assert result["status"] == "success"
        assert result["result"]["predicted_per_recipient"]["amount"] > 0
        assert result["result"]["level"] in ("low", "mid", "high")
        # Model 1 must NOT have been called
        assert spy_m1.call_count == 0


# ==============================================================================
# 2. Dependency Guard: Failed Model 1 Envelopes in Model 2
# ==============================================================================


@pytest.mark.parametrize(
    "bad_m1_envelope,expected_depends_on",
    [
        (
            {
                "analysis_id": "202",
                "status": "failed",
                "error": {"code": "MODEL1_FAILED", "message": "Inference crash"},
            },
            "model_1",
        ),
        (
            RE.failed("202", "support_type_classifier", "1.0", "KLUE-BERT", "ERR", "OOM"),
            "model_1",
        ),
        (
            {
                "analysis_id": "202",
                "status": "not_available",
                "error": {"code": "DEPENDENCY_NOT_AVAILABLE", "message": "Prior stage failed"},
            },
            "model_1",
        ),
        (
            {
                "analysis_id": "202",
                "status": "insufficient_data",
                "error": {"code": "NO_TEXT", "message": "Document empty"},
            },
            "model_1",
        ),
        (
            {"analysis_id": "202", "status": "unknown_status"},
            "model_1",
        ),
        (
            # Success status but result is None
            {"analysis_id": "202", "status": "success", "result": None},
            "model_1.support_type",
        ),
        (
            # Success status but support_type is empty string
            {"analysis_id": "202", "status": "success", "result": {"support_type": ""}},
            "model_1.support_type",
        ),
        (
            # Success status but support_type is None
            {"analysis_id": "202", "status": "success", "result": {"support_type": None}},
            "model_1.support_type",
        ),
    ],
)
def test_model_2_async_dependency_guard_adversarial_envelopes(
    sample_frozen_context: FrozenInspectionContext,
    bad_m1_envelope: dict,
    expected_depends_on: str,
) -> None:
    """Assert run_model_2_async cleanly returns not_available without uncaught exceptions for all failure variants."""
    result = asyncio.run(
        ORCH.run_model_2_async(bad_m1_envelope, sample_frozen_context)
    )

    assert result["status"] == "not_available"
    assert result["result"] is None
    assert result["metadata"]["depends_on"] == expected_depends_on
    assert result["error"]["code"] == "DEPENDENCY_NOT_AVAILABLE"
    assert expected_depends_on in result["error"]["message"]


# ==============================================================================
# 3. Edge Cases: FrozenInspectionContext Edge Cases
# ==============================================================================


def test_frozen_context_with_empty_title(
    sample_document_text: str,
) -> None:
    """Empty title should safely fall back to first line of text or default without crashing."""
    context = FrozenInspectionContext(
        context_version="ctx-adv-empty-title",
        case_id=301,
        title="",
        document_title="",
        document_text=sample_document_text,
        cpl_results={},
    )

    mock_m1_success = {
        "status": "success",
        "result": {"support_type": "연구개발", "confidence": 0.9, "trust_grade": "trusted"},
    }

    result = asyncio.run(
        ORCH.run_model_2_async(mock_m1_success, context, cohort="taxonomy")
    )
    assert result["status"] == "success"
    assert result["result"]["predicted_per_recipient"]["amount"] > 0


def test_frozen_context_with_none_title_and_valid_text() -> None:
    """None title and document_title with valid text should safely fall back and succeed."""
    context = FrozenInspectionContext(
        context_version="ctx-adv-none-title",
        case_id=302,
        title=None,
        document_title=None,
        document_text="디지털 혁신 선도 중소기업 기술개발 지원사업 공고\n사업기간: 2024~2026\n지원규모: 5억원",
        cpl_results={},
    )

    mock_m1_success = {
        "status": "success",
        "result": {"support_type": "연구개발", "confidence": 0.9, "trust_grade": "trusted"},
    }

    result = asyncio.run(
        ORCH.run_model_2_async(mock_m1_success, context, cohort="taxonomy")
    )
    assert result["status"] == "success"
    assert result["result"]["predicted_per_recipient"]["amount"] > 0


def test_frozen_context_with_empty_document_text_handled_gracefully() -> None:
    """Completely empty text fails Model 2 feature validation gracefully without uncaught exceptions."""
    context = FrozenInspectionContext(
        context_version="ctx-adv-empty-everything",
        case_id=302,
        title=None,
        document_title=None,
        document_text="",
        cpl_results={},
    )

    mock_m1_success = {
        "status": "success",
        "result": {"support_type": "연구개발", "confidence": 0.9, "trust_grade": "trusted"},
    }

    result = asyncio.run(
        ORCH.run_model_2_async(mock_m1_success, context, cohort="taxonomy")
    )
    # Evidence text is mandatory for Model 2; handled cleanly as failed envelope
    assert result["status"] == "failed"
    assert result["result"] is None
    assert result["error"]["code"] == "MODEL2_INFERENCE_FAILED"
    assert "evidence_text" in result["error"]["message"]


def test_frozen_context_missing_all_cpl_fields_model_1_guard() -> None:
    """When CPL results are empty and title is empty, run_model_1_async returns failed envelope cleanly."""
    context = FrozenInspectionContext(
        context_version="ctx-adv-no-cpl",
        case_id=303,
        title="",
        document_title="",
        document_text="",
        cpl_results={"items": []},
    )

    result = asyncio.run(ORCH.run_model_1_async(context))
    assert result["status"] == "failed"
    assert result["result"] is None
    assert result["error"]["code"] in ("MODEL1_INPUT_INVALID", "MODEL1_EXECUTION_FAILED")


def test_frozen_context_duck_typing_with_empty_cpl() -> None:
    """Verify duck typing properties on FrozenInspectionContext remain robust with empty CPL."""
    ctx_empty = FrozenInspectionContext(
        context_version="ctx-adv-duck",
        case_id=304,
        document_text="샘플 본문",
        cpl_results={},
    )
    assert ctx_empty.items == []
    assert ctx_empty.ruleset_version == ""
    assert ctx_empty.confirmed_count == 0
    assert ctx_empty.confirmation_rate == 0.0
    assert ctx_empty.total_count == 0


def test_frozen_context_duck_typing_with_null_items_behavior() -> None:
    """Document empirical behavior when cpl_results has null items: .get('items', []) yields None."""
    ctx_null_items = FrozenInspectionContext(
        context_version="ctx-adv-duck-null",
        case_id=305,
        document_text="샘플 본문",
        cpl_results={"items": None},
    )
    # Note: dict.get('items', []) returns None when key 'items' is explicitly None.
    assert ctx_null_items.items is None


# ==============================================================================
# 4. Concurrency Stress Test: 20 Parallel Invocations
# ==============================================================================


def test_concurrent_decoupled_execution_stress(
    sample_document_text: str,
) -> None:
    """Stress test 20 concurrent invocations of decoupled Model 1 and Model 2 without race conditions."""
    async def _run_concurrent():
        mock_m1_envelope = {
            "status": "success",
            "result": {"support_type": "연구개발", "confidence": 0.85, "trust_grade": "trusted"},
        }

        contexts = [
            FrozenInspectionContext(
                context_version=f"ctx-conc-{i}",
                case_id=1000 + i,
                title=f"동시성 검증 사업 {i}",
                document_title=f"동시성 검증 문서 {i}",
                document_text=sample_document_text,
                cpl_results={},
            )
            for i in range(20)
        ]

        tasks = [
            ORCH.run_model_2_async(mock_m1_envelope, ctx, cohort="taxonomy")
            for ctx in contexts
        ]

        return await asyncio.gather(*tasks)

    results = asyncio.run(_run_concurrent())

    assert len(results) == 20
    for i, res in enumerate(results):
        assert res["status"] == "success"
        assert res["analysis_id"] == str(1000 + i)
        assert res["result"]["predicted_per_recipient"]["amount"] > 0


# ==============================================================================
# 5. ML Branch Fault Isolation in analysis_pipeline
# ==============================================================================


def test_ml_branch_pipeline_fault_isolation(
    sample_frozen_context: FrozenInspectionContext,
) -> None:
    """When Model 1 fails inside _run_ml_branch, Model 2 and 3 must degrade gracefully to not_available."""
    from app.services.analysis_pipeline import _run_ml_branch

    async def _test():
        with patch("app.services.analysis_pipeline._run_model_1") as mock_m1:
            mock_m1.side_effect = RuntimeError("Simulated Model 1 torch exception")

            ml_output = await _run_ml_branch(sample_frozen_context)

            assert ml_output is not None
            assert ml_output["analysis_id"] == str(sample_frozen_context.case_id)
            assert ml_output["model_1"]["status"] == "failed"
            assert ml_output["model_2"]["status"] == "not_available"
            assert ml_output["model_2"]["metadata"]["depends_on"] == "model_1"
            assert ml_output["model_3"]["status"] == "not_available"
            assert ml_output["model_3"]["metadata"]["depends_on"] == "model_1"
            assert ml_output["summary"]["partial_failure"] is False
            assert ml_output["summary"]["all_success"] is False
            assert ml_output["summary"]["counts"]["failed"] == 1
            assert ml_output["summary"]["counts"]["not_available"] == 2
            return ml_output

    asyncio.run(_test())
