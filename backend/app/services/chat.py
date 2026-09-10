import json
import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.schemas.chat import (
    ChatAnswer,
    ChatMessageResponse,
    ChatMessagesResponse,
    ChatReference,
    ChatTurnResponse,
)
from app.schemas.report import ReportJsonV01
from app.services.chat_bridge import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_chat_context,
    build_user_prompt,
    check_grounding,
    context_evidence_ids,
)


logger = logging.getLogger(__name__)


CHAT_CONTEXT_MESSAGE_LIMIT = 20

# 화면이 한 번에 받는 대화 수. 이력 목록(5건)보다 크게 잡는다. 질문 몇 번이면
# 끝나는 분량이라 대부분 한 번에 다 보인다.
CHAT_PAGE_SIZE = 20


class ChatNotFoundError(LookupError):
    pass


class ChatNotReadyError(ValueError):
    pass


ChatFailureCode = Literal[
    "LLM_UNAVAILABLE",
    "LLM_TIMEOUT",
    "LLM_INVALID_RESPONSE",
]


@dataclass(frozen=True, slots=True)
class ChatGenerationError(RuntimeError):
    code: ChatFailureCode

    def __str__(self) -> str:
        return {
            "LLM_UNAVAILABLE": "The chat model is unavailable",
            "LLM_TIMEOUT": "The chat model timed out",
            "LLM_INVALID_RESPONSE": "The chat model returned an invalid response",
        }[self.code]


def get_chat_history(
    engine: Engine,
    owner_user_id: int,
    case_id: int,
    *,
    limit: int = CHAT_PAGE_SIZE,
    cursor: int | None = None,
) -> ChatMessagesResponse:
    """최근 대화부터 limit 개를 시간순으로 돌려준다.

    cursor 는 이전 쪽의 가장 오래된 sequence_no 다. 그보다 앞선 대화를 준다.
    """
    _load_ready_report(engine, owner_user_id, case_id)
    with engine.connect() as connection:
        session_id = connection.scalar(
            text(
                "SELECT id FROM sims.chat_session "
                "WHERE inspection_case_id = :case_id"
            ),
            {"case_id": case_id},
        )
        if session_id is None:
            return ChatMessagesResponse(messages=[])
        rows = connection.execute(
            text(
                """
                SELECT id, sequence_no, role, content,
                       "references", suggested_revision
                FROM sims.chat_message
                WHERE chat_session_id = :session_id
                  AND (CAST(:cursor AS integer) IS NULL
                       OR sequence_no < CAST(:cursor AS integer))
                ORDER BY sequence_no DESC
                LIMIT :limit
                """
            ),
            {"session_id": session_id, "cursor": cursor, "limit": limit + 1},
        ).mappings().all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    oldest_sequence_no = rows[-1]["sequence_no"] if rows else None
    return ChatMessagesResponse(
        # 조회는 최신순이지만 화면은 시간순으로 그린다.
        messages=[_to_message(row) for row in reversed(rows)],
        next_cursor=str(oldest_sequence_no) if has_more else None,
    )


async def answer_chat(
    engine: Engine,
    llm_client: LLMClient | None,
    settings: Settings,
    *,
    owner_user_id: int,
    case_id: int,
    question: str,
) -> ChatTurnResponse:
    question = question.strip()
    if not question:
        raise ValueError("Chat question must not be blank")

    report = _load_ready_report(engine, owner_user_id, case_id)
    history = get_chat_history(engine, owner_user_id, case_id)
    if llm_client is None:
        raise ChatGenerationError("LLM_UNAVAILABLE")

    context = build_chat_context(
        report.model_dump(mode="json"),
        question,
        conversation=[
            {"role": item.role, "content": item.content}
            for item in history.messages[-CHAT_CONTEXT_MESSAGE_LIMIT:]
        ],
    )
    try:
        response = await llm_client.generate_structured(
            task_name="result_grounded_chat",
            messages=_prompt_messages(context),
            response_schema=ChatAnswer,
            model_profile=settings.chat_model_profile,
        )
        if not isinstance(response, ChatAnswer):
            raise LLMInvalidResponseError("Unexpected structured response type")
        # 인용은 이번 질문에 실제로 실린 근거 안에서만 가능하다. 리포트 전체가
        # 아니라 Context 를 기준으로 본다 — 상세로 싣지 않은 Agent 의 근거 id 를
        # 답변이 가리키면, 그 답은 보지 않은 것을 인용한 것이다.
        allowed = context_evidence_ids(context)
        if any(
            reference.evidence_id is not None and reference.evidence_id not in allowed
            for reference in response.references
        ):
            raise LLMInvalidResponseError("Chat response cited unknown evidence")
    except LLMTimeoutError:
        raise ChatGenerationError("LLM_TIMEOUT") from None
    except LLMUnavailableError:
        raise ChatGenerationError("LLM_UNAVAILABLE") from None
    except (LLMInvalidResponseError, ValidationError, ValueError):
        raise ChatGenerationError("LLM_INVALID_RESPONSE") from None

    # 근거 점검은 답을 막지 않는다. 오탐이 있어서다 — 무엇이 걸렸는지만 남기고
    # 판단은 사람이 한다.
    warnings = check_grounding(response.answer, context, question)
    if warnings:
        logger.warning(
            "Chat answer grounding warnings case_id=%s scope=%s warnings=%s",
            case_id,
            ",".join(context["context_scope"]),
            " | ".join(warnings),
        )

    return _persist_chat_turn(
        engine,
        owner_user_id=owner_user_id,
        case_id=case_id,
        question=question,
        answer=response,
        model_name=settings.chat_model_profile,
    )


