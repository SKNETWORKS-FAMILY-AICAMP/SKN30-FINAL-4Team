"""Safety contracts for the six-notice full Existing Profile canary."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import stat
from zipfile import ZIP_STORED, ZipFile

import pytest

from worker import announcement_profiles
from worker.evaluation.existing_profile_diff import load_automatic_baseline


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_existing_profile_canary.py"
SPEC = importlib.util.spec_from_file_location("existing_profile_canary", SCRIPT)
assert SPEC and SPEC.loader
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)


def _documents() -> dict[str, dict]:
    return {
        notice_id: {
            "schema_version": "common_ir_v1",
            "document": {
                "document_id": f"pdf:{notice_id}",
                "source_kind": "pdf",
                "provenance": {"source_sha256": "a" * 64},
            },
            "blocks": [], "relations": [], "conflicts": [],
        }
        for notice_id in canary.CORRECTED_NOTICE_IDS
    }


def _plan() -> dict:
    return {
        "notices": [{"notice_id": notice_id} for notice_id in canary.CORRECTED_NOTICE_IDS],
        "calls": {"hard_budget": canary.MAX_OPENAI_CALLS, "task_budgets": dict(canary.TASK_BUDGETS)},
    }


def test_plan_does_not_open_or_receive_a_gold_root(monkeypatch: pytest.MonkeyPatch) -> None:
    documents = _documents()
    monkeypatch.setattr(canary, "_verify_prompt_pins", lambda: None)
    monkeypatch.setattr(
        canary.routing_canary,
        "_read_baseline_common_ir",
        lambda _path: (documents, "b" * 64, {notice_id: "c" * 64 for notice_id in documents}),
    )
    attachment_counts = iter([1, 1, 0, 0, 0, 0])
    monkeypatch.setattr(
        canary.routing_canary, "_attachment_count", lambda _document: next(attachment_counts)
    )

    loaded, plan = canary.build_plan(baseline_zip=Path("baseline.zip"))

    assert loaded == documents
    assert plan["gold_oracle"]["read_phase"] == "post_openai_only"
    assert plan["calls"]["task_budgets"] == canary.TASK_BUDGETS


def test_prompt_preflight_evaluates_source_selection_before_baseline_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_read = False

    def read_baseline(_path: Path) -> object:
        nonlocal baseline_read
        baseline_read = True
        raise AssertionError("baseline must not be read after prompt pin failure")

    monkeypatch.setattr(
        announcement_profiles,
        "_source_selection_instructions",
        lambda: "drifted source-selection prompt",
    )
    monkeypatch.setattr(canary.routing_canary, "_read_baseline_common_ir", read_baseline)

    with pytest.raises(canary.ExistingProfileCanaryError, match="prompt hash pin"):
        canary.build_plan(baseline_zip=Path("baseline.zip"))
    assert baseline_read is False


def test_prompt_preflight_pins_conditional_support_scale_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        announcement_profiles,
        "_SUPPORT_SCALE_REPAIR_INSTRUCTIONS",
        "drifted conditional repair prompt",
    )

    with pytest.raises(canary.ExistingProfileCanaryError, match="prompt hash pin"):
        canary._verify_prompt_pins()


def test_reviewed_prompt_pins_match_current_worker_bundle() -> None:
    canary._verify_prompt_pins()


def test_prompt_preflight_pins_conditional_explicit_list_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        announcement_profiles,
        "_EXPLICIT_LIST_REPAIR_INSTRUCTIONS",
        "drifted explicit-list repair prompt",
    )

    with pytest.raises(canary.ExistingProfileCanaryError, match="prompt hash pin"):
        canary._verify_prompt_pins()


def test_plan_preserves_safe_shared_baseline_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canary, "_verify_prompt_pins", lambda: None)
    monkeypatch.setattr(
        canary.routing_canary,
        "_read_baseline_common_ir",
        lambda _path: (_ for _ in ()).throw(
            canary.routing_canary.ExistingARoutingCanaryError(
                "baseline Common IR contains manual adjudication provenance"
            )
        ),
    )

    with pytest.raises(
        canary.ExistingProfileCanaryError,
        match="manual adjudication provenance",
    ):
        canary.build_plan(baseline_zip=Path("baseline.zip"))


def test_main_rejects_real_shared_guard_before_constructing_openai_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _documents()
    first = canary.CORRECTED_NOTICE_IDS[0]
    documents[first]["blocks"].append({
        "block_id": "pdf:b1",
        "kind": "text",
        "text": "must-not-reach-provider",
        "provenance": {
            "method": "pdf_inspector",
            "generator_version": "manual gold v1",
        },
    })
    baseline = tmp_path / "baseline.zip"
    with ZipFile(baseline, "w", compression=ZIP_STORED) as archive:
        for notice_id, document in documents.items():
            archive.writestr(
                f"{notice_id}/pipeline/common_ir_v1/{notice_id}.pdf.json",
                json.dumps(document, ensure_ascii=False),
            )
    monkeypatch.setattr(canary, "_verify_prompt_pins", lambda: None)
    monkeypatch.setattr(
        canary.routing_canary,
        "BASELINE_ARCHIVE_SHA256",
        sha256(baseline.read_bytes()).hexdigest(),
    )
    constructed = False

    def forbidden_client(**_kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("OpenAI client must not be constructed")

    monkeypatch.setattr(canary, "OpenAILLMClient", forbidden_client)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-secret")

    assert canary.main([
        "--baseline-zip", str(baseline),
        "--gold-root", str(tmp_path / "unused-gold"),
        "--execute-openai",
        "--model", canary.PINNED_OPENAI_MODEL_ID,
        "--output-dir", str(tmp_path / "unused-output"),
    ]) == 1
    assert constructed is False


def test_preflight_rejects_a_lexical_gold_child_without_opening_it(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold"
    output = gold / "empty-output"
    output.mkdir(parents=True)

    with pytest.raises(
        canary.ExistingProfileCanaryError,
        match="outside the frozen Gold root",
    ):
        canary._prepare_output_directory_before_openai(output, gold)


def test_call_guard_reserves_each_task_budget_before_delegate() -> None:
    class Delegate:
        async def generate_structured(self, **_kwargs: object) -> object:
            return object()

    guard = canary._CountingLlm(Delegate())  # type: ignore[arg-type]
    task = announcement_profiles.ANCHOR_CORRECTION_TASK
    guard._by_task[task] = canary.TASK_BUDGETS[task]
    with pytest.raises(canary.ExistingProfileCanaryError, match="task provider-call budget"):
        asyncio.run(guard.generate_structured(task_name=task))
    assert guard.calls == []


def test_execute_uses_native_exact_shadow_and_one_bounded_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _documents()
    seen: list[dict] = []

    class Delegate:
        async def generate_structured(self, **_kwargs: object) -> object:
            return object()

    def produce(_document: dict, llm: object, **kwargs: object):
        if len(seen) < 2:
            asyncio.run(llm.generate_structured(task_name=announcement_profiles.SECTION_SCOPE_TASK))  # type: ignore[attr-defined]
        asyncio.run(llm.generate_structured(task_name=announcement_profiles.BLOCK_ROUTER_TASK))  # type: ignore[attr-defined]
        asyncio.run(llm.generate_structured(task_name=announcement_profiles.SOURCE_SELECTION_TASK))  # type: ignore[attr-defined]
        seen.append(kwargs)
        return announcement_profiles.FinalizedAnnouncementProfileArtifacts(
            profile={"notice_id": "x"}, source_selection={"selection": {}}
        )

    monkeypatch.setattr(announcement_profiles, "structure_announcement_profile_artifacts", produce)
    report, artifacts = canary.execute_canary(
        documents=documents, plan=_plan(), llm_client=Delegate(),  # type: ignore[arg-type]
        model_id=canary.PINNED_OPENAI_MODEL_ID, timeout_seconds=1,
    )

    assert report["execution_status"] == "succeeded"
    assert report["calls"]["successful_plan_status"] == "valid"
    assert tuple(artifacts) == canary.CORRECTED_NOTICE_IDS
    assert all(row["composite_candidate_mode"] == "shadow" for row in seen)
    assert all(row["native_exact_candidate_mode"] == "lines+continuations" for row in seen)
    assert all(row["source_selection_attempts"] == 2 for row in seen)


def test_execute_rejects_a_zero_call_success_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def produce(_document: dict, _llm: object, **_kwargs: object):
        return announcement_profiles.FinalizedAnnouncementProfileArtifacts(
            profile={"notice_id": "x"}, source_selection={"selection": {}}
        )

    monkeypatch.setattr(
        announcement_profiles,
        "structure_announcement_profile_artifacts",
        produce,
    )
    report, _artifacts = canary.execute_canary(
        documents=_documents(),
        plan=_plan(),
        llm_client=object(),  # type: ignore[arg-type]
        model_id=canary.PINNED_OPENAI_MODEL_ID,
        timeout_seconds=1,
    )

    assert report["execution_status"] == "failed"
    assert report["calls"]["successful_plan_status"] == "failed"
    assert report["post_provider_error"] == {
        "stage": "call_plan_validation",
        "reason_code": "CALL_MINIMUM_OR_FIXED_COUNT_MISMATCH",
    }


def test_candidate_zip_is_deterministic_and_has_exactly_three_members_per_notice(
    tmp_path: Path,
) -> None:
    documents = _documents()
    artifacts = {
        notice_id: announcement_profiles.FinalizedAnnouncementProfileArtifacts(
            profile={"notice_id": notice_id}, source_selection={"notice_id": notice_id}
        )
        for notice_id in canary.CORRECTED_NOTICE_IDS
    }
    gold_root = tmp_path / "gold"
    gold_root.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    previous_umask = os.umask(0)
    try:
        first_zip, first_sha256 = canary.write_candidate_zip(
            artifacts, documents, output_dir=first, gold_root=gold_root
        )
        second_zip, second_sha256 = canary.write_candidate_zip(
            artifacts, documents, output_dir=second, gold_root=gold_root
        )
    finally:
        os.umask(previous_umask)

    assert first_zip.read_bytes() == second_zip.read_bytes()
    assert first_sha256 == second_sha256
    assert stat.S_IMODE(first_zip.stat().st_mode) == 0o600
    loaded = load_automatic_baseline(first_zip, expected_profile_count=6)
    assert tuple(loaded.profiles) == canary.CORRECTED_NOTICE_IDS
    with ZipFile(first_zip) as archive:
        assert len(archive.infolist()) == len(canary.CORRECTED_NOTICE_IDS) * 3
        assert all(
            (info.external_attr >> 16) & 0o777 == 0o600
            and info.date_time == (1980, 1, 1, 0, 0, 0)
            and info.compress_type == ZIP_STORED
            for info in archive.infolist()
        )
        assert archive.namelist() == [
            name
            for notice_id in canary.CORRECTED_NOTICE_IDS
            for name in (
                f"{notice_id}/pipeline/structured_profile.v0.2.json",
                f"{notice_id}/pipeline/source_selection.json",
                f"{notice_id}/pipeline/common_ir_v1/{notice_id}.pdf.json",
            )
        ]


def test_binary_publish_rolls_back_a_link_if_directory_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    gold = tmp_path / "gold"
    output.mkdir()
    gold.mkdir()
    output_fd, output_chain, gold_chain = canary._open_publish_directory(output, gold)
    real_fsync = canary.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(canary.os.fstat(descriptor).st_mode):
            raise OSError("synthetic directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(canary.os, "fsync", fail_directory_fsync)
    try:
        with pytest.raises(canary.ExistingProfileCanaryError, match="cannot publish"):
            canary._publish_fd_bytes(
                output_fd,
                gold_chain[-1],
                name="candidate.zip",
                payload=b"safe",
            )
    finally:
        canary._close_descriptors(gold_chain)
        canary._close_descriptors(output_chain)

    assert list(output.iterdir()) == []


def test_failure_report_is_published_without_a_candidate_zip(tmp_path: Path) -> None:
    gold_root = tmp_path / "gold"
    output = tmp_path / "output"
    gold_root.mkdir()
    output.mkdir()

    report_path = canary.write_report(
        {"execution_status": "failed", "semantic_profile_status": "not_run"},
        output_dir=output,
        gold_root=gold_root,
    )

    assert report_path.is_file()
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600


def test_semantic_gate_binds_all_three_loaded_corpus_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canary.routing_canary, "_load_gold_pins", lambda _root: {})
    monkeypatch.setattr(canary, "_regular_file_sha256", lambda *_args, **_kwargs: "c" * 64)
    monkeypatch.setattr(
        canary,
        "compare_semantic_profile_corpora",
        lambda *_args, **_kwargs: {
            "baseline": {"archive_sha256": "b" * 64},
            "gold": {"freeze_manifest_sha256": "g" * 64},
            "candidate": {"archive_sha256": "c" * 64},
            "counts": {"failed": 0, "passed": 6},
        },
    )

    summary, _full = canary._semantic_summary(
        Path("candidate.zip"),
        baseline_zip=Path("baseline.zip"),
        gold_root=Path("gold"),
        expected_baseline_sha256="b" * 64,
        expected_gold_freeze_manifest_sha256="g" * 64,
        expected_candidate_sha256="c" * 64,
    )

    assert summary["status"] == "passed"


def test_semantic_gate_rejects_a_loaded_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canary.routing_canary, "_load_gold_pins", lambda _root: {})
    monkeypatch.setattr(canary, "_regular_file_sha256", lambda *_args, **_kwargs: "c" * 64)
    monkeypatch.setattr(
        canary,
        "compare_semantic_profile_corpora",
        lambda *_args, **_kwargs: {
            "baseline": {"archive_sha256": "wrong"},
            "gold": {"freeze_manifest_sha256": "g" * 64},
            "candidate": {"archive_sha256": "c" * 64},
            "counts": {"failed": 0},
        },
    )

    with pytest.raises(canary.ExistingProfileCanaryError, match="baseline identity"):
        canary._semantic_summary(
            Path("candidate.zip"),
            baseline_zip=Path("baseline.zip"),
            gold_root=Path("gold"),
            expected_baseline_sha256="b" * 64,
            expected_gold_freeze_manifest_sha256="g" * 64,
            expected_candidate_sha256="c" * 64,
        )


def test_main_preserves_sanitized_usage_report_after_post_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    report = {
        "execution_status": "succeeded",
        "semantic_profile_status": "not_run",
        "baseline": {"archive_sha256": "b" * 64},
        "gold_oracle": {"freeze_manifest_sha256": "g" * 64},
        "calls": {"attempted": 14},
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(canary, "build_plan", lambda **_kwargs: (_documents(), _plan()))
    monkeypatch.setattr(
        canary,
        "execute_canary",
        lambda **_kwargs: (dict(report), {}),
    )
    monkeypatch.setattr(canary, "OpenAILLMClient", lambda **_kwargs: object())
    monkeypatch.setattr(
        canary,
        "write_candidate_zip",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("do not expose me")),
    )

    def record(value: dict, **_kwargs: object) -> Path:
        captured.update(value)
        return output / canary.REPORT_FILE_NAME

    monkeypatch.setattr(canary, "write_report", record)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-secret")

    assert canary.main([
        "--baseline-zip", "baseline.zip", "--gold-root", str(tmp_path / "gold"),
        "--execute-openai", "--model", canary.PINNED_OPENAI_MODEL_ID,
        "--output-dir", str(output),
    ]) == 2
    assert captured["semantic_profile_status"] == "failed"
    assert captured["semantic_gate"] == {
        "status": "failed",
        "reason_code": "POST_PROVIDER_ARTIFACT_OR_SEMANTIC_GATE_FAILURE",
    }
    assert captured["calls"]["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "provider_completed_calls": 0,
    }


def test_main_emits_source_free_accounting_if_report_cannot_be_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    report = {
        "execution_status": "failed",
        "semantic_profile_status": "not_run",
        "calls": {"attempted": 3},
    }
    monkeypatch.setattr(canary, "build_plan", lambda **_kwargs: (_documents(), _plan()))
    monkeypatch.setattr(
        canary,
        "execute_canary",
        lambda **_kwargs: (dict(report), {}),
    )
    monkeypatch.setattr(canary, "OpenAILLMClient", lambda **_kwargs: object())
    monkeypatch.setattr(
        canary,
        "write_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            canary.ExistingProfileCanaryError("unsafe output boundary")
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-secret")

    assert canary.main([
        "--baseline-zip", "baseline.zip", "--gold-root", str(tmp_path / "missing-gold"),
        "--execute-openai", "--model", canary.PINNED_OPENAI_MODEL_ID,
        "--output-dir", str(output),
    ]) == 1
    terminal = json.loads(capsys.readouterr().out)
    assert terminal == {
        "attempted_calls": 3,
        "completion_tokens": 0,
        "prompt_tokens": 0,
        "provider_completed_calls": 0,
        "reason_code": "REPORT_PUBLICATION_FAILED",
        "report_file": None,
        "status": "failed",
        "total_tokens": 0,
    }
