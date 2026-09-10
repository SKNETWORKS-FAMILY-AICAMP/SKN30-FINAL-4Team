"""챗봇 Context 구성 회귀 검사. DB·LLM·backend 를 붙이지 않는다.

    python -m pytest chatmessage/test_context.py

리포트를 pydantic 모델이 아니라 **dict 그대로** 넣는다. 이 패키지는 backend 를
import 하지 않기 때문이고, 실제 호출부도 `model_dump(mode="json")` 한 dict 를
넘긴다 — 테스트가 보는 모양과 운영이 보는 모양이 같아야 한다.
"""
import copy
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from chatmessage.context import (  # noqa: E402
    MAX_EVIDENCE_ITEMS,
    build_chat_context,
    context_evidence_ids,
    context_scope,
    wants_revision,
)
from chatmessage.schema import SECTION_KEYS  # noqa: E402

# 실제 코드값. backend 를 import 하지 않으려고 여기 적어 둔다. 개수(13/7)가
# 계약이라, 늘거나 줄면 이 테스트가 먼저 깨지는 편이 낫다.
CPL_FIELDS = (
    "REQUEST_TYPE", "PURPOSE_GOAL", "IMPLEMENTATION_PLAN", "BUSINESS_PERIOD",
    "NEW_OR_CHANGED_CONTENT", "BUSINESS_NEED", "LEGAL_BASIS", "LINKED_POLICY",
    "BUDGET", "TARGET_AND_CONDITIONS", "SUPPORT_CONTENT_AND_SCALE",
    "DELIVERY_SYSTEM", "EXPECTED_EFFECTS_AND_PERFORMANCE",
)
FIT_RELATIONS = ("FIT-1", "FIT-2", "FIT-3", "FIT-4", "FIT-5", "FIT-6", "FIT-7")


def evidence(reference: str, side: str = "REQUEST") -> dict:
    """근거 한 조각 — 파이프라인이 실제로 내보내는 모양 그대로.

    필드를 줄여 쓰면 안 된다. 중복 제거로 아끼는 양이 근거 하나의 크기에
    비례하는데, 실제 근거는 여기 있는 14개 필드를 전부 들고 있다.
    """
    return {
        "evidence_ref": reference,
        "source_side": side,
        "source_id": "SRC-1",
        "field_code": None,
        "axis_code": None,
        "source_role": None,
        "excerpt": "중소기업을 대상으로 기술개발 과제를 지원하며, 지원규모는 과제당 최대 3억원으로 한다.",
        "normalized_value": None,
        "block_id": None,
        "page_no": 3,
        "section_path": ["II. 사업개요", "2. 지원대상"],
        "source_locator": {"offset": 1024, "length": 48},
        "extraction_method": "RULE",
        "extraction_version": "v1",
    }


