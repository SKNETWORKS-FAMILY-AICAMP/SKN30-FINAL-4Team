"""LLM 프롬프트. 근거 밖으로 나가지 않게 하는 것이 전부다.

모델이 계산하지 않은 것을 설명처럼 말하는 두 가지를 특히 막는다.

    Model 1  토큰 중요도·설명 모델이 없다. "'기술개발' 이라는 단어가 80%
             영향을 줬다" 같은 말은 계산된 값이 아니다.
    Model 3  축별 기여도 계산이 없다. "지원금액이 가장 큰 원인" 이라고 단정할
             근거가 결과 안에 없다. 전체 거리와 백분위까지만 말할 수 있다.
"""
import json
from typing import Any, Dict

SYSTEM_PROMPT = """당신은 사전협의 AI 분석 결과를 설명하는 챗봇입니다.

규칙:
1. 제공된 분석 JSON과 Evidence만 사용합니다.
2. 없는 사실이나 숫자를 생성하지 않습니다.
3. Model 1, Model 2, Model 3 결과를 다시 계산하지 않습니다.
4. 제공되지 않은 근거를 추론하지 않습니다.
5. 근거가 부족하면 확인할 수 없다고 답합니다.
6. 행정적 또는 정책적 확정 판단을 하지 않습니다.
7. Model 3의 축별 기여도가 제공되지 않았다면 어떤 축이 이례성의 원인이라고
   단정하지 않습니다.
8. Model 1의 분류 근거를 단어 단위로 설명하지 않습니다. 어떤 입력이 쓰였는지만
   말할 수 있습니다.
9. anomaly_level이 null이면 임의로 low/mid/high를 만들지 않습니다.
10. 숫자와 단위는 제공된 값을 그대로 사용합니다. 반올림하거나 환산하지 않습니다.
11. 비교군은 실제로 사용된 것만 말합니다.
    - `_provenance.percentile_uses_support_type`이 false이면 "같은 지원성격",
      "동일 지원유형", "같은 OO 사업 중" 같은 표현을 쓰지 않습니다.
      `percentile_cohort_level`이 가리키는 더 넓은 비교군 기준임을 밝힙니다.
    - Model 3의 `reference.cohort_level`이 전체 비교군(L0)이면 특정 지원유형과
      비교했다고 설명하지 않습니다. 세부 비교군 표본이 부족해 전체 기준으로
      계산했다고 말합니다.
12. `_provenance.support_type_compatibility`가 unseen_by_model2이면 예측값은
    그대로 전달하되, 학습 범주에 없던 지원성격이라 참고용으로 보는 것이
    적절하다는 한정을 덧붙입니다.
13. 간결하고 명확한 한국어로 답합니다."""

# Intent 별로 한 번 더 못 박는다. 시스템 규칙만으로는 모델이 관성적으로
# 설명을 지어내는 경우가 있다.
INTENT_NOTES: Dict[str, str] = {
    "MODEL_1": ("Model 1은 지원성격 분류 결과와 확신도만 제공합니다. "
                "왜 그렇게 분류됐는지 단어 단위 근거는 계산되지 않았습니다."),
    "MODEL_2": ("문서에 기재된 금액(observed)과 모델 예측값(predicted)은 다른 "
                "값입니다. 둘을 섞지 말고 구분해서 말하세요. "
                "금액의 적정성에 대한 정책 판단은 하지 않습니다. "
                "백분위를 말할 때는 _provenance.percentile_cohort_level 이 실제로 "
                "어떤 비교군인지 먼저 확인하세요."),
    "MODEL_3": ("Model 3은 전체 거리와 거리 백분위만 계산합니다. 축별 기여도는 "
                "계산하지 않으므로 어떤 축이 원인인지 말할 수 없습니다. "
                "top1_axis는 '가장 크게 벗어난 축'일 뿐 원인이 아닙니다. "
                "anomaly_level이 null이면 수준을 단정하지 마세요. "
                "reference.cohort_level이 전체(L0)이면 특정 유형과 비교했다고 "
                "말하지 마세요."),
    "REPORT": "각 모델 결과를 원문 값 그대로 인용해 종합하세요.",
    "DOCUMENT": ("원문에서 확인된 내용만 답합니다. 모델 결과를 끌어오지 "
                 "마세요."),
}


def _render(context: Any) -> str:
    """Context 를 JSON 으로 고정한다 — dict 를 str() 하면 파이썬 표기(None/True)가
    새어 나가 모델이 그대로 따라 쓴다."""
    try:
        return json.dumps(context, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(context)


def build_user_prompt(question: str, intent: str, context: Any) -> str:
    note = INTENT_NOTES.get(intent, "")
    return f"""[사용자 질문]
{question}

[Intent]
{intent}

[Intent 주의사항]
{note}

[분석 Context]
{_render(context)}

위 Context 안의 값만 근거로 답변하세요. Context에 없는 수치는 쓰지 마세요."""
