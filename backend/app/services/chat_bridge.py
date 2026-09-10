"""backend ↔ chatmessage 경계.

`chatmessage/` 는 저장소 루트에 있고 backend 를 import 하지 않는다(의존 방향).
반대쪽 연결은 여기 한 곳에서만 한다 — chat 서비스나 스키마가 sys.path 를 만지는
일이 없도록. `ml/serving` 을 붙이는 `ml/pipeline_bridge.py` 와 같은 방식이다.

ml 쪽과 다른 점이 하나 있다. **chatmessage 는 선택 사항이 아니다.** torch 처럼
"없으면 그 기능만 건너뛴다" 가 아니라, 없으면 챗봇 자체가 성립하지 않는다.
그래서 import 실패를 조용히 삼키지 않고 그대로 올린다 — 예전에 이 자리에
try/except ImportError 가 있었고, 이름이 틀린 import 가 매번 조용히 실패하면서
아무도 모르는 채 죽은 코드로 남아 있었다.
"""
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ROOT = str(PROJECT_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from chatmessage import (  # noqa: E402 - sys.path 를 세운 뒤에야 import 할 수 있다
    AGENT_KEYS,
    CONTEXT_SECTION_BY_AGENT,
    SECTION_KEYS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ChatAnswer,
    ChatReference,
    ChatReferenceAgent,
    build_chat_context,
    build_user_prompt,
    check_grounding,
    context_evidence_ids,
    context_scope,
    wants_revision,
)

__all__ = [
    "AGENT_KEYS",
    "CONTEXT_SECTION_BY_AGENT",
    "SECTION_KEYS",
    "ChatAnswer",
    "ChatReference",
    "ChatReferenceAgent",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "build_chat_context",
    "build_user_prompt",
    "check_grounding",
    "context_evidence_ids",
    "context_scope",
    "wants_revision",
]