def report(**overrides) -> dict:
    payload = {
        "case": {
            "case_id": 42,
            "title": "기술개발 지원사업",
            "created_at": "2026-09-10T00:00:00Z",
            "completed_at": "2026-09-10T00:00:00Z",
        },
        "self_check": {
            "confirmed_count": 11,
            "total_count": 13,
            "confirmation_rate": 84.6,
            "items": [
                {
                    "field_code": field,
                    "status": "MISSING" if index == 0 else "PRESENT",
                    "reason_code": "NO_MATCH" if index == 0 else None,
                    "explanation": "근거 문장을 찾지 못했습니다." if index == 0 else None,
                    "occurrences": [evidence(f"request:{field}:0")],
                }
                for index, field in enumerate(CPL_FIELDS)
            ],
            "ruleset_version": "v1",
            "prompt_version": "cpl-v0.9",
            "model_profile": "gpt-4o-mini",
        },
        "structural_consistency": {
            "module_status": "AVAILABLE",
            "score": {
                "value": 71.0,
                "numerator": 5,
                "denominator": 7,
                "assessable_count": 7,
                "total_count": 7,
                "scoring_version": "v1",
            },
            "relations": [
                {
                    "relation_id": relation,
                    "status": "CONFLICT" if index == 1 else "FIT",
                    "score": 40 if index == 1 else 90,
                    "summary": "목적과 지원대상의 연결이 확인되지 않습니다.",
                    # FIT 좌변은 CPL 이 이미 든 근거를 그대로 다시 든다 — 실제
                    # 리포트가 그렇다. 통째로 실으면 같은 문장이 두 번 올라간다.
                    "left_evidence": [evidence("request:PURPOSE_GOAL:0")],
                    "right_evidence": [evidence(f"fit:{relation}:R")],
                    "rule_version": "v1",
                    "prompt_version": "fit-v0.5",
                }
                for index, relation in enumerate(FIT_RELATIONS)
            ],
            "ruleset_version": "v1",
            "prompt_version": "fit-v0.5",
            "scoring_version": "v1",
            "model_profile": "gpt-4o-mini",
        },
        "similar_candidates": [
            {
                "rank": 1,
                "announcement_id": "ANN-1",
                "announcement_version_id": 1,
                "title": "중소기업 기술혁신 지원사업",
                "source_url": "https://example.test/ann-1",
                "semantic_similarity": 0.81,
                "semantic_similarity_display": 81,
                "weighted_score": 74.0,
                "assessable_axis_count": 4,
                "review_grade": "FOCUS_REVIEW",
                "comparison_summary": "지원대상과 지원내용이 상당 부분 겹칩니다.",
                "axes": {
                    axis: {
                        "axis_id": axis_id,
                        "status": "SIMILAR",
                        "score": 80,
                        "summary": "유사합니다.",
                        "common_points": ["중소기업 대상"],
                        "differences": ["지원 규모가 다릅니다."],
                        "request_evidence": [evidence("request:TARGET_AND_CONDITIONS:0")],
                        "candidate_evidence": [
                            evidence(f"sim:{axis_id}:cand", "ANNOUNCEMENT")
                        ],
                    }
                    for axis, axis_id in (
                        ("purpose", "SIM-1"),
                        ("target", "SIM-2"),
                        ("content", "SIM-3"),
                        ("delivery", "SIM-4"),
                    )
                },
                "ruleset_version": "v1",
                "prompt_version": "sim-v0.3",
                "scoring_version": "v1",
                "model_profile": "gpt-4o-mini",
            }
        ],
        "models": {
            "model_1": {
                "status": "success",
                "result": {"support_type": "연구개발", "confidence": 0.82},
                "metadata": {"model_version": "m1-1.0"},
            },
            "model_2": {
                "status": "success",
                "result": {
                    "predicted_amount": 526390144,
                    "percentile": 75.2,
                    "buckets": {"p50": 1, "p90": 2},
                },
                "metadata": {
                    "percentile_cohort_level": "단위x출처",
                    "percentile_uses_support_type": False,
                    "model_version": "m2-1.0",
                },
            },
        },
        "dif": {
            "status": "insufficient_data",
            "result": None,
            "error": "비교군 표본 부족",
        },
        "module_summary": {"all_success": False},
        "review_issues": [
            {
                "issue_id": "ISS-1",
                "source": "CPL",
                "reference_id": CPL_FIELDS[0],
                "status": "MISSING",
                "summary": "항목을 확인하지 못했습니다.",
                "evidence": [],
            }
        ],
        "quality": "PARTIAL",
        "warnings": ["ML model 3 unavailable"],
    }
    payload.update(overrides)
    return payload


def test_scope_picks_every_section_the_question_needs() -> None:
    assert context_scope("확인이 필요한 부분을 알려줘") == {"cpl"}
    # 유사사업 질문은 검색과 비교가 한 쌍이라 둘 다 실린다.
    assert context_scope("비슷한 기존 사업은 뭐야?") == {"retrieval", "sim"}
    # 한 질문이 두 섹션을 가리킬 수 있다. 하나로 좁히지 않는다.
    assert {"fit", "model2"} <= context_scope("지원내용과 지원규모가 맞아?")
    # 요약 요청이면 전부 본다.
    assert context_scope("전체 리포트를 5줄로 요약해줘") == set(SECTION_KEYS)
    # 어디에도 안 걸리면 좁히지 않는다 — 범위를 안 준 질문이지 빈 질문이 아니다.
    assert context_scope("이 요청서 어때?") == set(SECTION_KEYS)


