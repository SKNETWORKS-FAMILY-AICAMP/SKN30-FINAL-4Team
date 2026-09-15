"""Existing Profile v0.2 source-selection finalization regressions.

These cases exercise the small, shared finalization boundary used by the
announcement worker.  They intentionally do not invoke an LLM or a database.
"""

from __future__ import annotations

import pytest
from unittest.mock import ANY

from worker import announcement_profiles
from worker import vendor  # noqa: F401 - installs the vendored profile package

from semantic_structuring.explicit_support_cap_candidates import (
    EXPLICIT_SUPPORT_CAP_CANDIDATE_VERSION,
    extract_explicit_support_cap_candidates,
)
from semantic_structuring.models import CandidatePack
from semantic_structuring.profile_v02 import ValueSource
from semantic_structuring.source_selection import (
    AmbiguousAnchorCorrectionError,
    AnchorCorrectionRequest,
    DuplicateResolvedValueSourceSpanError,
    SourceSelectionExtractionV02,
    SupportCapCompletenessError,
    SupportScaleFactRepairError,
    build_numeric_candidates,
    finalize_source_selection_v02,
    memoize_anchor_correction_resolver,
    normalize_semantic_duplicate_facts_v02,
    preserve_prior_server_validated_facts_v02,
)


def _pack(text: str) -> CandidatePack:
    return CandidatePack.model_validate({
        "pack_id": "existing-finalize-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "existing finalization regression",
        "blocks": [{"block_id": "body[0]", "text": text, "relation": "candidate"}],
    })


def _lineaged_pack(text: str) -> CandidatePack:
    return CandidatePack.model_validate({
        "pack_id": "existing-finalize-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "existing finalization regression",
        "generator": "common_ir_v1_candidate_pack",
        "generator_version": "1.1.0",
        "common_ir_document_id": "hwpx:PBLN-existing-finalize",
        "blocks": [{
            "block_id": "body[0]",
            "text": text,
            "relation": "candidate",
            "common_ir_block_id": "hwpx:block:0",
        }],
    })


def _selection(*facts: dict[str, object]) -> SourceSelectionExtractionV02:
    return SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-existing-finalize",
        "candidate_pack_id": "existing-finalize-pack",
        "component_decision": {"mode": "none", "no_component_reason": "one notice scope"},
        "support_components": [],
        "facts": list(facts),
    })


def _fact(
    fact_id: str,
    field_name: str,
    anchor_text: str,
    *,
    source_block_id: str = "body[0]",
    primary_component_id: str | None = None,
) -> dict[str, object]:
    return {
        "fact_id": fact_id,
        "field_name": field_name,
        "status": "identified",
        "value_anchor": {"source_block_id": source_block_id, "anchor_text": anchor_text},
        "primary_component_id": primary_component_id,
    }


def test_cap_candidates_keep_decimal_offsets_and_reject_financial_eligibility() -> None:
    caps = extract_explicit_support_cap_candidates(
        _pack("기업당 최대 8억원; 과제당 최대 6.475억원")
    )
    assert [(row.anchor_text, row.start_char, row.end_char) for row in caps] == [
        ("기업당 최대 8억원", 0, 10),
        ("과제당 최대 6.475억원", 12, 26),
    ]
    assert extract_explicit_support_cap_candidates(_pack("신청자격: 연매출 최대 800백만원")) == []
    assert extract_explicit_support_cap_candidates(_pack("지원기간 최대 6개월")) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("기업당 최대 5,000만원 이내", ["기업당 최대 5,000만원 이내"]),
        ("기업당 지원금 최대 5천만원", ["기업당 지원금 최대 5천만원"]),
        ("기업당 정부지원금 최대 5천만원 이내", ["기업당 정부지원금 최대 5천만원 이내"]),
        ("과제별 정부지원금 최대 1억원 내외", ["과제별 정부지원금 최대 1억원 내외"]),
        ("기업당 사업화지원금 최대 5천만원 이내", ["기업당 사업화지원금 최대 5천만원 이내"]),
        ("참여기업당 최대 5,000만원 이내", ["참여기업당 최대 5,000만원 이내"]),
        ("1개社 당 최대 5,000만원 이내", ["1개社 당 최대 5,000만원 이내"]),
        ("지원금 최대 5,000만원 이내", ["최대 5,000만원 이내"]),
        ("기업당 최대 5,000만원, 총사업비 3억원 이내", ["기업당 최대 5,000만원"]),
        ("기업당 최대 5,000만원, 총사업비 최대 3억원 이내", ["기업당 최대 5,000만원"]),
        ("기업당 최대 5천만원,2차 총사업비 최대 3억원 이내", ["기업당 최대 5천만원"]),
        ("기업당 최대 5천만원. 총사업비 최대 3억원 이내", ["기업당 최대 5천만원"]),
        ("지원금은 총사업비의 최대 80% 이내", ["최대 80% 이내"]),
        ("기업당 요건: 지원금 최대 5,000만원", ["최대 5,000만원"]),
        ("기업당 최대 5,000만원\n이내", ["기업당 최대 5,000만원"]),
        ("지원금 최대 5천만\n이내", ["최대 5천만"]),
    ],
)
def test_scale_candidates_preserve_only_directly_bound_qualifiers(
    text: str,
    expected: list[str],
) -> None:
    assert [row.anchor_text for row in extract_explicit_support_cap_candidates(_pack(text))] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("선정규모: 최대 20개사 내외", ["20개사 내외"]),
        ("최종 최대 8개팀 내외 선정", []),
        ("최대 20개사 신청", []),
    ],
)
def test_cap_inventory_keeps_only_named_selection_capacity_counts(
    text: str,
    expected: list[str],
) -> None:
    assert [
        candidate.anchor_text
        for candidate in extract_explicit_support_cap_candidates(_pack(text))
    ] == expected


