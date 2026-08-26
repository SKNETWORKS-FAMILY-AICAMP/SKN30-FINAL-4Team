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
    FitResult,
    FitSemanticResponse,
    FitStatus,
)
from app.services import analysis_pipeline
from app.services.fit.fit_engine import analyze_fit, load_fit_prompt, load_fit_scoring


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"


def settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret=JWT_SECRET,
        openai_api_key=None,
    )


def occurrence(
    field_code: CplFieldCode,
    axis_code: CplAxisCode,
    raw_text: str,
    *,
    source_role: CplSourceRole | None = None,
    normalized_value: object | None = None,
    suffix: str = "0",
) -> CplOccurrence:
    return CplOccurrence(
        raw_text=raw_text,
        normalized_value=(
            {"text": raw_text} if normalized_value is None else normalized_value
        ),
        axis_code=axis_code,
        source_role=source_role,
        page_no=list(CPL_FIELDS).index(field_code) + 1,
        section_path=[field_code.value],
        source_locator={"paragraph_index": suffix},
        block_id=f"block:{field_code.value}:{suffix}",
        extraction_method="LLM",
    )


def cpl_result(
    occurrences: dict[CplFieldCode, list[CplOccurrence]] | None = None,
) -> CplResult:
    values = occurrences or {}
    return CplResult(
        ruleset_version="cpl-alpha-v0.2",
        items=[
            CplItem(
                field_code=field_code,
                status=(
                    CplStatus.NEEDS_CONFIRMATION
                    if values.get(field_code)
                    else CplStatus.MISSING
                ),
                occurrences=values.get(field_code, []),
            )
            for field_code in CPL_FIELDS
        ],
    )


def complete_cpl_result() -> CplResult:
    return cpl_result(
        {
            CplFieldCode.PURPOSE_GOAL: [
                occurrence(
                    CplFieldCode.PURPOSE_GOAL,
                    CplAxisCode.PURPOSE_TARGET_CONDITION,
                    "중소기업",
                    suffix="target",
                ),
                occurrence(
                    CplFieldCode.PURPOSE_GOAL,
                    CplAxisCode.PURPOSE_DIRECTION,
                    "해외시장 진출 확대",
                    suffix="direction",
                ),
            ],
            CplFieldCode.TARGET_AND_CONDITIONS: [
                occurrence(
                    CplFieldCode.TARGET_AND_CONDITIONS,
                    CplAxisCode.TARGET_GROUP,
                    "제조 중소기업",
                    source_role=CplSourceRole.TARGET,
                    suffix="target",
                ),
                occurrence(
                    CplFieldCode.TARGET_AND_CONDITIONS,
                    CplAxisCode.COND_INDUSTRY,
                    "제조업",
                    source_role=CplSourceRole.CONDITION,
                    suffix="condition",
                ),
            ],
            CplFieldCode.SUPPORT_CONTENT_AND_SCALE: [
                occurrence(
                    CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
                    CplAxisCode.SUPPORT_ACTIVITY,
                    "해외전시회 참가",
                    source_role=CplSourceRole.SUPPORT_CONTENT,
                    suffix="activity",
                ),
                occurrence(
                    CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
                    CplAxisCode.PER_COMPANY_LIMIT,
                    "기업당 최대 3천만원",
                    source_role=CplSourceRole.SUPPORT_CONTENT,
                    normalized_value={"amount_won": 30_000_000, "unit": "KRW"},
                    suffix="content-limit",
                ),
                occurrence(
                    CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
                    CplAxisCode.PER_COMPANY_LIMIT,
                    "기업당 최대 3천만원",
                    source_role=CplSourceRole.SUPPORT_SCALE,
                    normalized_value={"amount_won": 30_000_000, "unit": "KRW"},
                    suffix="scale-limit",
                ),
            ],
            CplFieldCode.DELIVERY_SYSTEM: [
                occurrence(
                    CplFieldCode.DELIVERY_SYSTEM,
                    CplAxisCode.DELIVERY_ORG_NAME,
                    "A기관",
                    source_role=CplSourceRole.DELIVERY_ORG,
                    suffix="org",
                ),
                occurrence(
                    CplFieldCode.DELIVERY_SYSTEM,
                    CplAxisCode.DELIVERY_PROCEDURE_STEP,
                    "선정평가",
                    source_role=CplSourceRole.DELIVERY_PROCEDURE,
                    suffix="step",
                ),
                occurrence(
                    CplFieldCode.DELIVERY_SYSTEM,
                    CplAxisCode.DELIVERY_STEP_ROLE,
                    "A기관이 선정평가 수행",
                    source_role=CplSourceRole.DELIVERY_PROCEDURE,
                    suffix="role",
                ),
            ],
            CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE: [
                occurrence(
                    CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
                    CplAxisCode.EFFECT_DIRECTION,
                    "수출액 증가",
                    source_role=CplSourceRole.EXPECTED_EFFECT,
                    suffix="effect",
                ),
                occurrence(
                    CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
                    CplAxisCode.KPI_NAME,
                    "수출액 증가율",
                    source_role=CplSourceRole.PERFORMANCE_INDICATOR,
                    suffix="kpi",
                ),
            ],
        }
    )