def test_natural_language_reaches_the_right_sections() -> None:
    """사용자는 CPL·FIT·SIM 이라는 이름을 모른다.

    초기 화면 예시 질문 다섯 개가 업무 자연어만으로 라우팅되는지 본다.
    """
    assert context_scope("확인이 필요한 부분을 알려줘") == {"cpl"}
    assert context_scope("내용이 서로 맞지 않는 부분이 있는지 봐줘") == {"fit"}
    assert context_scope("비슷한 기존 사업과 어떤 차이가 있는지 알려줘") == {
        "retrieval",
        "sim",
    }
    assert context_scope("전체 리포트를 5줄로 요약해줘") == set(SECTION_KEYS)
    assert wants_revision("수정이 필요한 부분을 제안해줘")
    # 내부 이름으로 물어도 여전히 된다 — 개발·회귀 질문용이다.
    assert context_scope("CPL 결과만 보여줘") == {"cpl"}
    assert context_scope("FIT 결과를 설명해줘") == {"fit"}


def test_summary_word_narrows_when_a_section_is_named() -> None:
    # "확인이 필요한 부분만 요약해줘" 는 요약 요청이지 전체 요청이 아니다.
    assert context_scope("확인이 필요한 부분만 요약해줘") == {"cpl"}
    assert context_scope("정합성 결과를 쉽게 요약해줘") == {"fit"}
    assert context_scope("전체 리포트를 5줄로 요약해줘") == set(SECTION_KEYS)


def test_follow_up_question_inherits_the_previous_scope() -> None:
    # 후속 질문에는 Agent 어휘가 없다. 매번 리포트 전체를 다시 싣지 않는다.
    assert context_scope(
        "해당 판단의 원문 근거를 보여줘", "확인이 필요한 부분을 알려줘"
    ) == {"cpl"}
    assert context_scope("왜 그렇게 판단했어?", "비슷한 기존 사업은 뭐야?") == {
        "retrieval",
        "sim",
    }
    # 앞 질문도 범위를 안 줬으면 좁힐 근거가 없다.
    assert context_scope("왜 그렇게 판단했어?", "이건 어때?") == set(SECTION_KEYS)


def test_revision_request_is_detected() -> None:
    assert wants_revision("지원대상 문구를 어떻게 고치면 좋을까?")
    assert not wants_revision("FIT 결과를 설명해줘")


def test_context_carries_every_agent_and_names_them_by_spec() -> None:
    context = build_chat_context(report(), "전체 리포트를 요약해줘")

    assert context["analysis_id"] == "42"
    assert set(context["report"]) == {
        "cpl",
        "fit",
        "retrieval",
        "sim",
        "model1",
        "model2",
        "model3",
        "summary",
    }
    assert context["context_scope"] == sorted(SECTION_KEYS)
    assert context["revision_requested"] is False


def test_scoped_sections_are_full_and_the_rest_stay_overview() -> None:
    context = build_chat_context(report(), "확인이 필요한 부분만 알려줘")
    sections = context["report"]

    assert sections["cpl"]["detail"] == "full"
    assert sections["fit"]["detail"] == "overview"
    assert sections["sim"]["detail"] == "overview"
    # 초점이 아니어도 결과 자체는 남는다. 상태와 개수는 여전히 답할 수 있다.
    assert sections["fit"]["status_counts"]["CONFLICT"] == 1
    assert sections["summary"]["detail"] == "full"


def test_evidence_is_listed_once_and_sections_point_at_it() -> None:
    context = build_chat_context(report(), "전체 리포트를 요약해줘")

    ids = [item["evidence_id"] for item in context["evidence"]]
    assert len(ids) == len(set(ids))

    cpl_first = context["report"]["cpl"]["items"][0]
    assert cpl_first["evidence_ids"]
    assert set(cpl_first["evidence_ids"]) <= context_evidence_ids(context)

    # 한 근거가 여러 Agent 에 걸리면 어느 Agent 가 썼는지 함께 남는다.
    assert all(item["agents"] for item in context["evidence"])


