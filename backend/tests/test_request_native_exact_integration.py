"""Request worker seam for opt-in native exact CandidatePack transforms."""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401 - install vendored semantic_structuring path
from worker.profiles import (
    StageError,
    _request_type_preflight_diagnostic,
    structure_request_profile,
    transform_request_candidate_pack,
)

from semantic_structuring.models import CandidatePack, SourceBlock
from semantic_structuring.request_profile_v012 import (
    RequestSourceSelectionV012,
    assemble_request_profile_v012,
    resolve_request_type_from_candidate_pack,
)
from semantic_structuring.run_request_profile_v012 import (
    _selection_artifact,
    selection_request_payload,
)


def _document() -> dict:
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:request-native-test",
            "source_kind": "hwpx",
            "provenance": {
                "source_sha256": "a" * 64,
                "source_location": "synthetic://request-native-test",
                "generator": "test",
                "generator_version": "1",
            },
        },
        "blocks": [],
        "relations": [],
        "conflicts": [],
    }


def _checkbox_pack() -> CandidatePack:
    checkbox = SourceBlock(
        block_id="hwpx:p:checkbox",
        text="내용: ☑ 사업내용 변경\n(지원내용, 지원대상, 사업추진방식 등)",
        relation="candidate",
        block_kind="paragraph",
        section_id="main_notice",
        source_order=0,
        source_occurrence_ids=["checkbox:occ"],
        common_ir_block_id="hwpx:p:checkbox",
        common_ir_occurrence_ids=("checkbox:occ",),
    )
    return CandidatePack(
        pack_id="request-native-checkbox-base",
        notice_id="request-native-test",
        question="request profile",
        blocks=[checkbox],
        generator="semantic_structuring.common_ir_v1",
        generator_version="1",
        common_ir_document_id="hwpx:request-native-test",
    )


def test_two_line_checkbox_is_preflighted_from_base_not_transformed_duplicate() -> None:
    document = _document()
    base = _checkbox_pack()
    assert _request_type_preflight_diagnostic(base) is None
    assert resolve_request_type_from_candidate_pack(base)["selected_code"] == "program_content_change"

    transformed = transform_request_candidate_pack(
        base,
        native_exact_candidate_mode="lines",
    )
    assert transformed.parent_pack_id == base.pack_id
    assert any(block.block_kind == "native_line_atom" for block in transformed.blocks)
    # Directly resolving from the transformed pack is intentionally ambiguous:
    # the full checkbox container and its first native line both match.  The
    # worker must instead thread the pre-transform pack through this boundary.
    with pytest.raises(ValueError, match="missing or ambiguous"):
        resolve_request_type_from_candidate_pack(transformed)

    payload = selection_request_payload(
        transformed,
        document,
        "request:request-native-test",
        request_type_pack=base,
    )
    assert payload["read_only_context"]["request_type"] == {
        "selected_code": "program_content_change",
        "label": "사업내용 변경",
    }
    assert payload["candidate_pack"]["candidate_pack_id"] == transformed.pack_id
    assert payload["candidate_pack"]["parent_pack_id"] == base.pack_id

    selection = RequestSourceSelectionV012.model_validate(
        {
            "profile_id": "request:request-native-test",
            "candidate_pack_id": transformed.pack_id,
            "facts": [],
        }
    )
    profile = assemble_request_profile_v012(
        document,
        transformed,
        selection,
        request_type_pack=base,
    )
    assert profile["request_type"]["selected_code"] == "program_content_change"
    assert profile["processing_metadata"]["candidate_pack"]["parent_pack_id"] == base.pack_id
    selection_artifact = _selection_artifact(
        selection,
        transformed,
        document,
        [],
        retry_count=0,
        repair_diagnostics=[],
        request_type_pack=base,
    )
    assert selection_artifact["candidate_pack_lineage"]["parent_pack_id"] == base.pack_id


def test_off_returns_base_instance_and_invalid_mode_is_typed_without_source_text() -> None:
    base = _checkbox_pack()
    assert transform_request_candidate_pack(
        base,
        native_exact_candidate_mode="off",
    ) is base

    with pytest.raises(StageError) as raised:
        transform_request_candidate_pack(
            base,
            native_exact_candidate_mode="not-a-mode-지원내용",
        )
    diagnostic = raised.value.diagnostic
    assert diagnostic.stage == "native_exact_candidate_transform"
    assert diagnostic.reason_code == "MATERIALIZATION_FAILED"
    assert diagnostic.message == "request native exact CandidatePack mode is invalid"
    assert "지원" not in diagnostic.message