def fit_response(*, invalid_ref: bool = False) -> FitSemanticResponse:
    return FitSemanticResponse.model_validate(
        {
            "relations": [
                {
                    "relation_id": "FIT-1",
                    "status": "FIT",
                    "summary": "목적의 대상조건과 실제 대상 범위가 연결됩니다.",
                    "left_evidence_refs": [
                        "UNKNOWN:0" if invalid_ref else "PURPOSE_GOAL:0"
                    ],
                    "right_evidence_refs": ["TARGET_AND_CONDITIONS:0"],
                    "reason_code": None,
                },
                {
                    "relation_id": "FIT-2",
                    "status": "NEEDS_REVIEW",
                    "summary": "목적과 지원활동의 연결 설명을 확인해야 합니다.",
                    "left_evidence_refs": ["PURPOSE_GOAL:1"],
                    "right_evidence_refs": ["SUPPORT_CONTENT_AND_SCALE:0"],
                    "reason_code": "CONNECTION_UNCLEAR",
                },
                {
                    "relation_id": "FIT-3",
                    "status": "CONFLICT",
                    "summary": "목적과 성과 방향에 명시적 충돌이 있습니다.",
                    "left_evidence_refs": ["PURPOSE_GOAL:1"],
                    "right_evidence_refs": [
                        "EXPECTED_EFFECTS_AND_PERFORMANCE:0",
                        "EXPECTED_EFFECTS_AND_PERFORMANCE:1",
                    ],
                    "reason_code": "EXPLICIT_CONFLICT",
                },
                {
                    "relation_id": "FIT-5",
                    "status": "FIT",
                    "summary": "조건이 지원대상을 구체화합니다.",
                    "left_evidence_refs": ["TARGET_AND_CONDITIONS:0"],
                    "right_evidence_refs": ["TARGET_AND_CONDITIONS:1"],
                    "reason_code": None,
                },
                {
                    "relation_id": "FIT-6",
                    "status": "NEEDS_REVIEW",
                    "summary": "일부 절차의 담당기관 확인이 필요합니다.",
                    "left_evidence_refs": ["DELIVERY_SYSTEM:0"],
                    "right_evidence_refs": [
                        "DELIVERY_SYSTEM:1",
                        "DELIVERY_SYSTEM:2",
                    ],
                    "reason_code": "ROLE_UNCLEAR",
                },
            ]
        }
    )


class FakeLlm:
    def __init__(self, response: FitSemanticResponse) -> None:
        self.response = response
        self.user_input: dict | None = None
        self.calls = 0

    async def generate_structured(self, **kwargs):
        self.calls += 1
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


def relation(result: FitResult, relation_id: FitRelationId):
    return next(item for item in result.relations if item.relation_id == relation_id)


def quantitative_cpl(*values: CplOccurrence) -> CplResult:
    return cpl_result({CplFieldCode.SUPPORT_CONTENT_AND_SCALE: list(values)})


def quantitative_occurrence(
    axis: CplAxisCode,
    role: CplSourceRole | None,
    value: object,
    unit: str,
    *,
    suffix: str,
) -> CplOccurrence:
    value_key = {
        CplAxisCode.PER_COMPANY_LIMIT: "amount_won",
        CplAxisCode.TOTAL_SCALE: "amount_won",
        CplAxisCode.COMPANY_COUNT: "count",
        CplAxisCode.SUBSIDY_RATE: "ratio",
        CplAxisCode.SELF_BURDEN_RATE: "ratio",
    }[axis]
    return occurrence(
        CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
        axis,
        str(value),
        source_role=role,
        normalized_value={value_key: value, "unit": unit},
        suffix=suffix,
    )


