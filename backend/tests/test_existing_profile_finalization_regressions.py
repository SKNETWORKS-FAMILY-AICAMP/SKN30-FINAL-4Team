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
        ("최대 8억원", 4, 10),
        ("최대 6.475억원", 16, 26),
    ]
    assert extract_explicit_support_cap_candidates(_pack("신청자격: 연매출 최대 800백만원")) == []
    assert extract_explicit_support_cap_candidates(_pack("지원기간 최대 6개월")) == []


def test_finalizer_requires_one_distinct_materialized_fact_per_repeated_cap() -> None:
    text = "지원금 최대 1억원; 지원금 최대 1억원"
    pack = _pack(text)
    selection = _selection(_fact("scale", "support_scale", text))

    with pytest.raises(SupportCapCompletenessError) as raised:
        finalize_source_selection_v02(
            selection,
            pack,
            build_numeric_candidates(pack),
        )

    assert raised.value.finalize_stage == "support_cap_completeness_validation"
    assert len(raised.value.required_support_scale_anchors()) == 2


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
    }
    assert seen["request"]["explicit_support_cap_anchors"] == [{
        "source_block_id": "body[0]", "anchor_text": "최대 1억원",
    }]
    assert (
        seen["request"]["explicit_support_cap_candidate_version"]
        == EXPLICIT_SUPPORT_CAP_CANDIDATE_VERSION
    )
