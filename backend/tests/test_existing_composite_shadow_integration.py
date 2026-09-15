"""Rollout boundary tests for Existing Profile composite shadow diagnostics."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
from types import SimpleNamespace

import pytest

from worker import announcement_profiles
from worker import vendor  # noqa: F401 - install vendored semantic_structuring path

from semantic_structuring.models import CandidatePack
from semantic_structuring.composite_candidates import COMPOSITE_CANDIDATE_GENERATOR_VERSION
from semantic_structuring.final_profile_assembler import assemble_final_profile_v02


def _pack() -> CandidatePack:
    return CandidatePack.model_validate(
        {
            "pack_id": "shadow-a-pack",
            "notice_id": "PBLN-shadow",
            "extraction_scope": "candidate_pack",
            "question": "routed A only",
            "generator": "semantic_structuring.common_ir_v1",
            "generator_version": "1",
            "common_ir_document_id": "hwpx:PBLN-shadow",
            "blocks": [
                {
                    "block_id": "hwpx:p:0",
                    "text": "지원 대상은 중소기업이다.",
                    "relation": "candidate",
                    "common_ir_block_id": "hwpx:p:0",
                }
            ],
        }
    )


def _document() -> dict:
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:PBLN-shadow",
            "source_kind": "hwpx",
            "provenance": {"source_sha256": "a" * 64},
        },
        "blocks": [],
        "relations": [],
        "conflicts": [],
    }


def _minimal_v02_selection_artifact() -> tuple[dict, dict]:
    """Return a real assembler input with no selected facts.

    Keeping the fixture fact-free isolates the rollout boundary under test:
    transient shadow metrics may be attached to the selection artifact, but
    they must never affect the persisted Profile JSON.
    """

    source_sha256 = "a" * 64
    common_ir = {
        "document_id": "hwpx:PBLN-shadow",
        "schema_version": "common_ir_v1",
        "source_kind": "hwpx",
        "source_sha256": source_sha256,
        "source_location": "test://shadow.hwpx",
    }
    artifact = {
        "selection_contract": "v0.2_anchor",
        "selection": {
            "notice_id": "PBLN-shadow",
            "candidate_pack_id": "shadow-a-pack",
            "component_decision": {
                "mode": "none",
                "no_component_reason": "fact-free rollout fixture",
            },
            "support_components": [],
            "facts": [],
            "support_facets": [],
            "support_scale_measures": [],
        },
        "source_block_texts": {},
        "materialized_evidence": [],
        "materialized_components": [],
        "common_ir_identity": {
            "document_id": common_ir["document_id"],
            "source_kind": common_ir["source_kind"],
            "source_sha256": source_sha256,
        },
        "candidate_pack_lineage": {
            "candidate_pack_id": "shadow-a-pack",
            "candidate_pack_generator": "semantic_structuring.common_ir_v1",
            "candidate_pack_generator_version": "1",
            "common_ir_document_id": common_ir["document_id"],
            "common_ir_source_sha256": source_sha256,
            "text_basis": "common_ir_v1_candidate_pack",
        },
    }
    return artifact, {"notice_id": "PBLN-shadow", "common_ir": common_ir}


def _install_pipeline_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace costly stages while retaining the production seam and arguments."""

    pack = _pack()
    seen: dict[str, object] = {
        "pack": pack,
        "router_payloads": [],
        "selection_payloads": [],
        "metrics": [],
    }

    def fake_prepare(document: dict, _llm: object, _model: str) -> object:
        seen["prepared_document"] = deepcopy(document)
        return object()

    def fake_route(_prepared: object, _llm: object, _model: str):
        # This represents the router's model input.  Shadow work occurs only
        # after this return, so it cannot alter its prompt, request count, or
        # source blocks.
        seen["router_payloads"].append(
            {"source_blocks": [block.model_dump(mode="json") for block in pack.blocks]}
        )
        return pack, {"retained_table_count": 0}

    def fake_select(
        _document: dict,
        selected_pack: CandidatePack,
        metrics: dict,
        _llm: object,
        _model: str,
    ) -> dict:
        # This is exactly the data that the real source-selection model request
        # derives from: CandidatePack only, not input_table_metrics.
        seen["selection_payloads"].append(
            {
                "notice_id": selected_pack.notice_id,
                "candidate_pack_id": selected_pack.pack_id,
                "source_blocks": [block.model_dump(mode="json") for block in selected_pack.blocks],
            }
        )
        seen["metrics"].append(deepcopy(metrics))
        # Model/Profile output intentionally does not contain the transient
        # selection artifact metrics.
        return {"schema_version": "existing_program_profile/v0.2", "profile": "unchanged"}

    monkeypatch.setattr(announcement_profiles, "_prepare_scoped_notice", fake_prepare)
    monkeypatch.setattr(announcement_profiles, "_route_blocks", fake_route)
    monkeypatch.setattr(announcement_profiles, "_select_and_assemble", fake_select)
    return seen


