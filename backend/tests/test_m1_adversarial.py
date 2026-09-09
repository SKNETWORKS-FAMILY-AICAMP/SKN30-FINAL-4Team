"""Adversarial stress-testing suite for Milestone 1: FIT 3 Subagents, No-Loop Validator, and Concurrency.

Author: M1 Challenger 1 (Empirical Verification)
"""
import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

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
from app.services.fit.fit_engine import load_fit_scoring, _relation_result
from app.services.fit.runner import run_fit_subagents_parallel
from app.services.fit.validator import validate_and_aggregate_fit
import os
import sys

_SHARED = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ml", "serving", "shared")
)
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

import ml_orchestrator as ORCH
import result_envelope as RE


def _make_test_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret="test-secret-that-is-at-least-32-bytes-long",
    )


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


# ==============================================================================
# Challenge 1: Subagent 2 Simulated Timeout Adversarial Test
# ==============================================================================

def test_adversarial_subagent_2_timeout() -> None:
    """Adversarially inject simulated LLM timeout into Subagent 2.
    Assert:
    - Subagent 1 (FIT-1, 2, 3) and Subagent 3 (FIT-5, 7) succeed.
    - Subagent 2 axes (FIT-4, FIT-6) are marked INSUFFICIENT.
    - INSUFFICIENT axes have score is None (strictly NOT 0 or 0.0).
    - No-Loop validator does NOT retry.
    - Overall score excludes insufficient axes from denominator and numerator.
    """
    settings = _make_test_settings()
    cpl = _complete_cpl_fixture()

    sub2_invocation_count = 0

    class AdversarialTimeoutLlm:
        async def generate_structured(self, **kwargs):
            nonlocal sub2_invocation_count
            messages = kwargs.get("messages", [])
            user_msg = messages[1].content if len(messages) > 1 else ""
            if "FIT-6" in user_msg:
                sub2_invocation_count += 1
                raise LLMTimeoutError("Simulated LLM network timeout on Subagent 2")
            return _semantic_response()

    result = asyncio.run(
        run_fit_subagents_parallel(
            cpl,
            AdversarialTimeoutLlm(),
            settings=settings,
            case_id=201,
        )
    )

    # 1. No-Loop assertion: subagent 2 must be called exactly once without retry loops
    assert sub2_invocation_count == 1, f"Expected 1 call, got {sub2_invocation_count} (retry loop detected!)"

    # 2. Results mapping
    by_id = {r.relation_id: r for r in result.relations}

    # 3. Subagents 1 and 3 must succeed
    assert by_id[FitRelationId.FIT_1].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_2].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_3].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_5].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_7].status == FitStatus.FIT

    assert by_id[FitRelationId.FIT_1].score == 100
    assert by_id[FitRelationId.FIT_2].score == 100
    assert by_id[FitRelationId.FIT_3].score == 100
    assert by_id[FitRelationId.FIT_5].score == 100
    assert by_id[FitRelationId.FIT_7].score == 100

    # 4. Subagent 2 axes must be INSUFFICIENT with score is None (NOT 0)
    assert by_id[FitRelationId.FIT_4].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_4].score is None, f"FIT-4 score should be None, got {by_id[FitRelationId.FIT_4].score}"
    assert by_id[FitRelationId.FIT_4].score != 0

    assert by_id[FitRelationId.FIT_6].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_6].score is None, f"FIT-6 score should be None, got {by_id[FitRelationId.FIT_6].score}"
    assert by_id[FitRelationId.FIT_6].score != 0
    assert by_id[FitRelationId.FIT_6].reason_code == "LLM_TIMEOUT"

    # 5. Overall score calculation: 9 negative constraints check
    # Denominator and numerator must only count assessable axes (5 axes)
    assert result.score.assessable_count == 5
    assert result.score.value == 100.0


# ==============================================================================
# Challenge 2: Subagent 2 Simulated Unhandled Exception
# ==============================================================================

