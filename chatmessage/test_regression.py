"""자연어 회귀 검증 — LLM 을 부르지 않는다.

    python -m pytest chatmessage/test_regression.py

여기서 보는 것은 **라우팅**이다. 사용자가 실제로 할 법한 표현에서 필요한 결과
섹션이 골라지는지, 그리고 필요 없는 섹션까지 끌려오지 않는지.

답변 품질(문장·근거·수정 제안)은 LLM 이 필요해서 `scripts/run_regression.py`
가 본다. 그쪽은 API 키가 있어야 돌고, 이쪽은 항상 돈다 — 프롬프트나 어휘 표를
고쳤을 때 먼저 깨지는 것은 이쪽이어야 한다.
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chatmessage import build_chat_context, context_scope, wants_revision  # noqa: E402
from chatmessage.regression import (  # noqa: E402
    ALL,
    CASES,
    FOLLOW_UP_CHAINS,
    NON_DISCLOSURE_PROBES,
    Case,
)
from test_context import report  # noqa: E402


def ids(cases) -> list[str]:
    return [f"{case.group}-{case.question}" for case in cases]


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_required_sections_are_selected(case: Case) -> None:
    """필요한 섹션이 빠지면 답이 반쪽이 된다."""
    scope = context_scope(case.question)
    missing = case.expected - scope
    assert not missing, f"빠진 섹션 {sorted(missing)} — 실제 {sorted(scope)}"


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_scope_does_not_widen_needlessly(case: Case) -> None:
    """한 섹션을 물으면 리포트 전체가 딸려 오지 않아야 한다.

    넓어지는 것은 오답은 아니지만 매 질문마다 리포트 전체가 프롬프트에 실린다.
    """
    scope = context_scope(case.question)
    extra = case.forbidden & scope
    assert not extra, f"불필요한 섹션 {sorted(extra)} — 실제 {sorted(scope)}"


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.revision], ids=ids([c for c in CASES if c.revision])
)
def test_revision_requests_are_detected(case: Case) -> None:
    assert wants_revision(case.question)


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if not c.revision],
    ids=ids([c for c in CASES if not c.revision]),
)
def test_plain_questions_do_not_ask_for_a_revision(case: Case) -> None:
    """묻지도 않았는데 수정안을 붙이면 사용자가 확정된 조치로 읽는다."""
    assert not wants_revision(case.question)


@pytest.mark.parametrize("chain", FOLLOW_UP_CHAINS, ids=[c[0] for c in FOLLOW_UP_CHAINS])
def test_follow_up_questions_do_not_widen_the_scope(chain: tuple[str, ...]) -> None:
    """후속 질문이 리포트 전체를 다시 끌어오면 안 된다.

    같은 범위를 그대로 유지하거나 더 좁아지는 것은 둘 다 맞다. 후속 질문이
    스스로 범위를 말하면("차이점만", "첫 번째 항목만") 그쪽이 더 정확하다.
    막으려는 것은 **넓어지는 것** 이다 — 매 후속 질문마다 리포트 전체가
    프롬프트에 다시 실리는 상황.
    """
    first = context_scope(chain[0])
    conversation: list[dict[str, str]] = []
    for question in chain:
        context = build_chat_context(report(), question, conversation=conversation)
        scope = set(context["context_scope"])
        assert scope, f"{question!r} 에서 범위가 비었다"
        assert scope <= first, (
            f"{question!r} 에서 범위가 넓어졌다: {sorted(first)} → {sorted(scope)}"
        )
        conversation.append({"role": "USER", "content": question})
        conversation.append({"role": "ASSISTANT", "content": "..."})


@pytest.mark.parametrize("question", NON_DISCLOSURE_PROBES)
def test_probe_questions_never_load_internal_values(question: str) -> None:
    """내부값을 캐묻는 질문이어도 Context 에 그 값이 실리지 않는다.

    Context 에 없으면 LLM 이 말할 값 자체가 없다. 표현으로 추측하는 것은
    grounding 2차 검사가 잡는다.
    """
    import json

    from chatmessage.provenance import INTERNAL_VALUE_KEYS

    value = report()
    value["models"]["model_2"]["result"]["percentile"] = 75.2
    value["models"]["model_1"]["result"]["confidence"] = 0.82
    rendered = json.dumps(build_chat_context(value, question), ensure_ascii=False)
    for marker in INTERNAL_VALUE_KEYS:
        assert marker not in rendered, f"{question!r} 에서 {marker} 가 실렸다"


def test_every_group_is_covered() -> None:
    """질문 세트가 한쪽으로 쏠리지 않게 한다."""
    groups = {case.group for case in CASES}
    assert len(groups) >= 6, f"영역이 모자라다: {sorted(groups)}"
    assert len(CASES) >= 20, f"질문이 {len(CASES)}개뿐이다 (20~30개 권장)"


def test_summary_questions_read_everything() -> None:
    for case in CASES:
        if case.expected == ALL:
            assert context_scope(case.question) == ALL, case.question