def _shadow_log_payloads(caplog: pytest.LogCaptureFixture) -> list[dict]:
    """Return only the source-free structured shadow events from a test run."""

    payloads: list[dict] = []
    for record in caplog.records:
        if record.name != announcement_profiles.__name__:
            continue
        try:
            payload = json.loads(record.getMessage())
        except json.JSONDecodeError:
            continue
        if payload.get("event") == "existing_composite_candidate_shadow":
            payloads.append(payload)
    return payloads


def test_routing_only_seam_preserves_the_existing_prepare_router_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canary seam is exactly the production pipeline's pre-selection path."""

    prepared = object()
    pack = _pack()
    metrics = {"a_table_cell_total": 0}
    calls: list[tuple[str, object]] = []

    def fake_prepare(document: dict, llm: object, model: str) -> object:
        calls.append(("prepare", (document, llm, model)))
        return prepared

    def fake_route(value: object, llm: object, model: str):
        calls.append(("route", (value, llm, model)))
        return pack, metrics

    monkeypatch.setattr(announcement_profiles, "_prepare_scoped_notice", fake_prepare)
    monkeypatch.setattr(announcement_profiles, "_route_blocks", fake_route)
    document, llm = _document(), object()

    actual_pack, actual_metrics = announcement_profiles.route_announcement_a_pack(
        document, llm, model_profile="pinned-router"
    )

    assert (actual_pack, actual_metrics) == (pack, metrics)
    assert calls == [
        ("prepare", (document, llm, "pinned-router")),
        ("route", (prepared, llm, "pinned-router")),
    ]


def test_off_skips_generator_and_shadow_keeps_profile_and_model_payload_identical(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=announcement_profiles.__name__)
    seen = _install_pipeline_spies(monkeypatch)
    calls: list[tuple[dict, CandidatePack]] = []

    def fake_generator(document: dict, pack: CandidatePack):
        calls.append((deepcopy(document), pack))
        return SimpleNamespace(
            candidates=(
                SimpleNamespace(kind="table_axis_context"),
                SimpleNamespace(kind="complete_proposition"),
                SimpleNamespace(kind="table_axis_context"),
            ),
            diagnostics=(
                SimpleNamespace(code="TABLE_GEOMETRY_AMBIGUOUS"),
                SimpleNamespace(code="TABLE_GEOMETRY_AMBIGUOUS"),
                SimpleNamespace(code="PROPOSITION_NOT_COMPLETE"),
            ),
        )

    monkeypatch.setattr(announcement_profiles, "generate_composite_candidates", fake_generator)
    document = _document()
    off_profile = announcement_profiles.structure_announcement_profile(
        document, object(), model_profile="test", composite_candidate_mode="off"
    )
    shadow_profile = announcement_profiles.structure_announcement_profile(
        document, object(), model_profile="test", composite_candidate_mode="shadow"
    )

    assert len(calls) == 1
    assert calls[0][0] == document
    assert calls[0][1] is seen["pack"]
    # The profile and each actual model-bound payload are unchanged.  Shadow
    # candidates are never fed into either LLM stage.
    assert off_profile == shadow_profile
    router_payloads = seen["router_payloads"]
    selection_payloads = seen["selection_payloads"]
    assert router_payloads[0] == router_payloads[1]
    assert selection_payloads[0] == selection_payloads[1]

    off_metrics, shadow_metrics = seen["metrics"]
    assert off_metrics == {"retained_table_count": 0}
    assert shadow_metrics == {
        "retained_table_count": 0,
        "composite_candidate_shadow": {
            "mode": "shadow",
            "generator_version": COMPOSITE_CANDIDATE_GENERATOR_VERSION,
            "candidate_count": 3,
            "candidate_kind_counts": {
                "complete_proposition": 1,
                "table_axis_context": 2,
            },
            "diagnostic_code_counts": {
                "PROPOSITION_NOT_COMPLETE": 1,
                "TABLE_GEOMETRY_AMBIGUOUS": 2,
            },
        },
    }
    # The operation has an observable, parseable diagnostic without leaking a
    # source fragment, routed block id, CandidatePack id, or candidate/atom id.
    assert _shadow_log_payloads(caplog) == [
        {
            "event": "existing_composite_candidate_shadow",
            **shadow_metrics["composite_candidate_shadow"],
        }
    ]
    assert "지원 대상은 중소기업이다." not in caplog.text
    assert "hwpx:p:0" not in caplog.text
    assert "shadow-a-pack" not in caplog.text