def test_fit_config_defines_versioned_equal_weight_scoring() -> None:
    runtime = settings()
    policy = load_fit_scoring(runtime.fit_scoring_path)
    prompt = load_fit_prompt(runtime.fit_prompt_path)
    assert runtime.fit_ruleset_version == "fit-v0.2"
    assert runtime.fit_prompt_version == "fit-v0.2"
    assert runtime.fit_prompt_path.name == "fit-v0.2.txt"
    assert "Do not return FIT-4 or FIT-7" in prompt
    assert policy.status_scores == {
        FitStatus.FIT: 100,
        FitStatus.NEEDS_REVIEW: 50,
        FitStatus.CONFLICT: 0,
        FitStatus.INSUFFICIENT: None,
    }
    assert set(policy.weights) == set(FIT_RELATIONS)
    assert set(policy.weights.values()) == {1}


def test_missing_evidence_uses_rule_results_without_llm() -> None:
    class MustNotRunLlm:
        async def generate_structured(self, **_kwargs):
            raise AssertionError("LLM must not run without comparison evidence")

    result = run_fit(
        cpl_result(
            {
                CplFieldCode.IMPLEMENTATION_PLAN: [
                    occurrence(
                        CplFieldCode.IMPLEMENTATION_PLAN,
                        CplAxisCode.PROGRAM_LEVEL,
                        "내역사업",
                        source_role=CplSourceRole.SUBPROGRAM_PLAN,
                        normalized_value={"program_level": "SUBPROGRAM"},
                    )
                ]
            }
        ),
        MustNotRunLlm(),
    )
    assert [item.relation_id for item in result.relations] == list(FIT_RELATIONS)
    assert all(item.status == FitStatus.INSUFFICIENT for item in result.relations)
    assert relation(result, FitRelationId.FIT_4).reason_code == (
        "HIERARCHY_COMPARISON_NOT_AVAILABLE"
    )
    assert relation(result, FitRelationId.FIT_5).reason_code == (
        "NO_CONDITIONS_SPECIFIED"
    )
    assert relation(result, FitRelationId.FIT_7).reason_code == (
        "COMPARISON_EVIDENCE_MISSING"
    )
    assert result.score.value is None
    assert result.score.assessable_count == 0


def test_fit_filters_axes_and_never_sends_fit_4_or_fit_7_to_llm() -> None:
    fake = FakeLlm(fit_response())
    result = run_fit(complete_cpl_result(), fake)
    assert [item.status for item in result.relations] == [
        FitStatus.FIT,
        FitStatus.NEEDS_REVIEW,
        FitStatus.CONFLICT,
        FitStatus.INSUFFICIENT,
        FitStatus.FIT,
        FitStatus.NEEDS_REVIEW,
        FitStatus.FIT,
    ]
    assert result.score.value == pytest.approx(400 / 600 * 100)
    assert result.score.assessable_count == 6
    assert fake.calls == 1
    assert fake.user_input is not None
    requested = fake.user_input["requested_relations"]
    assert [item["relation_id"] for item in requested] == [
        "FIT-1",
        "FIT-2",
        "FIT-3",
        "FIT-5",
        "FIT-6",
    ]
    fit_5 = next(item for item in requested if item["relation_id"] == "FIT-5")
    assert fit_5["left_evidence"][0]["ref"] == "TARGET_AND_CONDITIONS:0"
    assert fit_5["right_evidence"][0]["ref"] == "TARGET_AND_CONDITIONS:1"
    assert fit_5["left_evidence"] != fit_5["right_evidence"]
    assert "axis_code" in fit_5["right_evidence"][0]
    assert "source_role" in fit_5["right_evidence"][0]
    assert "source_locator" not in json.dumps(fake.user_input)


def test_fit_5_without_conditions_is_not_an_issue() -> None:
    cpl = complete_cpl_result()
    target = next(
        item
        for item in cpl.items
        if item.field_code == CplFieldCode.TARGET_AND_CONDITIONS
    )
    target.occurrences = [target.occurrences[0]]
    result = run_fit(cpl, FakeLlm(fit_response()))
    fit_5 = relation(result, FitRelationId.FIT_5)
    assert fit_5.status == FitStatus.INSUFFICIENT
    assert fit_5.reason_code == "NO_CONDITIONS_SPECIFIED"


