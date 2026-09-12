"""의미 축을 CPL 이 확정한다는 것을 고정한다.

축은 값이 아니다. 모델은 원문 구역에서 축 이름과 인용문을 고르고, 서버가
요청한 ``evidence_ref`` 인지·어휘에 있는 축인지·인용문이 그 구역의
부분문자열인지 검사한다. 통과하지 못한 행은 버린다.

축을 판정 시점(FIT)이 아니라 확정 시점(CPL)에 붙이는 이유는, 같은 원문을
소비하는 두 단계가 각자 다시 해석하면 화면과 판정이 갈라지기 때문이다.

계약 정정 (260912): 축을 1 차 fact 에 ``replace()`` 로 덧붙이던 구현을 버렸다.
구조화가 구역의 일부만 값으로 고르는 것이 관측됐고 (mockup_08 사업목적은 구역
73 자 중 21 자만 값이 된 실행이 30 회 중 16 회), 그 상태에서 축 인용문이 값
밖에서 나오면 FIT 은 ``[9,27)`` 문구를 쓰면서 근거 id 와 span 은 ``[61,82)`` 를
가리킨다. 이제 축마다 인용 구간을 가리키는 fact 를 새로 만든다.

그래서 "한 원문이면 근거 한 줄" 도 함께 정정됐다. 그 규칙은 축들이 값과 좌표를
공유하던 구현의 전제였다. 축마다 실제 span 이 다른 지금 ``[9,27)`` 과
``[29,38)`` 은 중복이 아니며, 한 줄로 합치면 어느 span 을 대표로 삼을지 임의
결정이 생겨 Evidence 정확성을 다시 잃는다.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel
import pytest

from worker.contracts.cpl_result import PURPOSE_AXIS_UNRESOLVED, CplAxisCode
from worker.contracts.profile_snapshot import LLM_UNAVAILABLE
from worker.cpl import analyze_cpl, build_cpl_result
from worker.cpl_prompt import PURPOSE_AXIS_PROMPT_VERSION


_PURPOSE = "comparison_profile.purpose_goal"
_VALUE = "부산 관내 제조 중소기업의 기술경쟁력을 강화하여 매출 성장을 달성"
_LABEL = "○ (사업목적) "
_REGION = _LABEL + _VALUE


def _ir() -> dict[str, Any]:
    return {
        "document": {"document_id": "hwpx:d1"},
        "blocks": [{
            "block_id": "hwpx:t4",
            "occurrences": [{"occurrence_id": "occ:p0", "text": _REGION}],
        }],
    }


def _ref() -> str:
    from worker.cpl_coverage import build_fragments

    return build_fragments(_ir(), profile_field=_PURPOSE)[0].evidence_ref


def _profile() -> dict[str, Any]:
    """운영에서 오는 모양. 값에 Common IR occurrence 근거가 달려 있다."""

    return {
        "comparison_profile": {
            "purpose_goal": [{
                "fact_id": "fact_1", "value_raw": _VALUE, "status": "identified",
                "evidence": [{
                    "source_block_id": "hwpx:t4",
                    "common_ir_document_id": "hwpx:d1",
                    "common_ir_block_id": "hwpx:t4",
                    "common_ir_occurrence_ids": ["occ:p0"],
                }],
            }]
        },
        "field_states": [{"field_name": "purpose_goal", "status": "identified"}],
    }


def _ungrounded_profile() -> dict[str, Any]:
    """근거 없는 값. 구역을 짚을 수 없으므로 축을 물을 수 없다."""

    return {
        "comparison_profile": {
            "purpose_goal": [
                {"fact_id": "fact_1", "value_raw": _VALUE, "status": "identified"}
            ]
        },
        "field_states": [{"field_name": "purpose_goal", "status": "identified"}],
    }


class _Llm:
    """포트 계약대로 response_schema 인스턴스를 돌려준다."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    async def generate_structured(
        self, *, task_name: str, messages: list[Any],
        response_schema: type[BaseModel], model_profile: str,
    ) -> BaseModel:
        return response_schema(assignments=self._rows)


class _Dead:
    async def generate_structured(self, **_: Any) -> BaseModel:
        raise RuntimeError("포트 밖 예외")


def _purpose_facts(result) -> list[Any]:
    return [
        fact
        for item in result.items
        for subfield in item.subfields
        if subfield.profile_field == _PURPOSE
        for fact in subfield.facts
    ]


def _row(axis: str, quoted: str, ref: str | None = None) -> dict[str, str]:
    return {
        "evidence_ref": ref or _ref(), "axis_code": axis, "quoted_text": quoted,
    }