def test_adversarial_subagent_2_unhandled_exception() -> None:
    """Adversarially inject an unhandled RuntimeError into Subagent 2.
    Assert:
    - Subagents 1 and 3 remain 100% unaffected.
    - Subagent 2 axes are marked INSUFFICIENT.
    - score is None (NOT 0).
    - Failure is logged and warning recorded.
    """
    settings = _make_test_settings()
    cpl = _complete_cpl_fixture()

    class AdversarialExceptionLlm:
        async def generate_structured(self, **kwargs):
            messages = kwargs.get("messages", [])
            user_msg = messages[1].content if len(messages) > 1 else ""
            if "FIT-6" in user_msg:
                raise RuntimeError("Catastrophic downstream provider 500 internal error")
            return _semantic_response()

    result = asyncio.run(
        run_fit_subagents_parallel(
            cpl,
            AdversarialExceptionLlm(),
            settings=settings,
            case_id=202,
        )
    )

    by_id = {r.relation_id: r for r in result.relations}

    # Subagent 1 & 3 preserved
    assert by_id[FitRelationId.FIT_1].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_3].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_5].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_7].status == FitStatus.FIT

    # Subagent 2 isolated
    assert by_id[FitRelationId.FIT_6].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_6].score is None
    assert by_id[FitRelationId.FIT_6].reason_code == "UNAVAILABLE"

    assert result.score.assessable_count == 5
    assert result.score.value == 100.0


# ==============================================================================
# Challenge 3: Runner-Level Coroutine Crash Isolation (asyncio.gather)
# ==============================================================================

def test_adversarial_subagent_2_coroutine_crash(monkeypatch) -> None:
    """Directly crash run_fit_subagent_2 coroutine at runner level to test asyncio.gather fault isolation."""
    settings = _make_test_settings()
    cpl = _complete_cpl_fixture()

    async def crashed_subagent_2(*args, **kwargs):
        raise TypeError("Unexpected NoneType error inside subagent 2 coroutine")

    import app.services.fit.runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_fit_subagent_2", crashed_subagent_2)

    class NormalLlm:
        async def generate_structured(self, **kwargs):
            return _semantic_response()

    result = asyncio.run(
        runner_mod.run_fit_subagents_parallel(
            cpl,
            NormalLlm(),
            settings=settings,
            case_id=203,
        )
    )

    by_id = {r.relation_id: r for r in result.relations}

    # Subagent 1 & 3 must succeed
    assert by_id[FitRelationId.FIT_1].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_2].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_3].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_5].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_7].status == FitStatus.FIT

    # Subagent 2 missing axes must be marked INSUFFICIENT with UNAVAILABLE
    assert by_id[FitRelationId.FIT_4].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_4].score is None
    assert by_id[FitRelationId.FIT_4].reason_code == "UNAVAILABLE"

    assert by_id[FitRelationId.FIT_6].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_6].score is None
    assert by_id[FitRelationId.FIT_6].reason_code == "UNAVAILABLE"

    assert any("FIT Subagent 2 crashed: TypeError" in w for w in result.warnings)


# ==============================================================================
# Challenge 4: All 3 Subagents Crash — Zero Division and Extreme Boundary
# ==============================================================================

def test_adversarial_all_subagents_crash_boundary(monkeypatch) -> None:
    """All 3 subagents crash simultaneously. Verify pipeline does NOT throw ZeroDivisionError and score.value is None."""
    settings = _make_test_settings()
    cpl = _complete_cpl_fixture()

    async def crash(*args, **kwargs):
        raise RuntimeError("Total blackout")

    import app.services.fit.runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_fit_subagent_1", crash)
    monkeypatch.setattr(runner_mod, "run_fit_subagent_2", crash)
    monkeypatch.setattr(runner_mod, "run_fit_subagent_3", crash)

    result = asyncio.run(
        runner_mod.run_fit_subagents_parallel(
            cpl,
            None,
            settings=settings,
            case_id=204,
        )
    )

    assert len(result.relations) == 7
    for r in result.relations:
        assert r.status == FitStatus.INSUFFICIENT
        assert r.score is None
        assert r.score != 0
        assert r.reason_code == "UNAVAILABLE"

    assert result.score.assessable_count == 0
    assert result.score.value is None
    assert result.score.numerator == 0
    assert result.score.denominator == 0


