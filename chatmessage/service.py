"""챗봇 진입점 — 질문 접수와 답변 생성을 **두 단계로** 나눈다.

DB 가 그렇게 설계돼 있다. assistant 행을 `generating` 으로 먼저 만들고 워커가
채운다. 그래서 요청 하나를 응답 하나로 바꾸는 동기 함수를 두지 않는다.

    1단계  create_turn()    질문 접수 — 행 둘 + 큐 작업을 만들어 돌려준다
           (edge-conversation-create-message 가 한 트랜잭션으로 저장)

    2단계  complete_turn()  워커 — Intent 분류 · 근거 조회 · LLM · 변경분 반환
           (ops.processing_run 에서 집어 온 assistant_message 를 UPDATE)

    재시도 retry_turn()      실패한 assistant 행을 다시 generating 으로 돌린다

**저장은 하지 않는다.** 행 payload 만 만든다 — ml 쪽이 DB 커넥션을 들면 의존
방향이 거꾸로 선다. 트랜잭션 경계는 Edge Function 과 워커가 갖는다.
"""
from typing import Any, Dict, Optional

from .responder import answer_with_checks
from .router import classify_intent
from .schema import (ChatEvidence, ChatIntent, CompletedTurn, CreatedTurn,
                     MessageRow, QueueJob, RETRYABLE_CODES, SessionState,
                     TurnRequest)


class SessionNotUsableError(RuntimeError):
    """세션이 닫혔거나 만료됐거나, 분석 결과가 아직 준비되지 않았다."""


# ------------------------------------------------------------------ 1단계
def create_turn(request: TurnRequest, session: SessionState,
                next_sequence_no: int) -> CreatedTurn:
    """질문 접수 → 저장할 행 둘과 큐 작업 하나.

    next_sequence_no 는 이 세션의 다음 순번이다. 테이블에
    UNIQUE(analysis_session_pk, sequence_no) 가 걸려 있어 호출부가 현재 최대값을
    읽어 넘겨야 한다 — 여기서 세면 경쟁 조건이 생긴다.
    """
    if not session.usable:
        raise SessionNotUsableError(
            "세션 %s 사용 불가 (status=%s expired=%s case=%s)"
            % (session.analysis_session_id, session.status, session.expired,
               session.case_status))
    if session.analysis_case_id != request.analysis_case_id:
        raise SessionNotUsableError("세션과 분석 건이 다르다")

    user_msg = MessageRow(
        analysis_session_pk=request.analysis_session_id,
        role="user",
        sequence_no=next_sequence_no,
        content=request.content,
        message_status="completed",
    )
    assistant_msg = MessageRow(
        analysis_session_pk=request.analysis_session_id,
        role="assistant",
        sequence_no=next_sequence_no + 1,
        content=None,                      # 워커가 채운다
        message_status="generating",
        reply_to_message_pk=user_msg.message_pk,
    )
    user_msg.check_table_shape()
    assistant_msg.check_table_shape()

    return CreatedTurn(
        user_message=user_msg,
        assistant_message=assistant_msg,
        job=QueueJob(analysis_case_pk=request.analysis_case_id,
                     assistant_message_pk=assistant_msg.message_pk),
    )


# ------------------------------------------------------------------ 2단계
def _failed(message_pk: str, intent: str, code: str) -> CompletedTurn:
    return CompletedTurn(
        message_pk=message_pk,
        message_status="failed",
        content=None,
        error_code=code,
        retryable=code in RETRYABLE_CODES,
        intent=ChatIntent(type=intent),
    )


def complete_turn(assistant_message_pk: str, analysis_case_id: str,
                  question: str, context_loader, llm_client,
                  session: Optional[SessionState] = None) -> CompletedTurn:
    """워커가 부르는 함수. assistant 행에 적용할 변경분을 돌려준다.

    실패는 네 갈래로 구분한다. 전부 하나로 뭉개면 "질문을 바꿔 보라" 와
    "분석이 아직 안 끝났다" 와 "잠시 후 다시" 가 같은 문구로 안내된다.
    재시도가 의미 있는 것은 LLM 장애뿐이라 `retryable` 로 표시한다.
    """
    intent = classify_intent(question)

    if session is not None and not session.usable:
        return _failed(assistant_message_pk, intent, "SESSION_NOT_ACTIVE")

    if intent == "UNKNOWN":
        return _failed(assistant_message_pk, "UNKNOWN", "UNSUPPORTED_QUESTION")

    context: Dict[str, Any] = context_loader.get_context(
        analysis_case_id=analysis_case_id, intent=intent, question=question)

    status = context.get("status")
    if status != "success":
        code = ("ANALYSIS_RESULT_INSUFFICIENT" if status == "insufficient_data"
                else "ANALYSIS_RESULT_NOT_AVAILABLE")
        return _failed(assistant_message_pk, intent, code)

    try:
        answered = answer_with_checks(llm_client=llm_client, question=question,
                                      intent=intent, context=context["data"])
    except Exception:                                       # noqa: BLE001
        # 결과는 있는데 생성만 실패했다 — 재시도가 의미 있다.
        return _failed(assistant_message_pk, intent, "CHAT_LLM_FAILED")

    return CompletedTurn(
        message_pk=assistant_message_pk,
        message_status="completed",
        content=answered["text"],
        error_code=None,
        retryable=False,
        intent=ChatIntent(type=intent),
        evidence=_evidence(context.get("evidence")),
        warnings=answered["warnings"],
    )


# ------------------------------------------------------------------ 재시도
def retry_turn(assistant_message: MessageRow) -> dict:
    """실패한 assistant 행을 다시 큐에 올릴 변경분 + 작업.

    재시도해도 결과가 같은 실패(지원하지 않는 질문, 결과 없음)는 막는다 —
    큐만 돌고 사용자는 같은 문구를 다시 본다.
    """
    if assistant_message.message_status != "failed":
        raise ValueError("failed 상태만 재시도한다 (현재 %s)"
                         % assistant_message.message_status)
    if assistant_message.error_code not in RETRYABLE_CODES:
        raise ValueError("재시도해도 결과가 같다: %s" % assistant_message.error_code)

    return {
        "message_pk": assistant_message.message_pk,
        "message_status": "generating",
        "content": None,
        "error_code": None,
        "retry_count": assistant_message.retry_count + 1,
    }


def _evidence(raw) -> list:
    out = []
    for e in raw or []:
        if isinstance(e, ChatEvidence):
            out.append(e)
        elif isinstance(e, dict):
            out.append(ChatEvidence(
                source=str(e.get("source") or "unknown"),
                field=e.get("field"),
                value=e.get("value"),
                evidence_snapshot_id=(e.get("evidence_snapshot_id")
                                      or e.get("evidence_id")),
                metadata=e.get("metadata"),
            ))
    return out