def _run(profile: dict[str, Any], llm: Any) -> Any:
    return analyze_cpl(profile, llm, model_profile="default", common_ir=_ir())


# 한 원문이 축 둘을 가지면 축마다 fact 를 남긴다. 축이 단수라야 소비 쪽
# 필터가 (필드, 축) 한 쌍으로 끝난다.
def test_one_statement_carrying_two_axes_is_kept_as_two_facts() -> None:
    llm = _Llm([
        _row(CplAxisCode.TARGET_CONDITION.value, "부산 관내 제조 중소기업"),
        _row(CplAxisCode.DIRECTION.value, "기술경쟁력을 강화"),
    ])

    facts = _purpose_facts(_run(_profile(), llm))

    assert [fact.axis_code for fact in facts] == [
        CplAxisCode.TARGET_CONDITION.value,
        CplAxisCode.DIRECTION.value,
    ]
    # 값은 축이 실제로 가리키는 구간이다. 문장 전체가 아니다.
    assert [fact.value_raw for fact in facts] == [
        "부산 관내 제조 중소기업", "기술경쟁력을 강화"
    ]
    # 구조화가 만든 값이 아니므로 그 id 를 물려받지 않는다. 접지는
    # ``evidence_ref`` 가 한다.
    assert {fact.fact_id for fact in facts} == {None}
    assert {fact.evidence_ref for fact in facts} == {_ref()}
    # 좌표는 구역 안에서 다시 찾은 실제 인용 위치다.
    assert [(fact.start_char, fact.end_char) for fact in facts] == [
        (_REGION.index("부산"), _REGION.index("부산") + len("부산 관내 제조 중소기업")),
        (_REGION.index("기술"), _REGION.index("기술") + len("기술경쟁력을 강화")),
    ]
    assert [_REGION[f.start_char : f.end_char] for f in facts] == [
        fact.value_raw for fact in facts
    ]


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_row("PURPOSE_DIRECTION", "매출 성장을 달성", "없는_참조"), "모르는 evidence_ref"),
        (_row("어휘_밖의_축", "매출 성장을 달성"), "어휘 밖 axis_code"),
        (_row("PURPOSE_DIRECTION", "원문에 없는 인용문"), "부분문자열 아님"),
        (_row("PURPOSE_DIRECTION", ""), "빈 인용문"),
    ],
)
def test_server_drops_assignments_it_cannot_verify(row: dict[str, str], reason: str) -> None:
    result = _run(_profile(), _Llm([row]))

    # 축이 하나도 구체화되지 않았으므로 1 차 값이 축 없이 그대로 남는다.
    assert [fact.value_raw for fact in _purpose_facts(result)] == [_VALUE], reason
    assert _purpose_facts(result)[0].axis_code is None, reason
    assert result.purpose_axis.assignments == []
    assert result.purpose_axis.reason_code == PURPOSE_AXIS_UNRESOLVED


# 축을 못 받아도 값·근거·상태는 그대로다. 축은 값이 아니다.
def test_a_dead_model_never_erases_the_rule_result() -> None:
    deterministic = build_cpl_result(_profile())
    result = _run(_profile(), _Dead())

    assert result.purpose_axis.reason_code == LLM_UNAVAILABLE
    assert len(result.items) == len(deterministic.items) == 13
    assert [fact.value_raw for fact in _purpose_facts(result)] == [_VALUE]
    assert _purpose_facts(result)[0].axis_code is None


def test_the_deterministic_builder_stays_free_of_the_model() -> None:
    result = build_cpl_result(_profile())

    assert result.purpose_axis.attempted is False
    assert result.purpose_axis.prompt_version is None
    assert _purpose_facts(result)[0].axis_code is None


def test_the_prompt_version_travels_with_the_classification() -> None:
    llm = _Llm([_row(CplAxisCode.DIRECTION.value, "매출 성장을 달성")])

    result = _run(_profile(), llm)

    assert result.purpose_axis.prompt_version == PURPOSE_AXIS_PROMPT_VERSION


