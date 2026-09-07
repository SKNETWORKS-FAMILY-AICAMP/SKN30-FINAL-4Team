"""질문 → Intent 분류. **분류만 한다** — 답변은 만들지 않는다.

MVP 는 키워드 규칙이다. 순서가 규칙의 일부다.

    REPORT > MODEL_3 > MODEL_2 > MODEL_1 > DOCUMENT > UNKNOWN

왜 이 순서인가 — 좁은 의미가 넓은 의미를 이겨야 한다. "비교군 대비 이례적이야?"
에는 `비교군`(MODEL_2 어휘)과 `이례`(MODEL_3 어휘)가 함께 있는데, 묻고 있는 것은
이례성이다. MODEL_3 을 먼저 보면 그대로 해결된다. 반대로 두면 금액 질문으로
잘못 간다.

같은 이유로 DOCUMENT 를 맨 뒤에 둔다. `지원기간`·`지원대상` 은 원문 질문이지만
`지원규모` 처럼 모델 질문과 글자가 겹치기 쉬워, 모델 의도를 먼저 걸러낸 뒤 남은
것을 원문 질문으로 본다.
"""
import re

from .schema import IntentType

# 종합/요약 — 특정 모델 하나가 아니라 전체를 묻는다.
REPORT_KEYWORDS = (
    "종합", "전체 결과", "전체적으로", "전반적", "요약", "정리해",
    "분석 결과 전체", "총평", "한눈에",
)

# 설계 이례성. `이례`·`특이` 가 들어오면 금액 어휘가 같이 있어도 이쪽이다.
MODEL_3_KEYWORDS = (
    "이례", "특이", "이상치", "튀는", "anomaly", "dif",
    "비정형", "드문", "희귀",
)

# 지원규모 회귀 + 비교군 백분위.
# `얼마` 는 넣지 않는다. "얼마나 믿을 수 있어"(MODEL_1)와 "지원기간은
# 얼마야"(DOCUMENT)까지 끌어와 금액 질문으로 만든다. 금액을 묻는 문장은
# 아래 어휘 중 하나를 거의 항상 함께 쓴다.
MODEL_2_KEYWORDS = (
    "지원규모", "지원 규모", "지원금액", "지원 금액", "금액", "한도",
    "기업당", "과제당", "백분위", "percentile", "비교군",
    "높은 편", "낮은 편", "어느 정도 수준", "수준이야", "예측 금액",
    "예상한",
)

# 지원성격 분류와 그 신뢰도.
MODEL_1_KEYWORDS = (
    "지원성격", "지원 성격", "지원유형", "지원 유형", "분류", "유형",
    "신뢰도", "믿을", "믿어도", "confidence", "trust",
)

# 원문(파서/CPL)에서 답해야 하는 것.
DOCUMENT_KEYWORDS = (
    "지원대상", "지원 대상", "대상은", "지원기간", "지원 기간", "사업기간",
    "사업 기간", "지원내용", "지원 내용", "사업목적", "목적", "수행기관",
    "전담기관", "추진체계", "근거 법령", "법적 근거", "예산",
)

_RULES: tuple = (
    ("REPORT", REPORT_KEYWORDS),
    ("MODEL_3", MODEL_3_KEYWORDS),
    ("MODEL_2", MODEL_2_KEYWORDS),
    ("MODEL_1", MODEL_1_KEYWORDS),
    ("DOCUMENT", DOCUMENT_KEYWORDS),
)

_WS = re.compile(r"\s+")


def _normalize(message: str) -> str:
    """공백만 정리한다. 조사·어미는 건드리지 않는다 — 규칙이 부분일치라 필요 없다."""
    return _WS.sub(" ", (message or "").strip().lower())


def classify_intent(message: str) -> IntentType:
    """질문 하나 → Intent 하나. 어디에도 안 걸리면 UNKNOWN."""
    text = _normalize(message)
    if not text:
        return "UNKNOWN"
    for intent, keywords in _RULES:
        if any(k in text for k in keywords):
            return intent
    return "UNKNOWN"


def explain(message: str) -> dict:
    """어떤 규칙에 걸렸는지 — 오분류를 디버깅할 때 쓴다."""
    text = _normalize(message)
    for intent, keywords in _RULES:
        hits = [k for k in keywords if k in text]
        if hits:
            return {"intent": intent, "matched": hits}
    return {"intent": "UNKNOWN", "matched": []}
