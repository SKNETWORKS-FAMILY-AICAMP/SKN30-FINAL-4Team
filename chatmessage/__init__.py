"""사전협의 분석 결과 설명 챗봇.

DB 연결 전 단계다. 저장 스키마가 정해지면 `context_loader` 의 구현체 하나만
Supabase 조회로 갈아끼우고 나머지(router·prompt·responder·service·schema)는
그대로 쓴다.

    from chatmessage import handle_chat, ChatRequest, MockChatContextLoader
"""
from .context_loader import (ChatContextLoader, MockChatContextLoader,
                             INTENT_TO_KEY)
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .responder import (ChatLLMClient, answer_with_checks, check_grounding,
                        generate_answer, TEMPERATURE)
from .router import classify_intent, explain
from .schema import (ChatAnswer, ChatError, ChatEvidence, ChatIntent,
                     ChatRequest, ChatResponse, INTENTS)
from .service import handle_chat

__all__ = [
    "ChatAnswer", "ChatContextLoader", "ChatError", "ChatEvidence",
    "ChatIntent", "ChatLLMClient", "ChatRequest", "ChatResponse",
    "INTENTS", "INTENT_TO_KEY", "MockChatContextLoader", "SYSTEM_PROMPT",
    "TEMPERATURE", "answer_with_checks", "build_user_prompt",
    "check_grounding", "classify_intent", "explain", "generate_answer",
    "handle_chat",
]
