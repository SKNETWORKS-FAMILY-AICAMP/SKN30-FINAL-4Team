"""답변 사후 점검 — 프롬프트가 지켜졌는지 기계적으로 한 번 더 본다.

"없는 숫자를 만들지 말라"고 프롬프트에 적는 것은 확인이 아니다. 답이 나온 뒤에
실제로 Context 안의 값만 썼는지 본다.

**막지 않고 기록만 한다.** 오탐이 있다 — 526,390,144원을 "약 5억 2,639만원"으로
풀어 쓰면 글자만 보면 새 숫자로 보인다. 사용자에게 답을 안 주는 쪽이 더 나쁘다.
경고는 로그로 남기고 판단은 사람이 한다.

model2 백분위 비교군 검사는 없앴다. 백분위가 Context 에서 아예 제거돼(비노출
정책) 판정할 값이 없다. 대신 `internal_value_mentions` 가 그 자리를 대신한다 —
값 없이 "신뢰도가 높습니다" 처럼 표현만 끌어다 쓰는 경우를 잡는다.
"""
import re
from typing import Any


_DIGITS = re.compile(r"\d[\d,]*")
# 두 자리 이하는 순서·조사 표현에 흔하다("3가지", "13개 중"). 세지 않는다.
MIN_DIGITS = 3

# Model 3 축별 원인 단정. 기여도가 계산되지 않았는데 원인을 지목하면 안 된다.
_CAUSE = re.compile(
    r"(가장\s*큰\s*원인|주요\s*원인|원인\s*(입니다|이다|은|는))"
    r"|(가장\s*크게\s*(영향|기여))"
    r"|(때문에\s*이례)"
    r"|(이례적인\s*이유는)"
)

# 지원유형 기준 비교군인 것처럼 말하는 표현.
_TYPE_COHORT = re.compile(
    r"같은\s*지원\s*성격|동일\s*지원\s*성격"
    r"|같은\s*지원\s*유형|동일\s*지원\s*유형"
    r"|같은\s*유형|동일\s*유형|같은\s*성격|동일\s*성격"
    r"|같은\s*[가-힣·A-Za-z]{2,12}\s*사업\s*(?:중|대비|가운데|내|과|와)"
    r"|[가-힣·]{2,12}\s*사업군"
)

_M3_FALLBACK_HINTS = ("L0", "전체")


def _numbers_in(value: Any, out: list[str]) -> list[str]:
    if isinstance(value, dict):
        for nested in value.values():
            _numbers_in(nested, out)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _numbers_in(nested, out)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        out.append(str(value))
        out.append(("%f" % value).rstrip("0").rstrip("."))
    elif isinstance(value, str):
        out.extend(match.group(0).replace(",", "") for match in _DIGITS.finditer(value))
    return out


def ungrounded_numbers(answer: str, context: Any) -> list[str]:
    """답변에는 있는데 Context 에는 없는 수치."""
    haystack = " ".join(_numbers_in(context, []))
    bad = []
    for match in _DIGITS.finditer(answer or ""):
        token = match.group(0).replace(",", "")
        if len(token) < MIN_DIGITS:
            continue
        if token in haystack:  # 부분일치 허용 (단위 풀어쓰기 대응)
            continue
        bad.append(match.group(0))
    return sorted(set(bad))


# ML 내부 진단값을 말로 꺼내는 표현. 값 자체는 Context 에서 제거했지만, 모델이
# "신뢰도가 높습니다" 처럼 숫자 없이 단정하는 것까지는 막지 못한다.
# `확신도` 만 막으면 "모델은 이 금액에 대해 확신하고 있으며" 가 그대로 나간다
# (실측). 값이 없어도 어미만 바꿔 같은 말을 하므로 어간으로 잡는다.
_INTERNAL_MENTION = re.compile(
    r"백분위|퍼센타일|percentile"
    r"|확신|신뢰도|신뢰할|confidence"
    r"|확률(?:이|은|을|로|가|입니다|입니까)?"
    r"|상위\s*\d+\s*%|하위\s*\d+\s*%"
)


