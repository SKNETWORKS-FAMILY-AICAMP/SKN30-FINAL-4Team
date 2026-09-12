"""Deterministic question routing for the single result-grounded chat LLM."""

from __future__ import annotations

import re

from .contracts import ChatIntent


_MODEL_1 = (
    "지원유형",
    "지원 유형",
    "지원성격",
    "지원 성격",
    "분류",
    "유형",
    "모델 1",
    "model 1",
    "model1",
)
_MODEL_2 = (
    "예측금액",
    "예측 금액",
    "지원금액",
    "지원 금액",
    "지원규모",
    "지원 규모",
    "금액",
    "한도",
    "예산",
    "얼마",
    "모델 2",
    "model 2",
    "model2",
)
_MODEL_3 = (
    "이례",
    "특이",
    "이상치",
    "비정형",
    "희귀",
    "anomaly",
    "모델 3",
    "model 3",
    "model3",
)
_DOCUMENT = (
    "지원대상",
    "지원 대상",
    "지원조건",
    "지원 조건",
    "신청자격",
    "신청 자격",
    "자격요건",
    "자격 요건",
    "사업목적",
    "사업 목적",
    "지원내용",
    "지원 내용",
    "수행기관",
    "수행 기관",
    "사업기간",
    "사업 기간",
    "추진기간",
    "추진 기간",
)
# 인사·도움말만 남긴다. "할 수 있어"·"기능"·"도와줘" 는 빼야 한다 —
# "신청할 수 있어?", "이 사업 기능은?", "보완 좀 도와줘" 처럼 정상 질문에
# 부분문자열로 걸려서 LLM 까지 가지도 못하고 안내문으로 막혔다.
_UNKNOWN = (
    "안녕",
    "반가워",
    "무엇을 물어",
    "뭘 물어",
    "도움말",
)
_WHITESPACE = re.compile(r"\s+")


def _normalize(question: str) -> str:
    return _WHITESPACE.sub(" ", question.strip().lower())


def classify_intent(question: str) -> ChatIntent:
    """Return one supported scope without consulting the LLM.

    A question containing more than one model scope is a report question: the
    answer must be allowed to compare the stored model outputs together.

    **범위를 못 알아들으면 REPORT 다 — UNKNOWN 이 아니다.** 이 분류기는 컨텍스트를
    좁히는 최적화이지 질문을 거르는 관문이 아니다. 키워드가 안 걸렸다는 것은
    "우리 회사가 적합해?" 처럼 범위 어휘 없이 물었다는 뜻이지 답할 수 없다는
    뜻이 아니다. 못 좁히면 전부 싣고 LLM 에게 맡긴다.
    """

    if not isinstance(question, str) or not question.strip():
        return ChatIntent.UNKNOWN
    text = _normalize(question)
    if any(keyword in text for keyword in _UNKNOWN):
        return ChatIntent.UNKNOWN

    # DOCUMENT is intentionally checked before REPORT.  Natural questions
    # about a request form contain broad words such as "내용" or "확인" and
    # must still receive the CPL/evidence projection.
    if any(keyword in text for keyword in _DOCUMENT):
        return ChatIntent.DOCUMENT

    hits = [
        ChatIntent.MODEL_1 if any(keyword in text for keyword in _MODEL_1) else None,
        ChatIntent.MODEL_2 if any(keyword in text for keyword in _MODEL_2) else None,
        ChatIntent.MODEL_3 if any(keyword in text for keyword in _MODEL_3) else None,
    ]
    model_hits = [item for item in hits if item is not None]
    if len(model_hits) == 1:
        return model_hits[0]
    return ChatIntent.REPORT


__all__ = ["classify_intent"]
