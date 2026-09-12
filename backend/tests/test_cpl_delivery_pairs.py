"""수행관계 후보를 서버가 결정적으로 만드는 것을 고정한다.

LLM 에게 관계를 지어 보라고 하지 않는다. 어떤 칸과 어떤 칸이 짝이 될 수 있는지는
표 기하로 정해지므로 서버가 만들고, 모델은 그중 무엇을 채택할지만 고른다.
"""

from __future__ import annotations

from typing import Any

import pytest

from worker import vendor  # noqa: F401  (vendored 경로를 sys.path 에 넣는다)

from worker.cpl_coverage import detect_coverage_gaps
from worker.cpl_delivery import (
    CANDIDATE_ID_ALGORITHM,
    DELIVERY_PAIR_AMBIGUOUS,
    build_delivery_pair_candidates,
)


def _table(*cells: dict[str, Any]) -> dict[str, Any]:
    occurrences = []
    rows = []
    for index, cell in enumerate(cells):
        ids = []
        for order, text in enumerate(cell["texts"]):
            occurrence_id = f"occ:t:c{index}:p{order}"
            occurrences.append({"occurrence_id": occurrence_id, "text": text})
            ids.append(occurrence_id)
        rows.append({
            "cell_id": f"t:c{index}",
            "row_index": cell["row"], "col_index": cell["col"],
            "col_span": cell.get("span", 1), "row_span": 1,
            "text_occurrence_ids": ids,
        })
    return {
        "block_id": "hwp:t1", "kind": "table", "structure_status": "explicit",
        "reading_order": 1, "text": "표", "cells": rows, "occurrences": occurrences,
    }


def _ir(label: str, table: dict[str, Any] | None, *tail: str) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [{
        "block_id": "hwp:b0", "reading_order": 0,
        "occurrences": [{"occurrence_id": "occ:p0", "text": label}],
    }]
    if table is not None:
        blocks.append(table)
    for index, text in enumerate(tail):
        blocks.append({
            "block_id": f"hwp:b{index + 2}", "reading_order": index + 2,
            "occurrences": [{"occurrence_id": f"occ:p{index + 2}", "text": text}],
        })
    return {"document": {"document_id": "hwp:doc"}, "blocks": blocks}


def _chart() -> dict[str, Any]:
    return _table(
        {"row": 0, "col": 0, "span": 2, "texts": ["(주무부처)"]},
        {"row": 1, "col": 0, "span": 2, "texts": ["정책수립 및 예산 지원"]},
    )


def _profile() -> dict[str, Any]:
    return {
        "comparison_profile": {"purpose_goal": [{"value_raw": "목적"}], "delivery_relations": []},
        "field_states": [
            {"field_name": "purpose_goal", "status": "identified"},
            {"field_name": "delivery_relations", "status": "not_found"},
        ],
    }


def test_a_chart_region_produces_a_candidate_and_a_gap() -> None:
    ir = _ir("  ㅇ 사업추진체계", _chart())

    candidates, diagnostics = build_delivery_pair_candidates(ir)

    assert len(candidates) == 1 and diagnostics == []
    pair = candidates[0]
    assert [row.raw_text for row in pair.actor.occurrences] == ["(주무부처)"]
    assert [row.raw_text for row in pair.member.occurrences] == ["정책수립 및 예산 지원"]
    assert pair.allowed_member_kinds == ("role", "action")
    assert [gap.field_name for gap in detect_coverage_gaps(_profile(), ir)] == [
        "delivery_relations"
    ]


# 절차표는 단계와 주요내용의 행 관계이고 기관이 없다. 세로 셀 쌍 후보도,
# delivery_relations gap 근거도 아니다. 구역을 끝내는 경계로만 쓴다.
def test_a_procedure_region_alone_makes_no_candidate_and_no_gap() -> None:
    ir = _ir("  ㅇ 사업추진절차", _chart())

    assert build_delivery_pair_candidates(ir) == ([], [])
    assert detect_coverage_gaps(_profile(), ir) == []


def test_a_procedure_label_still_ends_the_chart_region() -> None:
    ir = _ir("  ㅇ 사업추진체계", _chart(), "  ㅇ 사업추진절차", "사업 공고 및 선정")

    candidates, _ = build_delivery_pair_candidates(ir)

    assert [pair.common_ir_block_id for pair in candidates] == ["hwp:t1"]


# 복합 제목은 지배를 넘기지 않는다. 뒤 표가 체계인지 절차인지 가릴 수 없다.
def test_a_compound_heading_alone_makes_no_candidate_and_no_gap() -> None:
    ir = _ir("□ 사업추진 체계 및 절차", _chart())

    assert build_delivery_pair_candidates(ir) == ([], [])
    assert detect_coverage_gaps(_profile(), ir) == []