@pytest.mark.parametrize(
    "text",
    [
        "신청기업 연매출 1개사당 최대 5억원",
        "자산액 참여기업당 최대 5억원",
        "총사업비 1개사당 최대 5억원",
        "지원금 신청자격: 연매출 1개사당 최대 5억원",
        "지원금 신청자격: 총사업비 1개사당 최대 5억원",
        "지원금 안내: 총사업비 1개사당 최대 5억원",
        "지원금 개요: 총예산 최대 5억원",
        "지원금 조건: 총액 최대 5억원",
        "기업당 최대 5억원의 매출을 보유한 기업",
        "기업별 최대 10억원 자산을 보유한 기업",
        "과제당 최대 3억원 규모의 수행실적 보유기관",
        "지원한도: 과제당 최대 3억원 규모의 수행실적 보유기관",
        "지원한도 안내: 기업별 최대 10억원 자산 보유 기업",
        "지원금 안내: 기업별 최대 10억원 자산 보유 기업",
        "지원한도 안내: 기업당 최대 5천만원 지원실적 보유 기업",
        "기업당 최대 5천만원 지원 실적을 보유한 기업",
        "기업당 최대 5천만원 지원이력 보유 기업",
    ],
)
def test_per_unit_scope_does_not_override_financial_or_aggregate_semantics(text: str) -> None:
    assert extract_explicit_support_cap_candidates(_pack(text)) == []


def test_support_heading_still_allows_an_immediate_post_cap_benefit_verb() -> None:
    assert [
        item.anchor_text
        for item in extract_explicit_support_cap_candidates(
            _pack("지원한도: 기업당 최대 5천만원 지원")
        )
    ] == ["기업당 최대 5천만원"]


def test_post_cap_support_plan_is_not_confused_with_historical_support() -> None:
    assert [
        item.anchor_text
        for item in extract_explicit_support_cap_candidates(
            _pack("기업당 최대 5천만원 지원 예정")
        )
    ] == ["기업당 최대 5천만원"]


def test_finalizer_requires_one_distinct_materialized_fact_per_repeated_cap() -> None:
    text = "지원금 최대 1억원; 지원금 최대 1억원"
    pack = _pack(text)
    selection = _selection(_fact("scale", "support_scale", text))

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(
            selection,
            pack,
            build_numeric_candidates(pack),
        )

    assert raised.value.finalize_stage == "support_scale_semantics_validation"
    assert len(raised.value.required_support_scale_anchors()) == 2
    assert raised.value.do_not_restore_fact_ids == frozenset({"scale"})


def test_finalizer_requires_cap_from_an_entire_routed_a_pack_not_only_cited_blocks() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "existing-finalize-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "existing finalization regression",
        "blocks": [
            {"block_id": "body[0]", "text": "지원내용: 컨설팅 제공", "relation": "candidate"},
            {"block_id": "body[1]", "text": "지원금 최대 1억원", "relation": "candidate"},
        ],
    })
    # The selection never mentions body[1].  It is still in the routed A
    # pack, so a scanner-confirmed explicit cap must trigger repair instead of
    # disappearing behind the model's omitted block reference.
    selection = _selection(_fact("content", "support_content", "컨설팅 제공"))

    with pytest.raises(SupportCapCompletenessError) as raised:
        finalize_source_selection_v02(selection, pack, build_numeric_candidates(pack))

    assert raised.value.required_support_scale_anchors() == [{
        "source_block_id": "body[1]",
        "anchor_text": "최대 1억원",
    }]


@pytest.mark.parametrize("unsafe_kind", ["table", "layout", "ocr", "image"])
def test_cap_completeness_keeps_unsafe_non_a_blocks_out_of_scope(unsafe_kind: str) -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "existing-finalize-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "existing finalization regression",
        "blocks": [
            {"block_id": "body[0]", "text": "지원내용: 컨설팅 제공", "relation": "candidate"},
            {
                "block_id": "body[1]",
                "text": "지원금 최대 1억원",
                "relation": "candidate",
                "block_kind": unsafe_kind,
            },
        ],
    })
    selection = _selection(_fact("content", "support_content", "컨설팅 제공"))

    # B/search-only native tables do not enter an A pack at assembly time;
    # unsafe table/layout/OCR/image blocks are additionally excluded by the
    # exact-cap scanner itself if one reaches this finalizer.
    finalized = finalize_source_selection_v02(selection, pack, build_numeric_candidates(pack))
    assert finalized[5] == []


