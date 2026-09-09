"""Unit tests for Decoupled Model 1 & 2 async functions and interfaces (Architecture v2.2)."""
import asyncio
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.analysis_result import FrozenInspectionContext

_SHARED = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ml", "serving", "shared")
)
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

import ml_orchestrator as ORCH
import result_envelope as RE

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
                        "raw_text": "시장수요최적화 R&D",
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
                        "raw_text": "ICT중소기업",
                        "block_id": "blk-03",
                        "extraction_method": "RULE",
                    }
                ],
            },
        ],
    }
    return FrozenInspectionContext(
        context_version="ctx-test-101-abcdef",
        case_id=101,
        title="ICT혁신기업 지원사업",
        document_title="ICT혁신기업 지원사업",
        document_text=sample_document_text,
        cpl_results=cpl_results,
    )


def test_frozen_context_immutability(sample_frozen_context: FrozenInspectionContext) -> None:
    """FrozenInspectionContext는 불변(frozen)이어야 하며 필드 변경 시 예외 발생 검증."""
    with pytest.raises((ValidationError, TypeError)):
        sample_frozen_context.document_text = "new text"  # type: ignore[misc]

    with pytest.raises((ValidationError, TypeError)):
        sample_frozen_context.case_id = 999  # type: ignore[misc]


def test_model_2_dependency_guard_sync(sample_frozen_context: FrozenInspectionContext) -> None:
    """Model 1이 실패하면 Model 2는 실행하지 않고 not_available을 반환해야 함."""
    failed_m1 = RE.failed(
        "101",
        "support_type_classifier",
        "1.0",
        "KLUE-BERT",
        code="MODEL1_FAILED",
        message="Model 1 classification error",
    )
    m2_result = ORCH.run_model_2(failed_m1, sample_frozen_context)
    assert m2_result["status"] == "not_available"
    assert m2_result["error"]["code"] == "DEPENDENCY_NOT_AVAILABLE"
    assert m2_result["metadata"]["depends_on"] == "model_1"
    assert m2_result["result"] is None


def test_model_2_dependency_guard_async(sample_frozen_context: FrozenInspectionContext) -> None:
    """run_model_2_async 비동기 호출에서도 선행 모델 실패 시 not_available 반환 검증."""
    failed_m1 = {"status": "failed", "error": {"code": "ERR", "message": "Failed"}}
    m2_result = asyncio.run(ORCH.run_model_2_async(failed_m1, sample_frozen_context))
    assert m2_result["status"] == "not_available"
    assert m2_result["metadata"]["depends_on"] == "model_1"


def test_model_3_dependency_guard_async(sample_frozen_context: FrozenInspectionContext) -> None:
    """run_model_3_async 비동기 호출에서도 Model 1 실패 시 not_available 반환 검증."""
    failed_m1 = {"status": "failed", "error": {"code": "ERR", "message": "Failed"}}
    m3_result = asyncio.run(ORCH.run_model_3_async(failed_m1, sample_frozen_context))
    assert m3_result["status"] == "not_available"
    assert m3_result["metadata"]["depends_on"] == "model_1"


def test_model_2_async_execution_with_context(sample_frozen_context: FrozenInspectionContext) -> None:
    """Model 1 성공 결과와 FrozenInspectionContext로 Model 2 비동기 실행 및 정상 결과 산출 검증."""
    mock_m1_success = {
        "status": "success",
        "result": {
            "support_type": "연구개발",
            "confidence": 0.95,
            "trust_grade": "trusted",
        },
    }
    m2_result = asyncio.run(
        ORCH.run_model_2_async(mock_m1_success, sample_frozen_context, cohort="taxonomy")
    )
    assert m2_result["status"] == "success"
    assert m2_result["result"]["predicted_per_recipient"]["amount"] > 0
    assert m2_result["result"]["level"] in ("low", "mid", "high")
    assert m2_result["result"]["reference"]["sample_count"] >= 30
    assert m2_result["result"]["cohort_percentile"] is not None


def test_model_3_async_execution_with_context(sample_frozen_context: FrozenInspectionContext) -> None:
    """Model 1 성공 결과와 FrozenInspectionContext로 Model 3 비동기 실행 및 정상 결과 산출 검증."""
    mock_m1_success = {
        "status": "success",
        "result": {
            "support_type": "연구개발",
            "confidence": 0.95,
            "trust_grade": "trusted",
        },
    }
    m3_result = asyncio.run(ORCH.run_model_3_async(mock_m1_success, sample_frozen_context))
    assert m3_result["status"] == "success"
    assert m3_result["result"]["distance_percentile"] >= 0.0
    assert m3_result["result"]["available_axis_count"] >= 2
    assert m3_result["result"]["anomaly_level"] is None
    assert m3_result["result"]["anomaly_level_status"] == "threshold_undetermined"
    assert "top1_axis" not in m3_result["result"]
    assert "top1_axis" in m3_result["metadata"]