def test_transform_failure_is_typed_without_source_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from worker import profiles

    base = _checkbox_pack()

    def broken(*_args: object, **_kwargs: object) -> object:
        raise ValueError("original source: 사업내용 변경")

    monkeypatch.setattr(profiles, "augment_pack_with_native_exact_transforms", broken)
    with pytest.raises(StageError) as raised:
        transform_request_candidate_pack(base, native_exact_candidate_mode="lines")
    assert raised.value.diagnostic.message == "request native exact CandidatePack transformation failed"
    assert "사업내용" not in raised.value.diagnostic.message


def test_request_type_pack_must_match_off_selection_pack_identity() -> None:
    pack = _checkbox_pack()
    forged_request_type_pack = pack.model_copy(update={"pack_id": "other-pack"})
    selection = RequestSourceSelectionV012.model_validate(
        {
            "profile_id": "request:request-native-test",
            "candidate_pack_id": pack.pack_id,
            "facts": [],
        }
    )

    with pytest.raises(ValueError, match="does not match selection CandidatePack"):
        assemble_request_profile_v012(
            _document(),
            pack,
            selection,
            request_type_pack=forged_request_type_pack,
        )


def test_foreign_request_type_pack_fails_before_selector_call() -> None:
    base = _checkbox_pack()
    transformed = transform_request_candidate_pack(
        base,
        native_exact_candidate_mode="lines",
    )
    foreign = base.model_copy(update={"pack_id": "foreign-checkbox-pack"})

    with pytest.raises(ValueError, match="does not match transformed CandidatePack parent"):
        selection_request_payload(
            transformed,
            _document(),
            "request:request-native-test",
            request_type_pack=foreign,
        )

    called = False

    def selector(*_args: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("foreign request_type pack must fail before selector")

    snapshot = structure_request_profile(
        document=_document(),
        pack=transformed,
        profile_id="request:request-native-test",
        selector=selector,
        model_id="not-called",
        request_type_pack=foreign,
    )

    assert snapshot.status == "FAILED"
    assert snapshot.selection_attempts == 0
    assert snapshot.diagnostics[0].reason_code == "MATERIALIZATION_FAILED"
    assert not called


def test_selected_native_line_keeps_parent_span_in_request_profile() -> None:
    base = _checkbox_pack()
    purpose = SourceBlock(
        block_id="hwpx:p:purpose",
        text="사업 목적\n혁신기업 육성",
        relation="candidate",
        block_kind="paragraph",
        section_id="main_notice",
        source_order=1,
        source_occurrence_ids=["purpose:occ"],
        common_ir_block_id="hwpx:p:purpose",
        common_ir_occurrence_ids=("purpose:occ",),
    )
    base = CandidatePack.model_validate({
        **base.model_dump(mode="python"),
        "blocks": [*base.blocks, purpose],
    })
    transformed = transform_request_candidate_pack(
        base,
        native_exact_candidate_mode="lines",
    )
    line = next(
        block
        for block in transformed.blocks
        if block.block_kind == "native_line_atom" and block.text == "혁신기업 육성"
    )
    selection = RequestSourceSelectionV012.model_validate(
        {
            "profile_id": "request:request-native-test",
            "candidate_pack_id": transformed.pack_id,
            "facts": [
                {
                    "fact_id": "purpose-1",
                    "field_name": "purpose_goal",
                    "value_anchor": {
                        "source_block_id": line.block_id,
                        "anchor_text": line.text,
                    },
                }
            ],
        }
    )

    profile = assemble_request_profile_v012(
        _document(),
        transformed,
        selection,
        request_type_pack=base,
    )
    evidence = profile["comparison_profile"]["purpose_goal"][0]["evidence"][0]

    assert evidence["native_parent_span"] == {
        "source_block_id": purpose.block_id,
        "start_char": purpose.text.index(line.text),
        "end_char": len(purpose.text),
        "exact_text": line.text,
    }