def test_fit_requires_the_expected_source_role_as_well_as_axis() -> None:
    cpl = complete_cpl_result()
    target = next(
        item
        for item in cpl.items
        if item.field_code == CplFieldCode.TARGET_AND_CONDITIONS
    )
    target.occurrences[1] = target.occurrences[1].model_copy(
        update={"source_role": CplSourceRole.TARGET}
    )
    fit_5 = relation(run_fit(cpl, FakeLlm(fit_response())), FitRelationId.FIT_5)
    assert fit_5.status == FitStatus.INSUFFICIENT
    assert fit_5.reason_code == "NO_CONDITIONS_SPECIFIED"


@pytest.mark.parametrize(
    ("values", "expected_status", "expected_reason"),
    [
        (
            [
                (CplAxisCode.PER_COMPANY_LIMIT, CplSourceRole.SUPPORT_CONTENT, 30, "KRW"),
                (CplAxisCode.PER_COMPANY_LIMIT, CplSourceRole.SUPPORT_SCALE, 30, "KRW"),
            ],
            FitStatus.FIT,
            None,
        ),
        (
            [
                (CplAxisCode.PER_COMPANY_LIMIT, CplSourceRole.SUPPORT_CONTENT, 30, "KRW"),
                (CplAxisCode.PER_COMPANY_LIMIT, CplSourceRole.SUPPORT_SCALE, 50, "KRW"),
            ],
            FitStatus.NEEDS_REVIEW,
            "NUMERIC_MISMATCH",
        ),
        (
            [(CplAxisCode.PER_COMPANY_LIMIT, CplSourceRole.SUPPORT_SCALE, 50, "KRW")],
            FitStatus.FIT,
            "SINGLE_SIDED_NO_CONFLICT",
        ),
        (
            [
                (CplAxisCode.PER_COMPANY_LIMIT, CplSourceRole.SUPPORT_CONTENT, 30, "KRW"),
                (CplAxisCode.TOTAL_SCALE, CplSourceRole.SUPPORT_SCALE, 50, "KRW"),
            ],
            FitStatus.FIT,
            "SINGLE_SIDED_NO_CONFLICT",
        ),
        ([], FitStatus.INSUFFICIENT, "COMPARISON_EVIDENCE_MISSING"),
    ],
)
def test_fit_7_rule_statuses(
    values: list[tuple[CplAxisCode, CplSourceRole, object, str]],
    expected_status: FitStatus,
    expected_reason: str | None,
) -> None:
    occurrences = [
        quantitative_occurrence(axis, role, value, unit, suffix=str(index))
        for index, (axis, role, value, unit) in enumerate(values)
    ]
    fit_7 = relation(
        run_fit(quantitative_cpl(*occurrences), None),
        FitRelationId.FIT_7,
    )
    assert fit_7.status == expected_status
    assert fit_7.reason_code == expected_reason


def test_fit_7_compares_all_values_and_deduplicates_equal_values() -> None:
    left_30 = quantitative_occurrence(
        CplAxisCode.PER_COMPANY_LIMIT,
        CplSourceRole.SUPPORT_CONTENT,
        30,
        "KRW",
        suffix="left-30",
    )
    left_50 = quantitative_occurrence(
        CplAxisCode.PER_COMPANY_LIMIT,
        CplSourceRole.SUPPORT_CONTENT,
        50,
        "KRW",
        suffix="left-50",
    )
    right_30 = quantitative_occurrence(
        CplAxisCode.PER_COMPANY_LIMIT,
        CplSourceRole.SUPPORT_SCALE,
        30,
        "KRW",
        suffix="right-30",
    )
    mismatch = relation(
        run_fit(quantitative_cpl(left_30, left_50, right_30), None),
        FitRelationId.FIT_7,
    )
    assert mismatch.status == FitStatus.NEEDS_REVIEW

    duplicate = left_30.model_copy(update={"block_id": "duplicate"})
    matched = relation(
        run_fit(quantitative_cpl(left_30, duplicate, right_30), None),
        FitRelationId.FIT_7,
    )
    assert matched.status == FitStatus.FIT