# 답변 본문의 링크·URL. Context 안에 있는 주소라도 요청서 근거로 붙으면 틀린
# 것이라, "Context 에 있는가" 로는 판정할 수 없다. 본문에 링크가 있다는 사실
# 자체를 경고한다 — 근거는 references 로만 가리키기로 했다.
_LINK_IN_ANSWER = re.compile(r"\[[^\]]*\]\((?:https?://|www\.)[^)]*\)|https?://\S+")


def link_mentions(answer: str) -> list[str]:
    """답변 본문에 링크를 붙였는가.

    실측: CPL 질문에 답하면서 유사공고의 `source_url` 을 "근거" 링크로 붙였다.
    주소는 Context 안에 있던 값이라 수치 검사에는 걸리지 않는다.
    """
    return sorted({match.group(0).strip() for match in _LINK_IN_ANSWER.finditer(answer or "")})


# "그 값은 없다" 고 답하는 표현. 사용자의 단어를 되받되 부정하는 경우를
# 가려내는 데 쓴다.
_DENIAL = re.compile(
    r"제공되지\s*않|계산되지\s*않|확인할\s*수\s*없|알\s*수\s*없"
    r"|없습니다|없다|담기지\s*않|포함되지\s*않|나와\s*있지\s*않"
)


def internal_value_mentions(answer: str, question: str = "") -> list[str]:
    """비노출 정책을 답변 쪽에서 한 번 더 본다.

    Context 에서 값을 지우는 것이 1차 방어다. 이건 2차 — 값 없이 표현만 끌어다
    쓰는 경우를 잡는다.

    사용자가 먼저 쓴 단어는 **없다고 답할 때만** 뺀다.

        "신뢰도가 높아?" → "신뢰도 정보는 제공되지 않았습니다"   경고 없음
        "얼마나 확신해?" → "확신하고 있다고 볼 수 있습니다"      경고 있음

    둘 다 사용자의 단어를 되받지만 뜻이 정반대다. 앞은 정확히 원하는 동작이고,
    뒤는 **값 없이 내부 진단값을 단정**한 것이다 — 숫자를 안 썼으니 수치 검사에도
    안 걸린다. 되받았다는 이유로 둘 다 빼면 뒤쪽이 통째로 새어 나간다.
    """
    found = {match.group(0).strip() for match in _INTERNAL_MENTION.finditer(answer or "")}
    if not found:
        return []
    asked = {match.group(0).strip()
             for match in _INTERNAL_MENTION.finditer(question or "")}
    if asked and _DENIAL.search(answer or ""):
        found -= asked
    return sorted(found)


# 사용자에게 보일 이유가 없는 내부 이름. 답변 본문은 업무 언어로 쓰기로 했다 —
# 사용자는 CPL·FIT·SIM 이 무엇인지 모른다.
# 정규식 순서가 규칙의 일부다. `FIT Agent` 를 `FIT` 로만 잡으면 경고 문구가
# 무엇이 문제인지 덜 말해 준다. 긴 것을 먼저 둔다.
_INTERNAL_NAME = re.compile(
    r"\b[A-Za-z]+\s*Agent\b|에이전트"
    r"|\bModel\s*[123]\b|모델\s*[123]"
    r"|\bCPL\b|\bFIT\b|\bSIM\b|\bDIF\b"
)


def internal_name_mentions(answer: str, question: str = "") -> list[str]:
    """답변이 내부 이름을 꺼냈는가. **사용자가 먼저 쓴 이름은 빼고** 본다.

    "CPL 결과 보여줘" 라고 물으면 답변이 CPL 이라고 되받는 것이 자연스럽다.
    그 경우까지 경고하면 개발·회귀 질문마다 경고가 뜬다. 사용자가 쓰지 않은
    이름만 남긴다.
    """
    asked = {match.group(0).strip().upper()
             for match in _INTERNAL_NAME.finditer(question or "")}
    found = {match.group(0).strip() for match in _INTERNAL_NAME.finditer(answer or "")}
    return sorted(name for name in found if name.upper() not in asked)


