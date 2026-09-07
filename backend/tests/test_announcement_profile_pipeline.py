"""Offline contract tests for the production announcement-profile producer."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
from worker import announcement_profiles
from worker.announcement_profiles import (
    BLOCK_ROUTER_TASK,
    PROMPT_BUNDLE_VERSION,
    SECTION_SCOPE_TASK,
    SOURCE_SELECTION_TASK,
    structure_announcement_profile,
)
from worker.contracts.profile_snapshot import LLM_INVALID_RESPONSE, MATERIALIZATION_FAILED
from worker.profiles import StageError


_ROOT = Path(__file__).resolve().parents[2]
_COMMON_IR = _ROOT / "packages" / "profile_structuring" / "examples" / "existing" / "common_ir_hwp_v1.json"


class AnnouncementFakeLLM:
    """Structured-output fake that returns fresh responses from each payload."""

    def __init__(
        self,
        *,
        invalid_router: bool = False,
        repair_selection: bool = True,
        always_invalid_selection: bool = False,
    ):
        self.invalid_router = invalid_router
        self.repair_selection = repair_selection
        self.always_invalid_selection = always_invalid_selection
        self.calls: list[tuple[str, dict, str]] = []
        self.selection_calls = 0

    async def generate_structured(self, *, task_name, messages, response_schema, model_profile):
        payload = json.loads(messages[-1].content)
        self.calls.append((task_name, payload, model_profile))
        if task_name == SECTION_SCOPE_TASK:
            return response_schema.model_validate({
                "notice_id": payload["notice_id"],
                "decisions": [
                    {"section_id": row["section_id"], "scope": "form_template"}
                    for row in payload["attachments"]
                ],
            })
        if task_name == BLOCK_ROUTER_TASK:
            candidates = []
            for index, block in enumerate(payload["source_blocks"]):
                block_id = "unknown-block" if self.invalid_router and index == 0 else block["block_id"]
                if block["block_kind"] == "table":
                    candidates.append({
                        "source_block_id": block_id,
                        "route_tags": ["table"],
                        "table_disposition": "b_search_only",
                    })
                else:
                    candidates.append({
                        "source_block_id": block_id,
                        "route_tags": ["purpose"],
                        "table_disposition": None,
                    })
            return response_schema.model_validate({
                "notice_id": payload["notice_id"],
                "candidate_pack_id": payload["candidate_pack_id"],
                "candidates": candidates,
            })
        if task_name == SOURCE_SELECTION_TASK:
            self.selection_calls += 1
            block_id = next(
                block["block_id"]
                for block in payload["source_blocks"]
                if block["block_id"] == "hwp:b13"
            )
            anchor = (
                "not in the CandidatePack"
                if self.always_invalid_selection
                or (self.repair_selection and self.selection_calls == 1)
                else "청년의 사회연대경제 분야 진출 지원 및 사회연대경제 활성화"
            )
            return response_schema.model_validate({
                "notice_id": payload["notice_id"],
                "candidate_pack_id": payload["candidate_pack_id"],
                "component_decision": {
                    "mode": "none",
                    "no_component_reason": "the fake notice has no separate package decision",
                },
                "support_components": [],
                "facts": [{
                    "fact_id": "purpose-1",
                    "field_name": "purpose_goal",
                    "value_anchor": {"source_block_id": block_id, "anchor_text": anchor},
                    "context_source_block_ids": [],
                    "status": "identified",
                    "semantic_role": None,
                    "subject_role": None,
                    "organization_anchors": [],
                    "role_anchor": None,
                    "canonical_role": None,
                    "primary_component_id": None,
                    "applicability_component_ids": [],
                    "modifies_fact_ids": [],
                    "recipient_fact_ids": [],
                    "basis_fact_ids": [],
                }],
                "support_facets": [],
                "support_scale_measures": [],
            })
        raise AssertionError(f"unexpected task: {task_name}")


def test_production_chain_runs_scope_router_selection_and_one_repair(monkeypatch):
    document = json.loads(_COMMON_IR.read_text(encoding="utf-8"))
    llm = AnnouncementFakeLLM()
    captured: dict[str, dict] = {}
    assemble = announcement_profiles.assemble_final_profile_v02

    def capture_artifact(artifact, metadata, *, derived_projections=None):
        captured["artifact"] = artifact
        return assemble(
            artifact, metadata, derived_projections=derived_projections
        )

    monkeypatch.setattr(
        announcement_profiles, "assemble_final_profile_v02", capture_artifact
    )

    profile = structure_announcement_profile(document, llm, model_profile="test-profile")

    assert profile["schema_version"] == "existing_program_profile/v0.2"
    assert profile["source_profile_id"] == document["document"]["document_id"]
    assert profile["comparison_profile"]["purpose_goal"][0]["value_raw"] == (
        "청년의 사회연대경제 분야 진출 지원 및 사회연대경제 활성화"
    )
    assert [call[0] for call in llm.calls] == [
        SECTION_SCOPE_TASK,
        BLOCK_ROUTER_TASK,
        SOURCE_SELECTION_TASK,
        SOURCE_SELECTION_TASK,
    ]
    assert llm.calls[-1][1]["previous_selection"]["facts"][0]["fact_id"] == "purpose-1"
    assert all(call[2] == "test-profile" for call in llm.calls)
    assert isinstance(captured["artifact"]["numeric_candidates"], list)
    generation = profile["processing_metadata"]["generation_lineage"]
    assert generation["model_profile"] == "test-profile"
    assert generation["prompt_bundle_version"] == PROMPT_BUNDLE_VERSION
    assert generation["source_sha256_hex"] == document["document"]["provenance"]["source_sha256"]


def test_prompt_bundle_version_binds_vendor_and_local_instructions():
    components = announcement_profiles._PROMPT_BUNDLE_COMPONENT_HASHES
    digest = hashlib.sha256(
        "\n".join(
            f"{name}={components[name]}" for name in sorted(components)
        ).encode("utf-8")
    ).hexdigest()

    assert set(components) == {
        "section_scope",
        "block_router",
        "source_selection",
        "anchor_correction",
        "repair",
    }
    assert PROMPT_BUNDLE_VERSION == f"announcement_profile_prompt_bundle/v1:{digest}"


def test_router_identifier_failure_isolated_before_selection():
    document = json.loads(_COMMON_IR.read_text(encoding="utf-8"))
    llm = AnnouncementFakeLLM(invalid_router=True)

    with pytest.raises(StageError) as raised:
        structure_announcement_profile(document, llm, model_profile="test-profile")

    assert raised.value.diagnostic.reason_code == LLM_INVALID_RESPONSE
    assert [call[0] for call in llm.calls] == [SECTION_SCOPE_TASK, BLOCK_ROUTER_TASK]


def test_grounding_failure_consumes_only_the_one_selection_repair():
    document = json.loads(_COMMON_IR.read_text(encoding="utf-8"))
    llm = AnnouncementFakeLLM(always_invalid_selection=True)

    with pytest.raises(StageError) as raised:
        structure_announcement_profile(document, llm, model_profile="test-profile")

    assert raised.value.diagnostic.reason_code == MATERIALIZATION_FAILED
    assert [call[0] for call in llm.calls] == [
        SECTION_SCOPE_TASK,
        BLOCK_ROUTER_TASK,
        SOURCE_SELECTION_TASK,
        SOURCE_SELECTION_TASK,
    ]
