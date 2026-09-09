"""Unit tests for FIT 3 Parallel Subagents, No-Loop Validator, and Runner (Architecture v2.2)."""
import asyncio
import json
from pathlib import Path
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.ports.llm_client import LLMTimeoutError
from app.schemas.analysis_result import FrozenInspectionContext
from app.schemas.cpl import (
    CPL_FIELDS,
    CplAxisCode,
    CplFieldCode,
    CplItem,
    CplOccurrence,
    CplResult,
    CplSourceRole,
    CplStatus,
)
from app.schemas.fit import (
    FIT_RELATIONS,
    FitRelationId,
    FitRelationResult,
    FitResult,
    FitSemanticResponse,
    FitStatus,
)
from app.services.fit.fit_engine import load_fit_prompt, load_fit_scoring, _relation_result
from app.services.fit.runner import run_fit_subagents_parallel
from app.services.fit.subagents import (
    run_fit_subagent_1,
    run_fit_subagent_2,
    run_fit_subagent_3,
)
from app.services.fit.validator import validate_and_aggregate_fit


class FakeSubagentLlm:
    def __init__(self, response: FitSemanticResponse) -> None:
        self.response = response
        self.calls = 0

    async def generate_structured(self, **kwargs):
        self.calls += 1
        return self.response


def _cpl_occurrence(
    field_code: CplFieldCode,
    axis_code: CplAxisCode,
    role: CplSourceRole | None = None,
    raw_text: str = "text",
    normalized: dict | None = None,
) -> CplOccurrence:
    return CplOccurrence(
        raw_text=raw_text,
        normalized_value=normalized or {"text": raw_text},
        axis_code=axis_code,
        source_role=role,
        block_id=f"block:{field_code.value}:0",
        source_locator={"index": "0"},
        extraction_method="RULE",
    )


def _complete_cpl_fixture() -> CplResult:
    items = []
    for field in CPL_FIELDS:
        occs = []
        if field == CplFieldCode.PURPOSE_GOAL:
            occs = [
                _cpl_occurrence(field, CplAxisCode.PURPOSE_TARGET_CONDITION, CplSourceRole.TARGET, "목적 대상"),
                _cpl_occurrence(field, CplAxisCode.PURPOSE_DIRECTION, CplSourceRole.SUPPORT_CONTENT, "목적 내용"),
            ]
        elif field == CplFieldCode.TARGET_AND_CONDITIONS:
            occs = [
                _cpl_occurrence(field, CplAxisCode.TARGET_GROUP, CplSourceRole.TARGET, "지원 대상"),
                _cpl_occurrence(field, CplAxisCode.COND_CERTIFICATION, CplSourceRole.CONDITION, "지원 조건"),
            ]
        elif field == CplFieldCode.SUPPORT_CONTENT_AND_SCALE:
            occs = [
                _cpl_occurrence(
                    field,
                    CplAxisCode.SUPPORT_ACTIVITY,
                    CplSourceRole.SUPPORT_CONTENT,
                    "지원 활동",
                ),
                _cpl_occurrence(
                    field,
                    CplAxisCode.PER_COMPANY_LIMIT,
                    CplSourceRole.SUPPORT_CONTENT,
                    "기업당 1억원",
                    {"amount_won": 100000000, "unit": "KRW"},
                ),
                _cpl_occurrence(
                    field,
                    CplAxisCode.PER_COMPANY_LIMIT,
                    CplSourceRole.SUPPORT_SCALE,
                    "기업당 1억원",
                    {"amount_won": 100000000, "unit": "KRW"},
                ),
            ]
        elif field == CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE:
            occs = [
                _cpl_occurrence(field, CplAxisCode.EFFECT_CONTENT, CplSourceRole.EXPECTED_EFFECT, "기술 효과"),
            ]
        elif field == CplFieldCode.DELIVERY_SYSTEM:
            occs = [
                _cpl_occurrence(field, CplAxisCode.DELIVERY_ORG_NAME, CplSourceRole.DELIVERY_ORG, "전담기관"),
                _cpl_occurrence(field, CplAxisCode.DELIVERY_PROCEDURE_STEP, CplSourceRole.DELIVERY_PROCEDURE, "절차"),
            ]
        items.append(
            CplItem(
                field_code=field,
                status=CplStatus.PRESENT if occs else CplStatus.NOT_APPLICABLE,
                occurrences=occs,
            )
        )
    return CplResult(ruleset_version="cpl-test-v1", items=items)