def test_native_line_cap_is_inventoried_once_and_covers_its_atomic_parent() -> None:
    text = "기업당 최대 5천만원"
    parent = {
        "block_id": "parent",
        "text": text,
        "relation": "candidate",
        "block_kind": "paragraph",
        "section_id": "main_notice",
        "source_order": 0,
        "source_occurrence_ids": ["occ:parent"],
        "common_ir_block_id": "ir:parent",
        "common_ir_occurrence_ids": ["occ:parent"],
    }
    line = {
        **parent,
        "block_id": "line",
        "block_kind": "native_line_atom",
        "native_parent_block_id": "parent",
        "native_start_char": 0,
        "native_end_char": len(text),
    }
    pack = CandidatePack.model_validate({
        "pack_id": "native-cap-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "native cap",
        "generator": "semantic_structuring.native_exact_transform",
        "generator_version": "native-exact-v2",
        "common_ir_document_id": "hwpx:PBLN-existing-finalize",
        "parent_pack_id": "source-pack",
        "parent_generator": "common_ir_v1_candidate_pack",
        "parent_generator_version": "1.1.0",
        "blocks": [parent, line],
    })

    candidates = extract_explicit_support_cap_candidates(pack)
    assert [(item.source_block_id, item.anchor_text) for item in candidates] == [
        ("parent", text)
    ]

    finalized = finalize_source_selection_v02(
        _selection(_fact("scale", "support_scale", text, source_block_id="line")),
        pack,
        build_numeric_candidates(pack),
    )
    assert [fact.fact_id for fact in finalized[0].facts] == ["scale"]


def _two_block_native_composite_pack(
    first: str,
    second: str,
) -> tuple[CandidatePack, str]:
    atomic_blocks = [
        {
            "block_id": block_id,
            "text": text,
            "relation": "candidate",
            "block_kind": "paragraph",
            "section_id": "main_notice",
            "source_order": order,
            "source_occurrence_ids": [f"occ:{block_id}"],
            "common_ir_block_id": f"ir:{block_id}",
            "common_ir_occurrence_ids": [f"occ:{block_id}"],
        }
        for order, (block_id, text) in enumerate((
            ("first", first),
            ("second", second),
        ))
    ]
    composite_text = f"{first} {second}"
    composite = {
        "block_id": "composite",
        "text": composite_text,
        "relation": "candidate",
        "block_kind": "native_composite",
        "section_id": "main_notice",
        "source_order": 0,
        "source_occurrence_ids": ["occ:first", "occ:second"],
        "common_ir_block_id": "ir:first",
        "common_ir_occurrence_ids": ["occ:first", "occ:second"],
        "source_spans": [
            {
                "source_block_id": block_id,
                "exact_text": text,
                "start_char": 0,
                "end_char": len(text),
                "separator_after": " " if order == 0 else "",
                "source_order": order,
                "section_id": "main_notice",
                "common_ir_block_id": f"ir:{block_id}",
                "common_ir_occurrence_ids": [f"occ:{block_id}"],
            }
            for order, (block_id, text) in enumerate((
                ("first", first),
                ("second", second),
            ))
        ],
    }
    return CandidatePack.model_validate({
        "pack_id": "native-two-block-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "native composite",
        "generator": "semantic_structuring.native_exact_transform",
        "generator_version": "native-exact-v2",
        "common_ir_document_id": "hwpx:PBLN-existing-finalize",
        "parent_pack_id": "source-pack",
        "parent_generator": "common_ir_v1_candidate_pack",
        "parent_generator_version": "1.1.0",
        "blocks": [*atomic_blocks, composite],
    }), composite_text


@pytest.mark.parametrize(
    ("first", "second", "broad_mode", "include_exact_cap", "missing_cap_count"),
    [
        ("기업당 최대 5천만원", "과제당 최대 1억원", "full", False, 2),
        ("신청자격 연매출 5억원 이하 및", "지원금 최대 1억원", "full", True, 0),
        ("신청자격 연매출 5억원 이하 및", "지원금 최대 1억원", "trim_last", True, 0),
        ("신청자격 연매출 5억원 이하 및", "지원금 최대 1억원", "before_cap", True, 0),
    ],
)
def test_one_native_composite_fact_cannot_discharge_atomic_caps(
    first: str,
    second: str,
    broad_mode: str,
    include_exact_cap: bool,
    missing_cap_count: int,
) -> None:
    pack, composite_text = _two_block_native_composite_pack(first, second)
    broad_anchor = {
        "full": composite_text,
        "trim_last": composite_text[:-1],
        "before_cap": composite_text[:composite_text.index("최대")].rstrip(),
    }[broad_mode]
    facts = [
        _fact(
            "too-broad",
            "support_scale",
            broad_anchor,
            source_block_id="composite",
        )
    ]
    if include_exact_cap:
        facts.append(_fact("exact", "support_scale", second, source_block_id="second"))
    selection = _selection(*facts)

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(selection, pack, build_numeric_candidates(pack))

    assert len(raised.value.required_support_scale_anchors()) == missing_cap_count
    expected_blocked = {"too-broad"}
    assert raised.value.do_not_restore_fact_ids == frozenset(expected_blocked)
    expected_reason = (
        "no_supported_measure_derived"
        if first.startswith("신청자격") and broad_mode == "before_cap"
        else "cross_atomic_support_cap_span"
    )
    assert {
        (item["fact_id"], item["reason"])
        for item in raised.value.repair_payload()
    } == {(fact_id, expected_reason) for fact_id in expected_blocked}


