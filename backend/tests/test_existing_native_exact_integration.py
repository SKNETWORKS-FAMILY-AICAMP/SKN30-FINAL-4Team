"""Existing Profile integration boundary for native exact CandidatePacks."""

from __future__ import annotations

from copy import deepcopy
import pytest

from worker import announcement_profiles
from worker import vendor  # noqa: F401 - install vendored semantic_structuring path
from worker.profiles import StageError

from semantic_structuring.models import CandidatePack


def _base_pack() -> CandidatePack:
    return CandidatePack.model_validate(
        {
            "pack_id": "existing-native-base-pack",
            "notice_id": "PBLN-existing-native",
            "question": "routed A only",
            "generator": "semantic_structuring.common_ir_v1",
            "generator_version": "1",
            "common_ir_document_id": "hwpx:PBLN-existing-native",
            "blocks": [
                {
                    "block_id": "hwpx:p:0",
                    "text": "지원\n대상은",
                    "relation": "candidate",
                    "block_kind": "paragraph",
                    "section_id": "main_notice",
                    "source_order": 0,
                    "source_occurrence_ids": ["hwpx:p:0:occ"],
                    "common_ir_block_id": "hwpx:p:0",
                    "common_ir_occurrence_ids": ["hwpx:p:0:occ"],
                },
                {
                    "block_id": "hwpx:p:1",
                    "text": "중소기업이다.",
                    "relation": "candidate",
                    "block_kind": "paragraph",
                    "section_id": "main_notice",
                    "source_order": 1,
                    "source_occurrence_ids": ["hwpx:p:1:occ"],
                    "common_ir_block_id": "hwpx:p:1",
                    "common_ir_occurrence_ids": ["hwpx:p:1:occ"],
                },
            ],
        }
    )


def _document() -> dict:
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:PBLN-existing-native",
            "source_kind": "hwpx",
            "provenance": {"source_sha256": "a" * 64},
        },
        "blocks": [],
        "relations": [],
        "conflicts": [],
    }


def _install_pipeline_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    base_pack = _base_pack()
    seen: dict[str, object] = {"base_pack": base_pack, "select_packs": [], "shadow_packs": []}

    monkeypatch.setattr(announcement_profiles, "_prepare_scoped_notice", lambda *_args: object())
    monkeypatch.setattr(
        announcement_profiles,
        "_route_blocks",
        lambda *_args: (base_pack, {"retained_table_count": 0}),
    )

    def fake_shadow(_document: dict, pack: CandidatePack) -> dict:
        seen["shadow_packs"].append(pack)
        return {
            "mode": "shadow",
            "generator_version": "test",
            "candidate_count": 0,
            "candidate_kind_counts": {},
            "diagnostic_code_counts": {},
        }

    def fake_select(
        _document: dict,
        pack: CandidatePack,
        _metrics: dict,
        _llm: object,
        _model: str,
    ) -> dict:
        seen["select_packs"].append(pack)
        return {"candidate_pack_id": pack.pack_id}

    monkeypatch.setattr(announcement_profiles, "_composite_shadow_metrics", fake_shadow)
    monkeypatch.setattr(announcement_profiles, "_select_and_assemble", fake_select)
    return seen


def test_native_mode_is_opt_in_and_runs_after_base_routing_and_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_pipeline_spies(monkeypatch)

    profile = announcement_profiles.structure_announcement_profile(
        _document(),
        object(),
        model_profile="test",
        composite_candidate_mode="shadow",
        native_exact_candidate_mode="lines+continuations",
    )

    base_pack = seen["base_pack"]
    selected_pack = seen["select_packs"][0]
    assert seen["shadow_packs"] == [base_pack]
    assert selected_pack is not base_pack
    assert selected_pack.parent_pack_id == base_pack.pack_id
    assert selected_pack.parent_generator == base_pack.generator
    assert selected_pack.parent_generator_version == base_pack.generator_version
    assert any(block.block_kind == "native_line_atom" for block in selected_pack.blocks)
    assert any(block.block_kind == "native_composite" for block in selected_pack.blocks)
    assert profile == {"candidate_pack_id": selected_pack.pack_id}


