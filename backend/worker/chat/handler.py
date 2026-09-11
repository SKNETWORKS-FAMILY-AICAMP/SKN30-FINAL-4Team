"""Storage-free chat job handler for the generic polling runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ..llm_call import generate
from ..ports.llm import LLMClient
from ..runtime import ClaimedJob
from .context import build_chat_context
from .contracts import ChatAnswer, ChatIntent
from .grounding import check_grounding, validate_references
from .intent import classify_intent
from .prompt import SYSTEM_PROMPT


class ChatJobContractError(RuntimeError):
    """A claimed queue payload cannot satisfy the chat contract."""


class ChatResultMissingError(ChatJobContractError):
    """The requested case has no persisted analysis result yet."""


class ResultGroundedChatHandler:
    """Turn one claimed chat job into a JSON-native answer.

    ``report`` is supplied by the queue repository in the claim payload.  The
    handler never opens a DB connection and never knows how the result was
    loaded, which keeps this core reusable in the API worker and tests.
    """

    def __init__(self, llm_client: LLMClient, *, model_profile: str = "chat") -> None:
        if not model_profile.strip():
            raise ValueError("model_profile must not be blank")
        self._llm = llm_client
        self._model_profile = model_profile

    def handle(self, job: ClaimedJob) -> dict[str, Any]:
        payload = job.payload
        if not isinstance(payload, Mapping):
            raise ChatJobContractError("chat job payload must be an object")
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ChatJobContractError("chat job question must not be blank")
        if len(question) > 4000:
            raise ChatJobContractError("chat job question is too long")

        intent = classify_intent(question)
        if intent is ChatIntent.UNKNOWN:
            return _as_payload(
                ChatAnswer(
                    content=(
                        "분석 결과에 대해 지원유형, 예측 지원금액, 이례성, "
                        "또는 전체 분석 결과를 질문해 주세요."
                    ),
                    intent=ChatIntent.UNKNOWN,
                    warnings=[],
                    references=[],
                )
            )

        # ``result_payload`` is the name returned by the chat claim RPC;
        # ``report``/``analysis_result`` keep the pure handler convenient for
        # fakes and future queue adapters.
        report = payload.get(
            "result_payload",
            payload.get("report", payload.get("analysis_result")),
        )
        if not isinstance(report, Mapping) or not report:
            raise ChatResultMissingError("analysis result is not available")
        conversation = payload.get("conversation")
        if conversation is not None and not isinstance(conversation, list):
            raise ChatJobContractError("chat conversation must be a list")

        context = build_chat_context(
            report,
            question,
            intent=intent,
            conversation=conversation,
        )
        try:
            answer = generate(
                self._llm,
                task_name="result_grounded_chat",
                instructions=SYSTEM_PROMPT,
                payload=context,
                response_schema=ChatAnswer,
                model_profile=self._model_profile,
            )
        except ValidationError as error:
            raise ChatJobContractError("chat response violated its contract") from error
        if not isinstance(answer, ChatAnswer):  # defensive for alternate ports
            raise ChatJobContractError("chat response has an unexpected type")

        warnings = [*answer.warnings]
        if answer.intent is not intent:
            warnings.append("llm_intent_mismatch: requested intent was preserved")
        warnings.extend(check_grounding(answer.content, context))
        warnings.extend(validate_references(answer, context))
        clean_warnings = list(dict.fromkeys(warnings))[:20]
        return {
            "content": answer.content,
            "intent": intent.value,
            "warnings": clean_warnings,
            "references": [reference.model_dump(mode="json") for reference in answer.references],
        }


def _as_payload(answer: ChatAnswer) -> dict[str, Any]:
    return answer.model_dump(mode="json")


__all__ = [
    "ChatJobContractError",
    "ChatResultMissingError",
    "ResultGroundedChatHandler",
]