def test_cap_split_across_native_sources_is_inventoryed_and_can_be_claimed() -> None:
    first = "기업당 최대"
    second = "5천만원 지원"
    pack, _composite_text = _two_block_native_composite_pack(first, second)
    cap_anchor = "기업당 최대 5천만원"

    candidates = extract_explicit_support_cap_candidates(pack)
    assert [
        (candidate.source_block_id, candidate.anchor_text)
        for candidate in candidates
    ] == [("composite", cap_anchor)]

    selection = _selection(
        _fact(
            "cross-source-cap",
            "support_scale",
            cap_anchor,
            source_block_id="composite",
        )
    )
    finalized = finalize_source_selection_v02(
        selection,
        pack,
        build_numeric_candidates(pack),
    )

    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].upper_value == 50_000_000


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("매출 확대를 위한 사업화 자금", "1억원 지원"),
        ("자산 고도화를 위한 지원금", "최대 1억원"),
    ],
)
def test_valid_cross_atomic_support_without_eligibility_semantics_is_preserved(
    first: str,
    second: str,
) -> None:
    pack, composite_text = _two_block_native_composite_pack(first, second)

    finalized = finalize_source_selection_v02(
        _selection(
            _fact(
                "support-amount",
                "support_scale",
                composite_text,
                source_block_id="composite",
            )
        ),
        pack,
        build_numeric_candidates(pack),
    )

    assert [fact.fact_id for fact in finalized[0].facts] == ["support-amount"]


def test_finalizer_accepts_repeated_cap_text_only_with_distinct_verified_positions() -> None:
    text = "지원금 최대 1억원; 지원금 최대 1억원"
    pack = _pack(text)
    anchor = "최대 1억원"
    first = text.index(anchor)
    second = text.index(anchor, first + 1)
    selection = _selection(
        _fact("scale-first", "support_scale", anchor),
        _fact("scale-second", "support_scale", anchor),
    )

    finalized = finalize_source_selection_v02(
        selection,
        pack,
        build_numeric_candidates(pack),
        value_source_overrides={
            "scale-first": ValueSource(
                source_block_id="body[0]", start_char=first, end_char=first + len(anchor)
            ),
            "scale-second": ValueSource(
                source_block_id="body[0]", start_char=second, end_char=second + len(anchor)
            ),
        },
    )

    assert [row.value_source.start_char for row in finalized[3]] == [first, second]
    assert len(finalized[5][0].measures) == 2


def test_worker_resolver_can_bind_repeated_cap_facts_to_distinct_positions() -> None:
    """Memoization reuses retries of one fact, not another fact's choice."""

    text = "A지원금 최대 1억원; B지원금 최대 1억원"
    pack = _lineaged_pack(text)
    anchor = "최대 1억원"
    selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-existing-finalize",
        "candidate_pack_id": "existing-finalize-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [
            {
                "support_component_id": "component-a",
                "component_kind": "support_package",
                "source_block_ids": ["body[0]"],
                "name_anchor": {
                    "source_block_id": "body[0]",
                    "anchor_text": "A지원금",
                },
            },
            {
                "support_component_id": "component-b",
                "component_kind": "support_package",
                "source_block_ids": ["body[0]"],
                "name_anchor": {
                    "source_block_id": "body[0]",
                    "anchor_text": "B지원금",
                },
            },
        ],
        "facts": [
            _fact(
                "scale-a",
                "support_scale",
                anchor,
                primary_component_id="component-a",
            ),
            _fact(
                "scale-b",
                "support_scale",
                anchor,
                primary_component_id="component-b",
            ),
        ],
    })
    calls: list[str] = []

    def choose_by_fact(request: AnchorCorrectionRequest) -> str:
        calls.append(request.fact_id)
        marker = "A지원금" if request.primary_component_id == "component-a" else "B지원금"
        return next(
            candidate.candidate_id
            for candidate in request.candidates
            if candidate.context_before.rstrip().endswith(marker)
        )

    resolver = memoize_anchor_correction_resolver(choose_by_fact)
    finalized = finalize_source_selection_v02(
        selection,
        pack,
        build_numeric_candidates(pack),
        common_ir_source_sha256="a" * 64,
        resolve_ambiguous_value_anchor=resolver,
    )

    starts = [row.value_source.start_char for row in finalized[3]]
    assert starts == [text.index(anchor), text.rindex(anchor)]
    assert calls == ["scale-a", "scale-b"]

    # Re-running the same facts (merged/unmerged fallback) is a cache hit.
    finalize_source_selection_v02(
        selection,
        pack,
        build_numeric_candidates(pack),
        common_ir_source_sha256="a" * 64,
        resolve_ambiguous_value_anchor=resolver,
    )
    assert calls == ["scale-a", "scale-b"]