def _semantic_response() -> FitSemanticResponse:
    return FitSemanticResponse.model_validate(
        {
            "relations": [
                {
                    "relation_id": "FIT-1",
                    "status": "FIT",
                    "summary": "목적 대상 일치",
                    "left_evidence_refs": ["PURPOSE_GOAL:0"],
                    "right_evidence_refs": ["TARGET_AND_CONDITIONS:0"],
                    "reason_code": None,
                },
                {
                    "relation_id": "FIT-2",
                    "status": "FIT",
                    "summary": "목적 내용 일치",
                    "left_evidence_refs": ["PURPOSE_GOAL:1"],
                    "right_evidence_refs": ["SUPPORT_CONTENT_AND_SCALE:0"],
                    "reason_code": None,
                },
                {
                    "relation_id": "FIT-3",
                    "status": "FIT",
                    "summary": "목적 효과 일치",
                    "left_evidence_refs": ["PURPOSE_GOAL:1"],
                    "right_evidence_refs": ["EXPECTED_EFFECTS_AND_PERFORMANCE:0"],
                    "reason_code": None,
                },
                {
                    "relation_id": "FIT-5",
                    "status": "FIT",
                    "summary": "대상 조건 일치",
                    "left_evidence_refs": ["TARGET_AND_CONDITIONS:0"],
                    "right_evidence_refs": ["TARGET_AND_CONDITIONS:1"],
                    "reason_code": None,
                },
                {
                    "relation_id": "FIT-6",
                    "status": "FIT",
                    "summary": "수행기관 절차 일치",
                    "left_evidence_refs": ["DELIVERY_SYSTEM:0"],
                    "right_evidence_refs": ["DELIVERY_SYSTEM:1"],
                    "reason_code": None,
                },
            ]
        }
    )


def test_subagents_parallel_execution() -> None:
    """3개 서브에이전트 병렬 실행 및 7축 정렬 검증."""
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret="test-secret-that-is-at-least-32-bytes",
    )
    cpl = _complete_cpl_fixture()
    fake_llm = FakeSubagentLlm(_semantic_response())

    result = asyncio.run(
        run_fit_subagents_parallel(
            cpl,
            fake_llm,
            settings=settings,
            case_id=101,
        )
    )

    assert isinstance(result, FitResult)
    assert len(result.relations) == 7
    relation_ids = [r.relation_id for r in result.relations]
    assert tuple(relation_ids) == FIT_RELATIONS
    assert result.ruleset_version == settings.fit_ruleset_version
    assert result.prompt_version == settings.fit_prompt_version


def test_subagent_2_fault_isolation() -> None:
    """Subagent 2에 LLM Timeout 발생 시 Sub 1 & Sub 3 결과는 100% 보존되고 FIT-6만 격리 검증."""
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret="test-secret-that-is-at-least-32-bytes",
    )
    cpl = _complete_cpl_fixture()

    class TimeoutLlmOnSub2:
        async def generate_structured(self, **kwargs):
            messages = kwargs.get("messages", [])
            user_msg = messages[1].content if len(messages) > 1 else ""
            if "FIT-6" in user_msg:
                raise LLMTimeoutError("FIT-6 timed out")
            return _semantic_response()

    result = asyncio.run(
        run_fit_subagents_parallel(
            cpl,
            TimeoutLlmOnSub2(),
            settings=settings,
            case_id=102,
        )
    )

    by_id = {r.relation_id: r for r in result.relations}
    # Sub 1 (FIT-1, 2, 3) must be preserved as FIT
    assert by_id[FitRelationId.FIT_1].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_2].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_3].status == FitStatus.FIT
    # Sub 2 (FIT-4 rule, FIT-6 timeout)
    assert by_id[FitRelationId.FIT_4].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_6].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_6].reason_code == "LLM_TIMEOUT"
    # Sub 3 (FIT-5, 7) must be preserved
    assert by_id[FitRelationId.FIT_5].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_7].status == FitStatus.FIT


