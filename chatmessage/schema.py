"""챗봇 계약 — Supabase PoC 스키마(`result.conversation_message`)에 맞춘 모양.

DB 가 이미 정한 것을 그대로 따른다
--------------------------------
대화는 단순 로그가 아니라 **비동기 생성 상태를 가진 행**이다. 질문이 들어오면
assistant 행을 `generating` + `content NULL` 로 먼저 만들고, 워커가 채운다.
테이블 제약이 그 모양을 강제한다.

    CHECK (
      (role='user'      AND message_status='completed'
                        AND content IS NOT NULL AND reply_to_message_pk IS NULL)
      OR
      (role='assistant' AND reply_to_message_pk IS NOT NULL)
    )

그래서 이 패키지는 요청 하나를 응답 하나로 바꾸지 않는다. **행 payload 를
만들어 돌려주고 저장은 호출부(Edge Function / 워커)가 한다** — ml 쪽이 DB
커넥션을 들면 의존 방향이 거꾸로 선다(SIM-R 때와 같은 이유).

식별자는 전부 UUID다. 스키마에 별도 분석번호나 합성 PK 가 없다.
"""
import uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

IntentType = Literal[
    "MODEL_1", "MODEL_2", "MODEL_3", "REPORT", "DOCUMENT", "UNKNOWN",
]
INTENTS: tuple = ("MODEL_1", "MODEL_2", "MODEL_3", "REPORT", "DOCUMENT",
                  "UNKNOWN")

MessageRole = Literal["user", "assistant"]
# result.conversation_message.message_status
MessageStatus = Literal["generating", "completed", "failed"]
# result.analysis_session.status
SessionStatus = Literal["active", "closed"]

# 답을 만들지 못한 이유. message_status='failed' 일 때 error_code 에 들어간다.
ERROR_CODES = (
    "UNSUPPORTED_QUESTION",          # 지원하지 않는 질문(UNKNOWN)
    "ANALYSIS_RESULT_NOT_AVAILABLE",  # 근거로 쓸 결과가 없음
    "ANALYSIS_RESULT_INSUFFICIENT",   # 결과는 있으나 근거 부족
    "CHAT_LLM_FAILED",               # LLM 장애 — 재시도가 의미 있다
    "SESSION_NOT_ACTIVE",            # 세션이 닫혔거나 만료됨
)
# 워커가 다시 시도해 볼 만한 것. 나머지는 재시도해도 같은 결과다.
RETRYABLE_CODES = ("CHAT_LLM_FAILED",)


def new_uuid() -> str:
    return str(uuid.uuid4())


class ChatEvidence(BaseModel):
    """근거 한 조각. `evidence_snapshot_pk` 는 result.evidence_snapshot 참조."""
    source: str
    field: Optional[str] = None
    value: Any = None
    evidence_snapshot_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ChatIntent(BaseModel):
    type: IntentType


class SessionState(BaseModel):
    """대화를 이어도 되는 상태인가. 만료·종료 판정에 필요한 것만 담는다."""
    model_config = ConfigDict(extra="forbid")

    analysis_session_id: str
    analysis_case_id: str
    status: SessionStatus = "active"
    expired: bool = False
    case_status: Literal["processing", "ready", "failed"] = "ready"

    @property
    def usable(self) -> bool:
        # 결과가 준비되지 않았으면 근거가 없어 답할 수 없다.
        return (self.status == "active" and not self.expired
                and self.case_status == "ready")


class TurnRequest(BaseModel):
    """사용자가 보낸 질문 하나."""
    model_config = ConfigDict(extra="forbid")

    analysis_session_id: str
    analysis_case_id: str
    content: str

    @field_validator("analysis_session_id", "analysis_case_id", "content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("빈 값은 허용하지 않는다")
        return v.strip()


class MessageRow(BaseModel):
    """`result.conversation_message` 한 행. 컬럼명을 그대로 쓴다."""
    model_config = ConfigDict(extra="forbid")

    message_pk: str = Field(default_factory=new_uuid)
    analysis_session_pk: str
    role: MessageRole
    sequence_no: int = Field(ge=1)
    content: Optional[str] = None
    message_status: MessageStatus = "completed"
    reply_to_message_pk: Optional[str] = None
    retry_count: int = Field(default=0, ge=0)
    error_code: Optional[str] = None

    @field_validator("sequence_no")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("sequence_no 는 1 이상이다")
        return v

    def check_table_shape(self) -> None:
        """DB CHECK 제약을 여기서 먼저 건다 — 잘못된 행을 만들어 보내면
        insert 가 거절되는데, 그때는 어느 단계가 틀렸는지 알기 어렵다."""
        if self.role == "user":
            if (self.message_status != "completed" or not self.content
                    or self.reply_to_message_pk is not None):
                raise ValueError(
                    "user 행은 completed · content 필수 · reply_to 없음이어야 한다")
        else:
            if self.reply_to_message_pk is None:
                raise ValueError("assistant 행은 reply_to_message_pk 가 있어야 한다")


class QueueJob(BaseModel):
    """`ops.processing_run` 에 넣을 채팅 작업.

    assistant_message_pk 에 부분 UNIQUE 인덱스가 걸려 있어 한 메시지에 작업이
    두 번 등록되지 않는다. 여기서는 그 행 모양만 만든다.
    """
    model_config = ConfigDict(extra="forbid")

    run_type: str = "chat"
    status: Literal["queued"] = "queued"
    analysis_case_pk: str
    assistant_message_pk: str


class CreatedTurn(BaseModel):
    """질문 접수 결과 — 저장할 행 둘과 큐 작업 하나.

    `edge-conversation-create-message` 가 이 셋을 한 트랜잭션으로 쓴다.
    """
    model_config = ConfigDict(extra="forbid")

    user_message: MessageRow
    assistant_message: MessageRow
    job: QueueJob


class CompletedTurn(BaseModel):
    """워커가 assistant 행에 적용할 변경분.

    행을 통째로 돌려주지 않고 바꿀 칸만 준다 — 워커가 UPDATE 로 쓴다.
    """
    model_config = ConfigDict(extra="forbid")

    message_pk: str
    message_status: MessageStatus
    content: Optional[str] = None
    error_code: Optional[str] = None
    retryable: bool = False

    intent: ChatIntent
    evidence: List[ChatEvidence] = Field(default_factory=list)
    # 근거 점검 결과. 답변을 막지 않고 무엇이 걸렸는지만 남긴다.
    warnings: List[str] = Field(default_factory=list)