def test_worker_resolver_rejects_distinct_but_swapped_component_occurrences() -> None:
    text = "A지원금 최대 1억원; B지원금 최대 1억원"
    pack = _lineaged_pack(text)
    anchor = "최대 1억원"
    selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-existing-finalize",
        "candidate_pack_id": "existing-finalize-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [
            {
                "support_component_id": "opaque-component-17",
                "component_kind": "support_package",
                "source_block_ids": ["body[0]"],
                "name_anchor": {
                    "source_block_id": "body[0]",
                    "anchor_text": "A지원금",
                },
            },
            {
                "support_component_id": "opaque-component-93",
                "component_kind": "support_package",
                "source_block_ids": ["body[0]"],
                "name_anchor": {
                    "source_block_id": "body[0]",
                    "anchor_text": "B지원금",
                },
            },
        ],
        "facts": [
            _fact(
                "scale-a",
                "support_scale",
                anchor,
                primary_component_id="opaque-component-17",
            ),
            _fact(
                "scale-b",
                "support_scale",
                anchor,
                primary_component_id="opaque-component-93",
            ),
        ],
    })

    def choose_swapped(request: AnchorCorrectionRequest) -> str:
        wrong_marker = (
            "B지원금"
            if request.primary_component_id == "opaque-component-17"
            else "A지원금"
        )
        return next(
            candidate.candidate_id
            for candidate in request.candidates
            if candidate.context_before.rstrip().endswith(wrong_marker)
        )

    with pytest.raises(
        AmbiguousAnchorCorrectionError,
        match="not uniquely bound to local semantic evidence",
    ):
        finalize_source_selection_v02(
            selection,
            pack,
            build_numeric_candidates(pack),
            common_ir_source_sha256="a" * 64,
            resolve_ambiguous_value_anchor=choose_swapped,
        )


def test_repeated_anchor_assignment_fails_closed_when_resolver_reuses_one_span() -> None:
    text = "A지원금 최대 1억원; B지원금 최대 1억원"
    pack = _lineaged_pack(text)
    selection = _selection(
        _fact("scale-a", "support_scale", "최대 1억원"),
        _fact("scale-b", "support_scale", "최대 1억원"),
    )

    def always_same(request: AnchorCorrectionRequest) -> str:
        return sorted(
            request.candidates, key=lambda candidate: candidate.candidate_id
        )[0].candidate_id

    with pytest.raises(DuplicateResolvedValueSourceSpanError):
        finalize_source_selection_v02(
            selection,
            pack,
            build_numeric_candidates(pack),
            common_ir_source_sha256="a" * 64,
            resolve_ambiguous_value_anchor=memoize_anchor_correction_resolver(always_same),
        )


def test_finalizer_rejects_two_raw_facts_owning_one_materialized_span() -> None:
    pack = _pack("지원내용: 컨설팅 제공")
    selection = _selection(
        _fact("purpose", "purpose_goal", "컨설팅 제공"),
        _fact("content", "support_content", "컨설팅 제공"),
    )
    start = pack.blocks[0].text.index("컨설팅 제공")
    source = ValueSource(
        source_block_id="body[0]", start_char=start, end_char=start + len("컨설팅 제공")
    )

    with pytest.raises(DuplicateResolvedValueSourceSpanError) as raised:
        finalize_source_selection_v02(
            selection,
            pack,
            build_numeric_candidates(pack),
            require_support_cap_completeness=False,
            value_source_overrides={"purpose": source, "content": source},
        )

    assert raised.value.finalize_stage == "value_source_uniqueness_validation"
    assert raised.value.conflicts == [
        {"fact_id": "purpose", "field_name": "purpose_goal"},
        {"fact_id": "content", "field_name": "support_content"},
    ]


def test_financial_eligibility_misclassified_as_scale_is_typed_and_fail_closed() -> None:
    pack = _pack("신청자격: 연매출 최대 800백만원인 기업")
    selection = _selection(
        _fact("financial-threshold", "support_scale", "연매출 최대 800백만원")
    )

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(
            selection,
            pack,
            build_numeric_candidates(pack),
        )

    assert raised.value.finalize_stage == "support_scale_semantics_validation"
    assert raised.value.repair_payload() == [{
        "fact_id": "financial-threshold",
        "source_block_id": "body[0]",
        "reason": "applicant_financial_eligibility_threshold",
        "numeric_candidate_count": 0,
        "derived_measure_count": 0,
    }]


@pytest.mark.parametrize(
    "text",
    [
        "신청자격 연매출 5억원 이하 및 지원금 최대 1억원",
        "총사업비 5억원 중 지원금 최대 1억원",
    ],
)
def test_atomic_cap_fact_cannot_absorb_an_unrelated_numeric_value(text: str) -> None:
    pack = _pack(text)
    broad = _selection(_fact("broad", "support_scale", text))

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(broad, pack, build_numeric_candidates(pack))

    assert raised.value.do_not_restore_fact_ids == frozenset({"broad"})
    assert raised.value.required_support_scale_anchors() == [{
        "source_block_id": "body[0]",
        "anchor_text": "최대 1억원",
    }]
    assert raised.value.repair_payload()[0]["reason"] == (
        "support_cap_span_contains_unrelated_numeric"
    )

    exact = _selection(_fact("exact", "support_scale", "최대 1억원"))
    finalized = finalize_source_selection_v02(
        exact,
        pack,
        build_numeric_candidates(pack),
    )
    assert [fact.fact_id for fact in finalized[0].facts] == ["exact"]


