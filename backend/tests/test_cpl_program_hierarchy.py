"""``program_hierarchy.nodes`` 상태를 서버가 정한다는 것을 고정한다.

``field_states`` 25 개에 ``nodes`` 는 없다. 누락이 아니라 계층이 LLM 의미 선택
대상이 아니라서다. 이름으로 조회하면 언제나 ``None`` 이 나오고, 완전히 접지된
노드가 화면에서 "상태 모름" 이 되며, 그 하나가 IMPLEMENTATION_PLAN 전체를
``needs_confirmation`` 에 묶어 둔다. ``request_type`` 과 같은 이유로 같은 처리를
한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from worker.contracts.cpl_result import (
    CplFieldCode,
    PROFILE_FIELD_STATE_MISSING,
    SERVER_RESOLVED_PROGRAM_HIERARCHY,
)
from worker.cpl import build_cpl_result


def _profile(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"program_hierarchy": {"nodes": nodes}, "field_states": []}


def _nodes_subfield(nodes: list[dict[str, Any]]):
    item = next(
        row
        for row in build_cpl_result(_profile(nodes)).items
        if row.field_code is CplFieldCode.IMPLEMENTATION_PLAN
    )
    return next(
        row for row in item.subfields if row.profile_field == "program_hierarchy.nodes"
    )


_DETAIL = {"program_node_id": "p1", "level": "detail_program", "name_raw": "세부사업"}
_SUB = {"program_node_id": "p2", "level": "sub_program", "name_raw": "내역사업"}


def test_grounded_nodes_are_never_reported_as_unknown_state() -> None:
    subfield = _nodes_subfield([_DETAIL, _SUB])

    assert subfield.status == "identified"
    assert subfield.reason_codes == [SERVER_RESOLVED_PROGRAM_HIERARCHY]
    assert PROFILE_FIELD_STATE_MISSING not in subfield.reason_codes
    assert len(subfield.facts) == 2


# AGENTS.md IMPLEMENTATION_PLAN: 세부사업만 명시되면 내역사업 존재를 추론하지
# 않는다. 값이 있다고 확인됨으로 올리지 않는다.
def test_detail_program_alone_does_not_imply_a_sub_program() -> None:
    assert _nodes_subfield([_DETAIL]).status == "mentioned_unresolved"


def test_absent_hierarchy_stays_not_found() -> None:
    assert _nodes_subfield([]).status == "not_found"


# 계층을 읽지 못한 노드를 확인됨으로 올리지 않는다. 부재와도 구분한다.
@pytest.mark.parametrize("node", [{"program_node_id": "p1"}, {"level": None}])
def test_unreadable_level_is_neither_confirmed_nor_absent(node: dict[str, Any]) -> None:
    assert _nodes_subfield([node]).status == "mentioned_unresolved"


def test_missing_container_does_not_raise() -> None:
    item = next(
        row
        for row in build_cpl_result({}).items
        if row.field_code is CplFieldCode.IMPLEMENTATION_PLAN
    )
    subfield = next(
        row for row in item.subfields if row.profile_field == "program_hierarchy.nodes"
    )
    assert subfield.status == "not_found"
