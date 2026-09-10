"""챗봇 계약 — 답변 하나의 모양.

이 패키지는 **DB 도 HTTP 도 모른다.** 분석 리포트(dict)와 질문을 받아 프롬프트
Context 를 만들고, LLM 이 돌려준 답을 검사하는 것까지가 전부다. 저장과 전송은
호출부(FastAPI)가 갖는다 — chatmessage 쪽이 DB 커넥션을 들면 의존 방향이
거꾸로 선다.

`ChatAnswer` 는 그대로 LLM 의 structured output 스키마로 쓰인다. 그래서 기본값을
두지 않는다 — OpenAI structured output 이 `strict: true` 면 모든 필드가
required 여야 하고, 기본값이 있는 필드는 required 에서 빠져 스키마가 거절된다.
값이 없을 때는 `references: []`, `suggested_revision: null` 로 **명시해서** 받는다.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# 답변이 인용할 수 있는 출처. **분석 단계에서 이미 실행된** Agent·Model 의
# 이름이다. 챗봇이 이것들을 다시 부르지는 않는다 — 저장된 결과를 인용할 때
# "어느 것이 낸 값인가" 를 밝히는 내부 식별자다.
#
# 이 이름은 references(내부 참조·디버그 정보)에만 쓴다. 사용자에게 보이는
# 답변 본문은 업무 언어로 쓴다("요청서의 필수 항목과 구조를 확인한 결과…").
ChatReferenceAgent = Literal[
    "CPL",
    "FIT",
    "RETRIEVAL",
    "SIM",
    "MODEL_1",
    "MODEL_2",
    "MODEL_3",
    "SUMMARY",
]
AGENT_KEYS: tuple[str, ...] = (
    "CPL",
    "FIT",
    "RETRIEVAL",
    "SIM",
    "MODEL_1",
    "MODEL_2",
    "MODEL_3",
    "SUMMARY",
)

# 저장된 분석 결과의 섹션 이름. `context_scope` 가 고르는 대상이고,
# Chat Context 의 `report` 아래 키다.
#
# **섹션을 고르는 것이지 Agent 를 실행하는 것이 아니다.** CPL·FIT·SIM·Model
# 1·2·3 은 분석 단계에서 이미 돌았고, 챗봇은 그 결과 중 이번 질문에 필요한
# 것만 상세로 싣는다.
SECTION_KEYS: tuple[str, ...] = (
    "cpl",
    "fit",
    "retrieval",
    "sim",
    "model1",
    "model2",
    "model3",
    "summary",
)

# 섹션 이름 ↔ references 의 agent 이름. Context 는 소문자 섹션 키를 쓰고,
# 응답 계약은 대문자 열거값을 쓴다. 두 이름을 한 곳에서 잇는다.
CONTEXT_SECTION_BY_AGENT: dict[str, str] = {
    "CPL": "cpl",
    "FIT": "fit",
    "RETRIEVAL": "retrieval",
    "SIM": "sim",
    "MODEL_1": "model1",
    "MODEL_2": "model2",
    "MODEL_3": "model3",
    "SUMMARY": "summary",
}

AGENT_BY_CONTEXT_SECTION: dict[str, str] = {
    section: agent for agent, section in CONTEXT_SECTION_BY_AGENT.items()
}


class ChatReference(BaseModel):
    """답변 한 건이 가리키는 분석 결과 한 조각.

    `evidence_id` 는 report_json 의 `evidence_ref` 다. 근거 원문이 없는
    결과(ML 예측값처럼 문서 인용이 아닌 것)도 있어 null 을 허용한다 — 필수로
    두면 LLM 이 자리를 채우려고 없는 id 를 만든다.
    """

    model_config = ConfigDict(extra="forbid")

    agent: ChatReferenceAgent
    item: str = Field(min_length=1, max_length=200)
    evidence_id: str | None


class ChatAnswer(BaseModel):
    """LLM 이 돌려주는 답 하나. provider 와 무관한 계약이다."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=12_000)
    references: list[ChatReference] = Field(max_length=30)
    # 수정·보완 제안. 제안일 뿐 확정 수정이 아니라서 answer 안에 섞지 않고
    # 자리를 따로 둔다 — 화면이 "제안" 으로 구분해 그릴 수 있어야 사용자가
    # 확정 판정으로 읽지 않는다.
    suggested_revision: str | None = Field(max_length=4_000)

    @property
    def evidence_ids(self) -> list[str]:
        """인용된 근거 id 만 순서대로. 중복은 접는다."""
        seen: list[str] = []
        for reference in self.references:
            if reference.evidence_id and reference.evidence_id not in seen:
                seen.append(reference.evidence_id)
        return seen