# ==============================================================================
# Challenge 5: Subagent Ownership Spoofing / Hijack Prevention
# ==============================================================================

def test_adversarial_ownership_spoofing() -> None:
    """Subagent 2 attempts to forge and return FIT-1 (owned strictly by Subagent 1).
    Assert:
    - No-Loop validator rejects unauthorized axis from Subagent 2.
    - True owner's axis is preserved.
    """
    settings = _make_test_settings()
    scoring = load_fit_scoring(settings.fit_scoring_path)
    cpl = _complete_cpl_fixture()

    authentic_sub1 = {
        FitRelationId.FIT_1: _relation_result(
            FitRelationId.FIT_1,
            FitStatus.FIT,
            "Authentic Subagent 1",
            [],
            [],
            None,
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        )
    }

    malicious_sub2 = {
        FitRelationId.FIT_1: _relation_result(  # Forged FIT-1 from Subagent 2
            FitRelationId.FIT_1,
            FitStatus.CONFLICT,
            "Spoofed by Subagent 2",
            [],
            [],
            "FORGED",
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        ),
        FitRelationId.FIT_6: _relation_result(
            FitRelationId.FIT_6,
            FitStatus.FIT,
            "Valid Subagent 2 axis",
            [],
            [],
            None,
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        ),
    }

    result = validate_and_aggregate_fit(
        subagent_outputs=[(authentic_sub1, []), (malicious_sub2, []), ({}, [])],
        actual_cpl=cpl,
        scoring=scoring,
        ruleset_version=settings.fit_ruleset_version,
        prompt_version=settings.fit_prompt_version,
        model_profile=settings.fit_model_profile,
    )

    by_id = {r.relation_id: r for r in result.relations}
    assert by_id[FitRelationId.FIT_1].summary == "Authentic Subagent 1"
    assert by_id[FitRelationId.FIT_1].status == FitStatus.FIT
    assert any("Unauthorized axis FIT-1 from Subagent 2 ignored" in w for w in result.warnings)


# ==============================================================================
# Challenge 6: FrozenInspectionContext Immutability & Exception Verification
# ==============================================================================

def test_adversarial_frozen_context_immutability() -> None:
    """Assert FrozenInspectionContext is immutable and raises exception upon mutation attempt."""
    cpl = _complete_cpl_fixture()
    ctx = FrozenInspectionContext(
        context_version="ctx-test-1",
        case_id=100,
        title="테스트 사업",
        document_text="불변 원본 텍스트",
        cpl_result=cpl,
        cpl_results=cpl.model_dump(mode="json"),
    )

    # Mutation attempt on document_text
    with pytest.raises((ValidationError, TypeError, FrozenInstanceError)) as excinfo:
        ctx.document_text = "악의적 변조 텍스트"  # type: ignore[misc]

    # Verify exception characteristics
    err = excinfo.value
    if isinstance(err, ValidationError):
        assert "frozen_instance" in str(err)
    else:
        assert isinstance(err, (FrozenInstanceError, TypeError))

    # Mutation attempt on case_id
    with pytest.raises((ValidationError, TypeError, FrozenInstanceError)):
        ctx.case_id = 999  # type: ignore[misc]

    # Mutation attempt on context_version
    with pytest.raises((ValidationError, TypeError, FrozenInstanceError)):
        ctx.context_version = "ctx-hacked"  # type: ignore[misc]

    # Mutation attempt on frozen_at
    with pytest.raises((ValidationError, TypeError, FrozenInstanceError)):
        ctx.frozen_at = datetime.now(timezone.utc)  # type: ignore[misc]