# 계약 정정 (260912). 예전 규칙은 "한 원문이면 근거 한 줄" 이었다. 그것은 축들이
# 값과 좌표를 공유하던 구현의 전제였고, 같은 값·같은 좌표를 축 수만큼 저장하는
# 것을 막으려는 것이었다.
#
# 이제 축마다 인용 구간이 실제로 다르다. ``[9,27)`` 대상 조건과 ``[29,38)`` 방향은
# 서로 다른 원문 구간이므로 중복이 아니다. 한 줄로 합치려면 어느 span 을 대표로
# 삼을지 임의로 정해야 하고, 그 순간 Evidence 정확성을 다시 잃는다.
def test_each_axis_keeps_its_own_evidence_row() -> None:
    from worker.contracts.fit_result import FitResult
    from worker.contracts.sim_result import SimComparisonResult
    from worker.result_payload import build_result_payload

    def rows(result) -> list[dict[str, Any]]:
        payload = build_result_payload(
            profile={},
            cpl=result,
            fit=FitResult(
                relations=[], purpose_axis=None, profile_id=None,
                common_ir_document_id=None, model_profile="default",
                ruleset_version="x", prompt_version="x",
            ),
            sim=SimComparisonResult(
                request_profile_id=None, candidates=[], model_profile="default",
                ruleset_version="x", prompt_version="x", scoring_version="x",
            ),
            sim_profiles={},
            retrieval_similarities={},
            profile_version_ids={},
        )
        return [
            row for row in payload["evidences"] if row["field_name"] == _PURPOSE
        ]

    llm = _Llm([
        _row(CplAxisCode.TARGET_CONDITION.value, "부산 관내 제조 중소기업"),
        _row(CplAxisCode.DIRECTION.value, "기술경쟁력을 강화"),
    ])
    with_axes = _run(_profile(), llm)

    assert len(_purpose_facts(with_axes)) == 2
    assert [row["raw_value"] for row in rows(with_axes)] == [
        "부산 관내 제조 중소기업", "기술경쟁력을 강화"
    ]
    # 같은 축이 같은 구간을 두 번 주면 그건 여전히 한 줄이다.
    twice = _run(_profile(), _Llm([
        _row(CplAxisCode.DIRECTION.value, "기술경쟁력을 강화"),
        _row(CplAxisCode.DIRECTION.value, "기술경쟁력을 강화"),
    ]))
    assert len(rows(twice)) == 1


# 축 중복 제거 키가 fact_id 만 보면 안 된다. delivery_relations 멤버는 자기
# id 가 없어서 (relation_id, member, member_index) 가 자리를 가리키는데, 같은
# 기관이 여러 relation 의 actor 로 나오면 값까지 같아진다. 실문서(sol4)에
# 실제로 있는 모양이다. 그 둘을 접으면 근거 한 줄이 조용히 사라진다.
def test_dedupe_never_folds_two_delivery_members_that_share_a_name() -> None:
    profile: dict[str, Any] = {
        "comparison_profile": {
            "delivery_relations": [
                {
                    "delivery_relation_id": "d1",
                    "actor": {"value_raw": "부산테크노파크", "status": "identified"},
                    "actions": [{"value_raw": "평가", "status": "identified"}],
                },
                {
                    "delivery_relation_id": "d2",
                    "actor": {"value_raw": "부산테크노파크", "status": "identified"},
                    "actions": [{"value_raw": "평가", "status": "identified"}],
                },
            ]
        },
        "field_states": [{"field_name": "delivery_relations", "status": "identified"}],
    }

    result = build_cpl_result(profile)
    facts = [
        fact
        for item in result.items
        for subfield in item.subfields
        if subfield.profile_field.endswith("delivery_relations")
        for fact in subfield.facts
    ]

    # 같은 이름이지만 서로 다른 relation 이라 좌표가 다르다.
    assert [fact.fact_id for fact in facts] == [None] * len(facts)
    keys = {
        (
            fact.fact_id, fact.relation_id, fact.member, fact.member_index,
            fact.source_block_id, fact.start_char, fact.end_char, fact.value_raw,
        )
        for fact in facts
    }
    assert len(keys) == len(facts)



# ---------------------------------------------------- fact_id 의 좌표 범위

# fact_id 는 프로필 내부 좌표다. 저장된 19개 프로필에서 110개 id 가 여러
# 프로필에 재사용되고, 그중 104개는 프로필마다 다른 span 을 가리킨다. 참조와
# 감사 기록에서는 (profile_id, fact_id) 로 구분해야 하며 bare fact_id 를 전역
# 키로 쓰면 서로 다른 근거가 같은 것으로 보인다.