@pytest.mark.parametrize(
    ("text", "anchor"),
    [
        ("지원대상: 연매출 최대 8억원인 기업", "최대 8억원"),
        (
            "매출액 또는 영업이익 10% 이상 감소기업: 1% 추가 지원",
            "매출액 또는 영업이익 10% 이상",
        ),
    ],
)
def test_financial_eligibility_needs_benefit_ownership_inside_selected_span(
    text: str,
    anchor: str,
) -> None:
    pack = _pack(text)
    selection = _selection(_fact("financial-threshold", "support_scale", anchor))

    with pytest.raises(SupportScaleFactRepairError):
        finalize_source_selection_v02(
            selection,
            pack,
            build_numeric_candidates(pack),
            require_support_cap_completeness=False,
        )


@pytest.mark.parametrize(
    "text",
    [
        "지원대상은 연매출 10억원 이하이며, 지원금 최대 1억원",
        "총사업비 5억원 중 지원금 최대 1억원",
        "지원금은 총사업비의 최대 80%",
        "지원금은 총사업비 대비 최대 80%",
        "지원금은 총사업비 중 최대 80%",
        "정부지원금: 총사업비의 최대 70% 이내",
        "정부지원금 총사업비의 최대 70% 이내",
        "정부지원금은 총사업비 기준 최대 70% 이내",
        "최대 1억원 지원",
        "연매출 10억원 이하 기업에 최대 1억원 지급",
        "지원대상은 연매출 10억원 이하인 기업이며, 최대 1억원 지원",
    ],
)
def test_cap_candidate_accepts_locally_owned_support_cap(text: str) -> None:
    assert [candidate.anchor_text for candidate in extract_explicit_support_cap_candidates(_pack(text))]


@pytest.mark.parametrize(
    "text",
    [
        "지원대상: 연매출 최대 800백만원",
        "총사업비 최대 5억원",
        "정부지원금 70%, 자부담 최대 1억원",
        "자부담 최대 1억원 지원",
        "총사업비 최대 1억원 지급",
        "총사업비 최대 1억원 지원",
    ],
)
def test_cap_candidate_rejects_non_benefit_bound_amount(text: str) -> None:
    assert extract_explicit_support_cap_candidates(_pack(text)) == []


@pytest.mark.parametrize(
    ("text", "anchor"),
    [
        (
            "중소벤처기업부 2025년 민관협력 사업을 통해 1억원의 사업화 자금을 지원",
            "중소벤처기업부 2025년 민관협력 사업을 통해 1억원의 사업화 자금을 지원",
        ),
        (
            "현지 투자사 대상 IR은 수요가 있는 기업 중 8개사를 선별해 진행",
            "8개사를 선별",
        ),
        (
            "매출액 또는 영업이익 10% 이상 감소기업: 1% 추가 지원",
            "매출액 또는 영업이익 10% 이상 감소기업: 1% 추가 지원",
        ),
        (
            "매출액 10% 이상 감소기업 지원금 1% 추가 지원",
            "매출액 10% 이상 감소기업 지원금 1% 추가 지원",
        ),
    ],
)
def test_valid_scale_is_not_rejected_by_year_investor_or_conditional_support(
    text: str,
    anchor: str,
) -> None:
    """Gold semantics must survive broad duration/financial keyword guards."""

    pack = _pack(text)
    selection = _selection(_fact("scale", "support_scale", anchor))

    finalized = finalize_source_selection_v02(
        selection,
        pack,
        build_numeric_candidates(pack),
        require_support_cap_completeness=False,
    )

    assert [fact.fact_id for fact in finalized[0].facts] == ["scale"]
    if text in {
        "매출액 또는 영업이익 10% 이상 감소기업: 1% 추가 지원",
        "매출액 10% 이상 감소기업 지원금 1% 추가 지원",
    }:
        measures = finalized[5][0].measures
        assert len(measures) == 1
        assert measures[0].source_numeric_candidate_id == "body[0]#num[1]"
        assert measures[0].comparator.value == "eq"
        assert measures[0].lower_value == measures[0].upper_value == 100


@pytest.mark.parametrize(
    ("text", "expected_type", "expected_value"),
    [
        (
            "전년도 매출액 1억원 대비 10% 이상 증가 시 지원금 5천만원 지급",
            "amount",
            50_000_000,
        ),
        ("영업이익 10% 이상 감소기업: 1% 추가 지원", "rate", 100),
        ("부채비율 200% 이하 기업: 5% 추가 지원", "rate", 500),
        ("고용인원 10명 이상 기업에 2명 추가 지원", "count", 2),
        ("고용인원 10명 지원대상 기업에 2명 추가 지원", "count", 2),
        (
            "매출 10억원 지원자격 기업에 지원금 1억원 지급",
            "amount",
            100_000_000,
        ),
    ],
)
def test_eligibility_numerics_are_not_materialized_as_support_measures(
    text: str,
    expected_type: str,
    expected_value: int,
) -> None:
    pack = _pack(text)
    finalized = finalize_source_selection_v02(
        _selection(_fact("scale", "support_scale", text)),
        pack,
        build_numeric_candidates(pack),
        require_support_cap_completeness=False,
    )

    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].measure_type.value == expected_type
    assert measures[0].lower_value == measures[0].upper_value == expected_value


