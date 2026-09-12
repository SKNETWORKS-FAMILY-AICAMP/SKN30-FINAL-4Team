"""신설·변경 주요내용(CPL-05)은 요청유형에 따라 묻는 것이 달라진다.

판별기준 §6 의 첫 문장이 "요청유형에 따라 의미를 다르게 본다" 다. §6.1 신설은
"새로 만드는 **사업단위**" 가 주어이고, §6.2 변경은 기존 사업계획과의 비교를
요구하는데 그 입력이 파이프라인에 없다.

같은 ``program_hierarchy.nodes`` 를 CPL-03 과 공유하지만 판정 규칙은 다르다.
저쪽은 "내역사업별 추진계획을 볼 수 있는가" 라 세부사업만으로는 모자라고,
여기서는 세부사업 신설 요청에 세부사업 노드면 충분하다.
"""

from __future__ import annotations

from typing import Any

from worker.contracts.cpl_result import (
    CONFIRMED,
    NEEDS_CONFIRMATION,
    NO_CONTENT,
    SERVER_DERIVED_HIERARCHY_STATE,
    CplFieldCode,
)
from worker.cpl import PRIOR_PLAN_UNAVAILABLE, build_cpl_result


def _node(node_id: str, level: str, name: str) -> dict[str, Any]:
    return {"program_node_id": node_id, "level": level, "name_raw": name}


def _profile(selected: str | None, *nodes: dict[str, Any], components: Any = None):
    rows = list(components or [])
    profile: dict[str, Any] = {
        "comparison_profile": {},
        "request_context": {},
        "program_hierarchy": {"nodes": list(nodes)},
        "support_components": rows,
        # 실제 프로파일의 field_states 25 개에 support_components 는 있고
        # program_hierarchy.nodes 는 없다. 픽스처도 그대로 맞춘다.
        "field_states": [
            {
                "field_name": "support_components",
                "status": "identified" if rows else "not_found",
            }
        ],
    }
    if selected is not None:
        profile["request_type"] = {"selected_code": selected}
    return profile


def _item(profile: dict[str, Any], code=CplFieldCode.NEW_OR_CHANGED_CONTENT):
    return next(row for row in build_cpl_result(profile).items if row.field_code is code)


def _nodes_row(item):
    return next(r for r in item.subfields if r.profile_field == "program_hierarchy.nodes")


# --- 신설 요청: 요청한 등급의 노드가 있으면 확인됨 ---------------------------


def test_a_detail_program_request_is_satisfied_by_a_detail_program_node() -> None:
    profile = _profile(
        "detail_program_new",
        _node("p1", "detail_program", "부산광역시 중소기업 기술혁신 지원사업"),
        components=[{"support_component_id": "c1", "name_raw": "스마트 기술사업화 지원"}],
    )

    item = _item(profile)

    assert _nodes_row(item).status == "identified"
    assert item.representative_status == CONFIRMED


def test_a_sub_program_request_needs_a_sub_program_node() -> None:
    only_detail = _profile(
        "sub_program_new",
        _node("p1", "detail_program", "기술혁신 지원사업"),
        components=[{"support_component_id": "c1", "name_raw": "스마트 기술사업화 지원"}],
    )

    assert _nodes_row(_item(only_detail)).status == "mentioned_unresolved"
    assert _item(only_detail).representative_status == NEEDS_CONFIRMATION

    with_sub = _profile(
        "sub_program_new",
        _node("p1", "detail_program", "기술혁신 지원사업"),
        _node("p2", "sub_program", "스마트 기술사업화 지원"),
        components=[{"support_component_id": "c1", "name_raw": "스마트 기술사업화 지원"}],
    )

    assert _nodes_row(_item(with_sub)).status == "identified"
    assert _item(with_sub).representative_status == CONFIRMED


# CPL-03 은 같은 경로를 다른 규칙으로 본다. 세부사업만으로는 내역사업별
# 추진계획을 볼 수 없다 — 두 항목이 같은 노드에서 다른 답을 내야 한다.
def test_the_same_nodes_answer_differently_for_the_implementation_plan() -> None:
    profile = _profile(
        "detail_program_new",
        _node("p1", "detail_program", "기술혁신 지원사업"),
        components=[{"support_component_id": "c1", "name_raw": "스마트 기술사업화 지원"}],
    )

    new_unit = _nodes_row(_item(profile))
    plan = next(
        r
        for r in _item(profile, CplFieldCode.IMPLEMENTATION_PLAN).subfields
        if r.profile_field == "program_hierarchy.nodes"
    )

    assert new_unit.status == "identified"
    assert plan.status == "mentioned_unresolved"
    assert SERVER_DERIVED_HIERARCHY_STATE in new_unit.reason_codes


# --- 변경 요청: 기존 계획이 없으면 확인했다고 말하지 않는다 -------------------


def test_a_content_change_request_cannot_be_confirmed_from_the_hierarchy_alone() -> None:
    profile = _profile(
        "program_content_change",
        _node("p1", "detail_program", "기술혁신 지원사업"),
        _node("p2", "sub_program", "스마트 기술사업화 지원"),
        components=[{"support_component_id": "c1", "name_raw": "스마트 기술사업화 지원"}],
    )

    row = _nodes_row(_item(profile))

    assert row.status == "mentioned_unresolved"
    assert PRIOR_PLAN_UNAVAILABLE in row.reason_codes
    assert _item(profile).representative_status == NEEDS_CONFIRMATION


# --- 경계 -------------------------------------------------------------------


def test_an_unreadable_request_type_does_not_promote_the_hierarchy() -> None:
    profile = _profile(None, _node("p1", "sub_program", "스마트 기술사업화 지원"))

    assert _nodes_row(_item(profile)).status == "mentioned_unresolved"


def test_no_hierarchy_at_all_is_no_content() -> None:
    profile = _profile("sub_program_new")

    item = _item(profile)

    assert _nodes_row(item).status == "not_found"
    assert item.representative_status == NO_CONTENT


# CPL-05 가 더 이상 하위 필드 없는 항목이 아니다. 빈 튜플이던 시절에는
# NO_PROFILE_FIELD 진단이 붙었다.
def test_the_item_no_longer_reports_a_missing_profile_field() -> None:
    profile = _profile("sub_program_new", _node("p1", "sub_program", "스마트"))

    result = build_cpl_result(profile)

    assert [r.profile_field for r in _item(profile).subfields] == [
        "program_hierarchy.nodes",
        "support_components",
    ]
    assert all(
        row.unit != CplFieldCode.NEW_OR_CHANGED_CONTENT.value
        or row.reason_code != "NO_PROFILE_FIELD"
        for row in result.diagnostics
    )
