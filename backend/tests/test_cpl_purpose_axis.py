"""의미 축을 CPL 이 확정한다는 것을 고정한다.

축은 값이 아니다. 모델은 이미 접지된 fact 에 축 이름과 인용문만 붙이고, 서버가
fact_id 존재·어휘 소속·인용문 부분문자열을 검사한다. 통과하지 못한 행은 버린다.

축을 판정 시점(FIT)이 아니라 확정 시점(CPL)에 붙이는 이유는, 같은 원문을
소비하는 두 단계가 각자 다시 해석하면 화면과 판정이 갈라지기 때문이다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
import pytest

from worker.contracts.cpl_result import PURPOSE_AXIS_UNRESOLVED, CplAxisCode
from worker.contracts.profile_snapshot import LLM_UNAVAILABLE
from worker.cpl import analyze_cpl, build_cpl_result
from worker.cpl_prompt import PURPOSE_AXIS_PROMPT_VERSION


_PURPOSE = "comparison_profile.purpose_goal"
_VALUE = "부산 관내 제조 중소기업의 기술경쟁력을 강화하여 매출 성장을 달성"


def _profile() -> dict[str, Any]:
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


def _row(axis: str, quoted: str, fact_id: str = "fact_1") -> dict[str, str]:
    return {"fact_id": fact_id, "axis_code": axis, "quoted_text": quoted}


# 한 원문이 축 둘을 가지면 축마다 fact 를 남긴다. 축이 단수라야 소비 쪽
# 필터가 (필드, 축) 한 쌍으로 끝난다.
def test_one_statement_carrying_two_axes_is_kept_as_two_facts() -> None:
    llm = _Llm([
        _row(CplAxisCode.TARGET_CONDITION.value, "부산 관내 제조 중소기업"),
        _row(CplAxisCode.DIRECTION.value, "매출 성장을 달성"),
    ])

    facts = _purpose_facts(analyze_cpl(_profile(), llm, model_profile="default"))

    assert [fact.axis_code for fact in facts] == [
        CplAxisCode.TARGET_CONDITION.value,
        CplAxisCode.DIRECTION.value,
    ]
    # 값·근거는 새로 만들지 않는다. 같은 원문이 축만 다르게 두 번 실린다.
    assert {fact.value_raw for fact in facts} == {_VALUE}
    assert {fact.fact_id for fact in facts} == {"fact_1"}


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_row("PURPOSE_DIRECTION", "매출 성장을 달성", "없는_fact"), "모르는 fact_id"),
        (_row("어휘_밖의_축", "매출 성장을 달성"), "어휘 밖 axis_code"),
        (_row("PURPOSE_DIRECTION", "원문에 없는 인용문"), "부분문자열 아님"),
        (_row("PURPOSE_DIRECTION", ""), "빈 인용문"),
    ],
)
def test_server_drops_assignments_it_cannot_verify(row: dict[str, str], reason: str) -> None:
    result = analyze_cpl(_profile(), _Llm([row]), model_profile="default")

    assert _purpose_facts(result)[0].axis_code is None, reason
    assert result.purpose_axis.assignments == []
    assert result.purpose_axis.reason_code == PURPOSE_AXIS_UNRESOLVED


# 축을 못 받아도 값·근거·상태는 그대로다. 축은 값이 아니다.
def test_a_dead_model_never_erases_the_rule_result() -> None:
    deterministic = build_cpl_result(_profile())
    result = analyze_cpl(_profile(), _Dead(), model_profile="default")

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

    result = analyze_cpl(_profile(), llm, model_profile="default")

    assert result.purpose_axis.prompt_version == PURPOSE_AXIS_PROMPT_VERSION


# 축 때문에 근거가 불어나면 프론트 근거 목록과 DB Evidence 행이 축 수만큼
# 늘어난다. 같은 값·같은 좌표는 축이 몇 개든 근거 한 줄이다.
def test_axis_split_does_not_multiply_public_evidence_rows() -> None:
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
        _row(CplAxisCode.DIRECTION.value, "매출 성장을 달성"),
    ])
    with_axes = analyze_cpl(_profile(), llm, model_profile="default")

    assert len(_purpose_facts(with_axes)) == 2  # 축별로 fact 는 둘
    assert len(rows(with_axes)) == len(rows(build_cpl_result(_profile())))  # 근거는 하나


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

    record = analyze_cpl(_profile(), llm, model_profile="default").purpose_axis

    assert record.prompt_version == prompt.version == "cpl-purpose-axis-v0.2"
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

    result = analyze_cpl(_profile(), llm, model_profile="default")

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