def test_typed_activity_count_repair_preserves_other_prior_facts_only() -> None:
    text = "사업 목적: 창업 촉진 / 멘토링 3회 / 대상 창업기업"
    pack = _pack(text)
    previous = _selection(
        _fact("purpose", "purpose_goal", "창업 촉진"),
        _fact("bad", "support_scale", "멘토링 3회"),
    )
    current = _selection(_fact("applicant", "applicant_eligibility", "창업기업"))

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(
            previous,
            pack,
            build_numeric_candidates(pack),
            require_support_cap_completeness=False,
        )

    assert raised.value.do_not_restore_fact_ids == frozenset({"bad"})
    assert raised.value.repair_payload()[0]["reason"] == (
        "activity_count_not_support_scale"
    )
    merged, changes = preserve_prior_server_validated_facts_v02(
        previous,
        current,
        pack,
        do_not_restore_fact_ids=raised.value.do_not_restore_fact_ids,
    )

    assert {fact.field_name.value for fact in merged.facts} == {
        "applicant_eligibility",
        "purpose_goal",
    }
    assert {change["original_fact_id"] for change in changes} == {"purpose"}
    finalize_source_selection_v02(
        merged,
        pack,
        build_numeric_candidates(pack),
        require_support_cap_completeness=False,
    )


@pytest.mark.parametrize(
    ("text", "expected_bps"),
    [
        ("지원금은 매출 대비 10%", 1_000),
        ("지원금은 매출 대비 10% 이내", 1_000),
        ("지원금은 영업이익 대비 5%", 500),
    ],
)
def test_explicit_support_basis_rate_is_not_filtered_as_eligibility(
    text: str,
    expected_bps: int,
) -> None:
    pack = _pack(text)
    finalized = finalize_source_selection_v02(
        _selection(_fact("scale", "support_scale", text)),
        pack,
        build_numeric_candidates(pack),
        require_support_cap_completeness=False,
    )

    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].measure_type.value == "rate"
    assert measures[0].upper_value == expected_bps


def test_support_owner_before_a_later_eligibility_role_does_not_override_it() -> None:
    text = "지원금 신청자격: 매출 대비 10% 감소"
    pack = _pack(text)

    with pytest.raises(SupportScaleFactRepairError):
        finalize_source_selection_v02(
            _selection(_fact("scale", "support_scale", text)),
            pack,
            build_numeric_candidates(pack),
            require_support_cap_completeness=False,
        )


def test_layout_equivalent_support_facts_are_normalized_in_one_scope() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "existing-finalize-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "existing finalization regression",
        "generator": "common_ir_v1_candidate_pack",
        "generator_version": "1.1.0",
        "common_ir_document_id": "hwpx:PBLN-existing-finalize",
        "blocks": [
            {
                "block_id": "wrapper[0]",
                "text": "- 컨설팅 지원",
                "relation": "candidate",
                "common_ir_block_id": "hwpx:block:wrapper",
                "common_ir_occurrence_ids": ["hwpx:occ:shared"],
            },
            {
                "block_id": "atomic[0]",
                "text": "① 컨설팅 지원",
                "relation": "candidate",
                "common_ir_block_id": "hwpx:block:atomic",
                "common_ir_occurrence_ids": ["hwpx:occ:shared"],
            },
        ],
    })
    selection = _selection(
        _fact(
            "content-bullet",
            "support_content",
            "- 컨설팅 지원",
            source_block_id="wrapper[0]",
        ),
        _fact(
            "content-enumerated",
            "support_content",
            "① 컨설팅 지원",
            source_block_id="atomic[0]",
        ),
    )

    finalized = finalize_source_selection_v02(
        selection,
        pack,
        build_numeric_candidates(pack),
        require_support_cap_completeness=False,
    )

    assert [fact.fact_id for fact in finalized[0].facts] == ["content-enumerated"]
    assert finalized[1] == [{
        "kind": "layout_equivalent_support_fact_removed",
        "removed_fact_id": "content-bullet",
        "retained_fact_id": "content-enumerated",
    }]


def test_equal_support_text_in_different_fields_is_not_semantically_merged() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "existing-finalize-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "existing finalization regression",
        "blocks": [
            {"block_id": "body[0]", "text": "컨설팅", "relation": "candidate"},
            {"block_id": "body[1]", "text": "컨설팅", "relation": "candidate"},
        ],
    })
    selection = _selection(
        _fact("method", "support_methods", "컨설팅"),
        _fact("item", "support_items", "컨설팅", source_block_id="body[1]"),
    )

    finalized = finalize_source_selection_v02(
        selection,
        pack,
        build_numeric_candidates(pack),
        require_support_cap_completeness=False,
    )

    assert [fact.fact_id for fact in finalized[0].facts] == ["method", "item"]
    assert finalized[1] == []


def test_decorated_equal_text_at_distinct_positions_is_not_merged_without_lineage() -> None:
    pack = _pack("1차 지원\n- 컨설팅 지원\n2차 지원\n① 컨설팅 지원")
    selection = _selection(
        _fact("first", "support_content", "- 컨설팅 지원"),
        _fact("second", "support_content", "① 컨설팅 지원"),
    )

    finalized = finalize_source_selection_v02(
        selection,
        pack,
        build_numeric_candidates(pack),
        require_support_cap_completeness=False,
    )

    assert [fact.fact_id for fact in finalized[0].facts] == ["first", "second"]
    assert finalized[1] == []


