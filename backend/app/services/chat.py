import json
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
    ChatTurnResponse,
)
from app.schemas.report import ReportJsonV01


CHAT_CONTEXT_MESSAGE_LIMIT = 20


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


def load_chat_prompt(path) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("Chat prompt must not be blank")
    return prompt


def get_chat_history(
    engine: Engine,
    owner_user_id: int,
    case_id: int,
) -> ChatMessagesResponse:
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
            return ChatMessagesResponse(case_id=case_id, messages=[])
        rows = connection.execute(
            text(
                """
                SELECT id, sequence_no, role, content, model_name,
                       model_version, input_tokens, output_tokens,
                       evidence_refs, created_at
                FROM sims.chat_message
                WHERE chat_session_id = :session_id
                ORDER BY sequence_no
                """
            ),
            {"session_id": session_id},
        ).mappings().all()
    return ChatMessagesResponse(
        case_id=case_id,
        chat_session_id=session_id,
        messages=[ChatMessageResponse.model_validate(row) for row in rows],
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

    try:
        response = await llm_client.generate_structured(
            task_name="result_grounded_chat",
            messages=_prompt_messages(
                settings,
                report,
                history,
                question,
            ),
            response_schema=ChatAnswer,
            model_profile=settings.chat_model_profile,
        )
        if not isinstance(response, ChatAnswer):
            raise LLMInvalidResponseError("Unexpected structured response type")
        allowed_refs = _report_evidence_refs(report)
        if any(reference not in allowed_refs for reference in response.evidence_refs):
            raise LLMInvalidResponseError("Chat response cited unknown evidence")
    except LLMTimeoutError:
        raise ChatGenerationError("LLM_TIMEOUT") from None
    except LLMUnavailableError:
        raise ChatGenerationError("LLM_UNAVAILABLE") from None
    except (LLMInvalidResponseError, ValidationError, ValueError):
        raise ChatGenerationError("LLM_INVALID_RESPONSE") from None

    return _persist_chat_turn(
        engine,
        owner_user_id=owner_user_id,
        case_id=case_id,
        question=question,
        answer=response,
        model_name=settings.chat_model_profile,
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


def _prompt_messages(
    settings: Settings,
    report: ReportJsonV01,
    history: ChatMessagesResponse,
    question: str,
) -> list[Message]:
    prior_messages = history.messages[-CHAT_CONTEXT_MESSAGE_LIMIT:]
    payload = {
        "question": question,
        "conversation": [
            {"role": item.role, "content": item.content}
            for item in prior_messages
        ],
        "report_json": report.model_dump(mode="json"),
    }
    return [
        Message(
            role="developer",
            content=load_chat_prompt(settings.chat_prompt_path),
        ),
        Message(
            role="user",
            content=json.dumps(payload, ensure_ascii=False),
        ),
    ]


def _report_evidence_refs(report: ReportJsonV01) -> set[str]:
    values: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            reference = value.get("evidence_ref")
            if isinstance(reference, str):
                values.add(reference)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(report.model_dump(mode="python"))
    return values


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
                    chat_session_id, sequence_no, role, content, evidence_refs
                ) VALUES (
                    :session_id, :sequence_no, 'USER', :content, '[]'::jsonb
                )
                RETURNING id, sequence_no, role, content, model_name,
                          model_version, input_tokens, output_tokens,
                          evidence_refs, created_at
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
                    model_name, evidence_refs
                ) VALUES (
                    :session_id, :sequence_no, 'ASSISTANT', :content,
                    :model_name, CAST(:evidence_refs AS jsonb)
                )
                RETURNING id, sequence_no, role, content, model_name,
                          model_version, input_tokens, output_tokens,
                          evidence_refs, created_at
                """
            ),
            {
                "session_id": session_id,
                "sequence_no": next_sequence + 1,
                "content": answer.answer,
                "model_name": model_name,
                "evidence_refs": json.dumps(answer.evidence_refs),
            },
        ).mappings().one()

    return ChatTurnResponse(
        case_id=case_id,
        chat_session_id=session_id,
        user_message=ChatMessageResponse.model_validate(user_row),
        assistant_message=ChatMessageResponse.model_validate(assistant_row),
    )