def test_subagent_uncaught_crash_isolation() -> None:
    """특정 서브에이전트가 예기치 않게 crash되더라도 형제 서브에이전트 결과는 온전히 보존됨을 검증."""
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret="test-secret-that-is-at-least-32-bytes",
    )
    scoring = load_fit_scoring(settings.fit_scoring_path)
    cpl = _complete_cpl_fixture()

    # Subagent 1 crashes with RuntimeError
    crashed_sub1 = RuntimeError("Unexpected disk error")
    # Subagent 2 produces valid result
    sub2_results = {
        FitRelationId.FIT_4: _relation_result(
            FitRelationId.FIT_4,
            FitStatus.INSUFFICIENT,
            "계층 기준 없음",
            [],
            [],
            "HIERARCHY_COMPARISON_NOT_AVAILABLE",
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        ),
        FitRelationId.FIT_6: _relation_result(
            FitRelationId.FIT_6,
            FitStatus.FIT,
            "정상",
            [],
            [],
            None,
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        ),
    }
    # Subagent 3 produces valid result
    sub3_results = {
        FitRelationId.FIT_5: _relation_result(
            FitRelationId.FIT_5,
            FitStatus.FIT,
            "정상",
            [],
            [],
            None,
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        ),
        FitRelationId.FIT_7: _relation_result(
            FitRelationId.FIT_7,
            FitStatus.FIT,
            "정상",
            [],
            [],
            None,
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        ),
    }

    result = validate_and_aggregate_fit(
        subagent_outputs=[crashed_sub1, (sub2_results, []), (sub3_results, [])],
        actual_cpl=cpl,
        scoring=scoring,
        ruleset_version=settings.fit_ruleset_version,
        prompt_version=settings.fit_prompt_version,
        model_profile=settings.fit_model_profile,
    )

    by_id = {r.relation_id: r for r in result.relations}
    # FIT-1, 2, 3 must be marked UNAVAILABLE without crashing
    assert by_id[FitRelationId.FIT_1].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_1].reason_code == "UNAVAILABLE"
    # Sub 2 and Sub 3 results remain intact
    assert by_id[FitRelationId.FIT_6].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_7].status == FitStatus.FIT


def test_no_loop_validator_policy_neutrality() -> None:
    """서술문에 '부적격', '중복사업' 등 단정적 표현 유입 시 가드레일이 '확인 필요'로 정제함을 검증."""
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret="test-secret-that-is-at-least-32-bytes",
    )
    scoring = load_fit_scoring(settings.fit_scoring_path)
    cpl = _complete_cpl_fixture()

    non_neutral_result = {
        FitRelationId.FIT_1: _relation_result(
            FitRelationId.FIT_1,
            FitStatus.CONFLICT,
            "이 사업은 타 사업과 중복사업으로 부적격 판정됩니다.",
            [],
            [],
            "EXPLICIT_CONFLICT",
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        )
    }

    result = validate_and_aggregate_fit(
        subagent_outputs=[(non_neutral_result, []), ({}, []), ({}, [])],
        actual_cpl=cpl,
        scoring=scoring,
        ruleset_version=settings.fit_ruleset_version,
        prompt_version=settings.fit_prompt_version,
        model_profile=settings.fit_model_profile,
    )

    fit1 = next(r for r in result.relations if r.relation_id == FitRelationId.FIT_1)
    assert "부적격" not in fit1.summary
    assert "중복사업" not in fit1.summary
    assert "확인 필요" in fit1.summary
    assert any("Policy judgment sanitized" in w for w in result.warnings)


def test_frozen_inspection_context_duck_typing() -> None:
    """FrozenInspectionContext의 duck-typing 속성 (.items, .confirmed_count 등) 검증."""
    cpl = _complete_cpl_fixture()
    ctx = FrozenInspectionContext(
        context_version="ctx-test-1",
        case_id=999,
        document_text="샘플 전문",
        cpl_result=cpl,
        cpl_results=cpl.model_dump(mode="json"),
    )

    assert len(ctx.items) == len(cpl.items)
    assert ctx.ruleset_version == cpl.ruleset_version
    assert ctx.total_count == cpl.total_count
    assert ctx.confirmed_count == cpl.confirmed_count
    assert ctx.confirmation_rate == cpl.confirmation_rate

    # Verify immutability
    with pytest.raises(Exception):
        ctx.document_text = "new text"
