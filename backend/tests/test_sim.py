import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.cpl import (
    CPL_FIELDS,
    CplAxisCode,
    CplFieldCode,
    CplItem,
    CplOccurrence,
    CplResult,
    CplStatus,
)
from app.schemas.sim import (
    SimAxis,
    SimReviewGrade,
    SimScoringPolicy,
    SimSemanticResponse,
    SimStatus,
)
from app.services.sim.sim_engine import (
    _Candidate,
    _candidate_profile,
    _compare_candidate,
    _request_profile,
    load_sim_prompt,
    load_sim_scoring,
)


def scoring() -> SimScoringPolicy:
    return SimScoringPolicy.model_validate(
        {
            "version": "sim-alpha-v0.1",
            "axis_weights": {
                "purpose": 0.30,
                "target": 0.30,
                "content": 0.30,
                "delivery": 0.10,
            },
            "status_scores": {
                "SIMILAR": 100,
                "PARTIAL": 50,
                "DIFFERENT": 0,
                "INSUFFICIENT": None,
            },
            "grades": {"focus_review_min": 70, "general_review_min": 40},
            "retrieval": {"top_k": 5},
        }
    )


def cpl_result() -> CplResult:
    values = {
        CplFieldCode.PURPOSE_GOAL: [
            occurrence(CplAxisCode.PURPOSE_PROBLEM_DOMAIN, "수출 기반 부족"),
            occurrence(CplAxisCode.PURPOSE_DIRECTION, "해외 진출 확대"),
            occurrence(
                CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE,
                "해외시장 진출",
            ),
        ],
        CplFieldCode.TARGET_AND_CONDITIONS: [
            occurrence(CplAxisCode.TARGET_GROUP, "제조 중소기업"),
            occurrence(CplAxisCode.COND_REGION, "서울 소재"),
        ],
        CplFieldCode.SUPPORT_CONTENT_AND_SCALE: [
            occurrence(CplAxisCode.SUPPORT_ACTIVITY, "수출 컨설팅"),
            occurrence(
                CplAxisCode.PER_COMPANY_LIMIT,
                "기업당 3천만원",
                normalized={"amount_won": 30_000_000, "unit": "KRW"},
            ),
        ],
        CplFieldCode.DELIVERY_SYSTEM: [
            occurrence(CplAxisCode.DELIVERY_ORG_NAME, "전담기관"),
        ],
    }
    return CplResult(
        ruleset_version="cpl-alpha-v0.2",
        prompt_version="cpl-semantic-v0.2",
        items=[
            CplItem(
                field_code=field_code,
                status=(
                    CplStatus.PRESENT
                    if field_code in values
                    else CplStatus.MISSING
                ),
                occurrences=values.get(field_code, []),
            )
            for field_code in CPL_FIELDS
        ],
    )


def occurrence(
    axis: CplAxisCode,
    raw_text: str,
    *,
    normalized: object | None = None,
) -> CplOccurrence:
    return CplOccurrence(
        raw_text=raw_text,
        normalized_value=normalized or {"text": raw_text},
        axis_code=axis,
        page_no=2,
        section_path=["사업 개요"],
        source_locator={"paragraph_index": 1},
        block_id=f"block:{axis.value}",
        extraction_method="LLM",
    )


def candidate() -> _Candidate:
    return _Candidate(
        retrieval_candidate_id=10,
        rank=1,
        announcement_id="PBLN-1",
        announcement_version_id=20,
        title="해외진출 지원",
        source_url="https://example.com/1",
        semantic_similarity=0.82,
        semantic_similarity_display=82,
        purpose="중소기업 해외시장 진출 확대",
        target="서울 소재 제조 중소기업",
        content="수출 컨설팅 지원",
        target_name="중소기업",
        jurisdiction_name="중소벤처기업부",
        executing_name="전담기관",
        detail_ref_fields=("target",),
    )


