"""Profile → CPL 13항목 연결의 명시적 매핑 (초안 §4.1, §6).

매핑은 코드가 아니라 표다. 여기 표에 없는 연결은 존재하지 않는 연결이고,
표에 있는데 프로파일에 없는 컨테이너는 "값 없음" 으로 남는다. 어느 쪽도
다른 필드에서 값을 끌어와 메우지 않는다 (초안 §6.1).

경로는 점 표기 한 단계까지만 쓴다. ponytail: 일반 경로 표현식 엔진을
만들 이유가 없다. 표가 13줄이고 깊이가 2단계를 넘지 않는다.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .contracts.cpl_result import CplEvidence, CplFact, CplFieldCode


# 초안 §6 의 연결표. 값은 프로파일 안의 점 표기 경로다.
#
# NEW_OR_CHANGED_CONTENT 는 대응 필드가 없다. 빈 튜플이 그 사실 자체다.
# 억지로 근사 필드를 붙이면 "변경내용" 근거가 없는데 있는 것처럼 보인다.
#
# BUSINESS_PERIOD 는 program_period 만 받는다. support_period 는 초안 §6 이
# "program_period 와 구분" 이라고 명시해 두었으므로 매핑하지 않고,
# 대신 unmapped_profile_fields 로 보고된다. 조용히 버리지 않는다.
CPL_FIELD_SOURCES: dict[CplFieldCode, tuple[str, ...]] = {
    CplFieldCode.REQUEST_TYPE: ("request_type",),
    CplFieldCode.PURPOSE_GOAL: ("comparison_profile.purpose_goal",),
    CplFieldCode.IMPLEMENTATION_PLAN: (
        "request_context.implementation_plan",
        "program_hierarchy.nodes",
        "support_components",
    ),
    CplFieldCode.BUSINESS_PERIOD: ("comparison_profile.program_period",),
    CplFieldCode.NEW_OR_CHANGED_CONTENT: (),
    CplFieldCode.BUSINESS_NEED: ("request_context.business_need",),
    CplFieldCode.LEGAL_BASIS: ("request_context.legal_basis",),
    CplFieldCode.LINKED_POLICY: ("request_context.linked_policy",),
    CplFieldCode.BUDGET: ("comparison_profile.total_budget",),
    CplFieldCode.TARGET_AND_CONDITIONS: (
        "comparison_profile.applicant_eligibility",
        "comparison_profile.support_target",
        "comparison_profile.beneficiary",
        "comparison_profile.eligibility_conditions",
        "comparison_profile.exclusions",
        "comparison_profile.participation_requirements",
    ),
    CplFieldCode.SUPPORT_CONTENT_AND_SCALE: (
        "comparison_profile.support_activities",
        "comparison_profile.support_methods",
        "comparison_profile.support_items",
        "comparison_profile.support_content",
        "comparison_profile.support_scale",
        "comparison_profile.cost_sharing",
    ),
    CplFieldCode.DELIVERY_SYSTEM: (
        "comparison_profile.delivery_relations",
        "comparison_profile.delivery_methods",
    ),
    CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE: (
        "request_context.expected_effect",
        "request_context.performance_indicator",
    ),
}

# import 시점에 13항목을 정확히 한 번씩 덮는지 고정한다. 항목이 늘거나
# 이름이 바뀌면 여기서 즉시 터진다.
assert tuple(CPL_FIELD_SOURCES) == tuple(CplFieldCode), (
    "CPL_FIELD_SOURCES 는 CplFieldCode 13항목을 선언 순서대로 정확히 한 번씩 "
    "가져야 한다"
)


# 컨테이너 항목이 자기 id 로 쓰는 키. fact_id 가 우선이고, 없으면 순서대로 본다.
_ID_KEYS = ("fact_id", "program_node_id", "support_component_id", "delivery_relation_id")
# 표시용 원문 값이 실려 있는 키. 둘 다 없으면 값은 None 으로 남긴다.
_VALUE_KEYS = ("value_raw", "name_raw")


def field_name_of(path: str) -> str:
    """점 표기 경로의 마지막 마디. field_states 대조는 이 이름으로 한다."""

    return path.rsplit(".", 1)[-1]


def read_path(profile: dict[str, Any], path: str) -> Any:
    """점 표기 경로를 안전하게 읽는다. 중간이 없거나 dict 가 아니면 None."""

    node: Any = profile
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _evidence(entry: dict[str, Any]) -> list[CplEvidence]:
    """근거 목록을 정규화한다.

    delivery_relations 는 항목 단위 ``evidence`` 대신 ``relation_container``
    한 개에 Common IR 접지를 담는다. 모양이 같으므로 그대로 옮긴다.
    """

    rows = entry.get("evidence")
    if not rows:
        container = entry.get("relation_container")
        rows = [container] if isinstance(container, dict) else []
    return [
        CplEvidence(
            source_block_id=row.get("source_block_id"),
            common_ir_document_id=row.get("common_ir_document_id"),
            common_ir_block_id=row.get("common_ir_block_id"),
            common_ir_occurrence_ids=list(row.get("common_ir_occurrence_ids") or []),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def normalise_fact(entry: dict[str, Any]) -> CplFact:
    """Fact·node·component·relation 한 건을 CplFact 로 옮긴다.

    없는 키는 None 으로 남긴다. 형제 필드나 다른 항목에서 값을 채우지 않는다.
    """

    id_key = next((key for key in _ID_KEYS if entry.get(key)), None)
    value_key = next((key for key in _VALUE_KEYS if entry.get(key) is not None), None)
    source = entry.get("value_source") or {}
    selection = entry.get("selection_source") or {}
    return CplFact(
        fact_id=entry.get(id_key) if id_key else None,
        value_raw=entry.get(value_key) if value_key else None,
        status=entry.get("status"),
        source_block_id=source.get("source_block_id"),
        start_char=source.get("start_char"),
        end_char=source.get("end_char"),
        text_basis=source.get("text_basis"),
        evidence=_evidence(entry),
        program_node_id=entry.get("program_node_id"),
        primary_component_id=entry.get("primary_component_id"),
        id_source_key=id_key,
        selection_glyph_raw=selection.get("glyph_raw"),
    )


# delivery_relations 항목만 모양이 다르다. 값이 최상위가 아니라 actor·role·
# actions 안에 있어서, relation 을 한 줄로 누르면 ``value_raw`` 가 None 이 되고
# 수행기관·역할 원문이 통째로 사라진다. 멤버마다 한 줄로 편다.
_RELATION_ID_KEY = "delivery_relation_id"
_RELATION_MEMBERS = ("actor", "role")


def _relation_facts(entry: dict[str, Any]) -> list[CplFact]:
    """relation 한 건을 멤버 단위로 편다.

    멤버에게 없는 ``fact_id`` 를 지어내지 않고 (relation_id, member) 좌표만
    남긴다. actor 를 대표값으로 올리지도 않는다. 프로파일이 정하지 않은 대표를
    코드가 만들어내는 셈이기 때문이다 (초안 §6.1 과 같은 이유).
    """

    container = entry.get("relation_container")
    actions = entry.get("actions")
    members: list[tuple[str, int | None, Any]] = [
        (name, None, entry.get(name)) for name in _RELATION_MEMBERS
    ]
    members += [
        ("action", index, row)
        for index, row in enumerate(actions if isinstance(actions, list) else [])
    ]

    facts = [
        replace(
            # relation_container 는 evidence 와 같은 모양이라, 멤버 자신의
            # 접지가 없을 때 relation 단위 접지가 그대로 남는다.
            normalise_fact({**node, "relation_container": container}),
            relation_id=entry.get(_RELATION_ID_KEY),
            member=member,
            member_index=member_index,
        )
        for member, member_index, node in members
        if isinstance(node, dict)
    ]
    # 멤버가 하나도 없는 relation 은 id 와 접지만 남은 한 줄로 둔다. 조용히
    # 사라지게 하지 않는다.
    return facts or [normalise_fact(entry)]


def facts_at(profile: dict[str, Any], path: str) -> list[CplFact]:
    """경로가 가리키는 컨테이너를 CplFact 목록으로 편다.

    request_type 은 리스트가 아니라 객체 하나다(체크박스로 서버가 정한 값).
    없는 컨테이너는 빈 목록이다. 예외를 던지지 않는다.
    """

    node = read_path(profile, path)
    if isinstance(node, dict):
        return [normalise_fact(node)]
    if isinstance(node, list):
        facts: list[CplFact] = []
        for row in node:
            if not isinstance(row, dict):
                continue
            if _RELATION_ID_KEY in row:
                facts.extend(_relation_facts(row))
            else:
                facts.append(normalise_fact(row))
        return facts
    return []


def field_states_by_name(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``field_states`` 를 field_name 으로 색인한다."""

    rows = profile.get("field_states")
    if not isinstance(rows, list):
        return {}
    return {
        row["field_name"]: row
        for row in rows
        if isinstance(row, dict) and row.get("field_name")
    }


def mapped_field_names() -> set[str]:
    """매핑이 참조하는 모든 경로의 마지막 마디."""

    return {
        field_name_of(path)
        for paths in CPL_FIELD_SOURCES.values()
        for path in paths
    }


def unmapped_profile_fields(profile: dict[str, Any]) -> list[str]:
    """어떤 CPL 항목도 참조하지 않는 field_states 이름 (초안 §6).

    support_period 처럼 의도적으로 매핑하지 않은 필드가 여기로 나온다.
    표시하지 않는 것과 조용히 버리는 것은 다르다.
    """

    mapped = mapped_field_names()
    return [name for name in field_states_by_name(profile) if name not in mapped]
