"""챗봇 요청·응답 계약.

status 는 ML Result envelope(`ml/serving/shared/result_envelope.py`)과 같은 4종을
쓴다. 챗봇이 답을 못 하는 이유와 모델이 결과를 못 낸 이유가 같은 어휘로
설명돼야 프런트가 분기를 두 벌 짜지 않는다.

    success            답변 생성
    failed             챗봇 자체 실패 (LLM 오류 등)
    not_available      분석 결과가 없어 답할 수 없음
    insufficient_data  결과는 있으나 근거가 모자라 답하지 않음
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

IntentType = Literal[
    "MODEL_1",
    "MODEL_2",
    "MODEL_3",
    "REPORT",
    "DOCUMENT",
    "UNKNOWN",
]

INTENTS: tuple = ("MODEL_1", "MODEL_2", "MODEL_3", "REPORT", "DOCUMENT", "UNKNOWN")

ChatStatus = Literal["success", "failed", "not_available", "insufficient_data"]


class ChatRequest(BaseModel):
    conversation_id: str
    analysis_id: str
    message: str

    @field_validator("conversation_id", "analysis_id", "message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        # 빈 질문은 LLM 에 보내 봐야 근거 없는 문장만 돌아온다. 여기서 막는다.
        if not v or not v.strip():
            raise ValueError("빈 값은 허용하지 않는다")
        return v.strip()


class ChatIntent(BaseModel):
    type: IntentType


class ChatEvidence(BaseModel):
    source: str
    field: Optional[str] = None
    value: Any = None
    evidence_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ChatAnswer(BaseModel):
    text: str
    evidence: List[ChatEvidence] = Field(default_factory=list)


class ChatError(BaseModel):
    code: str
    message: str


class ChatResponse(BaseModel):
    conversation_id: str
    analysis_id: str
    message_id: str

    intent: ChatIntent
    status: ChatStatus

    answer: Optional[ChatAnswer] = None
    error: Optional[ChatError] = None
    # 근거 점검 결과. 답변을 막지는 않고 무엇이 걸렸는지만 남긴다 —
    # 판단은 호출부(또는 사람)가 한다.
    warnings: List[str] = Field(default_factory=list)