def semantic_response(*, invalid_ref: bool = False) -> SimSemanticResponse:
    request = _request_profile(cpl_result())
    public, _ = _candidate_profile(candidate())

    def refs(axis: SimAxis, side: str) -> list[str]:
        values = request[axis] if side == "request" else public[axis]
        return list(values)[:1]

    return SimSemanticResponse.model_validate(
        {
            "axes": {
                "purpose": {
                    "status": "SIMILAR",
                    "summary": "목적이 명시적으로 겹칩니다.",
                    "common_points": ["해외 진출"],
                    "differences": [],
                    "request_evidence_refs": (
                        ["request:invalid"]
                        if invalid_ref
                        else refs(SimAxis.PURPOSE, "request")
                    ),
                    "candidate_evidence_refs": refs(SimAxis.PURPOSE, "candidate"),
                    "reason_code": None,
                },
                "target": {
                    "status": "PARTIAL",
                    "summary": "지원대상이 부분집합 관계입니다.",
                    "common_points": ["중소기업"],
                    "differences": ["지역"],
                    "request_evidence_refs": refs(SimAxis.TARGET, "request"),
                    "candidate_evidence_refs": refs(SimAxis.TARGET, "candidate"),
                    "reason_code": "SUBSET_OVERLAP",
                },
                "content": {
                    "status": "DIFFERENT",
                    "summary": "지원활동이 다릅니다.",
                    "common_points": [],
                    "differences": ["활동"],
                    "request_evidence_refs": refs(SimAxis.CONTENT, "request"),
                    "candidate_evidence_refs": refs(SimAxis.CONTENT, "candidate"),
                    "reason_code": "EXPLICIT_NON_OVERLAP",
                },
                "delivery": {
                    "status": "INSUFFICIENT",
                    "summary": "수행방식 정보가 부족합니다.",
                    "common_points": [],
                    "differences": [],
                    "request_evidence_refs": [],
                    "candidate_evidence_refs": [],
                    "reason_code": "COMPARISON_EVIDENCE_MISSING",
                },
            },
            "comparison_summary": "목적은 유사하나 대상과 내용 차이를 검토해야 합니다.",
        }
    )


class FakeLlm:
    def __init__(self, response: SimSemanticResponse) -> None:
        self.response = response
        self.user_input: dict | None = None

    async def generate_structured(self, **kwargs):
        assert kwargs["task_name"] == "sim_candidate_comparison"
        assert kwargs["response_schema"] is SimSemanticResponse
        self.user_input = json.loads(kwargs["messages"][1].content)
        return self.response


def test_versioned_sim_policy_and_prompt_load() -> None:
    policy = load_sim_scoring(Path("config/sim_scoring.json"))
    prompt = load_sim_prompt(Path("config/prompts/sim-v0.2.txt"))
    assert policy.version == "sim-alpha-v0.2"
    assert policy.axis_weights[SimAxis.DELIVERY] == 0.10
    assert policy.status_scores[SimStatus.INSUFFICIENT] is None
    assert policy.retrieval.top_k == 5
    assert "Do not calculate scores or grades" in prompt
    assert "Judge every axis independently" in prompt


def test_invalid_scoring_policy_is_rejected() -> None:
    value = scoring().model_dump(mode="json")
    del value["axis_weights"]["delivery"]
    with pytest.raises(ValidationError, match="every axis"):
        SimScoringPolicy.model_validate(value)


def test_request_profile_preserves_axes_occurrences_and_normalized_values() -> None:
    profile = _request_profile(cpl_result())
    assert {item.profile_key for item in profile[SimAxis.PURPOSE].values()} == {
        "problem_domain",
        "direction",
        "specific_objective",
    }
    assert {item.excerpt for item in profile[SimAxis.TARGET].values()} == {
        "제조 중소기업",
        "서울 소재",
    }
    assert {item.profile_key for item in profile[SimAxis.CONTENT].values()} == {
        "activity"
    }
    assert all(
        item.profile_key != "per_company_limit"
        for item in profile[SimAxis.CONTENT].values()
    )
    assert next(iter(profile[SimAxis.PURPOSE].values())).page_no == 2


