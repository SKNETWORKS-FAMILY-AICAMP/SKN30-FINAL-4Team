"""챗봇 진입점 — 요청 하나를 응답 하나로.

    질문 → Intent 분류 → Context 조회 → LLM → 응답

각 단계의 실패를 서로 다른 status 로 구분한다. 전부 `failed` 로 뭉개면 프런트가
"질문을 바꿔 보라" 와 "분석이 아직 안 끝났다" 를 같은 문구로 안내하게 된다.
"""
import uuid
from typing import Any, Dict, List

from .responder import answer_with_checks
from .router import classify_intent
from .schema import (ChatAnswer, ChatError, ChatEvidence, ChatIntent,
                     ChatRequest, ChatResponse)

UNSUPPORTED = ("UNSUPPORTED_QUESTION",
               "현재 분석 결과에서 지원하지 않는 질문입니다.")
NOT_AVAILABLE = ("ANALYSIS_RESULT_NOT_AVAILABLE",
                 "해당 분석 결과를 현재 사용할 수 없습니다.")
INSUFFICIENT = ("ANALYSIS_RESULT_INSUFFICIENT",
                "분석은 되었지만 답변에 필요한 근거가 부족합니다.")
LLM_FAILED = ("CHAT_LLM_FAILED", "답변 생성에 실패했습니다.")


def _message_id() -> str:
    return "msg_%s" % uuid.uuid4().hex[:12]


def _fail(request: ChatRequest, intent: str, status: str,
          err: tuple) -> ChatResponse:
    code, message = err
    return ChatResponse(
        conversation_id=request.conversation_id,
        analysis_id=request.analysis_id,
        message_id=_message_id(),
        intent=ChatIntent(type=intent),
        status=status,
        answer=None,
        error=ChatError(code=code, message=message),
    )


def _evidence(raw: List[Dict[str, Any]]) -> List[ChatEvidence]:
    """저장된 근거 dict → 응답 모델. 모양이 어긋난 항목은 조용히 버리지 않고
    source 만 채워 넘긴다 — 근거가 사라지는 것이 가장 나쁘다."""
    out = []
    for e in raw or []:
        if isinstance(e, ChatEvidence):
            out.append(e)
        elif isinstance(e, dict):
            out.append(ChatEvidence(
                source=str(e.get("source") or "unknown"),
                field=e.get("field"),
                value=e.get("value"),
                evidence_id=e.get("evidence_id"),
                metadata=e.get("metadata"),
            ))
    return out


def handle_chat(request: ChatRequest, context_loader,
                llm_client) -> ChatResponse:
    intent = classify_intent(request.message)

    if intent == "UNKNOWN":
        return _fail(request, "UNKNOWN", "not_available", UNSUPPORTED)

    context = context_loader.get_context(
        analysis_id=request.analysis_id,
        intent=intent,
        question=request.message,
    )

    status = context.get("status")
    if status != "success":
        if status == "insufficient_data":
            return _fail(request, intent, "insufficient_data", INSUFFICIENT)
        return _fail(request, intent, "not_available", NOT_AVAILABLE)

    try:
        answered = answer_with_checks(
            llm_client=llm_client,
            question=request.message,
            intent=intent,
            context=context["data"],
        )
    except Exception:                                       # noqa: BLE001
        # LLM 장애는 분석 결과 부재와 다르다. 결과는 있으니 재시도가 의미 있다.
        return _fail(request, intent, "failed", LLM_FAILED)

    return ChatResponse(
        conversation_id=request.conversation_id,
        analysis_id=request.analysis_id,
        message_id=_message_id(),
        intent=ChatIntent(type=intent),
        status="success",
        answer=ChatAnswer(text=answered["text"],
                          evidence=_evidence(context.get("evidence"))),
        error=None,
        warnings=answered["warnings"],
    )