def _two_profiles() -> tuple[dict[str, Any], dict[str, Any]]:
    def profile(profile_id: str, value: str, start: int) -> dict[str, Any]:
        return {
            "profile_id": profile_id,
            "comparison_profile": {
                "purpose_goal": [{
                    "fact_id": "fact_1", "value_raw": value, "status": "identified",
                    "value_source": {
                        "source_block_id": "blk", "start_char": start,
                        "end_char": start + len(value),
                    },
                }]
            },
            "field_states": [{"field_name": "purpose_goal", "status": "identified"}],
        }

    return profile("request:a", "가 사업의 목적", 0), profile("request:b", "나 사업의 목적", 40)


def test_one_profile_never_maps_a_fact_id_to_two_spans() -> None:
    left, _ = _two_profiles()

    spans = {
        (fact.source_block_id, fact.start_char, fact.end_char)
        for fact in _purpose_facts(build_cpl_result(left))
        if fact.fact_id == "fact_1"
    }

    assert len(spans) == 1


def test_the_same_fact_id_in_another_profile_is_a_different_fact() -> None:
    left, right = _two_profiles()

    a = _purpose_facts(build_cpl_result(left))[0]
    b = _purpose_facts(build_cpl_result(right))[0]

    assert a.fact_id == b.fact_id == "fact_1"          # 재사용은 정상이다
    assert (a.start_char, a.value_raw) != (b.start_char, b.value_raw)
    # 구분은 프로필 id 와 함께여야 성립한다.
    assert (left["profile_id"], a.fact_id) != (right["profile_id"], b.fact_id)


# 축 분류는 프로필 하나만 받는다. 여러 프로필의 fact 를 한 요청에 섞으면
# fact_id 가 무엇을 가리키는지 말할 수 없다.
def test_the_classifier_resolves_fact_ids_inside_one_profile_only() -> None:
    import inspect

    from worker.cpl import analyze_cpl as subject

    parameters = list(inspect.signature(subject).parameters)

    assert parameters[0] == "profile"
    assert "profiles" not in parameters


# ------------------------------------------------------------ 프롬프트 계약


def test_the_prompt_version_and_content_hash_travel_together() -> None:
    from worker.cpl_prompt import load_purpose_axis_prompt

    prompt = load_purpose_axis_prompt()
    llm = _Llm([_row(CplAxisCode.DIRECTION.value, "매출 성장을 달성")])

    record = _run(_profile(), llm).purpose_axis

    assert record.prompt_version == prompt.version == "cpl-purpose-axis-v0.3"
    assert record.prompt_sha256 == prompt.sha256


# 문구를 못 읽으면 기본값으로 대체하지 않는다. 어떤 문구로 만든 분류인지 말할
# 수 없는 결과를 내느니 축을 비운다. 값·근거·상태는 그대로다.
def test_an_unreadable_prompt_empties_the_axes_without_touching_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    from worker.contracts.cpl_result import PROMPT_UNAVAILABLE
    from worker.cpl_prompt import PURPOSE_AXIS_PROMPT_ENV

    monkeypatch.setenv(PURPOSE_AXIS_PROMPT_ENV, str(tmp_path / "없는파일.txt"))
    llm = _Llm([_row(CplAxisCode.DIRECTION.value, "매출 성장을 달성")])

    result = _run(_profile(), llm)

    assert result.purpose_axis.reason_code == PROMPT_UNAVAILABLE
    assert result.purpose_axis.assignments == []
    assert [fact.value_raw for fact in _purpose_facts(result)] == [_VALUE]
    assert len(result.items) == 13


def test_all_four_contract_axes_exist() -> None:
    assert [code.value for code in CplAxisCode] == [
        "PURPOSE_TARGET_CONDITION",
        "PURPOSE_PROBLEM_DOMAIN",
        "PURPOSE_SPECIFIC_OBJECTIVE",
        "PURPOSE_DIRECTION",
    ]


# --- 축 분류가 보는 범위 ----------------------------------------------------
# 구조화가 구역의 일부만 값으로 고르는 것이 관측됐다. 그 잘린 문자열만 주면
# 모델이 고를 수 있는 축이 남은 절에 갇힌다. 축은 값이 아니라 구역에서 고른다.

_PARTIAL_REGION = (
    "○ (사업목적) 부산 관내 제조 중소기업의 기술경쟁력을 강화하고 "
    "지원기업 120개사의 매출 성장을 달성"
)
# 1 차가 꼬리 절만 값으로 골랐다.
_TAIL = "지원기업 120개사의 매출 성장을 달성"
_HEAD = "부산 관내 제조 중소기업"


