"""원문의 정량 표현 하나하나에 비교 맥락을 붙인다.

FIT-7 은 같은 축의 값 집합을 비교한다. 그런데 축만 같고 맥락이 다른 값이
한 집합에 들어가면 양립 가능한 수치가 모순이 된다. 실문서에서 관측한 것:

    - 지원기업수: 120개사 (연 40개사)      총량과 연간이 ``COUNT:개사`` 하나로
    - 기업당 한도: 최대 5,000만원          기업당 한도와 묶음 총액이 ``AMOUNT_KRW`` 하나로
    - 총 지원규모: 6,000백만원

그래서 값이 아니라 **숫자 구간마다** 맥락을 붙인다. ``최대 5,000만원 (단가
5,000만원)`` 은 값 하나지만 성격이 다른 숫자 둘이다.

**수식어 전파를 좁게 잡는다.** 수식어는 숫자에 **직접 붙어 있을 때만** 그
숫자를 수식한다. 사이에 설명어가 끼면 끊는다::

    '기업당 5,000만원 지원 후 총 60억원 집행'
      5,000만원 -> 기업당 (직접 붙음)
      60억원    -> 미확정 ('지원 후 총' 이 인식 어휘가 아니다)

넓게 잡으면 병렬 나열(``연간 신규 20개사·계속 30개사``)까지 덮을 수 있지만, 모르는
전환 표현을 만났을 때 **틀린 맥락을 붙인다.** 덜 붙이는 쪽이 안전하다 — 미확정은
비교 보류로 끝나지만 틀린 맥락은 거짓 판정을 만든다.

아래 ``_MODIFIERS`` 는 **인식 범위의 선언**이지 문서를 해석하는 완전한 어휘가
아니다. 모르는 표현은 미확정으로 남는다.

이번 범위에서 뺀 것: 비율(``%``), 범위 표현(``9~15억원``), 복합 단위
(``1억 5000만원``, ``3천만원``), 표 머리글 상속. 각각 비교 의미를 따로 정해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re


# 단위는 긴 것부터 본다. '백만' 을 '만' 보다 뒤에 두면 6,000백만원 이
# 6,000만원 으로 읽힌다.
_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(백만|조|억|만|천)?\s*원")
_COUNT_RE = re.compile(r"(\d[\d,]*)\s*개?\s*(팀|명|개사|건|개)")
_TIMES_RE = re.compile(r"([월주년일])?\s*(\d[\d,]*)\s*회")
_DIGITS_RE = re.compile(r"\d+")
# 행정 예산 문서는 백만원을 표준 단위로 쓴다 (총사업비 6,600백만원).
_AMOUNT_SCALES = {"조": 10**12, "억": 10**8, "백만": 10**6, "만": 10**4, "천": 10**3}
_GROUPED_NUMBER_RE = re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^\d+(?:\.\d+)?$")

_MODIFIERS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("period", "연간", re.compile(r"연\s*간|매년|연차별|연(?=\s*\d)")),
    ("period", "전체기간", re.compile(r"전체\s*기간|다년도\s*총")),
    ("scope", "기업당", re.compile(r"기업\s*당|업체\s*당")),
    ("scope", "과제당", re.compile(r"과제\s*당|건\s*당|팀\s*당")),
    ("scope", "묶음전체", re.compile(r"총\s*지원\s*규모|총\s*사업비|총액")),
    # '지원한도' 처럼 접두어가 붙은 형태를 통째로 인식한다. '한도' 만 잡으면
    # 앞의 '지원' 이 수식어 사슬을 끊어 '기업당 지원한도 5,000만원' 에서
    # 대상 기준을 잃는다.
    ("nature", "한도", re.compile(r"(?:지원\s*)?(?:최대|한도|상한)|이내")),
    ("nature", "단가", re.compile(r"단가")),
)
DIMENSIONS = ("period", "scope", "nature")

# 절 경계. 천 단위 쉼표는 구분자가 아니다 — '5,000만원' 이 잘린다.
_BOUNDARY_RE = re.compile(r"(?<!\d),(?!\d)|[，;/\n·()（）]|(?:^|\s)-\s|[○◦□■●▪‣]")
# 수식어와 숫자 사이에 있어도 연결이 끊기지 않는 문자.
_GLUE_RE = re.compile(r"^[\s:：]*$")


@dataclass(frozen=True, slots=True)
class QuantityModifier:
    """숫자에 맥락을 준 원문 조각. 어디서 왔는지 남긴다."""

    dimension: str
    label: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class QuantitySpan:
    """원문의 정량 표현 하나와 그 비교 맥락.

    좌표는 이 표현을 읽은 텍스트 기준이다. 값·근거를 새로 만들지 않는다.
    """

    axis: str
    value: int
    start: int
    end: int
    modifiers: tuple[QuantityModifier, ...] = ()

    def dimension(self, name: str) -> str | None:
        """그 차원의 확정값. 미확정이거나 충돌이면 None."""

        labels = {row.label for row in self.modifiers if row.dimension == name}
        return labels.pop() if len(labels) == 1 else None

    def context(self, dimensions: tuple[str, ...]) -> tuple[str, ...] | None:
        """필요한 차원이 모두 확정됐을 때만 맥락 키를 준다.

        하나라도 미확정이면 None 이고, 호출자는 그 값을 비교에서 보류한다.
        표현이 없다는 이유로 기본값을 채우지 않는다 — 그러면 서로 다른 수량이
        같은 자리에서 비교된다.
        """

        key: list[str] = []
        for name in dimensions:
            value = self.dimension(name)
            if value is None:
                return None
            key.append(value)
        return tuple(key)


def _plain_number(literal: str) -> str | None:
    if not _GROUPED_NUMBER_RE.match(literal):
        return None
    return literal.replace(",", "")


def _numbers(text: str) -> list[QuantitySpan]:
    out: list[QuantitySpan] = []
    for match in _AMOUNT_RE.finditer(text):
        literal = _plain_number(match.group(1))
        if literal is None:
            return []
        won = Decimal(literal) * _AMOUNT_SCALES.get(match.group(2), 1)
        if won != won.to_integral_value():
            return []
        out.append(QuantitySpan("AMOUNT_KRW", int(won), *match.span()))
    for match in _COUNT_RE.finditer(text):
        literal = _plain_number(match.group(1))
        if literal is None:
            return []
        out.append(QuantitySpan(f"COUNT:{match.group(2)}", int(literal), *match.span()))
    for match in _TIMES_RE.finditer(text):
        literal = _plain_number(match.group(2))
        if literal is None:
            return []
        # 기간 표현이 없는 횟수를 총횟수로 단정하지 않는다. 명시된 '총 8회' 와
        # 그냥 '8회' 는 같은 수량이라는 근거가 없다.
        axis = f"TIMES:{match.group(1)}" if match.group(1) else "TIMES:UNQUALIFIED"
        out.append(QuantitySpan(axis, int(literal), *match.span()))
    return sorted(out, key=lambda row: row.start)


def _attach(text: str, spans: list[QuantitySpan]) -> list[QuantitySpan]:
    """숫자에 직접 붙은 수식어만 단다.

    수식어와 숫자 사이에 공백·콜론과 다른 수식어만 있을 때 연결로 본다.
    ``기업당 한도: 최대 5,000만원`` 은 셋이 사슬로 이어져 모두 붙고,
    ``기업당 5,000만원 지원 후 총 60억원`` 의 60억원에는 아무것도 붙지 않는다.
    """

    found: list[QuantityModifier] = []
    for dimension, label, pattern in _MODIFIERS:
        for match in pattern.finditer(text):
            found.append(QuantityModifier(dimension, label, *match.span()))
    found.sort(key=lambda row: row.start)

    out: list[QuantitySpan] = []
    for index, span in enumerate(spans):
        floor = spans[index - 1].end if index else 0
        for boundary in _BOUNDARY_RE.finditer(text, floor, span.start):
            floor = max(floor, boundary.end())
        chain: list[QuantityModifier] = []
        cursor = span.start
        for modifier in reversed(found):
            if modifier.end > cursor or modifier.start < floor:
                continue
            if not _GLUE_RE.match(text[modifier.end : cursor]):
                break
            chain.append(modifier)
            cursor = modifier.start
        out.append(
            QuantitySpan(
                span.axis, span.value, span.start, span.end, tuple(reversed(chain))
            )
        )
    return out


def read_quantities(text: str | None) -> tuple[QuantitySpan, ...]:
    """원문에서 정량 표현과 맥락을 읽는다. 읽지 못하면 빈 튜플이다.

    **숫자 시퀀스 소비 규칙**은 기존 계약 그대로 값 전체 단위다. 숫자 하나라도
    어떤 매치에도 안 걸리면 이 값을 통째로 버린다. 일부만 읽으면 서로 다른
    값이 일치로 판정된다 (``9~15억원`` 에서 ``15억원`` 만, ``1억 5000만원``
    에서 ``5000만원`` 만).
    """

    if not text:
        return ()
    spans = _numbers(text)
    covered = [(row.start, row.end) for row in spans]
    for digits in _DIGITS_RE.finditer(text):
        if not any(
            start <= digits.start() and digits.end() <= end for start, end in covered
        ):
            return ()
    return tuple(_attach(text, spans))


# 축마다 (확정돼야 하는 차원, 키에 함께 넣는 차원).
#
# **확정 필요**: 미확정이면 그 값은 비교에서 보류한다.
# **키에 포함**: 미확정이어도 키에 그대로 넣는다. 한쪽만 명시된 경우
# ``연간`` 과 ``None`` 이 다른 키가 되어 같은 자리에서 비교되지 않는다.
#
# 금액에서 기간을 '확정 필요' 로 두면 ``기업당 지원한도`` 처럼 기간과 무관하게
# 대응하는 비교까지 막힌다. 그렇다고 무시하면 ``기업당 연간 한도 5,000만원``
# 과 ``기업당 전체기간 한도 7,000만원`` 이 한 집합에 들어가 거짓 불일치가 된다.
# 키에 넣으면 둘 다 해결된다 — 양쪽이 같은 상태일 때만 같은 자리다.
_COMPARISON_DIMENSIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "AMOUNT_KRW": (("scope", "nature"), ("period",)),
}


def comparison_dimensions(axis: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """그 축의 (확정 필요 차원, 키에 함께 넣는 차원)."""

    if axis.startswith("COUNT:"):
        # 기간이 총량과 연간을 가르고, 대상 기준이 어느 집합을 세는지를 가른다.
        # 신규 선정기업과 누적 수혜기업은 단위·기간이 같아도 다른 수량이다.
        # 지금 인식 어휘에는 그 구분을 주는 표현이 없으므로 개수는 사실상 늘
        # 보류된다 — 비교가 성립하지 않는다는 사실을 그대로 두는 쪽이 맞다.
        return ("period", "scope"), ()
    if axis == "TIMES:UNQUALIFIED":
        # 기간 표현이 없는 횟수다. '8회' 와 '10회' 가 같은 기준이라는 근거가
        # 없으므로 확정할 수 없는 차원을 요구해 비교를 보류시킨다.
        return ("period",), ()
    if axis.startswith("TIMES:"):
        return (), ()
    return _COMPARISON_DIMENSIONS.get(axis, (DIMENSIONS, ()))


def comparison_key(span: "QuantitySpan") -> tuple[object, ...] | None:
    """비교 집합 키. 확정이 부족하면 None 이고 호출자가 보류한다."""

    required, keyed = comparison_dimensions(span.axis)
    confirmed = span.context(required)
    if confirmed is None:
        return None
    return (*confirmed, *(span.dimension(name) for name in keyed))
