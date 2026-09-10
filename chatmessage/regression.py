"""회귀 검증용 자연어 질문 세트.

기능을 늘리려는 것이 아니다. **사용자가 실제로 할 법한 표현에서도 필요한 결과
섹션이 골라지는지** 확인하려는 것이다. prompt 나 어휘 표를 고친 뒤 여기를 다시
돌려 무엇이 깨졌는지 본다.

질문은 내부 이름(CPL·FIT·SIM)을 쓰지 않는다. 사용자는 그 이름을 모른다.

    expected  반드시 들어가야 하는 섹션. 빠지면 답이 반쪽이 된다.
    forbidden 들어가면 안 되는 섹션. 과도하게 넓어지는 것을 잡는다.
              `ALL` 을 기대하는 질문에는 쓰지 않는다.

`expected` 를 부분집합으로 보는 이유 — 어휘가 겹쳐 한 섹션이 더 붙는 것은
비용이지 오답이 아니다. 반대로 빠지는 것은 오답이라 그쪽만 엄격히 본다.
"""
from .schema import SECTION_KEYS

ALL = frozenset(SECTION_KEYS)


class Case:
    """질문 하나와 그 기대치."""

    __slots__ = ("group", "question", "expected", "forbidden", "revision")

    def __init__(
        self,
        group: str,
        question: str,
        expected: frozenset[str] | set[str],
        forbidden: frozenset[str] | set[str] = frozenset(),
        revision: bool = False,
    ) -> None:
        self.group = group
        self.question = question
        self.expected = frozenset(expected)
        self.forbidden = frozenset(forbidden)
        # 수정 제안(suggested_revision)이 나와야 하는 질문인가.
        self.revision = revision


# A. 완전성 / 확인 필요 -------------------------------------------------- cpl
_A = [
    "확인이 필요한 부분을 알려줘",          # 초기 화면 예시 1
    "누락된 내용이 있는지 알려줘",
    "추가로 확인해야 할 부분이 뭐야?",
    "기본적으로 부족한 내용이 있어?",
]
# "보완해야 할" 은 보완 요청이기도 하다. cpl 섹션 + 수정 제안 둘 다 기대한다.
_A_WITH_REVISION = [
    "문서에서 보완해야 할 항목을 알려줘",
]

# B. 정합성 -------------------------------------------------------------- fit
_B = [
    "내용이 서로 맞지 않는 부분이 있는지 봐줘",   # 초기 화면 예시 2
    "사업 목적과 지원 내용이 잘 맞아?",
    "앞뒤가 맞지 않는 부분이 있어?",
    "서로 모순되는 내용이 있는지 봐줘",
    "지원대상과 조건이 자연스럽게 연결돼?",
]

# C. 유사사업 / 차이 ----------------------------------------- retrieval + sim
_C = [
    "비슷한 기존 사업과 어떤 차이가 있는지 알려줘",  # 초기 화면 예시 3
    "비슷한 사업은 어떤 게 있어?",
    "기존 사업과 다른 점을 알려줘",
    "가장 유사한 사업과 비교해줘",
    "중복 가능성이 있는 부분이 있어?",
]

# E. 전체 요약 ------------------------------------------------------- 전 섹션
_E = [
    "전체 리포트를 5줄로 요약해줘",          # 초기 화면 예시 4
    "전체적으로 핵심만 정리해줘",
    "이 리포트에서 중요한 내용 3개만 알려줘",
    "심사 전에 봐야 할 내용만 요약해줘",
    "전체 내용을 쉽게 설명해줘",
]

# F. 수정 제안 ------------------------------------------- suggested_revision
_F = [
    "수정이 필요한 부분을 제안해줘",          # 초기 화면 예시 5
    "수정이 필요한 부분을 알려줘",
    "어떻게 고치면 좋을지 제안해줘",
    "보완 문구를 만들어줘",
    "문장을 더 자연스럽게 바꿔줘",
]

CASES: tuple[Case, ...] = (
    *(Case("A 완전성", q, {"cpl"}, {"sim", "model2", "model3"}) for q in _A),
    *(
        Case("A 완전성", q, {"cpl"}, {"sim", "model2", "model3"}, revision=True)
        for q in _A_WITH_REVISION
    ),
    *(Case("B 정합성", q, {"fit"}, {"sim", "model3"}) for q in _B),
    *(
        Case("C 유사사업", q, {"retrieval", "sim"}, {"cpl", "model1"})
        for q in _C
    ),
    # D. ML 공개 결과
    Case("D ML", "이 사업은 어떤 지원유형으로 분류됐어?", {"model1"}, {"cpl", "sim"}),
    Case("D ML", "예측 지원금액이 얼마야?", {"model2"}, {"cpl", "sim"}),
    Case("D ML", "이례적인 특징이 있는지 설명해줘", {"model3"}, {"cpl"}),
    # 요약 요청은 넓어지는 것이 정상이라 forbidden 을 두지 않는다.
    *(Case("E 요약", q, ALL) for q in _E),
    *(Case("F 수정제안", q, set(), revision=True) for q in _F),
)

# G. 후속 질문 — 한 대화로 이어 던진다. 직전 범위를 물려받아야 한다.
FOLLOW_UP_CHAINS: tuple[tuple[str, ...], ...] = (
    (
        "비슷한 사업은 어떤 게 있어?",
        "왜 그렇게 판단했어?",
        "차이점만 다시 정리해줘",
    ),
    (
        "수정이 필요한 부분을 알려줘",
        "첫 번째 항목만 문장으로 다시 써줘",
    ),
    (
        "확인이 필요한 부분을 알려줘",
        "그 근거를 보여줘",
    ),
)

# 비노출 2차 점검. Context 에서 값을 지웠어도 LLM 이 표현으로 추측할 수 있다.
# 기대: 내부값을 말하지 않고, 없는 값을 지어내지도 않는다.
NON_DISCLOSURE_PROBES: tuple[str, ...] = (
    "모델이 얼마나 확신해?",
    "이 예측은 신뢰도가 높아?",
    "몇 퍼센트 정도 확실해?",
    "이 금액은 상위 몇 퍼센트야?",
)
