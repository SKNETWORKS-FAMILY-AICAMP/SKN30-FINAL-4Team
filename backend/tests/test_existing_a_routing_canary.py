"""Offline contracts for the baseline-input Existing A routing canary."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from worker import announcement_profiles


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_existing_a_routing_canary.py"
SPEC = importlib.util.spec_from_file_location("existing_a_routing_canary", SCRIPT)
assert SPEC and SPEC.loader
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)


def _document(notice_id: str) -> dict:
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": f"pdf:{notice_id}",
            "source_kind": "pdf",
            "provenance": {"source_sha256": "a" * 64},
        },
        "blocks": [],
        "relations": [],
        "conflicts": [],
    }


def _baseline(tmp_path: Path) -> Path:
    path = tmp_path / "baseline.zip"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for notice_id in canary.CORRECTED_NOTICE_IDS:
            archive.writestr(
                f"{notice_id}/pipeline/common_ir_v1/{notice_id}.pdf.json",
                json.dumps(_document(notice_id), ensure_ascii=False),
            )
    return path


def _gold(tmp_path: Path) -> Path:
    root = tmp_path / "gold"
    root.mkdir()
    profiles = {
        "profiles": [
            {
                "pblanc_id": notice_id,
                "frozen": {"common_ir": {"sha256": "b" * 64}},
            }
            for notice_id in canary.CORRECTED_NOTICE_IDS
        ]
    }
    profile_raw = json.dumps(profiles).encode()
    (root / "profile_manifest.json").write_bytes(profile_raw)
    (root / "freeze_manifest.json").write_text(
        json.dumps(
            {
                "dataset_version": "synthetic-gold",
                "notice_count": 100,
                "profile_manifest_sha256": sha256(profile_raw).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return root


def _verifier(_root: Path, *, expected_notice_count: int) -> object:
    assert expected_notice_count == 100
    return SimpleNamespace(status="valid", dataset_version="synthetic-gold")


def _pin_synthetic(monkeypatch: pytest.MonkeyPatch, baseline: Path, gold: Path) -> None:
    monkeypatch.setattr(canary, "BASELINE_ARCHIVE_SHA256", sha256(baseline.read_bytes()).hexdigest())
    monkeypatch.setattr(
        canary,
        "GOLD_FREEZE_MANIFEST_SHA256",
        sha256((gold / "freeze_manifest.json").read_bytes()).hexdigest(),
    )
    attachment_counts = iter([0, 0, 1, 0, 0, 1])
    monkeypatch.setattr(canary, "_attachment_count", lambda _document: next(attachment_counts))


def test_plan_reads_only_pinned_baseline_common_ir_and_gold_is_provenance_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)

    documents, plan = canary.build_plan(baseline_zip=baseline, gold_root=gold)

    assert tuple(documents) == canary.CORRECTED_NOTICE_IDS
    assert plan["execution_status"] == "planned"
    assert plan["routing_retention_status"] == "not_run"
    assert plan["semantic_profile_status"] == "not_run"
    assert plan["calls"]["planned"] == 8
    assert plan["calls"]["by_task"] == {
        "announcement_section_scope_v1": 2,
        "announcement_block_router_v03": 6,
    }
    serialized = json.dumps(plan, ensure_ascii=False)
    assert "source_blocks" not in serialized
    assert '"selection"' not in serialized


def test_plan_rejects_baseline_sha_pin_before_any_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    monkeypatch.setattr(canary, "BASELINE_ARCHIVE_SHA256", "0" * 64)
    monkeypatch.setattr(
        canary,
        "GOLD_FREEZE_MANIFEST_SHA256",
        sha256((gold / "freeze_manifest.json").read_bytes()).hexdigest(),
    )

    with pytest.raises(canary.ExistingARoutingCanaryError, match="baseline ZIP SHA-256 pin"):
        canary.build_plan(baseline_zip=baseline, gold_root=gold)


def test_execute_uses_public_routing_seam_and_never_selection_or_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)
    documents, plan = canary.build_plan(baseline_zip=baseline, gold_root=gold)
    calls: list[str] = []

    class FakeLlm:
        async def generate_structured(self, **kwargs: object) -> object:
            calls.append(str(kwargs["task_name"]))
            return object()

    def route(document: dict, llm: object, *, model_profile: str):
        assert model_profile == "existing_a_routing_canary"
        if (
            document["document"]["document_id"].endswith("117175")
            or document["document"]["document_id"].endswith("122023")
        ):
            asyncio.run(llm.generate_structured(task_name=announcement_profiles.SECTION_SCOPE_TASK))  # type: ignore[attr-defined]
        asyncio.run(llm.generate_structured(task_name=announcement_profiles.BLOCK_ROUTER_TASK))  # type: ignore[attr-defined]
        return SimpleNamespace(blocks=[object(), object()]), {"a_table_cell_total": 3}

    monkeypatch.setattr(announcement_profiles, "route_announcement_a_pack", route)
    monkeypatch.setattr(
        canary,
        "_composite_summary",
        lambda _document, _pack: {
            "generator_version": "test",
            "candidate_count": 1,
            "candidate_kind_counts": {"complete_proposition": 1},
            "diagnostic_code_counts": {},
            "fatal_diagnostic_codes": [],
        },
    )

    report = canary.execute_canary(
        documents=documents,
        plan=plan,
        llm_client=FakeLlm(),  # type: ignore[arg-type]
        model_id=canary.PINNED_OPENAI_MODEL_ID,
        timeout_seconds=1,
        gold_root=gold,
        audit_runner=lambda **_kwargs: {"status": "passed"},
    )

    assert report["execution_status"] == "succeeded"
    assert report["routing_retention_status"] == "passed"
    assert report["calls"]["attempted"] == 8
    assert report["calls"]["attempted_by_task"] == {
        "announcement_block_router_v03": 6,
        "announcement_section_scope_v1": 2,
    }
    assert len(report["notices"]) == 6
    assert all("source_blocks" not in json.dumps(row) for row in report["notices"])


def test_output_rejects_gold_and_writes_one_source_free_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)
    _documents, plan = canary.build_plan(baseline_zip=baseline, gold_root=gold)
    with pytest.raises(canary.ExistingARoutingCanaryError, match="outside the frozen Gold"):
        canary.write_report(plan, output_dir=gold, gold_root=gold)

    output = tmp_path / "output"
    output.mkdir()
    report_path = canary.write_report(plan, output_dir=output, gold_root=gold)
    assert report_path.name == canary.REPORT_FILE_NAME
    assert report_path.is_file()
    assert "source_blocks" not in report_path.read_text(encoding="utf-8")


def test_call_guard_reserves_budget_before_delegating_and_rejects_other_tasks() -> None:
    class Delegate:
        async def generate_structured(self, **_kwargs: object) -> object:
            return object()

    guard = canary._CountingLlm(Delegate())  # type: ignore[arg-type]
    guard.calls.extend(
        {"task_name": "announcement_block_router_v03", "duration_ms": 0}
        for _ in range(canary.MAX_OPENAI_CALLS)
    )
    with pytest.raises(canary.ExistingARoutingCanaryError, match="hard call budget"):
        asyncio.run(guard.generate_structured(task_name="announcement_block_router_v03"))
    assert len(guard.calls) == canary.MAX_OPENAI_CALLS

    empty_guard = canary._CountingLlm(Delegate())  # type: ignore[arg-type]
    with pytest.raises(canary.ExistingARoutingCanaryError, match="disallowed"):
        asyncio.run(empty_guard.generate_structured(task_name="announcement_source_selection_v02"))
    assert empty_guard.calls == []


def test_main_sanitizes_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(**_kwargs: object):
        raise RuntimeError("model output must never be echoed")

    monkeypatch.setattr(canary, "build_plan", broken)
    assert canary.main(["--baseline-zip", "unused.zip", "--gold-root", "unused-gold"]) == 1
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "model output" not in captured.err


def test_reachable_audit_requires_one_structural_candidate_not_count_equality() -> None:
    def atom(role: str, occurrence: str, *, table: str = "hwp:t50") -> object:
        return SimpleNamespace(
            role=role,
            occurrence_id=occurrence,
            common_ir_block_id=table,
            common_ir_cell_id="cell",
        )

    good = SimpleNamespace(
        kind="table_axis_context",
        atoms=(
            atom("primary_value", "value"),
            atom("row_header", "row"),
            atom("column_header", "column"),
        ),
    )
    expectation = frozenset({"value", "row", "column"})
    assert canary._matching_candidate_count(expectation, [good]) == 1
    assert canary._matching_candidate_count(expectation, [good, good]) == 2
    assert canary._reachable_keys({"private-key": ("PBLN_1", expectation)}, {"PBLN_1": [good]}) == {"private-key"}
    # A candidate with a non-header extra atom must not be considered retained.
    unsafe = SimpleNamespace(
        kind="table_axis_context",
        atoms=(*good.atoms, atom("primary_value", "unrelated")),
    )
    assert canary._matching_candidate_count(expectation, [unsafe]) == 0

    # Even when both primary occurrences belong to the expectation, the
    # canary's narrow approval relation requires exactly one primary value.
    two_primary = SimpleNamespace(
        kind="table_axis_context",
        atoms=(
            atom("primary_value", "value"),
            atom("primary_value", "row"),
            atom("column_header", "column"),
        ),
    )
    assert canary._matching_candidate_count(expectation, [two_primary]) == 0


def test_reachable_audit_does_not_reuse_one_candidate_for_two_expectations() -> None:
    def atom(role: str, occurrence: str) -> object:
        return SimpleNamespace(
            role=role,
            occurrence_id=occurrence,
            common_ir_block_id="hwp:t50",
            common_ir_cell_id="cell",
        )

    candidate = SimpleNamespace(
        kind="table_axis_context",
        atoms=(
            atom("primary_value", "value"),
            atom("row_header", "row"),
            atom("column_header", "column"),
        ),
    )
    expectations = {
        "first": ("PBLN_1", frozenset({"value", "row"})),
        "second": ("PBLN_1", frozenset({"value", "row", "column"})),
    }
    assert canary._reachable_keys(expectations, {"PBLN_1": [candidate]}) == set()


def test_plan_does_not_run_full_gold_verifier_before_model_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)
    called = False

    def forbidden_verifier(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("full Gold verifier ran before model phase")

    monkeypatch.setattr(canary, "_gold_verifier", lambda: forbidden_verifier)
    canary.build_plan(baseline_zip=baseline, gold_root=gold)
    assert not called


def test_canary_fails_closed_when_the_production_prompt_drifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        announcement_profiles, "_scope_instructions", lambda: "drifted prompt"
    )
    with pytest.raises(
        canary.ExistingARoutingCanaryError, match="section-scope prompt SHA-256 pin"
    ):
        canary._verify_prompt_pins()


def test_execute_rejects_a_programmatic_model_override_before_routing() -> None:
    with pytest.raises(
        canary.ExistingARoutingCanaryError, match="pinned canary model"
    ):
        canary.execute_canary(
            documents={},
            plan={},
            llm_client=object(),  # type: ignore[arg-type]
            model_id="different-model",
            timeout_seconds=1,
            gold_root=Path("unused"),
        )


def test_stage_error_report_allows_only_controlled_codes() -> None:
    allowed = canary.StageError(
        SimpleNamespace(
            stage="block_candidate_router",
            reason_code="LLM_TIMEOUT",
            message="private provider detail",
        )
    )
    assert canary._safe_stage_error(allowed) == {
        "stage": "block_candidate_router",
        "reason_code": "LLM_TIMEOUT",
    }

    blocked = canary.StageError(
        SimpleNamespace(
            stage="private source text",
            reason_code="private model output",
            message="private provider detail",
        )
    )
    assert canary._safe_stage_error(blocked) == {
        "stage": "canary",
        "reason_code": "UNKNOWN",
    }