def _partial_ir() -> dict[str, Any]:
    return {
        "document": {"document_id": "hwpx:d9"},
        "blocks": [{
            "block_id": "hwpx:t4",
            "occurrences": [{"occurrence_id": "occ:p0", "text": _PARTIAL_REGION}],
        }],
    }


def _partial_profile() -> dict[str, Any]:
    return {
        "comparison_profile": {"purpose_goal": [{
            "fact_id": "fact_1",
            "value_raw": _TAIL,
            "value_source": {
                "source_block_id": "hwpx:t4",
                "start_char": _PARTIAL_REGION.index(_TAIL),
                "end_char": len(_PARTIAL_REGION),
            },
            "evidence": [{
                "source_block_id": "hwpx:t4",
                "common_ir_document_id": "hwpx:d9",
                "common_ir_block_id": "hwpx:t4",
                "common_ir_occurrence_ids": ["occ:p0"],
            }],
        }]},
        "field_states": [{"field_name": "purpose_goal", "status": "identified"}],
    }


def _partial_ref() -> str:
    from worker.cpl_coverage import build_fragments

    return build_fragments(_partial_ir(), profile_field=_PURPOSE)[0].evidence_ref


class _Capture:
    """응답은 고정하고 payload 만 본다."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.payloads: list[Any] = []

    async def generate_structured(
        self, *, task_name: str, messages: list[Any],
        response_schema: type[BaseModel], model_profile: str,
    ) -> BaseModel:
        self.payloads.append(json.loads(messages[-1].content))
        return response_schema(assignments=self.rows)


def _run_partial(llm: Any) -> Any:
    return analyze_cpl(
        _partial_profile(), llm, model_profile="default", common_ir=_partial_ir()
    )


def test_the_axis_call_sees_the_whole_region_not_only_the_selected_value() -> None:
    llm = _Capture([])

    _run_partial(llm)

    (sent,) = llm.payloads[0]["purpose_regions"]
    assert sent["evidence_ref"] == _partial_ref()
    # 앞절이 잘려 나가지 않는다. 대상과 방향이 여기 있다.
    assert sent["raw_text"] == _PARTIAL_REGION
    assert _HEAD in sent["raw_text"]


def test_a_quote_from_outside_the_selected_value_becomes_its_own_fact() -> None:
    llm = _Capture([{
        "evidence_ref": _partial_ref(),
        "axis_code": CplAxisCode.TARGET_CONDITION.value,
        "quoted_text": _HEAD,
    }])

    facts = _purpose_facts(_run_partial(llm))

    # 1 차 값은 꼬리 절이었지만 축은 구역 앞절에서 나온다.
    assert [(f.axis_code, f.value_raw) for f in facts] == [
        (CplAxisCode.TARGET_CONDITION.value, _HEAD)
    ]
    # 좌표가 잘린 값의 것을 물려받지 않는다. 실제 인용 위치다.
    start = _PARTIAL_REGION.index(_HEAD)
    assert (facts[0].start_char, facts[0].end_char) == (start, start + len(_HEAD))
    assert _PARTIAL_REGION[facts[0].start_char : facts[0].end_char] == _HEAD
    assert facts[0].evidence_ref == _partial_ref()


def test_a_quote_from_outside_the_region_is_still_dropped() -> None:
    llm = _Capture([{
        "evidence_ref": _partial_ref(),
        "axis_code": CplAxisCode.DIRECTION.value,
        "quoted_text": "원문에 없는 말",
    }])

    result = _run_partial(llm)

    assert result.purpose_axis.dropped == [_partial_ref()]
    # 축이 없으니 1 차 값이 그대로 남는다.
    assert [f.value_raw for f in _purpose_facts(result)] == [_TAIL]


# --- 근거 없는 값 -----------------------------------------------------------
# 구역을 짚을 수 없으면 축을 묻지 않는다. ``value_raw`` 로 대신 물으면 좌표
# 없는 축이 다시 생긴다.


def test_a_value_without_grounding_is_never_classified() -> None:
    llm = _Capture([_row(CplAxisCode.TARGET_CONDITION.value, _HEAD)])

    result = analyze_cpl(
        _ungrounded_profile(), llm, model_profile="default", common_ir=_ir()
    )

    assert llm.payloads == []  # 호출 자체가 없다
    assert result.purpose_axis.attempted is False
    assert result.purpose_axis.reason_code == PURPOSE_AXIS_UNRESOLVED
    # 값은 그대로 살아 있고 축만 없다.
    assert [(f.fact_id, f.value_raw, f.axis_code) for f in _purpose_facts(result)] == [
        ("fact_1", _VALUE, None)
    ]
