"""챗봇 패키지 계약 검사 — 답변 계약과 프롬프트가 어긋나지 않는지.

    python -m pytest chatmessage/

세부 회귀는 옆 두 파일이 본다.

    test_context.py    Context 구성 · context_scope · 근거 중복 제거
    test_grounding.py  답변 사후 점검

여기서는 **패키지 밖으로 나가는 계약**만 본다. LLM 은 부르지 않는다.

Supabase PoC 시절 검사(2단계 turn 생성, conversation_message 행 모양, Intent
라우터, SupabaseChatContextLoader)는 그 구조와 함께 없앴다. 지금 챗봇은 FastAPI
가 저장한 report_json 을 읽고, 행 저장은 backend 가 한다.
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import chatmessage  # noqa: E402
from chatmessage import (  # noqa: E402
    AGENT_KEYS,
    CONTEXT_SECTION_BY_AGENT,
    SECTION_KEYS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ChatAnswer,
    ChatReference,
    build_chat_context,
    build_user_prompt,
)
from test_context import report  # noqa: E402 - 리포트 fixture 를 함께 쓴다


def test_public_api_is_importable() -> None:
    missing = [name for name in chatmessage.__all__ if not hasattr(chatmessage, name)]
    assert not missing, f"__all__ 에 있는데 없는 이름: {missing}"


def test_answer_schema_is_strict_output_compatible() -> None:
    """OpenAI structured output 이 strict 면 모든 필드가 required 여야 한다.

    기본값을 하나라도 두면 그 필드가 required 에서 빠지고 스키마가 통째로
    거절된다. 값이 없을 때는 `[]` 와 `null` 을 명시해서 받는다.
    """
    schema = ChatAnswer.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False

    reference = schema["$defs"]["ChatReference"]
    assert set(reference["required"]) == set(reference["properties"])
    assert reference["additionalProperties"] is False


def test_empty_answer_is_expressible() -> None:
    answer = ChatAnswer(
        answer="이번 분석으로는 확인할 수 없습니다.",
        references=[],
        suggested_revision=None,
    )
    assert answer.evidence_ids == []


def test_evidence_ids_dedupe_in_order() -> None:
    answer = ChatAnswer(
        answer="설명",
        references=[
            ChatReference(agent="CPL", item="BUDGET", evidence_id="request:BUDGET:0"),
            ChatReference(agent="FIT", item="FIT-1", evidence_id="request:BUDGET:0"),
            ChatReference(agent="MODEL_2", item="predicted_amount", evidence_id=None),
            ChatReference(agent="SIM", item="SIM-1", evidence_id="sim:SIM-1:req"),
        ],
        suggested_revision=None,
    )
    assert answer.evidence_ids == ["request:BUDGET:0", "sim:SIM-1:req"]


def test_reference_agents_match_context_sections() -> None:
    """응답의 agent 이름과 Context 의 섹션 키가 1:1 이어야 한다.

    한쪽에만 있는 이름이 생기면 LLM 이 Context 에 없는 주체를 인용하거나,
    Context 에 실은 결과를 인용할 방법이 없어진다.
    """
    assert set(CONTEXT_SECTION_BY_AGENT) == set(AGENT_KEYS)
    sections = set(build_chat_context(report(), "전체 요약")["report"])
    assert set(CONTEXT_SECTION_BY_AGENT.values()) == sections
    assert set(SECTION_KEYS) == sections


def test_prompt_names_every_agent_and_output_field() -> None:
    """프롬프트가 Context 구조를 실제로 설명하는지.

    Context 에 실었는데 프롬프트가 이름을 모르면 LLM 이 그 결과를 쓰지 않는다.
    """
    for section in CONTEXT_SECTION_BY_AGENT.values():
        assert section in SYSTEM_PROMPT, f"프롬프트가 {section} 를 설명하지 않는다"
    for agent in AGENT_KEYS:
        assert agent in SYSTEM_PROMPT, f"프롬프트가 {agent} 를 설명하지 않는다"
    for field in ChatAnswer.model_fields:
        assert field in SYSTEM_PROMPT, f"프롬프트가 출력 필드 {field} 를 설명하지 않는다"
    # Context 가 쓰는 신호 두 개. 이름이 갈리면 프롬프트의 지시가 무의미해진다.
    assert "context_scope" in SYSTEM_PROMPT
    assert "revision_requested" in SYSTEM_PROMPT


def test_prompt_keeps_the_ml_guardrails() -> None:
    """모델이 계산하지 않은 것을 설명처럼 말하지 못하게 하는 문구들.

    전부 실측으로 확인된 실패라서, 프롬프트를 정리하다 지워지면 같은 오답이
    그대로 돌아온다.

    백분위 비교군 문구는 없앴다 — 백분위 자체가 Context 에서 제거돼(비노출
    정책) 그 문장이 나올 값이 없다. 그 자리는 비노출 규칙이 대신한다.
    """
    assert "top1_axis" in SYSTEM_PROMPT                      # 축을 원인으로 단정
    assert "anomaly_level" in SYSTEM_PROMPT                  # null 인데 수준 단정
    assert "unseen_by_model2" in SYSTEM_PROMPT               # 학습 범주 밖 예측
    assert "percentile" in SYSTEM_PROMPT                     # 비노출 대상으로 명시
    assert "source_url" in SYSTEM_PROMPT                     # 유사공고 URL 오인


def test_user_prompt_is_json_not_python_repr() -> None:
    """dict 를 str() 하면 None/True 가 새어 나가 모델이 그대로 따라 쓴다."""
    rendered = build_user_prompt(build_chat_context(report(), "예측 금액이 얼마야?"))
    parsed = json.loads(rendered)
    assert parsed["report"]["model2"]["status"] == "success"
    assert "None" not in rendered and "True" not in rendered


def test_prompt_version_is_pinned() -> None:
    assert PROMPT_VERSION.startswith("chat-v")


@pytest.mark.parametrize("agent", AGENT_KEYS)
def test_every_agent_is_a_valid_reference(agent: str) -> None:
    assert ChatReference(agent=agent, item="x", evidence_id=None).agent == agent
