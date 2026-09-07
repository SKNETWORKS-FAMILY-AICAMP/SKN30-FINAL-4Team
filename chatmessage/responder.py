"""LLM 호출과 **근거 점검**.

프롬프트로 "없는 숫자를 만들지 말라" 고 적는 것만으로는 확인이 안 된다. 답변이
나온 뒤에 실제로 Context 안의 값만 썼는지 기계적으로 한 번 더 본다.

점검은 **막지 않고 표시만** 한다. 오탐이 있을 수 있어서다 — 예를 들어
526,390,144원을 "약 5억 2,639만원" 으로 풀어 쓰면 글자만 보면 새 숫자처럼
보인다. 그래서 자릿수 부분일치까지 허용하고, 그래도 걸리면 경고로 남긴다.
판단은 사람이 한다.
"""
import re
from typing import Any, Dict, List

from .prompt import SYSTEM_PROMPT, build_user_prompt

TEMPERATURE = 0.0


class ChatLLMClient:
    """LLM provider 경계. 챗봇 로직은 이 인터페이스만 안다."""

    def generate(self, system_prompt: str, user_prompt: str,
                 temperature: float = TEMPERATURE) -> str:
        raise NotImplementedError


# ------------------------------------------------------------------ 근거 점검
_DIGITS = re.compile(r"\d[\d,]*")
MIN_DIGITS = 3          # 두 자리 이하는 조사·순서 표현에 흔해 세지 않는다


def _numbers_in(value: Any, out: List[str]) -> List[str]:
    """Context 안의 모든 숫자를 문자열로 모은다(중첩 dict/list 포함)."""
    if isinstance(value, dict):
        for v in value.values():
            _numbers_in(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _numbers_in(v, out)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        out.append(str(value))
        out.append(("%f" % value).rstrip("0").rstrip("."))
    elif isinstance(value, str):
        out.extend(m.group(0).replace(",", "") for m in _DIGITS.finditer(value))
    return out


def ungrounded_numbers(answer: str, context: Any) -> List[str]:
    """답변에는 있는데 Context 에는 없는 수치. 없으면 빈 목록."""
    haystack = " ".join(_numbers_in(context, []))
    bad = []
    for m in _DIGITS.finditer(answer or ""):
        token = m.group(0).replace(",", "")
        if len(token) < MIN_DIGITS:
            continue
        if token in haystack:            # 부분일치 허용 (단위 풀어쓰기 대응)
            continue
        bad.append(m.group(0))
    return sorted(set(bad))


# Model 3 축별 원인 단정. 기여도가 계산되지 않았는데 원인을 지목하면 안 된다.
_CAUSE_RE = re.compile(
    r"(가장\s*큰\s*원인|주요\s*원인|원인\s*(입니다|이다|은|는))"
    r"|(가장\s*크게\s*(영향|기여))"
    r"|(때문에\s*이례)"
    r"|(이례적인\s*이유는)")


def axis_cause_claims(answer: str, context: Any) -> List[str]:
    """축별 기여도가 없는데 원인을 단정했는가."""
    has_contribution = (isinstance(context, dict)
                        and any(k in context for k in
                                ("axis_contribution", "contributions",
                                 "shap", "feature_importance")))
    if has_contribution:
        return []
    return [m.group(0) for m in _CAUSE_RE.finditer(answer or "")]


def check_grounding(answer: str, intent: str, context: Any) -> List[str]:
    """답변 하나에 대한 경고 목록."""
    warnings: List[str] = []
    bad = ungrounded_numbers(answer, context)
    if bad:
        warnings.append("context 에 없는 수치: %s" % ", ".join(bad))
    if intent == "MODEL_3":
        claims = axis_cause_claims(answer, context)
        if claims:
            warnings.append("축별 기여도 없이 원인을 단정: %s" % ", ".join(claims))
        if isinstance(context, dict) and context.get("anomaly_level") is None:
            if re.search(r"(높은|낮은|중간)\s*수준(입니다|이다)", answer or ""):
                warnings.append("anomaly_level 이 null 인데 수준을 단정")
    return warnings


# ------------------------------------------------------------------ 호출
def generate_answer(llm_client: ChatLLMClient, question: str, intent: str,
                    context: Any) -> str:
    """LLM 한 번 호출. temperature 는 항상 0.0 이다 — 같은 근거면 같은 답이어야 한다."""
    user_prompt = build_user_prompt(question=question, intent=intent,
                                    context=context)
    return llm_client.generate(system_prompt=SYSTEM_PROMPT,
                               user_prompt=user_prompt,
                               temperature=TEMPERATURE)


def answer_with_checks(llm_client: ChatLLMClient, question: str, intent: str,
                       context: Any) -> Dict[str, Any]:
    """답변 + 경고. service 가 이걸 그대로 응답에 싣는다."""
    text = generate_answer(llm_client, question, intent, context)
    return {"text": text, "warnings": check_grounding(text, intent, context)}