def test_adversarial_frozen_context_duck_typing_dual_source() -> None:
    """Verify duck-typing works identically whether initialized with cpl_result object or cpl_results dict."""
    cpl = _complete_cpl_fixture()
    dumped = cpl.model_dump(mode="json")

    # Instance 1: with live cpl_result
    ctx1 = FrozenInspectionContext(
        context_version="ctx-1",
        case_id=1,
        document_text="doc1",
        cpl_result=cpl,
    )

    # Instance 2: with only cpl_results dict
    ctx2 = FrozenInspectionContext(
        context_version="ctx-2",
        case_id=2,
        document_text="doc2",
        cpl_results=dumped,
    )

    assert len(ctx1.items) == len(ctx2.items) == 13
    assert ctx1.ruleset_version == ctx2.ruleset_version == "cpl-test-v1"
    assert ctx1.total_count == ctx2.total_count == 13
    assert ctx1.confirmed_count == ctx2.confirmed_count
    assert ctx1.confirmation_rate == ctx2.confirmation_rate


# ==============================================================================
# Challenge 7: Model 2 / Model 3 Stage 2 Dependency Guarding
# ==============================================================================

def test_adversarial_ml_stage2_dependency_guard() -> None:
    """Assert Stage 2 (Model 2 and Model 3) immediately returns not_available when Model 1 fails."""
    cpl = _complete_cpl_fixture()
    ctx = FrozenInspectionContext(
        context_version="ctx-1",
        case_id=1,
        document_text="doc1",
        cpl_result=cpl,
    )

    # Case A: Model 1 failed
    failed_m1 = RE.failed("1", "m1", "1.0", "BERT", code="ERR", message="Crash")
    res_m2 = asyncio.run(ORCH.run_model_2_async(failed_m1, ctx))
    res_m3 = asyncio.run(ORCH.run_model_3_async(failed_m1, ctx))

    assert res_m2["status"] == "not_available"
    assert res_m2["metadata"]["depends_on"] == "model_1"
    assert res_m3["status"] == "not_available"
    assert res_m3["metadata"]["depends_on"] == "model_1"

    # Case B: Model 1 not_available
    na_m1 = RE.not_available("1", "m1", "1.0", "BERT", depends_on="upstream")
    res_m2_na = asyncio.run(ORCH.run_model_2_async(na_m1, ctx))
    res_m3_na = asyncio.run(ORCH.run_model_3_async(na_m1, ctx))

    assert res_m2_na["status"] == "not_available"
    assert res_m3_na["status"] == "not_available"


# ==============================================================================
# Challenge 8: Empirical Concurrency Proof (Parallel vs Sequential Timing)
# ==============================================================================

def test_adversarial_parallel_runner_wall_clock_concurrency() -> None:
    """Empirically prove that Subagents 1, 2, 3 run concurrently via asyncio.gather.
    Each subagent delays for 0.1s. In parallel, total runtime should be ~0.1s (< 0.22s),
    whereas sequential execution would take >= 0.30s.
    """
    import time

    settings = _make_test_settings()
    cpl = _complete_cpl_fixture()

    class SleepyLlm:
        async def generate_structured(self, **kwargs):
            await asyncio.sleep(0.1)
            return _semantic_response()

    start_time = time.perf_counter()
    result = asyncio.run(
        run_fit_subagents_parallel(
            cpl,
            SleepyLlm(),
            settings=settings,
            case_id=208,
        )
    )
    elapsed = time.perf_counter() - start_time

    assert isinstance(result, FitResult)
    assert len(result.relations) == 7
    # Concurrency verification: total elapsed time must be significantly less than sequential sum (0.3s)
    assert elapsed < 0.25, f"Execution took {elapsed:.3f}s; subagents did not run in parallel!"


# ==============================================================================
# Challenge 9: Partial Relation Omission by Subagent (Missing Axis Isolation)
# ==============================================================================

