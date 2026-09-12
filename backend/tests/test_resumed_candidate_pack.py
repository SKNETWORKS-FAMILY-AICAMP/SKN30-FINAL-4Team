"""캐시로 이어받은 실행도 신규 실행과 같은 정량 근거를 낸다는 것을 고정한다.

그 실행이 쓴 CandidatePack 은 저장되지 않는다. 저장된 Common IR 로 다시 만들되
그때와 같은 조건이었는지 대조하고, 아니면 값은 그대로 둔 채 정량 맥락만 보류한다.
같은 분석이 신규 실행인지 재개인지에 따라 비교 근거가 달라지면 안 된다.
"""

from __future__ import annotations

from typing import Any

from worker.analysis_job import _resumed_candidate_pack
from worker.cpl import QUANTITY_CONTEXT_UNAVAILABLE, build_cpl_result, _with_quantities
from worker.profiles import build_pack


# 값이 된 것은 '최대 5,000만원' 뿐이고 '기업당' 은 그 앞 라벨에만 있다. 팩 원문을
# 봐야만 대상 기준이 파생되므로, 팩이 없으면 맥락이 사라지는 것이 드러난다.
_LINE = "- 기업당 한도: 최대 5,000만원"
_VALUE = "최대 5,000만원"


_PROVENANCE = {
    "method": "rhwp",
    "source_location": "raw/fixture#/ir/body/0",
    "bbox": None,
    "coordinate_space": None,
    "page": None,
}


def _ir() -> dict[str, Any]:
    """build_pack 이 받는 최소 Common IR. 실제 문서 골격을 따른다."""

    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:resume-1",
            "source_kind": "hwpx",
            "artifact_role": "production",
            "page_count": 1,
            "raw_artifact_ids": [],
            "provenance": {"source_sha256": "0" * 64, **_PROVENANCE},
        },
        "blocks": [{
            "block_id": "hwpx:b1",
            "kind": "paragraph",
            "reading_order": 0,
            "text": _LINE,
            "boundary_markers": [],
            "page": None,
            "section_path": "section[0]/para[0]",
            "source_block_label": "paragraph",
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ:p1"],
            "provenance": dict(_PROVENANCE),
            "occurrences": [{
                "occurrence_id": "occ:p1",
                "text": _LINE,
                "role": None,
                "provenance": dict(_PROVENANCE),
            }],
        }],
        "relations": [],
        "conflicts": [],
    }


def _block_of(pack, text: str):
    return next(row for row in pack.blocks if row.text == text)


def _profile(**metadata: Any) -> dict[str, Any]:
    """저장된 프로파일 모양. 좌표는 팩에서 직접 읽어 붙인다."""

    block = _block_of(build_pack(_ir()), _LINE)
    start = block.text.index(_VALUE)
    recorded = {
        "candidate_pack_generator": "semantic_structuring.common_ir_v1",
        "candidate_pack_generator_version": "1",
    }
    recorded.update(metadata.pop("candidate_pack", {}) or {})
    return {
        "comparison_profile": {"support_scale": [{
            "fact_id": "fact_1", "value_raw": _VALUE, "status": "identified",
            "value_source": {
                "source_block_id": block.block_id,
                "start_char": start,
                "end_char": start + len(_VALUE),
            },
        }]},
        "field_states": [{"field_name": "support_scale", "status": "identified"}],
        "processing_metadata": {"candidate_pack": recorded, **metadata},
    }


def _context(result) -> list[tuple[int, str | None, str | None]]:
    return [
        (span.value, span.dimension("scope"), span.dimension("nature"))
        for item in result.items
        for subfield in item.subfields
        for fact in subfield.facts
        for span in fact.quantities
    ]


def _facts(result) -> list[str | None]:
    return [
        fact.value_raw
        for item in result.items
        for subfield in item.subfields
        for fact in subfield.facts
    ]


def _holds(result) -> list[str | None]:
    return [
        row.reason_code
        for row in result.diagnostics
        if row.reason_code == QUANTITY_CONTEXT_UNAVAILABLE
    ]


def test_a_resumed_run_derives_the_same_quantities_as_a_fresh_one() -> None:
    profile, common_ir = _profile(), _ir()

    fresh = _with_quantities(build_cpl_result(profile), build_pack(common_ir))
    pack, reason = _resumed_candidate_pack(profile, common_ir)
    resumed = _with_quantities(build_cpl_result(profile), pack, reason)

    assert reason is None
    # 값에 없는 '기업당' 이 양쪽 모두에서 나온다.
    assert _context(fresh) == [(50_000_000, "기업당", "한도")]
    assert _context(resumed) == _context(fresh)
    assert _holds(resumed) == []


def test_a_missing_generator_record_holds_the_derivation_with_a_reason() -> None:
    common_ir = _ir()
    profile = _profile()
    profile["processing_metadata"].pop("candidate_pack")

    pack, reason = _resumed_candidate_pack(profile, common_ir)
    result = _with_quantities(build_cpl_result(profile), pack, reason)

    assert pack is None and reason is not None
    assert _context(result) == []                                  # 맥락만 없다
    assert _facts(result) == [_VALUE]                              # 값은 그대로
    assert _holds(result) == [QUANTITY_CONTEXT_UNAVAILABLE]


def test_a_different_generator_version_holds_the_derivation() -> None:
    common_ir = _ir()
    profile = _profile(candidate_pack={"candidate_pack_generator_version": "99"})

    pack, reason = _resumed_candidate_pack(profile, common_ir)
    result = _with_quantities(build_cpl_result(profile), pack, reason)

    assert pack is None
    assert reason is not None and "99" in reason
    assert _context(result) == []
    assert _facts(result) == [_VALUE]
    assert _holds(result) == [QUANTITY_CONTEXT_UNAVAILABLE]


def test_a_different_generator_name_holds_the_derivation() -> None:
    common_ir = _ir()
    profile = _profile(candidate_pack={"candidate_pack_generator": "다른.생성기"})

    pack, reason = _resumed_candidate_pack(profile, common_ir)

    assert pack is None and reason is not None


# 값 span 후보 생성기는 기간 후보 목록을 바꾸지만 블록 텍스트·좌표는 건드리지
# 않는다. 정량 파생의 필수 조건으로 요구하지 않는다.
def test_a_span_candidate_version_change_does_not_hold_the_derivation() -> None:
    common_ir = _ir()
    profile = _profile(value_span_candidate_generator_version="99")

    pack, reason = _resumed_candidate_pack(profile, common_ir)

    assert pack is not None and reason is None
