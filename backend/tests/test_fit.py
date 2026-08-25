import asyncio
import json
import pytest

from app.core.config import Settings
from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.schemas.cpl import (
    CPL_FIELDS,
    CplFieldCode,
    CplItem,
    CplOccurrence,
    CplResult,
    CplStatus,
)
from app.schemas.fit import FIT_RELATIONS, FitResult, FitSemanticResponse, FitStatus
from app.services.fit.fit_engine import (
    analyze_fit,
    load_fit_prompt,
    load_fit_scoring,
)


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"


def settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret=JWT_SECRET,
        openai_api_key=None,
    )


def occurrence(field_code: CplFieldCode) -> CplOccurrence:
    normalized_value: object = {"text": f"{field_code.value} normalized"}
    if field_code == CplFieldCode.REQUEST_TYPE:
        normalized_value = {
            "request_reason": "DETAIL_NEW",
            "selected": True,
            "mark": "■",
        }
    return CplOccurrence(
        raw_text=f"{field_code.value} evidence",
        normalized_value=normalized_value,
        page_no=list(CPL_FIELDS).index(field_code) + 1,
        section_path=[field_code.value],
        source_locator={"paragraph_index": list(CPL_FIELDS).index(field_code)},
        block_id=f"block:{field_code.value}",
        extraction_method="RULE",
    )


def cpl_result(*present_fields: CplFieldCode) -> CplResult:
    present = set(present_fields)
    return CplResult(
        ruleset_version="cpl-alpha-v0.1",
        items=[
            CplItem(
                field_code=field_code,
                status=(
                    CplStatus.PRESENT
                    if field_code == CplFieldCode.REQUEST_TYPE
                    and field_code in present
                    else CplStatus.NEEDS_CONFIRMATION
                    if field_code in present
                    else CplStatus.MISSING
                ),
                occurrences=(
                    [occurrence(field_code)] if field_code in present else []
                ),
            )
            for field_code in CPL_FIELDS
        ],
    )


def complete_cpl_result() -> CplResult:
    return cpl_result(
        CplFieldCode.REQUEST_TYPE,
        CplFieldCode.PURPOSE_GOAL,
        CplFieldCode.IMPLEMENTATION_PLAN,
        CplFieldCode.TARGET_AND_CONDITIONS,
        CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
        CplFieldCode.DELIVERY_SYSTEM,
        CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
    )


def fit_response(*, invalid_ref: bool = False) -> FitSemanticResponse:
    target_ref = "TARGET_AND_CONDITIONS:0"
    return FitSemanticResponse.model_validate(
        {
            "relations": [
                {
                    "relation_id": "FIT-1",
                    "status": "FIT",
                    "summary": "목적과 대상의 최소 연결성이 확인됩니다.",
                    "left_evidence_refs": [
                        "UNKNOWN:0" if invalid_ref else "PURPOSE_GOAL:0"
                    ],
                    "right_evidence_refs": [target_ref],
                    "reason_code": None,
                },
                {
                    "relation_id": "FIT-2",
                    "status": "NEEDS_REVIEW",
                    "summary": "목적과 지원내용의 연결 설명을 확인해야 합니다.",
                    "left_evidence_refs": ["PURPOSE_GOAL:0"],
                    "right_evidence_refs": ["SUPPORT_CONTENT_AND_SCALE:0"],
                    "reason_code": "CONNECTION_UNCLEAR",
                },
                {
                    "relation_id": "FIT-3",
                    "status": "CONFLICT",
                    "summary": "목적과 성과 방향에 명시적 충돌이 있습니다.",
                    "left_evidence_refs": ["PURPOSE_GOAL:0"],
                    "right_evidence_refs": [
                        "EXPECTED_EFFECTS_AND_PERFORMANCE:0"
                    ],
                    "reason_code": "EXPLICIT_CONFLICT",
                },
                {
                    "relation_id": "FIT-4",
                    "status": "INSUFFICIENT",
                    "summary": "사업 계층 비교정보가 충분하지 않습니다.",
                    "left_evidence_refs": [],
                    "right_evidence_refs": [],
                    "reason_code": "HIERARCHY_UNCLEAR",
                },
                {
                    "relation_id": "FIT-5",
                    "status": "FIT",
                    "summary": "조건이 지원대상을 구체화합니다.",
                    "left_evidence_refs": [target_ref],
                    "right_evidence_refs": [target_ref],
                    "reason_code": None,
                },
                {
                    "relation_id": "FIT-6",
                    "status": "NEEDS_REVIEW",
                    "summary": "일부 절차의 담당기관 확인이 필요합니다.",
                    "left_evidence_refs": ["DELIVERY_SYSTEM:0"],
                    "right_evidence_refs": ["DELIVERY_SYSTEM:0"],
                    "reason_code": "ROLE_UNCLEAR",
                },
                {
                    "relation_id": "FIT-7",
                    "status": "CONFLICT",
                    "summary": "동일 지원한도 수치가 서로 다릅니다.",
                    "left_evidence_refs": ["SUPPORT_CONTENT_AND_SCALE:0"],
                    "right_evidence_refs": ["SUPPORT_CONTENT_AND_SCALE:0"],
                    "reason_code": "EXPLICIT_NUMERIC_CONFLICT",
                },
            ]
        }
    )