def test_off_preserves_historical_base_pack_shape_and_explicit_mode_beats_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_pipeline_spies(monkeypatch)
    monkeypatch.setenv(
        announcement_profiles.EXISTING_NATIVE_EXACT_CANDIDATE_MODE_ENV,
        "lines+continuations",
    )

    announcement_profiles.structure_announcement_profile(
        _document(),
        object(),
        model_profile="test",
        native_exact_candidate_mode="off",
    )

    selected_pack = seen["select_packs"][0]
    assert selected_pack is seen["base_pack"]
    assert selected_pack.model_dump(mode="json") == seen["base_pack"].model_dump(mode="json")
    assert selected_pack.parent_pack_id is None
    assert not any(block.block_kind.startswith("native_") for block in selected_pack.blocks)


def test_invalid_native_mode_fails_before_section_scope_or_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def should_not_prepare(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("invalid native mode must fail before any pipeline stage")

    monkeypatch.setattr(announcement_profiles, "_prepare_scoped_notice", should_not_prepare)
    with pytest.raises(
        announcement_profiles.NativeExactCandidateModeError,
        match=(
            "PREREVIEW_EXISTING_NATIVE_EXACT_CANDIDATE_MODE must be one of "
            "lines, lines\\+continuations, off"
        ),
    ):
        announcement_profiles.structure_announcement_profile(
            _document(), object(), model_profile="test", native_exact_candidate_mode="not-a-mode"
        )
    assert not called


def test_transform_failure_is_a_stable_document_local_stage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_pipeline_spies(monkeypatch)

    def broken_transform(*_args: object, **_kwargs: object):
        raise ValueError("source fragment must never be exposed: 지원 대상")

    monkeypatch.setattr(announcement_profiles, "augment_pack_with_native_exact_transforms", broken_transform)
    with pytest.raises(StageError) as raised:
        announcement_profiles.structure_announcement_profile(
            _document(),
            object(),
            model_profile="test",
            native_exact_candidate_mode="lines",
        )

    diagnostic = raised.value.diagnostic
    assert diagnostic.stage == "native_exact_candidate_transform"
    assert diagnostic.reason_code == "MATERIALIZATION_FAILED"
    assert diagnostic.message == "native exact CandidatePack transformation failed"
    assert "지원" not in diagnostic.message
    assert seen["select_packs"] == []


def test_selection_artifact_records_parent_lineage_only_for_transformed_pack() -> None:
    base_pack = _base_pack()
    transformed = announcement_profiles._apply_native_exact_candidates(  # noqa: SLF001 - contract seam
        _document(), base_pack, mode="lines"
    )
    extraction = announcement_profiles.SourceSelectionExtractionV02.model_validate(
        {
            "notice_id": transformed.notice_id,
            "candidate_pack_id": transformed.pack_id,
            "component_decision": {"mode": "none", "no_component_reason": "fixture"},
            "support_components": [],
            "facts": [],
        }
    )

    artifact = announcement_profiles._selection_artifact(  # noqa: SLF001 - serialized contract
        extraction=extraction,
        evidence=[],
        components=[],
        measures=[],
        numeric_candidates=[],
        pack=transformed,
        pack_metrics={},
        document=deepcopy(_document()),
        attempt_count=1,
        correction_call_count=0,
        corrected_anchor_audit=[],
        component_normalizations=[],
        condition_variant_normalizations=[],
        preserved_fact_normalizations=[],
        preservation_fallback=None,
    )

    assert artifact["candidate_pack_lineage"] == {
        "candidate_pack_id": transformed.pack_id,
        "candidate_pack_generator": transformed.generator,
        "candidate_pack_generator_version": transformed.generator_version,
        "common_ir_document_id": transformed.common_ir_document_id,
        "common_ir_source_sha256": "a" * 64,
        "text_basis": "common_ir_v1_candidate_pack",
        "parent_pack_id": base_pack.pack_id,
        "parent_generator": base_pack.generator,
        "parent_generator_version": base_pack.generator_version,
    }
