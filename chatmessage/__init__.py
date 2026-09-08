"""사전협의 분석 결과 설명 챗봇 — Supabase PoC 스키마용.

대화는 `result.conversation_message` 가 갖는다. 질문이 오면 assistant 행을
`generating` 으로 먼저 만들고 워커가 채우는 2단계 구조라, 이 패키지도 그렇게
나뉜다. **저장은 하지 않는다** — 행 payload 만 만들고 트랜잭션은 호출부가 갖는다.

    from chatmessage import TurnRequest, SessionState, create_turn, complete_turn

    created = create_turn(request, session, next_sequence_no)   # Edge Function
    patch   = complete_turn(created.assistant_message.message_pk,
                            case_id, question, loader, llm)     # 워커
"""
from .context_loader import (ChatContextLoader, MockChatContextLoader,
                             INTENT_SOURCE, TABLES)
from .model_result import (DB_STATUSES, INTENT_TO_MODEL_NAME, MODEL_NAMES,
                           PIPELINE_TO_MODEL_NAME, envelope_to_row,
                           row_to_context, to_rows)
from .supabase_context_loader import SupabaseChatContextLoader
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .responder import (ChatLLMClient, answer_with_checks, check_grounding,
                        generate_answer, TEMPERATURE)
from .router import classify_intent, explain
from .schema import (ChatEvidence, ChatIntent, CompletedTurn, CreatedTurn,
                     ERROR_CODES, INTENTS, MessageRow, QueueJob,
                     RETRYABLE_CODES, SessionState, TurnRequest)
from .service import (SessionNotUsableError, complete_turn, create_turn,
                      retry_turn)

__all__ = [
    "ChatContextLoader", "ChatEvidence", "ChatIntent", "ChatLLMClient",
    "CompletedTurn", "CreatedTurn", "DB_STATUSES", "ERROR_CODES", "INTENTS",
    "INTENT_SOURCE", "INTENT_TO_MODEL_NAME", "MODEL_NAMES", "MessageRow",
    "MockChatContextLoader", "PIPELINE_TO_MODEL_NAME", "QueueJob",
    "RETRYABLE_CODES", "SYSTEM_PROMPT", "SessionNotUsableError",
    "SessionState", "SupabaseChatContextLoader", "TABLES", "TEMPERATURE",
    "TurnRequest", "answer_with_checks", "build_user_prompt",
    "check_grounding", "classify_intent", "complete_turn", "create_turn",
    "envelope_to_row", "explain", "generate_answer", "retry_turn",
    "row_to_context", "to_rows",
]