class FakeLlm:
    def __init__(self, response: FitSemanticResponse) -> None:
        self.response = response
        self.user_input: dict | None = None

    async def generate_structured(self, **kwargs):
        assert kwargs["task_name"] == "fit_internal_consistency"
        assert kwargs["response_schema"] is FitSemanticResponse
        self.user_input = json.loads(kwargs["messages"][1].content)
        return self.response


def run_fit(cpl: CplResult, llm_client) -> FitResult:
    runtime = settings()
    return asyncio.run(
        analyze_fit(
            cpl,
            llm_client,
            scoring=load_fit_scoring(runtime.fit_scoring_path),
            prompt=load_fit_prompt(runtime.fit_prompt_path),
            ruleset_version=runtime.fit_ruleset_version,
            prompt_version=runtime.fit_prompt_version,
            model_profile=runtime.fit_model_profile,
        )
    )


def test_fit_config_defines_versioned_equal_weight_scoring() -> None:
    runtime = settings()
    policy = load_fit_scoring(runtime.fit_scoring_path)
    assert policy.version == "fit-alpha-v0.1"
    assert policy.status_scores == {
        FitStatus.FIT: 100,
        FitStatus.NEEDS_REVIEW: 50,
        FitStatus.CONFLICT: 0,
        FitStatus.INSUFFICIENT: None,
    }
    assert set(policy.weights) == set(FIT_RELATIONS)
    assert set(policy.weights.values()) == {1}
    assert load_fit_prompt(runtime.fit_prompt_path)


def test_missing_cpl_evidence_makes_all_relations_insufficient_without_llm() -> None:
    class MustNotRunLlm:
        async def generate_structured(self, **_kwargs):
            raise AssertionError("LLM must not run without comparison evidence")

    result = run_fit(cpl_result(), MustNotRunLlm())
    assert [relation.relation_id for relation in result.relations] == list(
        FIT_RELATIONS
    )
    assert all(
        relation.status == FitStatus.INSUFFICIENT
        and relation.score is None
        and relation.reason_code == "COMPARISON_EVIDENCE_MISSING"
        for relation in result.relations
    )
    assert result.score.value is None
    assert result.score.numerator == 0
    assert result.score.denominator == 0
    assert result.score.assessable_count == 0
    assert result.score.total_count == 7


def test_fit_grounds_evidence_and_excludes_insufficient_from_score() -> None:
    fake = FakeLlm(fit_response())
    result = run_fit(complete_cpl_result(), fake)
    assert [relation.status for relation in result.relations] == [
        FitStatus.FIT,
        FitStatus.NEEDS_REVIEW,
        FitStatus.CONFLICT,
        FitStatus.INSUFFICIENT,
        FitStatus.FIT,
        FitStatus.NEEDS_REVIEW,
        FitStatus.CONFLICT,
    ]
    assert [relation.score for relation in result.relations] == [
        100,
        50,
        0,
        None,
        100,
        50,
        0,
    ]
    assert result.score.value == 50
    assert result.score.numerator == 300
    assert result.score.denominator == 600
    assert result.score.assessable_count == 6
    assert result.score.total_count == 7
    assert result.score.scoring_version == "fit-alpha-v0.1"
    fit_1 = result.relations[0]
    assert fit_1.left_evidence[0].raw_text == "PURPOSE_GOAL evidence"
    assert fit_1.left_evidence[0].page_no == 2
    assert fit_1.left_evidence[0].source_locator == {"paragraph_index": 1}
    assert fake.user_input is not None
    assert len(fake.user_input["requested_relations"]) == 7
    assert "source_locator" not in json.dumps(fake.user_input)


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (LLMUnavailableError("unavailable"), "LLM_UNAVAILABLE"),
        (LLMTimeoutError("timeout"), "LLM_TIMEOUT"),
        (LLMInvalidResponseError("invalid"), "LLM_INVALID_RESPONSE"),
    ],
)
def test_fit_maps_llm_failures_to_insufficient_and_keeps_evidence(
    error: Exception,
    reason_code: str,
) -> None:
    class FailingLlm:
        async def generate_structured(self, **_kwargs):
            raise error

    result = run_fit(complete_cpl_result(), FailingLlm())
    assert all(
        relation.status == FitStatus.INSUFFICIENT
        and relation.score is None
        and relation.reason_code == reason_code
        for relation in result.relations
    )
    assert result.relations[0].left_evidence[0].block_id == "block:PURPOSE_GOAL"
    assert f"FIT semantic analysis incomplete: {reason_code}" in result.warnings


def test_invalid_llm_evidence_reference_invalidates_semantic_batch() -> None:
    result = run_fit(complete_cpl_result(), FakeLlm(fit_response(invalid_ref=True)))
    assert all(
        relation.status == FitStatus.INSUFFICIENT
        and relation.reason_code == "LLM_INVALID_RESPONSE"
        for relation in result.relations
    )
    assert result.score.value is None


def test_relative_fit_config_paths_resolve_from_project_root() -> None:
    runtime = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret=JWT_SECRET,
        fit_scoring_path="backend/config/fit_scoring.json",
        fit_prompt_path="backend/config/prompts/fit-v0.1.txt",
    )
    assert runtime.fit_scoring_path.is_absolute()
    assert runtime.fit_prompt_path.is_absolute()
