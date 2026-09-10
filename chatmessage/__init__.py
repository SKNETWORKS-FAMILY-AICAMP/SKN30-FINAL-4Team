"""사전협의 분석 결과 챗봇 — **단일 Chat Agent** 코어.

챗봇은 멀티에이전트가 아니다. 분석 단계와 챗봇 단계는 분리돼 있다.

    [분석 단계]  요청서 → Parser → CPL/FIT/Retrieval/SIM/Model 1·2·3 → 결과 저장
    [챗봇 단계]  질문 → 필요한 결과 섹션 선택 → Chat Context → 하나의 Chat LLM
                 → 답변 / 요약 / 설명 / 수정 제안 → Grounding 검사

**채팅 중에 CPL·FIT·SIM·Model 을 다시 실행하지 않는다.** 이미 저장된 리포트와
Evidence 에서 질문에 필요한 것만 골라 하나의 LLM 이 답한다.

    from chatmessage import build_chat_context, ChatAnswer, check_grounding

    context = build_chat_context(report_json, question, conversation)
    answer  = llm.generate_structured(..., response_schema=ChatAnswer)   # 호출부
    for warning in check_grounding(answer.answer, context):
        log(warning)

**DB 도 HTTP 도 모른다.** 리포트(dict)와 질문을 받아 Context 를 만들고, 돌아온
답을 검사하는 것까지가 전부다. 저장·전송은 backend 가 갖는다 — 이쪽이 DB 커넥션을
들면 의존 방향이 거꾸로 선다.

Supabase Edge Function + 워커 2단계 구조(`create_turn`/`complete_turn`,
`result.conversation_message`, `SupabaseChatContextLoader`)는 없앴다. Model 1·2·3
하나만 고르던 Intent 라우팅도 같이 없앴다 — 한 질문이 여러 결과 섹션을 동시에
필요로 할 수 있어서 `context_scope` 로 대체했다.
"""
from .context import (
    MAX_EVIDENCE_ITEMS,
    MAX_EXCERPT_CHARS,
    build_chat_context,
    context_evidence_ids,
    context_scope,
    wants_revision,
)
from .grounding import (
    GROUNDING_CATEGORIES,
    check_grounding,
    internal_value_mentions,
    link_mentions,
    ungrounded_numbers,
    warning_category,
)
from .loader import (
    AnalysisResultLoader,
    DictLoader,
    JsonFileLoader,
    LoaderError,
    describe,
)
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from .provenance import (
    INTERNAL_VALUE_KEYS,
    PROVENANCE_KEYS,
    is_internal_key,
    provenance_of,
    strip_internal_values,
)
from .schema import (
    AGENT_BY_CONTEXT_SECTION,
    AGENT_KEYS,
    CONTEXT_SECTION_BY_AGENT,
    SECTION_KEYS,
    ChatAnswer,
    ChatReference,
    ChatReferenceAgent,
)

__all__ = [
    "AGENT_BY_CONTEXT_SECTION",
    "AGENT_KEYS",
    "SECTION_KEYS",
    "AnalysisResultLoader",
    "DictLoader",
    "GROUNDING_CATEGORIES",
    "INTERNAL_VALUE_KEYS",
    "JsonFileLoader",
    "LoaderError",
    "CONTEXT_SECTION_BY_AGENT",
    "ChatAnswer",
    "ChatReference",
    "ChatReferenceAgent",
    "MAX_EVIDENCE_ITEMS",
    "MAX_EXCERPT_CHARS",
    "PROMPT_VERSION",
    "PROVENANCE_KEYS",
    "SYSTEM_PROMPT",
    "build_chat_context",
    "build_user_prompt",
    "check_grounding",
    "context_evidence_ids",
    "context_scope",
    "describe",
    "internal_value_mentions",
    "is_internal_key",
    "link_mentions",
    "provenance_of",
    "strip_internal_values",
    "ungrounded_numbers",
    "warning_category",
    "wants_revision",
]
