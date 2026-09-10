"""Result-grounded chat worker core.

The package is deliberately storage-agnostic.  A queue adapter puts the
persisted analysis result in ``ClaimedJob.payload`` and the handler returns a
small JSON-native answer for the repository to persist.
"""

from .context import build_chat_context, context_evidence_ids
from .contracts import ChatAnswer, ChatIntent, ChatReference
from .grounding import check_grounding, validate_references
from .handler import (
    ChatJobContractError,
    ChatResultMissingError,
    ResultGroundedChatHandler,
)
from .intent import classify_intent
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt

__all__ = [
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "ChatAnswer",
    "ChatIntent",
    "ChatJobContractError",
    "ChatReference",
    "ChatResultMissingError",
    "ResultGroundedChatHandler",
    "build_chat_context",
    "build_user_prompt",
    "check_grounding",
    "classify_intent",
    "context_evidence_ids",
    "validate_references",
]
