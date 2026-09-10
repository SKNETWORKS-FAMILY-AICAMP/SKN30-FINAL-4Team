"""ML 내부 진단값 비노출 회귀 검사.

    python -m pytest chatmessage/test_nondisclosure.py

정책은 두 겹이다.

    1차  Context 생성 단계에서 값을 지운다   — 새어 나갈 값 자체를 없앤다
    2차  답변에서 표현을 잡는다              — 값 없이 "신뢰도가 높다" 로 말하는 경우

1차가 본체다. 프롬프트로만 막으면 값은 이미 프롬프트에 실려 있고, 모델이 규칙을
어기는 순간 그대로 나간다.
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chatmessage import SYSTEM_PROMPT, build_chat_context  # noqa: E402
from chatmessage.grounding import (  # noqa: E402
    check_grounding,
    internal_name_mentions,
    internal_value_mentions,
    link_mentions,
)
from chatmessage.provenance import (  # noqa: E402
    INTERNAL_VALUE_KEYS,
    is_internal_key,
    strip_internal_values,
)
from test_context import report  # noqa: E402

FIXTURES = os.path.join(_HERE, "fixtures")


def load_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


# ----------------------------------------------------------------- 1차 방어
@pytest.mark.parametrize(
    "key",
    [
        "percentile",
        "distance_percentile",
        "percentile_cohort_level",
        "percentile_uses_support_type",
        "confidence",
        "support_type_confidence",
        "class_probabilities",
        "probability",
        "raw_score",
        "raw_scores",
        "PERCENTILE",
    ],
)
def test_internal_keys_are_recognised(key: str) -> None:
    assert is_internal_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "predicted_amount",
        "support_type",
        "anomaly_level",
        "description",
        "support_type_compatibility",
        "cohort_level",
        "status",
    ],
)
def test_public_keys_survive(key: str) -> None:
    assert not is_internal_key(key)


def test_stripping_reaches_nested_structures() -> None:
    value = strip_internal_values(
        {
            "result": {"predicted_amount": 1, "percentile": 75.2},
            "list": [{"confidence": 0.9, "support_type": "연구개발"}],
            "deep": {"a": {"b": {"raw_score": 3, "keep": 1}}},
        }
    )
    assert value == {
        "result": {"predicted_amount": 1},
        "list": [{"support_type": "연구개발"}],
        "deep": {"a": {"b": {"keep": 1}}},
    }


def test_internal_values_never_reach_the_context() -> None:
    """내부 값이 섞여 온 입력으로도 Context 에 남지 않아야 한다."""
    context = build_chat_context(load_fixture("sample_ml_context.json"), "전체 요약")
    rendered = json.dumps(context, ensure_ascii=False)

    for marker in INTERNAL_VALUE_KEYS:
        assert marker not in rendered, f"Context 에 {marker} 가 남았다"
    # 공개 가능한 값은 그대로 있어야 한다.
    assert context["report"]["model2"]["result"]["predicted_amount"] == 526390144
    assert context["report"]["model1"]["result"]["support_type"] == "연구개발"
    assert context["report"]["model3"]["result"]["description"]


def test_similarity_score_is_not_in_the_context() -> None:
    """유사도 점수는 결과 화면도 쓰지 않는다. 챗봇만 숫자를 말하면 어긋난다."""
    context = build_chat_context(load_fixture("sample_full_context.json"), "비슷한 기존 사업 알려줘")
    rendered = json.dumps(context["report"]["retrieval"], ensure_ascii=False)
    assert "semantic_similarity" not in rendered
    # 순위는 점수가 아니라 순서라 남긴다.
    assert context["report"]["retrieval"]["candidates"][0]["rank"] == 1


# ------------------------------------------------------- Loader 입력 계약
@pytest.mark.parametrize(
    "status,expected",
    [
        ("available", "success"),
        ("success", "success"),
        ("ok", "success"),
        ("insufficient_data", "insufficient_data"),
        ("insufficient_evidence", "insufficient_data"),
        ("failed", "not_available"),
        (None, "not_available"),
    ],
)
def test_model_status_aliases(status, expected) -> None:
    """저장 구조가 확정되지 않아 status 이름이 무엇으로 올지 모른다.

    입구에서 흡수하고 챗봇이 읽는 이름은 하나로 고정한다.
    """
    value = report()
    value["models"] = {"model_1": {"status": status, "result": {"support_type": "x"}}}
    context = build_chat_context(value, "지원유형 알려줘")
    assert context["report"]["model1"]["status"] == expected


def test_missing_ml_becomes_not_available() -> None:
    """ML 저장 구조가 아직 없으면 그냥 넣지 않으면 된다. 오류가 아니다."""
    context = build_chat_context(load_fixture("sample_cpl_fit_sim.json"), "전체 요약")
    for key in ("model1", "model2", "model3"):
        assert context["report"][key]["status"] == "not_available"
        assert context["report"][key]["result"] is None
    # 나머지는 그대로 답할 수 있어야 한다.
    assert context["report"]["cpl"]["detail"] == "full"
    assert context["report"]["sim"]["candidates"]


def test_failure_reasons_stay_distinct() -> None:
    """'실행 못 했다' 와 '근거가 모자랐다' 를 같은 문구로 안내하면 안 된다."""
    context = build_chat_context(load_fixture("sample_not_available.json"), "전체 요약")
    assert context["report"]["model1"]["status"] == "not_available"
    assert context["report"]["model3"]["status"] == "insufficient_data"


# ----------------------------------------------------------------- 2차 방어
@pytest.mark.parametrize(
    "answer",
    [
        "신뢰도가 높습니다.",
        "모델은 이 금액에 대해 확신하고 있으며",
        "상위 25% 에 해당합니다.",
        "백분위는 75입니다.",
        "확률이 60% 입니다.",
    ],
)
def test_internal_wording_is_flagged(answer: str) -> None:
    assert internal_value_mentions(answer)


def test_public_wording_is_not_flagged() -> None:
    assert internal_value_mentions("예측 지원금액은 526,390,144원입니다.") == []
    assert internal_value_mentions("확인이 필요한 항목은 1개입니다.") == []


def test_echoing_the_users_own_word_to_decline_is_not_flagged() -> None:
    """"신뢰도가 높아?" 에 "신뢰도 정보는 없습니다" 라고 답하는 것은 정답이다."""
    assert internal_value_mentions(
        "예측의 신뢰도에 대한 정보는 제공되지 않았습니다.", "이 예측은 신뢰도가 높아?"
    ) == []
    # 묻지도 않았는데 꺼내면 여전히 걸린다.
    assert internal_value_mentions("신뢰도가 높습니다", "예측 금액이 얼마야?") == ["신뢰도"]


def test_asserting_an_internal_value_is_flagged_even_when_echoed() -> None:
    """되받되 **단정**하면 걸려야 한다.

    "얼마나 확신해?" → "확신하고 있다고 볼 수 있습니다" 는 값 없이 내부
    진단값을 단정한 것이다. 숫자를 안 썼으니 수치 검사에도 안 걸린다 —
    되받았다는 이유로 빼면 통째로 새어 나간다. 실측에서 나온 답변이다.
    """
    assert internal_value_mentions(
        "모델이 이 예측에 대해 확신하고 있다고 볼 수 있습니다.", "모델이 얼마나 확신해?"
    ) == ["확신"]


def test_prompt_forbids_item_codes_in_the_answer_body() -> None:
    """모듈명뿐 아니라 항목 코드도 사용자에게 보일 이유가 없다."""
    assert "FIT-1" in SYSTEM_PROMPT and "REQUEST_TYPE" in SYSTEM_PROMPT
    assert "항목 코드도 본문에 쓰지 않습니다" in SYSTEM_PROMPT


def test_links_in_the_answer_body_are_flagged() -> None:
    """유사공고 URL 을 요청서 근거처럼 붙이던 실측 사례."""
    assert link_mentions("근거: [요청 문서](https://example.test/ann-1)")
    assert link_mentions("https://example.test/ann-1 을 보세요")
    assert link_mentions("유사공고 제목은 중소기업 기술혁신 지원사업입니다") == []


def test_grounding_reports_both_defences() -> None:
    context = build_chat_context(load_fixture("sample_full_context.json"), "전체 요약")
    warnings = check_grounding(
        "신뢰도가 높고 자세한 내용은 https://example.test/ann-1 을 보세요.", context
    )
    assert any("내부 진단값" in warning for warning in warnings)
    assert any("링크" in warning for warning in warnings)


def test_internal_names_in_the_answer_are_flagged() -> None:
    """사용자는 CPL·FIT·SIM 을 모른다. 답변은 업무 언어로 쓴다."""
    assert internal_name_mentions(
        "CPL 분석 결과에 따르면 FIT Agent 가 판단하기로",
        "확인이 필요한 부분을 알려줘",
    ) == ["CPL", "FIT Agent"]
    # 업무 언어로 쓰면 걸리지 않는다.
    assert internal_name_mentions(
        "요청서의 필수 항목과 구조를 확인한 결과 한 가지가 비어 있습니다.",
        "확인이 필요한 부분을 알려줘",
    ) == []


def test_internal_names_the_user_asked_for_are_allowed() -> None:
    """개발·회귀 질문은 내부 이름으로 묻는다. 되받는 것까지 경고하지 않는다."""
    assert internal_name_mentions("CPL 결과는 다음과 같습니다", "CPL 결과만 보여줘") == []
    assert internal_name_mentions("FIT 관계 7개 중", "FIT 결과를 설명해줘") == []


def test_grounding_uses_the_question_from_the_context() -> None:
    """question 을 따로 안 줘도 Context 안의 질문으로 판단한다."""
    context = build_chat_context(
        load_fixture("sample_full_context.json"), "확인이 필요한 부분을 알려줘"
    )
    warnings = check_grounding("CPL Agent 가 판단한 결과입니다.", context)
    assert any("내부 이름" in warning for warning in warnings)


# ----------------------------------------------------------------- 프롬프트
def test_prompt_states_the_non_disclosure_policy() -> None:
    for marker in ("percentile", "confidence", "probability", "raw score"):
        assert marker in SYSTEM_PROMPT, f"프롬프트가 {marker} 비노출을 말하지 않는다"
    assert "예측 지원금액" in SYSTEM_PROMPT
    assert "URL" in SYSTEM_PROMPT


def test_prompt_requires_business_language() -> None:
    """내부 이름 대신 쓸 표현이 프롬프트에 실제로 적혀 있어야 한다."""
    assert "업무 언어" in SYSTEM_PROMPT
    for phrase in (
        "요청서의 필수 항목과 구조",
        "정합성",
        "유사사업과 비교한 결과",
    ):
        assert phrase in SYSTEM_PROMPT, f"프롬프트에 {phrase} 대체 표현이 없다"


def test_prompt_says_the_chatbot_does_not_rerun_analysis() -> None:
    """멀티에이전트 챗봇이 아니라는 것을 프롬프트가 말해야 한다."""
    assert "다시 분석하지 않고" in SYSTEM_PROMPT
    assert "저장된 분석 결과" in SYSTEM_PROMPT