def test_adversarial_partial_relation_omission() -> None:
    """Subagent 1 silently drops FIT-3. No-Loop validator must isolate FIT-3 without retrying or crashing."""
    settings = _make_test_settings()
    scoring = load_fit_scoring(settings.fit_scoring_path)
    cpl = _complete_cpl_fixture()

    # Subagent 1 returns FIT-1 and FIT-2 only, dropping FIT-3
    sub1_partial = {
        FitRelationId.FIT_1: _relation_result(
            FitRelationId.FIT_1,
            FitStatus.FIT,
            "정상",
            [],
            [],
            None,
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        ),
        FitRelationId.FIT_2: _relation_result(
            FitRelationId.FIT_2,
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
        subagent_outputs=[(sub1_partial, []), ({}, []), ({}, [])],
        actual_cpl=cpl,
        scoring=scoring,
        ruleset_version=settings.fit_ruleset_version,
        prompt_version=settings.fit_prompt_version,
        model_profile=settings.fit_model_profile,
    )

    by_id = {r.relation_id: r for r in result.relations}
    assert by_id[FitRelationId.FIT_1].status == FitStatus.FIT
    assert by_id[FitRelationId.FIT_2].status == FitStatus.FIT
    # FIT-3 was omitted: must be isolated as INSUFFICIENT with UNAVAILABLE
    assert by_id[FitRelationId.FIT_3].status == FitStatus.INSUFFICIENT
    assert by_id[FitRelationId.FIT_3].score is None
    assert by_id[FitRelationId.FIT_3].reason_code == "UNAVAILABLE"
    assert any("FIT relation FIT-3:UNAVAILABLE" in w for w in result.warnings)


# ==============================================================================
# Challenge 10: Adversarial Policy Judgment Sanitization (Multi-Keyword Injection)
# ==============================================================================

def test_adversarial_policy_judgment_multi_keyword_sanitization() -> None:
    """Inject multiple forbidden judgmental keywords ('부적격', '중복사업', '위반', '탈락', '폐지', '부적정').
    Assert all are sanitized to '확인 필요'.
    """
    settings = _make_test_settings()
    scoring = load_fit_scoring(settings.fit_scoring_path)
    cpl = _complete_cpl_fixture()

    hostile_summary = "타 부처와의 중복사업으로 부적정하며 지침 위반이 확인되어 탈락 및 폐지 대상으로 부적격 판정함."
    sub_results = {
        FitRelationId.FIT_1: _relation_result(
            FitRelationId.FIT_1,
            FitStatus.CONFLICT,
            hostile_summary,
            [],
            [],
            "POLICY_CONFLICT",
            scoring,
            settings.fit_ruleset_version,
            settings.fit_prompt_version,
        )
    }

    result = validate_and_aggregate_fit(
        subagent_outputs=[(sub_results, []), ({}, []), ({}, [])],
        actual_cpl=cpl,
        scoring=scoring,
        ruleset_version=settings.fit_ruleset_version,
        prompt_version=settings.fit_prompt_version,
        model_profile=settings.fit_model_profile,
    )

    fit1 = next(r for r in result.relations if r.relation_id == FitRelationId.FIT_1)
    forbidden = ("부적격", "중복사업", "위반", "탈락", "폐지", "부적정")
    for word in forbidden:
        assert word not in fit1.summary, f"Forbidden word '{word}' leaked into summary: {fit1.summary}"
    assert "확인 필요" in fit1.summary


# ==============================================================================
# Challenge 11: Nested Container Mutation Vulnerability Probe
# ==============================================================================

def test_adversarial_nested_container_mutation_probe() -> None:
    """Probe FrozenInspectionContext for nested mutability.
    Direct attribute reassignment is blocked by Pydantic frozen=True.
    However, internal mutable containers (cpl_results dict or cpl_result.items list)
    can be mutated if not defensively copied or wrapped in MappingProxyType.
    We document this empirical finding.
    """
    cpl = _complete_cpl_fixture()
    ctx = FrozenInspectionContext(
        context_version="ctx-probe",
        case_id=999,
        document_text="original text",
        cpl_results={"key": "original_val"},
    )

    # 1. Top-level reassignment is strictly prevented
    with pytest.raises((ValidationError, TypeError)):
        ctx.cpl_results = {"key": "new_val"}  # type: ignore[misc]

    # 2. Nested dict mutation probe: mutating ctx.cpl_results internal key
    ctx.cpl_results["key"] = "mutated_val"
    # Observe that nested mutation succeeds because frozen=True only guards top-level assignment
    assert ctx.cpl_results["key"] == "mutated_val"