def test_fit_7_invalid_values_are_excluded_with_warning() -> None:
    invalid = quantitative_occurrence(
        CplAxisCode.PER_COMPANY_LIMIT,
        CplSourceRole.SUPPORT_CONTENT,
        30,
        "PERCENT",
        suffix="invalid-unit",
    )
    ignored = quantitative_occurrence(
        CplAxisCode.PER_COMPANY_LIMIT,
        None,
        30,
        "KRW",
        suffix="unknown-role",
    )
    result = run_fit(quantitative_cpl(invalid, ignored), None)
    fit_7 = relation(result, FitRelationId.FIT_7)
    assert fit_7.status == FitStatus.INSUFFICIENT
    assert fit_7.reason_code == "COMPARISON_VALUE_INVALID"
    assert result.warnings == ["FIT-7 excluded 1 invalid quantitative occurrence(s)"]


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (LLMUnavailableError("unavailable"), "LLM_UNAVAILABLE"),
        (LLMTimeoutError("timeout"), "LLM_TIMEOUT"),
        (LLMInvalidResponseError("invalid"), "LLM_INVALID_RESPONSE"),
    ],
)
def test_fit_maps_llm_failures_and_keeps_rule_results(
    error: Exception,
    reason_code: str,
) -> None:
    class FailingLlm:
        async def generate_structured(self, **_kwargs):
            raise error

    result = run_fit(complete_cpl_result(), FailingLlm())
    semantic = [
        item
        for item in result.relations
        if item.relation_id not in {FitRelationId.FIT_4, FitRelationId.FIT_7}
    ]
    assert all(
        item.status == FitStatus.INSUFFICIENT and item.reason_code == reason_code
        for item in semantic
    )
    assert relation(result, FitRelationId.FIT_4).reason_code == (
        "HIERARCHY_COMPARISON_NOT_AVAILABLE"
    )
    assert relation(result, FitRelationId.FIT_7).status == FitStatus.FIT
    assert f"FIT semantic analysis incomplete: {reason_code}" in result.warnings


def test_invalid_llm_evidence_invalidates_only_semantic_batch() -> None:
    result = run_fit(complete_cpl_result(), FakeLlm(fit_response(invalid_ref=True)))
    semantic = [
        item
        for item in result.relations
        if item.relation_id not in {FitRelationId.FIT_4, FitRelationId.FIT_7}
    ]
    assert all(
        item.status == FitStatus.INSUFFICIENT
        and item.reason_code == "LLM_INVALID_RESPONSE"
        for item in semantic
    )
    assert relation(result, FitRelationId.FIT_7).status == FitStatus.FIT


def test_pipeline_fit_boundary_uses_runtime_contract() -> None:
    result = asyncio.run(
        analysis_pipeline._run_fit(
            complete_cpl_result(),
            FakeLlm(fit_response()),
            settings(),
            case_id=1,
        )
    )
    assert result is not None
    assert result.ruleset_version == "fit-v0.2"
    assert result.prompt_version == "fit-v0.2"


def test_analysis_pipeline_calls_fit_retrieval_then_sim(monkeypatch) -> None:
    cpl = complete_cpl_result()
    calls: list[str] = []
    fit_marker = object()

    async def parse(*_args):
        calls.append("parse")

    async def complete(*_args):
        calls.append("cpl")
        return cpl

    async def fit(*_args):
        calls.append("fit")
        return fit_marker

    retrieval_marker = object()

    async def retrieval(*_args):
        calls.append("retrieval")
        return retrieval_marker

    async def sim(*args):
        calls.append("sim")
        assert args[1] is retrieval_marker

    monkeypatch.setattr(analysis_pipeline, "run_case_parsing", parse)
    monkeypatch.setattr(analysis_pipeline, "_case_status", lambda *_args: "CHECKING")
    monkeypatch.setattr(
        analysis_pipeline,
        "_load_parsed_document",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        analysis_pipeline,
        "evaluate_cpl_rules",
        lambda *_args, **_kwargs: cpl,
    )
    monkeypatch.setattr(analysis_pipeline, "_complete_semantic_review", complete)
    monkeypatch.setattr(analysis_pipeline, "run_cpl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        analysis_pipeline,
        "request_reason_from_result",
        lambda *_args: None,
    )
    monkeypatch.setattr(analysis_pipeline, "_run_fit", fit)
    monkeypatch.setattr(analysis_pipeline, "_run_retrieval", retrieval)
    monkeypatch.setattr(analysis_pipeline, "_run_sim", sim)

    result = asyncio.run(
        analysis_pipeline.run_analysis_pipeline(
            object(),
            object(),
            object(),
            None,
            settings(),
            case_id=1,
        )
    )
    assert result is fit_marker
    assert calls == ["parse", "cpl", "fit", "retrieval", "sim"]


def test_relative_fit_config_paths_resolve_from_project_root() -> None:
    runtime = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret=JWT_SECRET,
        fit_scoring_path="backend/config/fit_scoring.json",
        fit_prompt_path="backend/config/prompts/fit-v0.2.txt",
    )
    assert runtime.fit_scoring_path.is_absolute()
    assert runtime.fit_prompt_path.is_absolute()