# 한쪽만 여러 문단이면 후보다. 셀 쌍이 단위이고 곱집합을 만들지 않는다.
def test_one_multi_occurrence_side_is_still_one_candidate() -> None:
    ir = _ir("  ㅇ 사업추진체계", _table(
        {"row": 0, "col": 0, "texts": ["협력기관"]},
        {"row": 1, "col": 0, "texts": ["한국벤처투자협회", "한국창업보육협회"]},
    ))

    candidates, diagnostics = build_delivery_pair_candidates(ir)

    assert len(candidates) == 1 and diagnostics == []
    assert len(candidates[0].member.occurrences) == 2


# 양쪽 모두 여러 문단이면 어느 문단이 어느 문단과 짝인지 원문만으로 정할 수
# 없다. 곱집합을 만들지 않고 제외하되 왜 빠졌는지 남긴다.
def test_both_multi_occurrence_sides_are_skipped_with_a_diagnostic() -> None:
    ir = _ir("  ㅇ 사업추진체계", _table(
        {"row": 0, "col": 0, "texts": ["기관 A", "기관 B"]},
        {"row": 1, "col": 0, "texts": ["역할 A", "역할 B"]},
    ))

    candidates, diagnostics = build_delivery_pair_candidates(ir)

    assert candidates == []
    assert [row.reason_code for row in diagnostics] == [DELIVERY_PAIR_AMBIGUOUS]
    assert diagnostics[0].actor_cell_id == "t:c0"


# 열이나 병합 폭이 다르면 같은 칸을 가리키는 것이 아니다.
def test_a_different_column_span_is_not_a_pair() -> None:
    ir = _ir("  ㅇ 사업추진체계", _table(
        {"row": 0, "col": 0, "span": 2, "texts": ["(주무부처)"]},
        {"row": 1, "col": 0, "span": 3, "texts": ["정책수립"]},
    ))

    assert build_delivery_pair_candidates(ir) == ([], [])


# 다음 비어 있지 않은 의미 행이어야 한다. 사이에 다른 내용 행이 있으면 짝이 아니다.
def test_only_the_immediate_next_semantic_row_pairs() -> None:
    ir = _ir("  ㅇ 사업추진체계", _table(
        {"row": 0, "col": 0, "texts": ["(주무부처)"]},
        {"row": 1, "col": 5, "texts": ["다른 칸"]},
        {"row": 2, "col": 0, "texts": ["정책수립"]},
    ))

    assert build_delivery_pair_candidates(ir) == ([], [])


# 추론한 격자에서 관계를 만들지 않는다.
def test_an_implicit_table_makes_no_candidate() -> None:
    table = _chart()
    table["structure_status"] = "inferred"

    assert build_delivery_pair_candidates(_ir("  ㅇ 사업추진체계", table)) == ([], [])


# candidate_id 는 같은 문서를 다시 처리하면 같아야 저장된 재검 기록과 대조된다.
def test_candidate_ids_are_deterministic_and_positional() -> None:
    ir = _ir("  ㅇ 사업추진체계", _chart())

    first, _ = build_delivery_pair_candidates(ir)
    second, _ = build_delivery_pair_candidates(ir)

    assert [row.candidate_id for row in first] == [row.candidate_id for row in second]
    assert first[0].candidate_id.startswith("dpc:")

    moved = _ir("  ㅇ 사업추진체계", _table(
        {"row": 0, "col": 4, "span": 2, "texts": ["(주무부처)"]},
        {"row": 1, "col": 4, "span": 2, "texts": ["정책수립 및 예산 지원"]},
    ))
    other, _ = build_delivery_pair_candidates(moved)

    # 같은 원문이라도 좌표가 다르면 다른 후보다.
    assert other[0].candidate_id != first[0].candidate_id


def test_the_candidate_id_algorithm_is_pinned() -> None:
    assert CANDIDATE_ID_ALGORITHM == "delivery_pair_v1"


# evidence_ref 는 occurrence 를 가리키며 모델이 고를 대상이다. 서버는 응답의
# ref 가 이 후보의 그 칸에 있던 것인지만 확인하면 된다.
def test_each_side_exposes_its_evidence_refs() -> None:
    (pair,), _ = build_delivery_pair_candidates(_ir("  ㅇ 사업추진체계", _chart()))

    assert pair.evidence_refs("actor") == (pair.actor.occurrences[0].evidence_ref,)
    assert pair.evidence_refs("member") == (pair.member.occurrences[0].evidence_ref,)
    assert pair.actor.occurrences[0].evidence_ref.startswith("hwp:doc#occ:t:c0:p0@")


# 새 HWP 실문서. 절차표를 빼면 체계도만 남는다.
def test_the_new_hwp_chart_yields_only_the_diagram_pairs() -> None:
    import json
    from pathlib import Path as _Path

    trace = (
        _Path(__file__).resolve().parents[2]
        / ".runtime/pipeline-traces/manual-test-terra17-new-hwp/01_common_ir.json"
    )
    if not trace.exists():
        pytest.skip("저장된 트레이스가 없는 환경")
    ir = json.loads(trace.read_text(encoding="utf-8"))

    candidates, _ = build_delivery_pair_candidates(ir)

    assert {pair.common_ir_block_id for pair in candidates} == {"hwp:t87.c5.b1"}
    assert len(candidates) == 9
