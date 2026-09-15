"""Focused regressions for the conservative Existing-profile list audit."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from worker import announcement_profiles
from worker import vendor  # noqa: F401 - installs the vendored package

from semantic_structuring.explicit_list_completeness import (
    ExplicitListCompletenessError,
    discover_explicit_list_item_regions,
    explicit_list_repair_has_invalid_overlaps_v02,
    validate_explicit_list_completeness_v02,
)
from semantic_structuring.models import CandidatePack, FactField
from semantic_structuring.profile_v02 import ValueSource
from semantic_structuring.source_selection import (
    SourceSelectionRepairIssuesError,
    SourceSelectionExtractionV02,
    build_numeric_candidates,
    finalize_source_selection_v02,
    required_explicit_list_item_regions_for_typed_repair_v02,
    required_support_scale_anchors_for_typed_repair_v02,
    required_support_scale_fact_repairs_for_typed_repair_v02,
    validate_typed_repair_replacements_v02,
)


def _pack(*blocks: dict) -> CandidatePack:
    return CandidatePack.model_validate({
        "pack_id": "list-audit-pack",
        "notice_id": "PBLN-list-audit",
        "question": "list audit",
        "generator": "semantic_structuring.common_ir_v1",
        "generator_version": "1",
        "common_ir_document_id": "hwpx:PBLN-list-audit",
        "blocks": blocks,
    })


def _block(
    block_id: str,
    text: str,
    kind: str | None,
    order: int,
    *,
    section_id: str = "main_notice",
) -> dict:
    return {
        "block_id": block_id,
        "text": text,
        "relation": "candidate",
        "block_kind": kind,
        "section_id": section_id,
        "source_order": order,
        "common_ir_block_id": f"ir:{block_id}",
        "common_ir_occurrence_ids": [f"occ:{block_id}"],
    }


def _evidence(fact_id: str, field: FactField, block_id: str, start: int, end: int):
    return SimpleNamespace(
        fact_id=fact_id,
        field_name=field,
        value_source=ValueSource(source_block_id=block_id, start_char=start, end_char=end),
    )


def test_inventory_requires_exact_heading_visible_markers_and_stops_at_operational_heading() -> None:
    pack = _pack(
        _block("h1", "지원 대상:", "heading", 0),
        _block("b1", "- 창업기업\n- 예비창업자", "paragraph", 1),
        _block("close", "제출서류", "heading", 2),
        _block("b2", "- 사업자등록증", "paragraph", 3),
        _block("prose", "지원대상은 별도 안내합니다.", "paragraph", 4),
    )

    regions = discover_explicit_list_item_regions(pack)

    assert [(item.source_block_id, item.field_group) for item in regions] == [
        ("b1", "eligibility"), ("b1", "eligibility"),
    ]


def test_inventory_accepts_omitted_kind_atomic_bullet_after_heading() -> None:
    pack = _pack(
        _block("heading", "지원대상", "heading", 0),
        _block("item", "- 창업기업", None, 1),
    )

    regions = discover_explicit_list_item_regions(pack)

    assert [(item.source_block_id, item.field_group, item.item_text) for item in regions] == [
        ("item", "eligibility", "- 창업기업"),
    ]


def test_inventory_closes_on_any_unrelated_heading_and_section_change() -> None:
    pack = _pack(
        _block("h1", "지원내용", "heading", 0),
        _block("included", "- 컨설팅", "paragraph", 1),
        _block("other", "교육일정", "heading", 2),
        _block("excluded", "- 5월 집합교육", "paragraph", 3),
        _block("h2", "지원대상", "heading", 4),
        _block(
            "other-section",
            "- 별도 첨부 대상",
            "paragraph",
            5,
            section_id="main_notice_resume_1",
        ),
    )

    regions = discover_explicit_list_item_regions(pack)

    assert [(item.source_block_id, item.field_group) for item in regions] == [
        ("included", "support"),
    ]


def test_inventory_closes_when_routing_left_a_source_order_gap() -> None:
    pack = _pack(
        _block("h", "지원내용", "heading", 0),
        _block("included", "- 컨설팅", "paragraph", 1),
        _block("gap", "- 평가위원 구성", "paragraph", 4),
    )

    regions = discover_explicit_list_item_regions(pack)

    assert [item.source_block_id for item in regions] == ["included"]


def test_inventory_closes_after_plain_paragraph_before_unrelated_bullet() -> None:
    pack = _pack(
        _block("h", "지원내용", "heading", 0),
        _block("included", "- 컨설팅", "paragraph", 1),
        _block("plain", "자세한 일정은 별도 공지합니다.", "paragraph", 2),
        _block("excluded", "- 제출서류 원본", "paragraph", 3),
    )

    assert [
        item.item_text for item in discover_explicit_list_item_regions(pack)
    ] == ["- 컨설팅"]


@pytest.mark.parametrize(
    "marker",
    ["1.", "2)", "(3)", "①", "⑳", "가.", "나)"],
)
def test_inventory_accepts_high_confidence_numbering_markers(marker: str) -> None:
    text = f"{marker} 명시 항목"
    pack = _pack(_block("h", "지원내용", "heading", 0), _block("item", text, "paragraph", 1))

    assert [item.item_text for item in discover_explicit_list_item_regions(pack)] == [text]


@pytest.mark.parametrize(
    "text",
    ["①창업기업", "•창업기업", "1.창업기업", "가.창업기업"],
)
def test_inventory_accepts_compact_unambiguous_list_markers(text: str) -> None:
    pack = _pack(
        _block("h", "지원대상", "heading", 0),
        _block("item", text, "paragraph", 1),
    )

    assert [item.item_text for item in discover_explicit_list_item_regions(pack)] == [text]


@pytest.mark.parametrize(
    "text",
    ["1.2 버전 안내", "v1.2 버전 안내", "-하이픈으로 시작하는 일반 문장"],
)
def test_inventory_rejects_compact_numeric_version_and_hyphen_prose(text: str) -> None:
    pack = _pack(
        _block("h", "지원내용", "heading", 0),
        _block("body", text, "paragraph", 1),
    )

    assert discover_explicit_list_item_regions(pack) == []


@pytest.mark.parametrize(
    "text",
    [
        "1",
        "(1)",
        "-",
        "* 단, 동일 대표자는 중복 신청할 수 없습니다",
        "지원내용은 추후 안내합니다",
    ],
)
def test_inventory_rejects_bare_markers_and_unmarked_prose(text: str) -> None:
    pack = _pack(_block("h", "지원내용", "heading", 0), _block("body", text, "paragraph", 1))

    assert discover_explicit_list_item_regions(pack) == []


def test_heading_body_can_hold_exact_heading_and_marked_items() -> None:
    text = "지원대상:\n- 창업기업\n(2) 예비창업자"
    pack = _pack(_block("combined", text, "heading_body", 0))

    regions = discover_explicit_list_item_regions(pack)

    assert [(item.start_char, item.end_char, item.item_text) for item in regions] == [
        (len("지원대상:\n"), len("지원대상:\n- 창업기업"), "- 창업기업"),
        (len("지원대상:\n- 창업기업\n"), len(text), "(2) 예비창업자"),
    ]


def test_inventory_keeps_indented_continuation_in_preceding_item_and_next_marker() -> None:
    text = "- 창업기업으로서\n  업력 7년 이내\n- 예비창업자"
    pack = _pack(
        _block("heading", "지원대상", "heading", 0),
        _block("items", text, "paragraph", 1),
    )

    regions = discover_explicit_list_item_regions(pack)

    assert [(item.start_char, item.end_char, item.item_text) for item in regions] == [
        (0, len("- 창업기업으로서\n  업력 7년 이내"), "- 창업기업으로서\n  업력 7년 이내"),
        (len("- 창업기업으로서\n  업력 7년 이내\n"), len(text), "- 예비창업자"),
    ]


def test_heading_body_keeps_indented_continuation_in_preceding_item() -> None:
    text = "지원대상\n- 창업기업으로서\n  업력 7년 이내\n- 예비창업자"
    pack = _pack(_block("combined", text, "heading_body", 0))

    regions = discover_explicit_list_item_regions(pack)

    first_start = len("지원대상\n")
    first_text = "- 창업기업으로서\n  업력 7년 이내"
    assert [(item.start_char, item.end_char, item.item_text) for item in regions] == [
        (first_start, first_start + len(first_text), first_text),
        (first_start + len(first_text) + 1, len(text), "- 예비창업자"),
    ]


def test_inventory_same_block_unindented_prose_still_closes_scope() -> None:
    text = "- 창업기업\n자세한 조건은 별도 공지합니다.\n- 예비창업자"
    pack = _pack(
        _block("heading", "지원대상", "heading", 0),
        _block("items", text, "paragraph", 1),
    )

    assert [
        item.item_text for item in discover_explicit_list_item_regions(pack)
    ] == ["- 창업기업"]


def test_inventory_keeps_bullets_before_same_block_footnote_then_closes_scope() -> None:
    text = "- 창업기업\n- 예비창업자\n※ 세부요건은 별도 안내를 참고"
    pack = _pack(
        _block("heading", "지원대상", "heading", 0),
        _block("items", text, "paragraph", 1),
        _block("after", "- 제출서류 대상", "paragraph", 2),
    )

    assert [
        item.item_text for item in discover_explicit_list_item_regions(pack)
    ] == ["- 창업기업", "- 예비창업자"]


def test_heading_body_keeps_bullets_before_footnote_then_closes_scope() -> None:
    text = "지원대상\n- 창업기업\n- 예비창업자\n※ 세부요건은 별도 안내를 참고"
    pack = _pack(
        _block("combined", text, "heading_body", 0),
        _block("after", "- 제출서류 대상", "paragraph", 1),
    )

    assert [
        item.item_text for item in discover_explicit_list_item_regions(pack)
    ] == ["- 창업기업", "- 예비창업자"]


def test_heading_keyword_inside_heading_body_prose_does_not_open_scope() -> None:
    pack = _pack(
        _block("combined", "지원대상은 다음과 같습니다\n- 창업기업", "heading_body", 0)
    )

    assert discover_explicit_list_item_regions(pack) == []


def test_heading_body_stops_before_unmarked_inline_subsection() -> None:
    text = "지원대상\n- 창업기업\n제출서류\n- 사업자등록증"
    pack = _pack(_block("combined", text, "heading_body", 0))

    assert [
        item.item_text for item in discover_explicit_list_item_regions(pack)
    ] == ["- 창업기업"]


def test_heading_body_inline_subsection_also_closes_following_block_scope() -> None:
    pack = _pack(
        _block(
            "combined",
            "지원대상\n- 창업기업\n제출서류",
            "heading_body",
            0,
        ),
        _block("next", "- 사업자등록증", "paragraph", 1),
    )

    assert [
        item.item_text for item in discover_explicit_list_item_regions(pack)
    ] == ["- 창업기업"]


def test_wrong_field_is_repair_excluded_and_error_never_persists_item_text() -> None:
    text = "- 창업기업"
    pack = _pack(_block("h", "지원대상", "heading", 0), _block("item", text, "paragraph", 1))
    wrong = _evidence("wrong", FactField.SUPPORT_CONTENT, "item", 0, len(text))

    with pytest.raises(ExplicitListCompletenessError) as raised:
        validate_explicit_list_completeness_v02(pack, [wrong])

    error = raised.value
    assert error.do_not_restore_fact_ids == frozenset({"wrong"})
    assert error.required_list_item_regions == [{
        "source_block_id": "item",
        "start_char": 0,
        "end_char": len(text),
        "field_group": "eligibility",
        "item_text": text,
        "allowed_field_names": [
            "applicable_entity",
            "applicant_eligibility",
            "beneficiary",
            "eligibility_conditions",
            "participation_requirements",
            "support_target",
        ],
        "replace_fact_ids": ["wrong"],
    }]
    assert "창업기업" not in str(error)
    assert "지원대상" not in str(error)


def test_compatible_list_fact_must_cover_the_full_substantive_item() -> None:
    text = "- 업력 7년 이내이면서 매출 10억원 이하인 기업"
    content = text[2:]
    pack = _pack(
        _block("h", "지원대상", "heading", 0),
        _block("item", text, "paragraph", 1),
    )
    short = _evidence(
        "noun-only", FactField.APPLICANT_ELIGIBILITY,
        "item", text.rindex("기업"), len(text),
    )

    with pytest.raises(ExplicitListCompletenessError) as raised:
        validate_explicit_list_completeness_v02(pack, [short])

    required = raised.value.required_list_item_regions
    assert required[0]["replace_fact_ids"] == []
    assert not explicit_list_repair_has_invalid_overlaps_v02(pack, [short], required)

    full_content = _evidence(
        "full-content", FactField.APPLICANT_ELIGIBILITY,
        "item", 2, 2 + len(content),
    )
    validate_explicit_list_completeness_v02(pack, [full_content])
    assert not explicit_list_repair_has_invalid_overlaps_v02(
        pack, [full_content], required,
    )


def test_exact_support_cap_can_remain_beside_repaired_full_list_item() -> None:
    text = "- 기업당 최대 5천만원 지원, 자부담 20%"
    cap_text = "기업당 최대 5천만원"
    cap_start = text.index(cap_text)
    pack = _pack(
        _block("h", "지원내용", "heading", 0),
        _block("item", text, "paragraph", 1),
    )
    cap = _evidence(
        "cap", FactField.SUPPORT_SCALE,
        "item", cap_start, cap_start + len(cap_text),
    )

    with pytest.raises(ExplicitListCompletenessError) as raised:
        validate_explicit_list_completeness_v02(pack, [cap])

    required = raised.value.required_list_item_regions
    assert raised.value.do_not_restore_fact_ids == frozenset()
    assert required[0]["replace_fact_ids"] == []

    full_item = _evidence(
        "item-content", FactField.SUPPORT_CONTENT, "item", 2, len(text),
    )
    validate_explicit_list_completeness_v02(pack, [cap, full_item])
    assert not explicit_list_repair_has_invalid_overlaps_v02(
        pack, [cap, full_item], required,
    )


def test_repair_drops_wrong_field_eligibility_fragment_but_keeps_valid_subfacts() -> None:
    text = "- 창업기업으로서 업력 7년 이내, 컨설팅 지원 (기업당 최대 5천만원)"
    pack = _pack(
        _block("h", "지원대상", "heading", 0),
        _block("item", text, "paragraph", 1),
    )
    required = [discover_explicit_list_item_regions(pack)[0].repair_payload()]
    wrong_fragment = _evidence(
        "wrong-fragment", FactField.SUPPORT_CONTENT, "item",
        text.index("창업기업"), text.index("창업기업") + len("창업기업"),
    )
    eligibility_subfact = _evidence(
        "eligibility-subfact", FactField.ELIGIBILITY_CONDITIONS, "item",
        text.index("업력"), text.index("업력") + len("업력 7년 이내"),
    )
    benefit_subfact = _evidence(
        "benefit-subfact", FactField.SUPPORT_CONTENT, "item",
        text.index("컨설팅"), text.index("컨설팅") + len("컨설팅 지원"),
    )
    cap_subfact = _evidence(
        "cap-subfact", FactField.SUPPORT_SCALE, "item",
        text.index("기업당"), text.index("기업당") + len("기업당 최대 5천만원"),
    )

    assert explicit_list_repair_has_invalid_overlaps_v02(
        pack, [wrong_fragment], required,
    )
    assert not explicit_list_repair_has_invalid_overlaps_v02(
        pack, [eligibility_subfact, benefit_subfact, cap_subfact], required,
    )


def test_same_group_subfact_does_not_discharge_wrapped_item_coverage() -> None:
    text = "- 창업기업으로서\n  업력 7년 이내"
    pack = _pack(
        _block("h", "지원대상", "heading", 0),
        _block("item", text, "paragraph", 1),
    )
    condition_start = text.index("업력")
    condition = _evidence(
        "condition", FactField.ELIGIBILITY_CONDITIONS, "item",
        condition_start, condition_start + len("업력 7년 이내"),
    )

    with pytest.raises(ExplicitListCompletenessError):
        validate_explicit_list_completeness_v02(pack, [condition])


@pytest.mark.parametrize(
    ("heading", "field"),
    [
        ("신청자격", FactField.ELIGIBILITY_CONDITIONS),
        ("지원제외 대상", FactField.EXCLUSIONS),
        ("지원항목 및 내용", FactField.SUPPORT_ITEMS),
    ],
)
def test_each_heading_group_accepts_only_its_compatible_fact_family(
    heading: str, field: FactField,
) -> None:
    text = "- 명시 항목"
    pack = _pack(_block("h", heading, "heading", 0), _block("item", text, "paragraph", 1))

    validate_explicit_list_completeness_v02(pack, [_evidence("right", field, "item", 2, len(text))])


def test_one_parent_block_span_cannot_discharge_two_list_items() -> None:
    text = "- 창업기업\n- 예비창업자"
    pack = _pack(_block("h", "신청자격", "heading", 0), _block("items", text, "paragraph", 1))
    parent = _evidence("all", FactField.APPLICANT_ELIGIBILITY, "items", 0, len(text))

    with pytest.raises(ExplicitListCompletenessError) as raised:
        validate_explicit_list_completeness_v02(pack, [parent])

    assert len(raised.value.required_list_item_regions) == 2
    assert raised.value.do_not_restore_fact_ids == frozenset({"all"})
    assert all(
        region["replace_fact_ids"] == ["all"]
        for region in raised.value.required_list_item_regions
    )


def test_repair_rejects_same_invalid_fact_even_under_a_new_id() -> None:
    text = "- 창업기업"
    pack = _pack(_block("h", "지원대상", "heading", 0), _block("item", text, "paragraph", 1))
    previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "component_decision": {"mode": "none", "no_component_reason": "one notice scope"},
        "support_components": [],
        "facts": [{
            "fact_id": "wrong",
            "field_name": "support_content",
            "status": "identified",
            "value_anchor": {"source_block_id": "item", "anchor_text": text},
        }],
    })
    with pytest.raises(ExplicitListCompletenessError) as raised:
        finalize_source_selection_v02(previous, pack, build_numeric_candidates(pack))
    repaired_plus_old = SourceSelectionExtractionV02.model_validate({
        **previous.model_dump(mode="json"),
        "facts": [
            {
                **previous.facts[0].model_dump(mode="json"),
                "fact_id": "renamed-but-still-wrong",
            },
            {
                **previous.facts[0].model_dump(mode="json"),
                "fact_id": "right",
                "field_name": "applicant_eligibility",
            },
        ],
    })

    with pytest.raises(ExplicitListCompletenessError):
        validate_typed_repair_replacements_v02(
            previous, repaired_plus_old, raised.value
        )


def test_repair_allows_same_id_when_field_is_corrected() -> None:
    text = "- 창업기업"
    pack = _pack(_block("h", "지원대상", "heading", 0), _block("item", text, "paragraph", 1))
    previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "component_decision": {"mode": "none", "no_component_reason": "one notice scope"},
        "support_components": [],
        "facts": [{
            "fact_id": "repair-me",
            "field_name": "support_content",
            "status": "identified",
            "value_anchor": {"source_block_id": "item", "anchor_text": text},
        }],
    })
    with pytest.raises(ExplicitListCompletenessError) as raised:
        finalize_source_selection_v02(previous, pack, build_numeric_candidates(pack))
    corrected = previous.model_copy(update={
        "facts": [previous.facts[0].model_copy(update={
            "field_name": FactField.APPLICANT_ELIGIBILITY,
        })],
    })

    validate_typed_repair_replacements_v02(previous, corrected, raised.value)
    finalize_source_selection_v02(corrected, pack, build_numeric_candidates(pack))


def test_finalizer_runs_list_audit_after_materialization_and_before_components() -> None:
    text = "- 창업기업"
    pack = _pack(_block("h", "지원대상", "heading", 0), _block("item", text, "paragraph", 1))
    selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "component_decision": {"mode": "none", "no_component_reason": "one notice scope"},
        "support_components": [],
        "facts": [{
            "fact_id": "wrong",
            "field_name": "support_content",
            "status": "identified",
            "value_anchor": {"source_block_id": "item", "anchor_text": text},
        }],
    })

    with pytest.raises(ExplicitListCompletenessError) as raised:
        finalize_source_selection_v02(selection, pack, build_numeric_candidates(pack))

    assert raised.value.finalize_stage == "explicit_list_completeness_validation"


def test_native_line_atom_coverage_projects_back_to_original_item_coordinates() -> None:
    text = "- 창업기업"
    heading = _block("h", "모집대상", "heading", 0)
    parent = _block("parent", text, "paragraph", 1)
    atom = {
        **_block("line", text, "native_line_atom", 1),
        "native_parent_block_id": "parent",
        "native_start_char": 0,
        "native_end_char": len(text),
        "common_ir_block_id": "ir:parent",
        "common_ir_occurrence_ids": ["occ:parent"],
    }
    pack = CandidatePack.model_validate({
        "pack_id": "list-native-pack",
        "notice_id": "PBLN-list-audit",
        "question": "list audit",
        "generator": "semantic_structuring.native_exact_transform",
        "generator_version": "1",
        "common_ir_document_id": "hwpx:PBLN-list-audit",
        "parent_pack_id": "source-pack",
        "parent_generator": "semantic_structuring.common_ir_v1",
        "parent_generator_version": "1",
        "blocks": [heading, parent, atom],
    })

    validate_explicit_list_completeness_v02(
        pack, [_evidence("eligible", FactField.APPLICANT_ELIGIBILITY, "line", 2, len(text))]
    )


def _native_composite_pack() -> CandidatePack:
    item_text = "- 창업기업"
    detail_text = "업력 7년 이내"
    extra_text = "소재지 서울"
    heading = _block("h", "지원대상", "heading", 0)
    item = _block("item", item_text, "paragraph", 1)
    detail = _block("detail", detail_text, "paragraph", 2)
    extra = _block("extra", extra_text, "paragraph", 3)
    composite = {
        "block_id": "composite",
        "text": f"{item_text} {detail_text}",
        "relation": "candidate",
        "block_kind": "native_composite",
        "section_id": "main_notice",
        "source_order": 1,
        "source_occurrence_ids": ["occ:item", "occ:detail"],
        "common_ir_block_id": "ir:item",
        "common_ir_occurrence_ids": ["occ:item", "occ:detail"],
        "source_spans": [
            {
                "source_block_id": "item",
                "exact_text": item_text,
                "start_char": 0,
                "end_char": len(item_text),
                "separator_after": " ",
                "source_order": 1,
                "section_id": "main_notice",
                "common_ir_block_id": "ir:item",
                "common_ir_occurrence_ids": ["occ:item"],
            },
            {
                "source_block_id": "detail",
                "exact_text": detail_text,
                "start_char": 0,
                "end_char": len(detail_text),
                "separator_after": "",
                "source_order": 2,
                "section_id": "main_notice",
                "common_ir_block_id": "ir:detail",
                "common_ir_occurrence_ids": ["occ:detail"],
            },
        ],
    }
    composite_wide = {
        **composite,
        "block_id": "composite-wide",
        "text": f"{item_text} {detail_text} {extra_text}",
        "source_occurrence_ids": ["occ:item", "occ:detail", "occ:extra"],
        "common_ir_occurrence_ids": ["occ:item", "occ:detail", "occ:extra"],
        "source_spans": [
            *composite["source_spans"][:-1],
            {**composite["source_spans"][-1], "separator_after": " "},
            {
                "source_block_id": "extra",
                "exact_text": extra_text,
                "start_char": 0,
                "end_char": len(extra_text),
                "separator_after": "",
                "source_order": 3,
                "section_id": "main_notice",
                "common_ir_block_id": "ir:extra",
                "common_ir_occurrence_ids": ["occ:extra"],
            },
        ],
    }
    return CandidatePack.model_validate({
        "pack_id": "list-composite-pack",
        "notice_id": "PBLN-list-audit",
        "question": "list audit",
        "generator": "semantic_structuring.native_exact_transform",
        "generator_version": "native-exact-v2",
        "common_ir_document_id": "hwpx:PBLN-list-audit",
        "parent_pack_id": "source-pack",
        "parent_generator": "semantic_structuring.common_ir_v1",
        "parent_generator_version": "1",
        "blocks": [heading, item, detail, extra, composite, composite_wide],
    })


def test_native_composite_single_constituent_span_covers_item() -> None:
    pack = _native_composite_pack()
    item_text = "- 창업기업"

    validate_explicit_list_completeness_v02(
        pack,
        [_evidence("eligible", FactField.APPLICANT_ELIGIBILITY, "composite", 0, len(item_text))],
    )


def test_native_composite_cross_constituent_span_cannot_cover_item() -> None:
    pack = _native_composite_pack()
    composite = next(block for block in pack.blocks if block.block_id == "composite")

    with pytest.raises(ExplicitListCompletenessError) as raised:
        validate_explicit_list_completeness_v02(
            pack,
            [_evidence(
                "too-broad",
                FactField.APPLICANT_ELIGIBILITY,
                "composite",
                0,
                len(composite.text),
            )],
        )

    assert raised.value.do_not_restore_fact_ids == frozenset({"too-broad"})


def test_materialized_repair_rejects_same_broad_native_coordinates_via_wider_composite() -> None:
    pack = _native_composite_pack()
    narrow = next(block for block in pack.blocks if block.block_id == "composite")
    wide = next(block for block in pack.blocks if block.block_id == "composite-wide")

    def selected(*facts: dict) -> SourceSelectionExtractionV02:
        return SourceSelectionExtractionV02.model_validate({
            "notice_id": pack.notice_id,
            "candidate_pack_id": pack.pack_id,
            "component_decision": {
                "mode": "none",
                "no_component_reason": "one notice scope",
            },
            "support_components": [],
            "facts": list(facts),
        })

    prior = selected({
        "fact_id": "bad",
        "field_name": "support_content",
        "status": "identified",
        "value_anchor": {
            "source_block_id": narrow.block_id,
            "anchor_text": narrow.text,
        },
    })
    with pytest.raises(ExplicitListCompletenessError) as raised:
        finalize_source_selection_v02(
            prior,
            pack,
            build_numeric_candidates(pack),
            require_support_cap_completeness=False,
        )
    repaired_plus_wide = selected(
        {
            "fact_id": "renamed-bad",
            "field_name": "support_content",
            "status": "identified",
            "value_anchor": {
                "source_block_id": wide.block_id,
                "anchor_text": wide.text,
            },
        },
        {
            "fact_id": "right",
            "field_name": "applicant_eligibility",
            "status": "identified",
            "value_anchor": {
                "source_block_id": "item",
                "anchor_text": "- 창업기업",
            },
        },
    )

    with pytest.raises(ExplicitListCompletenessError):
        finalize_source_selection_v02(
            repaired_plus_wide,
            pack,
            build_numeric_candidates(pack),
            require_support_cap_completeness=False,
            typed_repair_error=raised.value,
        )


def test_materialized_repair_rejects_changed_but_still_incompatible_field() -> None:
    text = "- 창업기업 및 예비창업자"
    pack = _pack(_block("h", "지원대상", "heading", 0), _block("item", text, "paragraph", 1))

    def selected(*facts: dict) -> SourceSelectionExtractionV02:
        return SourceSelectionExtractionV02.model_validate({
            "notice_id": pack.notice_id,
            "candidate_pack_id": pack.pack_id,
            "component_decision": {
                "mode": "none",
                "no_component_reason": "one notice scope",
            },
            "support_components": [],
            "facts": list(facts),
        })

    prior = selected({
        "fact_id": "bad",
        "field_name": "support_content",
        "status": "identified",
        "value_anchor": {"source_block_id": "item", "anchor_text": "창업기업"},
    })
    with pytest.raises(ExplicitListCompletenessError) as raised:
        finalize_source_selection_v02(prior, pack, build_numeric_candidates(pack))
    changed_plus_right = selected(
        {
            "fact_id": "bad",
            "field_name": "support_methods",
            "status": "identified",
            "value_anchor": {"source_block_id": "item", "anchor_text": "창업기업"},
        },
        {
            "fact_id": "right",
            "field_name": "applicant_eligibility",
            "status": "identified",
            "value_anchor": {"source_block_id": "item", "anchor_text": "예비창업자"},
        },
    )

    with pytest.raises(ExplicitListCompletenessError):
        finalize_source_selection_v02(
            changed_plus_right,
            pack,
            build_numeric_candidates(pack),
            typed_repair_error=raised.value,
        )


def test_finalizer_aggregates_list_scale_and_support_cap_repair_payloads() -> None:
    first = "- 창업기업"
    second = "- 예비창업자"
    cap = "총사업비 3억원 중 지원금 최대 5천만원"
    pack = _pack(
        _block("h", "지원대상", "heading", 0),
        _block("items", f"{first}\n{second}", "paragraph", 1),
        _block("other", "지원내용", "heading", 2),
        _block("period", "지원기간 6개월", "paragraph", 3),
        _block("cap", cap, "paragraph", 4),
    )
    selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "component_decision": {"mode": "none", "no_component_reason": "one notice scope"},
        "support_components": [],
        "facts": [
            {
                "fact_id": "first",
                "field_name": "applicant_eligibility",
                "status": "identified",
                "value_anchor": {"source_block_id": "items", "anchor_text": first},
            },
            {
                "fact_id": "duration-as-scale",
                "field_name": "support_scale",
                "status": "identified",
                "value_anchor": {
                    "source_block_id": "period",
                    "anchor_text": "지원기간 6개월",
                },
            },
        ],
    })

    with pytest.raises(SourceSelectionRepairIssuesError) as raised:
        finalize_source_selection_v02(selection, pack, build_numeric_candidates(pack))

    error = raised.value
    assert error.finalize_stage == "repair_issue_aggregation"
    assert required_explicit_list_item_regions_for_typed_repair_v02(error)[0][
        "item_text"
    ] == second
    assert required_support_scale_anchors_for_typed_repair_v02(error) == [{
        "source_block_id": "cap",
        "anchor_text": "최대 5천만원",
    }]
    assert required_support_scale_fact_repairs_for_typed_repair_v02(error) == [{
        "fact_id": "duration-as-scale",
        "source_block_id": "period",
        "reason": "duration_bearing_anchor",
        "numeric_candidate_count": 0,
        "derived_measure_count": 0,
    }]
    assert error.do_not_restore_fact_ids == frozenset({"duration-as-scale"})


def test_worker_repairs_list_scale_and_cap_together_in_its_only_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "- 창업기업"
    second = "- 예비창업자"
    cap = "총사업비 3억원 중 지원금 최대 5천만원"
    pack = _pack(
        _block("h", "지원대상", "heading", 0),
        _block("items", f"{first}\n{second}", "paragraph", 1),
        _block("support", "지원내용", "heading", 2),
        _block("period", "지원기간 6개월", "paragraph", 3),
        _block("cap", cap, "paragraph", 4),
    )

    def selection(*facts: dict) -> SourceSelectionExtractionV02:
        return SourceSelectionExtractionV02.model_validate({
            "notice_id": pack.notice_id,
            "candidate_pack_id": pack.pack_id,
            "component_decision": {
                "mode": "none",
                "no_component_reason": "one notice scope",
            },
            "support_components": [],
            "facts": list(facts),
        })

    first_fact = {
        "fact_id": "first",
        "field_name": "applicant_eligibility",
        "status": "identified",
        "value_anchor": {"source_block_id": "items", "anchor_text": first},
    }
    invalid_duration = {
        "fact_id": "duration-as-scale",
        "field_name": "support_scale",
        "status": "identified",
        "value_anchor": {
            "source_block_id": "period",
            "anchor_text": "지원기간 6개월",
        },
    }
    wrong_second = {
        "fact_id": "wrong-second",
        "field_name": "support_content",
        "status": "identified",
        "value_anchor": {"source_block_id": "items", "anchor_text": second},
    }
    broad_cap = {
        "fact_id": "broad-cap",
        "field_name": "support_scale",
        "status": "identified",
        "value_anchor": {"source_block_id": "cap", "anchor_text": cap},
    }
    responses = [
        selection(first_fact, wrong_second, invalid_duration, broad_cap),
        selection(
            first_fact,
            {
                "fact_id": "second",
                "field_name": "applicant_eligibility",
                "status": "identified",
                "value_anchor": {"source_block_id": "items", "anchor_text": second},
            },
            {
                "fact_id": "cap",
                "field_name": "support_scale",
                "status": "identified",
                "value_anchor": {
                    "source_block_id": "cap",
                    "anchor_text": "최대 5천만원",
                },
            },
        ),
    ]
    requests: list[dict[str, object]] = []

    def fake_llm(*_args: object, **kwargs: object) -> SourceSelectionExtractionV02:
        requests.append(kwargs["payload"])
        return responses[len(requests) - 1]

    monkeypatch.setattr(announcement_profiles, "_call_llm", fake_llm)
    monkeypatch.setattr(
        announcement_profiles, "_trusted_source_sha256", lambda *_args: "a" * 64
    )
    monkeypatch.setattr(
        announcement_profiles,
        "_selection_artifact",
        lambda **kwargs: {
            "fact_ids": [fact.fact_id for fact in kwargs["extraction"].facts]
        },
    )
    monkeypatch.setattr(
        announcement_profiles,
        "assemble_final_profile_v02",
        lambda artifact, *_args, **_kwargs: dict(artifact),
    )
    monkeypatch.setattr(announcement_profiles, "common_ir_v1_metadata", lambda _doc: {})
    monkeypatch.setattr(announcement_profiles, "candidate_pack_artifact", lambda *_args: {})
    monkeypatch.setattr(
        announcement_profiles, "build_corrected_anchor_audit", lambda *_args: []
    )

    profile = announcement_profiles._select_and_assemble(
        {"document": {"document_id": pack.common_ir_document_id}},
        pack,
        {},
        object(),
        "test-model",
    )

    assert len(requests) == 2
    repair = requests[1]
    assert repair["required_support_scale_anchors"] == [{
        "source_block_id": "cap",
        "anchor_text": "최대 5천만원",
    }]
    assert {
        (item["fact_id"], item["reason"])
        for item in repair["required_support_scale_fact_repairs"]
    } == {
        ("broad-cap", "support_cap_span_contains_unrelated_numeric"),
        ("duration-as-scale", "duration_bearing_anchor"),
    }
    assert repair["required_list_item_regions"][0]["item_text"] == second
    assert repair["required_list_item_regions"][0]["replace_fact_ids"] == [
        "wrong-second"
    ]
    assert repair["do_not_restore_fact_ids"] == [
        "broad-cap",
        "duration-as-scale",
        "wrong-second",
    ]
    assert set(profile["fact_ids"]) == {"first", "second", "cap"}