def _to_message(row) -> ChatMessageResponse:
    """DB 행 하나 → 말풍선 하나. POST 와 GET 이 같은 함수를 쓴다.

    두 곳에서 따로 만들면 한쪽에만 필드를 더하는 일이 생긴다. 실제로 근거가
    POST 응답에만 실려 새로고침하면 사라지던 것이 그 경우였다.
    """
    references = row["references"] if "references" in row.keys() else None
    return ChatMessageResponse(
        id=row["id"],
        role=row["role"],
        content=row["content"],
        references=[
            ChatReference.model_validate(item) for item in (references or [])
        ],
        suggested_revision=(
            row["suggested_revision"] if "suggested_revision" in row.keys() else None
        ),
    )


def _load_ready_report(
    engine: Engine,
    owner_user_id: int,
    case_id: int,
) -> ReportJsonV01:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT c.status, r.report_json
                FROM sims.inspection_case c
                LEFT JOIN sims.inspection_report r
                  ON r.inspection_case_id = c.id
                WHERE c.id = :case_id AND c.owner_user_id = :owner_user_id
                """
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        ).mappings().one_or_none()
    if row is None:
        raise ChatNotFoundError
    if row["status"] != "COMPLETED" or row["report_json"] is None:
        raise ChatNotReadyError("Chat is available after report completion")
    try:
        return ReportJsonV01.model_validate(row["report_json"])
    except ValidationError as error:
        raise ChatNotReadyError("Stored report is not a valid chat context") from error


def _prompt_messages(context: dict) -> list[Message]:
    return [
        Message(role="developer", content=SYSTEM_PROMPT),
        Message(role="user", content=build_user_prompt(context)),
    ]


def _persist_chat_turn(
    engine: Engine,
    *,
    owner_user_id: int,
    case_id: int,
    question: str,
    answer: ChatAnswer,
    model_name: str,
) -> ChatTurnResponse:
    with engine.begin() as connection:
        case_status = connection.scalar(
            text(
                """
                SELECT c.status
                FROM sims.inspection_case c
                JOIN sims.inspection_report r ON r.inspection_case_id = c.id
                WHERE c.id = :case_id AND c.owner_user_id = :owner_user_id
                FOR UPDATE OF c
                """
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        )
        if case_status is None:
            raise ChatNotFoundError
        if case_status != "COMPLETED":
            raise ChatNotReadyError("Chat is available after report completion")

        session_id = connection.scalar(
            text(
                """
                INSERT INTO sims.chat_session (inspection_case_id)
                VALUES (:case_id)
                ON CONFLICT (inspection_case_id) DO NOTHING
                RETURNING id
                """
            ),
            {"case_id": case_id},
        )
        if session_id is None:
            session_id = connection.scalar(
                text(
                    "SELECT id FROM sims.chat_session "
                    "WHERE inspection_case_id = :case_id FOR UPDATE"
                ),
                {"case_id": case_id},
            )
        if session_id is None:
            raise ChatNotReadyError("Chat session could not be created")

        next_sequence = connection.scalar(
            text(
                "SELECT COALESCE(MAX(sequence_no), 0) "
                "FROM sims.chat_message WHERE chat_session_id = :session_id"
            ),
            {"session_id": session_id},
        ) + 1
        user_row = connection.execute(
            text(
                """
                INSERT INTO sims.chat_message (
                    chat_session_id, sequence_no, role, content
                ) VALUES (
                    :session_id, :sequence_no, 'USER', :content
                )
                RETURNING id, role, content, "references", suggested_revision
                """
            ),
            {
                "session_id": session_id,
                "sequence_no": next_sequence,
                "content": question,
            },
        ).mappings().one()
        assistant_row = connection.execute(
            text(
                """
                INSERT INTO sims.chat_message (
                    chat_session_id, sequence_no, role, content,
                    model_name, model_version, "references", suggested_revision
                ) VALUES (
                    :session_id, :sequence_no, 'ASSISTANT', :content,
                    :model_name, :prompt_version,
                    CAST(:references AS jsonb), :suggested_revision
                )
                RETURNING id, role, content, "references", suggested_revision
                """
            ),
            {
                "session_id": session_id,
                "sequence_no": next_sequence + 1,
                "content": answer.answer,
                "model_name": model_name,
                # 어느 프롬프트에서 나온 답인지 남긴다. 프롬프트를 고친 뒤
                # 답변 품질이 달라졌을 때 되짚을 수 있는 유일한 단서다.
                "prompt_version": PROMPT_VERSION,
                "references": json.dumps(
                    [reference.model_dump() for reference in answer.references],
                    ensure_ascii=False,
                ),
                "suggested_revision": answer.suggested_revision,
            },
        ).mappings().one()

    return ChatTurnResponse(
        user_message=_to_message(user_row),
        assistant_message=_to_message(assistant_row),
    )