def test_overview_sections_do_not_leak_evidence_ids() -> None:
    context = build_chat_context(report(), "확인이 필요한 항목만 알려줘")
    allowed = context_evidence_ids(context)

    assert all(not reference.startswith("sim:") for reference in allowed)
    assert all(not reference.startswith("fit:") for reference in allowed)


def test_model_sections_keep_provenance_and_failure_reason() -> None:
    context = build_chat_context(report(), "예측 금액이 얼마야?")
    model2 = context["report"]["model2"]

    assert model2["status"] == "success"
    assert model2["result"]["predicted_amount"] == 526390144
    # 백분위와 그 출처 정보는 비노출이라 Context 에 남지 않는다.
    assert "percentile" not in model2["result"]
    assert "percentile_uses_support_type" not in model2["provenance"]
    # 예측값을 한정하는 provenance 는 남는다 — 금액 자체가 공개 대상이라
    # "학습 범주 밖이었다" 는 단서가 함께 있어야 한다.
    assert "percentile" not in json.dumps(model2, ensure_ascii=False)

    # 실패한 모델은 이유를 구분해 남긴다 — '없음' 과 '근거 부족' 은 다르다.
    model3 = context["report"]["model3"]
    assert model3["status"] == "insufficient_data"
    assert model3["result"] is None


def test_unfocused_model_section_drops_nested_result_values() -> None:
    context = build_chat_context(report(), "확인이 필요한 항목만 알려줘")
    model2 = context["report"]["model2"]

    assert model2["detail"] == "overview"
    assert "buckets" not in model2["result"]
    assert model2["result"]["predicted_amount"] == 526390144


def test_full_report_with_evidence_fits_under_the_cap() -> None:
    """근거를 가장 많이 요구하는 질문에서도 잘리지 않아야 한다.

    실제 리포트에 가깝게 부풀린다 — CPL 항목마다 근거 3개, 유사공고 후보 5건.
    잘리면 챗봇이 못 본 근거를 없는 것으로 답한다.
    """
    value = report()
    for item in value["self_check"]["items"]:
        item["occurrences"] = [
            evidence(f"request:{item['field_code']}:{index}") for index in range(3)
        ]
    base = value["similar_candidates"][0]
    value["similar_candidates"] = [
        {
            **copy.deepcopy(base),
            "rank": rank,
            "title": f"유사사업 {rank}",
            "axes": {
                axis: {
                    **copy.deepcopy(base["axes"][axis]),
                    "request_evidence": [evidence(f"sim:{rank}:{axis}:req")],
                    "candidate_evidence": [
                        evidence(f"sim:{rank}:{axis}:cand", "ANNOUNCEMENT")
                    ],
                }
                for axis in base["axes"]
            },
        }
        for rank in range(1, 6)
    ]

    context = build_chat_context(value, "전체 리포트를 근거와 함께 요약해줘")
    assert context["evidence_truncated"] is False
    assert context["evidence_omitted_count"] == 0
    assert len(context["evidence"]) < MAX_EVIDENCE_ITEMS


def test_truncation_is_declared_not_silent() -> None:
    """상한에 걸리면 그 사실이 Context 에 남아야 한다.

    조용히 빠지면 챗봇이 리포트의 근거를 전부 본 것처럼 답한다.
    """
    value = report()
    value["self_check"]["items"][0]["occurrences"] = [
        evidence(f"request:MANY:{index}") for index in range(MAX_EVIDENCE_ITEMS + 20)
    ]
    context = build_chat_context(value, "전체 리포트를 근거와 함께 요약해줘")
    assert context["evidence_truncated"] is True
    assert context["evidence_omitted_count"] > 0
    assert len(context["evidence"]) == MAX_EVIDENCE_ITEMS


@pytest.mark.parametrize(
    "question",
    ["확인이 필요한 부분을 알려줘", "전체 리포트를 요약해줘"],
)
def test_scoped_context_is_smaller_than_the_whole_report(question: str) -> None:
    value = report()
    whole = json.dumps(value, ensure_ascii=False)
    context = json.dumps(build_chat_context(value, question), ensure_ascii=False)
    assert len(context) < len(whole)