def _model(context: dict[str, Any], key: str) -> dict[str, Any]:
    section = ((context.get("report") or {}).get(key)) or {}
    return section if isinstance(section, dict) else {}


def model3_claims(answer: str, context: dict[str, Any]) -> list[str]:
    """축별 기여도 없는 원인 단정 · null 인데 수준 단정 · 비교군 오인."""
    section = _model(context, "model3")
    result = section.get("result") or {}
    warnings: list[str] = []

    has_contribution = any(
        key in result
        for key in ("axis_contribution", "contributions", "shap", "feature_importance")
    )
    if not has_contribution:
        causes = [match.group(0) for match in _CAUSE.finditer(answer or "")]
        if causes:
            warnings.append("축별 기여도 없이 원인을 단정: %s" % ", ".join(causes))

    if "anomaly_level" in result and result.get("anomaly_level") is None:
        if re.search(r"(높은|낮은|중간)\s*수준(입니다|이다)", answer or ""):
            warnings.append("anomaly_level 이 null 인데 수준을 단정")

    level = str((result.get("reference") or {}).get("cohort_level") or "")
    if level and any(hint in level for hint in _M3_FALLBACK_HINTS):
        claims = [match.group(0).strip() for match in _TYPE_COHORT.finditer(answer or "")]
        if claims:
            warnings.append(
                "전체 비교군(%s)인데 특정 유형 비교군처럼 서술: %s"
                % (level, ", ".join(claims))
            )
    return warnings


# 경고 종류. 로그에서 바로 골라낼 수 있게 문구 앞에 붙인다 — 비노출 정책
# 위반이 반복되는지 보려면 문구를 정규식으로 훑는 것이 아니라 이 이름으로
# 세어야 한다.
GROUNDING_CATEGORIES: tuple[str, ...] = (
    "unsupported_numeric_claim",   # Context 에 없는 수치
    "unsupported_causal_claim",    # 근거 없는 원인·수준 단정
    "internal_value_mentions",     # 백분위·신뢰도 같은 내부 진단값
    "internal_name_mentions",      # CPL·FIT 같은 내부 이름
    "link_mentions",               # 답변 본문의 링크
)


def check_grounding(
    answer: str, context: dict[str, Any], question: str | None = None
) -> list[str]:
    """답변 하나에 대한 경고 목록. 비어 있으면 걸린 것이 없다.

    각 항목은 `"<category>: <설명>"` 이다. category 는 `GROUNDING_CATEGORIES`
    중 하나다.

    `question` 을 주면 내부 이름 노출을 함께 본다 — 사용자가 먼저 쓴 이름은
    경고하지 않기 위해 필요하다. Context 안에 질문이 들어 있으면 그것을 쓴다.
    """
    warnings: list[str] = []
    if question is None:
        question = str(context.get("question") or "")

    bad = ungrounded_numbers(answer, context)
    if bad:
        warnings.append(
            "unsupported_numeric_claim: context 에 없는 수치 — %s" % ", ".join(bad)
        )
    links = link_mentions(answer)
    if links:
        warnings.append(
            "link_mentions: 답변 본문에 링크를 붙였다(근거는 references 로만"
            " 가리킨다) — %s" % ", ".join(links)
        )
    internal = internal_value_mentions(answer, question)
    if internal:
        warnings.append(
            "internal_value_mentions: 사용자에게 노출하면 안 되는 내부 진단값 —"
            " %s" % ", ".join(internal)
        )
    names = internal_name_mentions(answer, question)
    if names:
        warnings.append(
            "internal_name_mentions: 사용자가 묻지 않은 내부 이름을 답변에 썼다"
            "(업무 언어로 쓴다) — %s" % ", ".join(names)
        )
    warnings.extend(
        "unsupported_causal_claim: %s" % claim
        for claim in model3_claims(answer, context)
    )
    return warnings


def warning_category(warning: str) -> str:
    """경고 문구 하나에서 종류만 떼어 낸다. 집계할 때 쓴다."""
    head = warning.split(":", 1)[0].strip()
    return head if head in GROUNDING_CATEGORIES else "unknown"
