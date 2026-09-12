"""기대효과 구역의 미대응 원문을 보완하는 경로를 고정한다.

목적·수행체계 재검은 "값이 하나도 없을 때" 돌았다. 기대효과는 값이 이미 있고
구역의 일부만 값이 된 경우다. 그래서 후보 조건도, 결과 병합도 다르다.

**미선택은 재검 후보 신호이지 확인된 누락이 아니다.** 그 사실만으로 상태를
낮추거나 ``EXTRACTION_COVERAGE_GAP`` 을 붙이지 않는다.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from worker.contracts.cpl_result import CplEvidence, CplFact
from worker.cpl import analyze_cpl
from worker.cpl_effect import EFFECT_FIELD, build_effect_candidates


_LABEL = "□ 기대효과"
_BODY_A = " ㅇ (혁신 생태계 고도화) 전주기 지원으로 혁신 생태계 고도화에 기여"
_BODY_B = " ㅇ (4차 산업혁명 경쟁력 강화) 민간 투자 유인으로 글로벌 기술경쟁력 확보"
_SUMMARY = "◦(파급효과) 전주기 지원으로 혁신 생태계 고도화에 기여"
_SELECTED = "전주기 지원으로 혁신 생태계 고도화에 기여"
_ADDED = "민간 투자 유인으로 글로벌 기술경쟁력 확보"


class _Block:
    def __init__(self, block_id: str, text: str, occurrence: str) -> None:
        self.block_id = block_id
        self.text = text
        self.common_ir_occurrence_ids = [occurrence]


class _Pack:
    def __init__(self, *blocks: _Block) -> None:
        self.blocks = list(blocks)


def _ir(*lines: tuple[str, str]) -> dict[str, Any]:
    return {
        "document": {"document_id": "hwp:d1"},
        "blocks": [
            {
                "block_id": f"hwp:b{index}",
                "reading_order": index,
                "occurrences": [{"occurrence_id": occurrence, "text": text}],
            }
            for index, (occurrence, text) in enumerate(lines)
        ],
    }


def _document() -> dict[str, Any]:
    return _ir(
        ("occ:c9", _SUMMARY),
        ("occ:p1", _LABEL),
        ("occ:p2", _BODY_A),
        ("occ:p3", _BODY_B),
        ("occ:p9", "□ 사업추진 체계 및 절차"),
    )


def _pack() -> _Pack:
    return _Pack(
        _Block("pack:c9", _SUMMARY, "occ:c9"),
        _Block("pack:p2", _BODY_A, "occ:p2"),
        _Block("pack:p3", _BODY_B, "occ:p3"),
    )


def _existing() -> CplFact:
    start = _SUMMARY.index(_SELECTED)
    return CplFact(
        fact_id="fact_1",
        value_raw=_SELECTED,
        status="identified",
        source_block_id="pack:c9",
        start_char=start,
        end_char=start + len(_SELECTED),
        text_basis="common_ir_v1_candidate_pack",
        evidence=[CplEvidence("pack:c9", "hwp:d1", "hwp:b0", ["occ:c9"])],
    )


# --- 후보 생성과 좌표 대응 ---------------------------------------------------


def test_an_unmatched_body_occurrence_makes_a_candidate() -> None:
    found = build_effect_candidates(_document(), [_existing()], _pack())

    covered = {row.common_ir_occurrence_id: row.covered for row in found.regions}
    assert covered == {"occ:c9": True, "occ:p2": False, "occ:p3": False}
    assert found.needs_recheck
    # 기존 선택은 그 구역의 raw_text 좌표로 옮겨진다.
    summary = next(r for r in found.regions if r.common_ir_occurrence_id == "occ:c9")
    span = summary.already_selected[0]
    assert summary.raw_text[span.start : span.end] == _SELECTED


def test_a_fully_matched_region_asks_nothing() -> None:
    found = build_effect_candidates(_ir(("occ:c9", _SUMMARY)), [_existing()], _pack())

    assert [row.covered for row in found.regions] == [True]
    assert not found.needs_recheck


# 부모 셀의 문구가 자식 문단 여럿에 각각 한 번씩 나올 수 있다. 문단마다
# 유일하다는 이유로 둘 다 선택됨으로 처리하면 안 된다.
def test_a_parent_quote_matching_two_children_stays_unplaced() -> None:
    repeated = " ㅇ 같은 문장이 두 곳에 나온다"
    quote = repeated.strip()
    document = _ir(
        ("occ:cell", "머리말 " + repeated),
        ("occ:p1", _LABEL),
        ("occ:p2", repeated),
        ("occ:p3", repeated),
    )
    parent = CplFact(
        fact_id="fact_1", value_raw=quote, status="identified",
        source_block_id="pack:cell", start_char=0, end_char=len(quote),
        text_basis="x",
        evidence=[CplEvidence("pack:cell", "hwp:d1", "hwp:b0", ["occ:cell"])],
    )
    pack = _Pack(_Block("pack:cell", quote, "occ:cell"))

    found = build_effect_candidates(document, [parent], pack)

    assert [row.covered for row in found.regions] == [False, False]
    # 버리지 않는다. 어느 자리인지 모를 뿐이라 문맥으로는 전달한다.
    assert found.unplaced_selections == (quote,)


def test_a_repeated_quote_inside_one_block_stays_unplaced() -> None:
    pack = _Pack(_Block("pack:c9", "가나 가나", "occ:c9"))
    fact = CplFact(
        fact_id="fact_1", value_raw="가나", status="identified",
        source_block_id="pack:c9", start_char=0, end_char=2, text_basis="x",
        evidence=[CplEvidence("pack:c9", "hwp:d1", "hwp:b0", ["occ:c9"])],
    )

    found = build_effect_candidates(_document(), [fact], pack)

    assert found.unplaced_selections == ("가나",)
    assert all(not row.covered for row in found.regions)


# --- 재검 응답 처리 ----------------------------------------------------------


class _Llm:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.tasks: list[str] = []
        self.payloads: list[Any] = []

    async def generate_structured(
        self, *, task_name: str, messages: list[Any],
        response_schema: type[BaseModel], model_profile: str,
    ) -> BaseModel:
        self.tasks.append(task_name)
        self.payloads.append(json.loads(messages[-1].content))
        return response_schema(effect_occurrences=self.rows)


class _Dead:
    async def generate_structured(self, **_: Any) -> BaseModel:
        raise RuntimeError("포트 밖 예외")


def _profile() -> dict[str, Any]:
    start = _SUMMARY.index(_SELECTED)
    return {
        "comparison_profile": {},
        "request_context": {"expected_effect": [{
            "fact_id": "fact_1", "value_raw": _SELECTED, "status": "identified",
            "value_source": {
                "source_block_id": "pack:c9",
                "start_char": start,
                "end_char": start + len(_SELECTED),
            },
            "evidence": [{
                "source_block_id": "pack:c9",
                "common_ir_document_id": "hwp:d1",
                "common_ir_block_id": "hwp:b0",
                "common_ir_occurrence_ids": ["occ:c9"],
            }],
        }]},
        "field_states": [{"field_name": "expected_effect", "status": "identified"}],
    }


def _effect(result):
    return next(
        subfield
        for item in result.items
        for subfield in item.subfields
        if subfield.profile_field == EFFECT_FIELD
    )


def _run(llm: Any):
    return analyze_cpl(
        _profile(), llm, model_profile="cpl",
        common_ir=_document(), candidate_pack=_pack(),
    )


def _row(occurrence: str, text: str, quote: str, placed: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {
        "evidence_ref": f"hwp:d1#{occurrence}@0-{len(text)}",
        "raw_text": quote,
    }
    if placed:
        row["start"] = text.index(quote)
        row["end"] = text.index(quote) + len(quote)
    return row


def test_an_added_quote_is_promoted_with_its_own_span() -> None:
    llm = _Llm([_row("occ:p3", _BODY_B, _ADDED)])

    subfield = _effect(_run(llm))

    assert llm.tasks == ["cpl_recheck"]
    # 기존 Fact 는 그대로 있고 추가분이 붙는다.
    assert [fact.value_raw for fact in subfield.facts] == [_SELECTED, _ADDED]
    added = subfield.facts[1]
    assert added.fact_id is None
    assert added.text_basis == "cpl_effect_recheck"
    assert _BODY_B[added.start_char : added.end_char] == _ADDED


# 기존 근거와 같은 인용만 돌아오는 것은 정상이다. 모델이 "기존 것으로 충분하다"
# 고 판단하면 그렇게 답한다.
def test_returning_only_the_existing_quote_is_a_normal_empty_answer() -> None:
    llm = _Llm([_row("occ:c9", _SUMMARY, _SELECTED)])

    subfield = _effect(_run(llm))

    assert [fact.value_raw for fact in subfield.facts] == [_SELECTED]
    assert subfield.reason_codes == []


def test_adding_nothing_is_a_normal_answer() -> None:
    subfield = _effect(_run(_Llm([])))

    assert [fact.value_raw for fact in subfield.facts] == [_SELECTED]
    assert subfield.reason_codes == []


def test_wrong_coordinates_reject_the_row_without_searching_elsewhere() -> None:
    row = _row("occ:p3", _BODY_B, _ADDED)
    row["start"], row["end"] = 0, len(_ADDED)      # 실제 자리가 아니다
    llm = _Llm([row])

    subfield = _effect(_run(llm))

    assert [fact.value_raw for fact in subfield.facts] == [_SELECTED]
    assert "RECHECK_NO_VALID_OCCURRENCE" in subfield.reason_codes


def test_a_reference_from_another_section_is_rejected() -> None:
    llm = _Llm([{"evidence_ref": "hwp:d1#occ:다른곳@0-10", "raw_text": "아무 말"}])

    subfield = _effect(_run(llm))

    assert [fact.value_raw for fact in subfield.facts] == [_SELECTED]
    assert "RECHECK_NO_VALID_OCCURRENCE" in subfield.reason_codes


def test_the_existing_status_is_never_lowered_by_a_failed_effect_recheck() -> None:
    from worker.cpl import build_cpl_result

    def representative(result) -> str:
        return next(
            row.representative_status for row in result.items
            if any(sf.profile_field == EFFECT_FIELD for sf in row.subfields)
        )

    baseline = build_cpl_result(_profile())
    result = _run(_Dead())
    subfield = _effect(result)

    # 값과 상태는 그대로고 실패만 사유로 남는다. 대표 상태는 재검 이전과 같다.
    assert [fact.value_raw for fact in subfield.facts] == [_SELECTED]
    assert subfield.status == "identified"
    assert representative(result) == representative(baseline)
    assert "LLM_UNAVAILABLE" in subfield.reason_codes


def test_the_request_carries_the_existing_selection_with_coordinates() -> None:
    llm = _Llm([])

    _run(llm)

    regions = llm.payloads[0]["effect_regions"]
    assert len(regions) == 3
    summary = next(row for row in regions if row["already_selected"])
    span = summary["already_selected"][0]
    # 좌표는 그 행의 raw_text 기준이다.
    assert summary["raw_text"][span["start"] : span["end"]] == span["raw_text"]


# 추가가 있으면 사유 코드는 안 붙는다. 그래도 왜 떨어졌는지는 남아야 한다 —
# 개수만 남기면 사후에 확인할 방법이 없다.
def test_a_partial_drop_keeps_its_reason_in_the_diagnostic() -> None:
    good = _row("occ:p3", _BODY_B, _ADDED)
    bad = {"evidence_ref": "hwp:d1#occ:다른곳@0-10", "raw_text": "아무 말"}
    llm = _Llm([good, bad])

    result = _run(llm)
    subfield = _effect(result)
    detail = " ".join(row.message for row in result.diagnostics)

    assert [fact.value_raw for fact in subfield.facts] == [_SELECTED, _ADDED]
    assert subfield.reason_codes == []          # 추가가 있으니 사유 코드는 없다
    assert "탈락 1건" in detail
    assert "기대효과 섹션에 준 참조가 아니다" in detail
