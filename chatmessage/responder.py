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


# ------------------------------------------------------------ 비교군 provenance
#
# 숫자가 맞아도 **어느 비교군에서 나온 숫자인지**가 틀리면 답이 틀린 것이다.
# 실측: 연구개발|grant|project|taxonomy 비교군이 15건뿐이라 MIN_COHORT(30)에
# 못 미쳐 `단위x출처` 로 물러난다. 그 단계는 support_type 을 키에 쓰지 않는다.
# 그런데 백분위 숫자는 정상으로 보여서, "같은 연구개발 사업 중 75.2%" 라고
# 쓰면 아무도 틀렸다는 걸 모른다.
_TYPE_COHORT_RE = re.compile(
    r"같은\s*지원\s*성격|동일\s*지원\s*성격"
    r"|같은\s*지원\s*유형|동일\s*지원\s*유형"
    r"|같은\s*유형|동일\s*유형|같은\s*성격|동일\s*성격"
    r"|같은\s*[가-힣·A-Za-z]{2,12}\s*사업\s*(?:중|대비|가운데|내|과|와)"
    r"|[가-힣·]{2,12}\s*사업군")

# Model 3 이 전체 비교군으로 물러났는지. score_pool 이 "L0 전체" 를 돌려준다.
_M3_FALLBACK_HINTS = ("L0", "전체")


def _provenance(context: Any) -> Dict[str, Any]:
    return (context or {}).get("_provenance") or {} if isinstance(context, dict) else {}


def percentile_cohort_claims(answer: str, context: Any) -> List[str]:
    """Rule A — support_type 을 안 쓴 백분위인데 성격 비교군인 것처럼 말했는가."""
    prov = _provenance(context)
    if "percentile_uses_support_type" not in prov:
        return []                    # 판단 근거가 없으면 판정하지 않는다
    if prov.get("percentile_uses_support_type"):
        return []                    # 실제로 썼으면 그 표현이 맞다
    return [m.group(0).strip() for m in _TYPE_COHORT_RE.finditer(answer or "")]


def model3_cohort_claims(answer: str, context: Any) -> List[str]:
    """Rule B — 전체 비교군으로 물러났는데 특정 유형 비교군이라고 말했는가."""
    if not isinstance(context, dict):
        return []
    level = str((context.get("reference") or {}).get("cohort_level") or "")
    if not level:
        return []
    if not any(h in level for h in _M3_FALLBACK_HINTS):
        return []                    # 유형 단계에서 나왔으면 그 표현이 맞다
    return [m.group(0).strip() for m in _TYPE_COHORT_RE.finditer(answer or "")]


def check_grounding(answer: str, intent: str, context: Any) -> List[str]:
    """답변 하나에 대한 경고 목록."""
    warnings: List[str] = []
    bad = ungrounded_numbers(answer, context)
    if bad:
        warnings.append("context 에 없는 수치: %s" % ", ".join(bad))
    if intent == "MODEL_2":
        bad_cohort = percentile_cohort_claims(answer, context)
        if bad_cohort:
            warnings.append(
                "percentile comparison incorrectly implies support_type-based "
                "cohort (실제 %s): %s"
                % (_provenance(context).get("percentile_cohort_level") or "?",
                   ", ".join(bad_cohort)))

    if intent == "MODEL_3":
        claims = axis_cause_claims(answer, context)
        if claims:
            warnings.append("축별 기여도 없이 원인을 단정: %s" % ", ".join(claims))
        if isinstance(context, dict) and context.get("anomaly_level") is None:
            if re.search(r"(높은|낮은|중간)\s*수준(입니다|이다)", answer or ""):
                warnings.append("anomaly_level 이 null 인데 수준을 단정")
        bad_m3 = model3_cohort_claims(answer, context)
        if bad_m3:
            warnings.append(
                "model3 comparison incorrectly implies type-specific cohort "
                "(실제 %s): %s"
                % ((context.get("reference") or {}).get("cohort_level"),
                   ", ".join(bad_m3)))
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