def test_candidate_summary_adapter_preserves_source_and_detail_warning() -> None:
    profile, warnings = _candidate_profile(candidate())
    assert {item.extraction_method for item in profile[SimAxis.PURPOSE].values()} == {
        "SOURCE"
    }
    assert {item.profile_key for item in profile[SimAxis.CONTENT].values()} == {
        "source_text",
    }
    assert warnings == [
        "Candidate summary refers to an unparsed detail attachment for: target"
    ]


def test_candidate_profile_does_not_replicate_unstructured_summary_across_keys() -> None:
    profile, _ = _candidate_profile(candidate())

    for axis, source_field in (
        (SimAxis.PURPOSE, "purpose"),
        (SimAxis.TARGET, "target"),
        (SimAxis.CONTENT, "content"),
    ):
        source_evidence = [
            evidence
            for evidence in profile[axis].values()
            if evidence.source_locator.get("source_field") == source_field
        ]
        assert len(source_evidence) == 1
        assert source_evidence[0].profile_key == "source_text"


def test_candidate_profile_matches_the_authored_sim_golden() -> None:
    golden = json.loads(
        (Path(__file__).parents[2] / "samples" / "golden" / "sim_profile_01.json")
        .read_text(encoding="utf-8")
    )
    profile, _ = _candidate_profile(candidate())
    actual = {}
    for axis in SimAxis:
        actual[axis.value] = sorted(
            [
                {
                    "source_field": evidence.source_locator["source_field"],
                    "profile_key": evidence.profile_key,
                    "excerpt": evidence.excerpt,
                }
                for evidence in profile[axis].values()
            ],
            key=lambda value: (value["source_field"], value["profile_key"]),
        )
    expected = {
        axis: sorted(
            values,
            key=lambda value: (value["source_field"], value["profile_key"]),
        )
        for axis, values in golden["expected"].items()
    }
    assert actual == expected
    assert golden["invariants"]["unstructured_source_excerpt_max_profile_keys"] == 1


def test_semantic_comparison_is_grounded_and_scored_without_delivery() -> None:
    llm = FakeLlm(semantic_response())
    request = _request_profile(cpl_result())
    public, warnings = _candidate_profile(candidate())
    result = asyncio.run(
        _compare_candidate(
            candidate(),
            request,
            public,
            warnings,
            llm,
            scoring(),
            "prompt",
            "sim-v0.1",
            "sim-v0.1",
            "gpt-4o-mini",
        )
    )
    assert result.assessable_axis_count == 3
    assert result.weighted_score == pytest.approx(50.0)
    assert result.review_grade == SimReviewGrade.GENERAL_REVIEW
    assert result.axes.delivery.score is None
    assert result.axes.purpose.axis_id == "SIM-1"
    assert llm.user_input is not None
    assert set(llm.user_input["axes"]) == {axis.value for axis in SimAxis}


def test_invalid_llm_evidence_invalidates_only_its_axis() -> None:
    request = _request_profile(cpl_result())
    public, warnings = _candidate_profile(candidate())
    result = asyncio.run(
        _compare_candidate(
            candidate(),
            request,
            public,
            warnings,
            FakeLlm(semantic_response(invalid_ref=True)),
            scoring(),
            "prompt",
            "sim-v0.1",
            "sim-v0.1",
            "gpt-4o-mini",
        )
    )
    assert result.review_grade == SimReviewGrade.ON_HOLD
    assert result.weighted_score is None
    assert result.assessable_axis_count == 2
    assert result.axes.purpose.reason_code == "LLM_INVALID_RESPONSE"
    assert result.axes.purpose.request_evidence
    assert result.axes.purpose.candidate_evidence
    assert result.axes.target.status == SimStatus.PARTIAL
    assert result.axes.content.status == SimStatus.DIFFERENT
    assert result.axes.delivery.status == SimStatus.INSUFFICIENT
    assert result.comparison_summary == "일부 비교축을 완료하지 못했습니다."
    assert result.warnings[-1] == (
        "SIM semantic axis incomplete: SIM-1:LLM_INVALID_RESPONSE"
    )