def test_real_v02_assembler_ignores_transient_shadow_metrics_byte_for_byte() -> None:
    """The real Profile assembler must not persist or react to shadow data."""

    off_artifact, metadata = _minimal_v02_selection_artifact()
    shadow_artifact = deepcopy(off_artifact)
    shadow_artifact["input_table_metrics"] = {
        "retained_table_count": 0,
        "composite_candidate_shadow": {
            "mode": "shadow",
            "generator_version": COMPOSITE_CANDIDATE_GENERATOR_VERSION,
            "candidate_count": 2,
            "candidate_kind_counts": {
                "complete_proposition": 1,
                "table_axis_context": 1,
            },
            "diagnostic_code_counts": {},
        },
    }

    off_profile = assemble_final_profile_v02(off_artifact, metadata)
    shadow_profile = assemble_final_profile_v02(shadow_artifact, metadata)
    canonical = lambda value: json.dumps(  # noqa: E731 - compact byte comparator
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert canonical(off_profile) == canonical(shadow_profile)
    assert "composite_candidate_shadow" not in canonical(shadow_profile).decode("utf-8")


def test_empty_or_missing_environment_defaults_off_and_invalid_mode_fails_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert announcement_profiles.existing_composite_candidate_mode_from_env({}) == "off"
    assert announcement_profiles.existing_composite_candidate_mode_from_env(
        {announcement_profiles.EXISTING_COMPOSITE_CANDIDATE_MODE_ENV: "  "}
    ) == "off"
    assert announcement_profiles.existing_composite_candidate_mode_from_env(
        {announcement_profiles.EXISTING_COMPOSITE_CANDIDATE_MODE_ENV: "SHADOW"}
    ) == "shadow"

    called = False

    def should_not_prepare(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("invalid mode must fail before any pipeline stage")

    monkeypatch.setattr(announcement_profiles, "_prepare_scoped_notice", should_not_prepare)
    monkeypatch.setenv(announcement_profiles.EXISTING_COMPOSITE_CANDIDATE_MODE_ENV, "unsafe")
    with pytest.raises(
        announcement_profiles.CompositeCandidateModeError,
        match="PREREVIEW_EXISTING_COMPOSITE_CANDIDATE_MODE must be one of off, shadow",
    ):
        announcement_profiles.structure_announcement_profile(_document(), object(), model_profile="test")
    assert not called


def test_shadow_generator_failure_is_aggregate_only_and_does_not_change_profile(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=announcement_profiles.__name__)
    seen = _install_pipeline_spies(monkeypatch)

    def broken_generator(*_args: object, **_kwargs: object):
        raise RuntimeError("source text must not escape this diagnostic")

    monkeypatch.setattr(announcement_profiles, "generate_composite_candidates", broken_generator)
    profile = announcement_profiles.structure_announcement_profile(
        _document(), object(), model_profile="test", composite_candidate_mode="shadow"
    )

    assert profile == {"schema_version": "existing_program_profile/v0.2", "profile": "unchanged"}
    assert seen["metrics"] == [
        {
            "retained_table_count": 0,
            "composite_candidate_shadow": {
                "mode": "shadow",
                "generator_version": COMPOSITE_CANDIDATE_GENERATOR_VERSION,
                "candidate_count": 0,
                "candidate_kind_counts": {},
                "diagnostic_code_counts": {"GENERATION_FAILED": 1},
            },
        }
    ]
    assert _shadow_log_payloads(caplog) == [
        {
            "event": "existing_composite_candidate_shadow",
            "mode": "shadow",
            "generator_version": COMPOSITE_CANDIDATE_GENERATOR_VERSION,
            "candidate_count": 0,
            "candidate_kind_counts": {},
            "diagnostic_code_counts": {"GENERATION_FAILED": 1},
        }
    ]
    assert "source text must not escape this diagnostic" not in caplog.text


def test_shadow_logging_failure_never_fails_profile_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_pipeline_spies(monkeypatch)
    monkeypatch.setattr(
        announcement_profiles,
        "generate_composite_candidates",
        lambda *_args, **_kwargs: SimpleNamespace(candidates=(), diagnostics=()),
    )

    def broken_log(*_args: object, **_kwargs: object) -> None:
        raise OSError("telemetry unavailable")

    monkeypatch.setattr(announcement_profiles.LOGGER, "info", broken_log)

    profile = announcement_profiles.structure_announcement_profile(
        _document(), object(), model_profile="test", composite_candidate_mode="shadow"
    )

    assert profile == {
        "schema_version": "existing_program_profile/v0.2",
        "profile": "unchanged",
    }
