"""CPL 의미 축 분류 프롬프트. 버전과 문구를 한곳에 둔다.

축은 값이 아니다. 모델은 이미 접지된 fact 에 축 이름과 인용문만 붙이고,
값·오프셋·근거는 프로파일 것을 그대로 쓴다. 서버가 fact_id 존재·인용문
부분문자열·어휘 소속을 검사하고 통과하지 못한 항목은 버린다.

문구를 바꾸면 버전을 올린다. 산출물에 실려 나가므로 어떤 문구로 만든
분류인지 나중에 구분할 수 있어야 한다.
"""

from __future__ import annotations


PURPOSE_AXIS_PROMPT_VERSION = "cpl-purpose-axis-v0.1"


def purpose_axis_instruction(axis_codes: list[str]) -> str:
    """축 어휘를 받아 지시문을 만든다. 어휘가 늘면 문구가 따라간다."""

    return (
        "You classify existing purpose statements into meaning axes. "
        "Return only {fact_id, axis_code, quoted_text} for facts given in the payload. "
        f"axis_code must be one of {sorted(axis_codes)}. "
        "quoted_text must be copied verbatim from that fact's value_raw. "
        "A single fact may carry more than one axis: return one row per axis. "
        "Never invent a fact_id, a new value, an offset, or evidence. "
        "Omit any fact you cannot classify; an empty list is a valid answer."
    )


__all__ = ["PURPOSE_AXIS_PROMPT_VERSION", "purpose_axis_instruction"]
