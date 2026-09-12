from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import BaseModel

from worker.chat import (
    ChatAnswer,
    ChatIntent,
    ChatReference,
    ChatResultMissingError,
    ResultGroundedChatHandler,
    build_chat_context,
    classify_intent,
)
from worker.runtime import ClaimedJob


class _FakeLLM:
    def __init__(self, answer: ChatAnswer) -> None:
        self.answer = answer
        self.calls: list[dict[str, object]] = []

    async def generate_structured(self, **kwargs: object) -> BaseModel:
        self.calls.append(kwargs)
        return self.answer


def _job(**payload: object) -> ClaimedJob:
    return ClaimedJob(job_pk=uuid4(), processing_run_pk=uuid4(), payload=payload)


def _report() -> dict[str, object]:
    return {
        "case": {"program_name": "스마트공장"},
        "ml": {
            "model_1": {
                "status": "OK",
                "support_type": "연구개발",
                "message": "지원유형을 확인했습니다.",
                "reason_code": None,
            },
            "model_2": {
                "status": "OK",
                "predicted_amount_won": 599920000,
                "message": "예측 지원금액입니다.",
                "reason_code": None,
            },
            "model_3": {
                "status": "OK",
                "anomaly_level": "HIGH",
                "cause_axes": [],
                "message": "이례성이 높습니다.",
                "reason_code": None,
            },
        },
        "evidences": [
            {"evidence_id": "e-1", "field_name": "support", "excerpt": "연구개발"}
        ],
        "internal": {"confidence": 0.99},
    }


def test_classify_intent_routes_model_questions_and_unknown_without_llm() -> None:
    assert classify_intent("지원금액이 얼마야?") is ChatIntent.MODEL_2
    assert classify_intent("지원유형이 뭐야?") is ChatIntent.MODEL_1
    assert classify_intent("이례적인 부분이 있어?") is ChatIntent.MODEL_3
    assert classify_intent("안녕, 뭘 물어볼 수 있어?") is ChatIntent.UNKNOWN
    assert classify_intent("지원 대상과 사업 기간을 알려줘") is ChatIntent.DOCUMENT
    assert classify_intent("모델 1과 모델 2 결과를 같이 설명해줘") is ChatIntent.REPORT


def test_unrecognised_scope_falls_back_to_report_not_unknown() -> None:
    """범위 어휘가 없는 정상 질문은 전체 컨텍스트로 LLM 에 가야 한다.

    좁히기 실패를 거절로 바꾸면 안 된다. "신청할 수 있어?" 가 도움말 키워드
    "할 수 있어" 에 걸려 안내문으로 막히던 것이 이 회귀의 계기다.
    """

    for question in (
        "어떤 유형의 기업이 신청할 수 있어?",
        "우리 회사가 이 사업에 적합해?",
        "경쟁률 어때?",
        "보완할 점 알려줘",
        "이 사업 기능은 뭐야?",
    ):
        assert classify_intent(question) is not ChatIntent.UNKNOWN, question


def test_unknown_question_is_completed_without_llm_call() -> None:
    llm = _FakeLLM(
        ChatAnswer(
            content="불필요",
            intent=ChatIntent.REPORT,
            warnings=[],
            references=[],
        )
    )
    result = ResultGroundedChatHandler(llm).handle(_job(question="안녕하세요"))
    assert result["intent"] == "UNKNOWN"
    assert "지원유형" in result["content"]
    assert llm.calls == []


def test_missing_result_is_a_domain_failure() -> None:
    llm = _FakeLLM(
        ChatAnswer(
            content="불필요",
            intent=ChatIntent.MODEL_2,
            warnings=[],
            references=[],
        )
    )
    with pytest.raises(ChatResultMissingError):
        ResultGroundedChatHandler(llm).handle(_job(question="예측금액 알려줘"))
    assert llm.calls == []


def test_handler_builds_current_ml_context_and_adds_grounding_warnings() -> None:
    llm = _FakeLLM(
        ChatAnswer(
            content="예측 지원금액은 599,920,000원이고 신뢰도는 높습니다.",
            intent=ChatIntent.MODEL_2,
            warnings=[],
            references=[
                ChatReference(section="ml", label="model_2", evidence_id=None),
                ChatReference(section="ml", label="bad", evidence_id="missing"),
            ],
        )
    )
    result = ResultGroundedChatHandler(llm).handle(
        _job(question="예측금액 알려줘", report=_report())
    )
    assert result["intent"] == "MODEL_2"
    assert "internal_value_mentions" in " ".join(result["warnings"])
    assert "invalid_reference" in " ".join(result["warnings"])
    sent = json.loads(llm.calls[0]["messages"][1].content)  # type: ignore[index]
    assert isinstance(sent, dict)
    assert sent["result"]["ml"] == {"model_2": {  # type: ignore[index]
        "status": "OK",
        "predicted_amount_won": 599920000,
        "message": "예측 지원금액입니다.",
        "reason_code": None,
    }}
    assert "confidence" not in str(sent)


def test_model3_empty_cause_axes_warns_on_causal_claim() -> None:
    llm = _FakeLLM(
        ChatAnswer(
            content="주요 원인은 지원금액입니다.",
            intent=ChatIntent.MODEL_3,
            warnings=[],
            references=[],
        )
    )
    result = ResultGroundedChatHandler(llm).handle(
        _job(question="이례성의 원인은 뭐야?", report=_report())
    )
    assert any(item.startswith("unsupported_causal_claim") for item in result["warnings"])


def test_context_has_evidence_and_previous_conversation_shape() -> None:
    context = build_chat_context(
        _report(),
        "결과를 설명해줘",
        intent=ChatIntent.REPORT,
        conversation=[{"role": "USER", "content": "앞 질문"}],
    )
    assert context["evidence"][0]["evidence_id"] == "e-1"
    assert context["conversation"] == [{"role": "user", "content": "앞 질문"}]


def test_document_context_is_cpl_and_evidence_focused() -> None:
    context = build_chat_context(
        _report(),
        "지원 대상이 어떻게 돼?",
        intent=ChatIntent.DOCUMENT,
    )
    assert context["result"].keys() == {"case", "cpl", "evidence"}
    assert context["result"]["evidence"][0]["evidence_id"] == "e-1"