def test_shared_occurrence_facts_with_different_relations_are_not_merged() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "existing-finalize-pack",
        "notice_id": "PBLN-existing-finalize",
        "question": "existing finalization regression",
        "generator": "common_ir_v1_candidate_pack",
        "generator_version": "1.1.0",
        "common_ir_document_id": "hwpx:PBLN-existing-finalize",
        "blocks": [
            {
                "block_id": "wrapper[0]",
                "text": "- 컨설팅 지원",
                "relation": "candidate",
                "common_ir_block_id": "hwpx:block:wrapper",
                "common_ir_occurrence_ids": ["hwpx:occ:shared"],
            },
            {
                "block_id": "atomic[0]",
                "text": "① 컨설팅 지원",
                "relation": "candidate",
                "common_ir_block_id": "hwpx:block:atomic",
                "common_ir_occurrence_ids": ["hwpx:occ:shared"],
            },
            {
                "block_id": "beneficiary[0]",
                "text": "A기업",
                "relation": "candidate",
                "common_ir_block_id": "hwpx:block:a",
            },
            {
                "block_id": "beneficiary[1]",
                "text": "B기업",
                "relation": "candidate",
                "common_ir_block_id": "hwpx:block:b",
            },
        ],
    })
    first = _fact(
        "support-a",
        "support_content",
        "- 컨설팅 지원",
        source_block_id="wrapper[0]",
    )
    first["recipient_fact_ids"] = ["beneficiary-a"]
    second = _fact(
        "support-b",
        "support_content",
        "① 컨설팅 지원",
        source_block_id="atomic[0]",
    )
    second["recipient_fact_ids"] = ["beneficiary-b"]
    beneficiary_a = _fact(
        "beneficiary-a",
        "beneficiary",
        "A기업",
        source_block_id="beneficiary[0]",
    )
    beneficiary_a["subject_role"] = "financial_recipient"
    beneficiary_b = _fact(
        "beneficiary-b",
        "beneficiary",
        "B기업",
        source_block_id="beneficiary[1]",
    )
    beneficiary_b["subject_role"] = "financial_recipient"
    selection = _selection(beneficiary_a, beneficiary_b, first, second)

    normalized, changes = normalize_semantic_duplicate_facts_v02(selection, pack)

    facts = {fact.fact_id: fact for fact in normalized.facts}
    assert facts["support-a"].recipient_fact_ids == ["beneficiary-a"]
    assert facts["support-b"].recipient_fact_ids == ["beneficiary-b"]
    assert changes == []


def test_announcement_worker_uses_shared_finalizer_and_exposes_cap_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker must not recreate a private v0.2 finalize ordering."""

    pack = _pack("지원금 최대 1억원")
    selection = _selection(_fact("purpose", "purpose_goal", "지원금 최대 1억원"))
    seen: dict[str, object] = {}

    def fake_llm(*_args: object, **kwargs: object) -> SourceSelectionExtractionV02:
        seen["request"] = kwargs["payload"]
        return selection

    def fake_finalize(*args: object, **kwargs: object):
        seen["finalize_args"] = args
        seen["finalize_kwargs"] = kwargs
        return selection, [], [], [], [], []

    monkeypatch.setattr(announcement_profiles, "_call_llm", fake_llm)
    monkeypatch.setattr(announcement_profiles, "_trusted_source_sha256", lambda *_: "a" * 64)
    monkeypatch.setattr(announcement_profiles, "finalize_source_selection_v02", fake_finalize)
    monkeypatch.setattr(announcement_profiles, "_selection_artifact", lambda **_kwargs: {})
    monkeypatch.setattr(announcement_profiles, "assemble_final_profile_v02", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(announcement_profiles, "common_ir_v1_metadata", lambda _document: {})
    monkeypatch.setattr(announcement_profiles, "candidate_pack_artifact", lambda *_args: {})
    monkeypatch.setattr(announcement_profiles, "build_corrected_anchor_audit", lambda *_args: [])

    profile = announcement_profiles._select_and_assemble(
        {"document": {"document_id": "test"}}, pack, {}, object(), "test-model"
    )

    assert profile["processing_metadata"]["candidate_pack_artifact"] == {}
    assert (
        profile["processing_metadata"]["generation_lineage"][
            "explicit_support_cap_candidate_version"
        ]
        == EXPLICIT_SUPPORT_CAP_CANDIDATE_VERSION
    )
    assert seen["finalize_args"][:3] == (selection, pack, build_numeric_candidates(pack))
    assert seen["finalize_kwargs"] == {
        "common_ir_source_sha256": "a" * 64,
        "resolve_ambiguous_value_anchor": ANY,
        "typed_repair_error": None,
    }
    assert seen["request"]["explicit_support_cap_anchors"] == [{
        "source_block_id": "body[0]", "anchor_text": "최대 1억원",
    }]
    assert (
        seen["request"]["explicit_support_cap_candidate_version"]
        == EXPLICIT_SUPPORT_CAP_CANDIDATE_VERSION
    )
