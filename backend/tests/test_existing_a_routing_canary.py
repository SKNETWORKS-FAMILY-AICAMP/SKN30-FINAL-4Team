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
from worker.evaluation import pristine_common_ir


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


def _baseline(tmp_path: Path, *, injected_block: dict | None = None) -> Path:
    path = tmp_path / "baseline.zip"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for notice_id in canary.CORRECTED_NOTICE_IDS:
            document = _document(notice_id)
            if injected_block is not None and notice_id == canary.CORRECTED_NOTICE_IDS[0]:
                document["blocks"].append(injected_block)
            archive.writestr(
                f"{notice_id}/pipeline/common_ir_v1/{notice_id}.pdf.json",
                json.dumps(document, ensure_ascii=False),
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
    documents: dict[str, dict] = {}
    member_sha256: dict[str, str] = {}
    with ZipFile(baseline) as archive:
        for notice_id in canary.CORRECTED_NOTICE_IDS:
            member = f"{notice_id}/pipeline/common_ir_v1/{notice_id}.pdf.json"
            raw = archive.read(member)
            documents[notice_id] = json.loads(raw)
            member_sha256[notice_id] = sha256(raw).hexdigest()
    def load_input(_path: Path, *, manifest_path: Path | None = None) -> pristine_common_ir.LoadedPristineCommonIr:
        del manifest_path
        for document in documents.values():
            try:
                pristine_common_ir.require_automatic_common_ir(document)
            except pristine_common_ir.CommonIrProvenanceError as error:
                raise pristine_common_ir.PristineCommonIrError("Common IR contains manual adjudication provenance") from error
        return pristine_common_ir.LoadedPristineCommonIr(documents, "i" * 64, member_sha256, {})
    monkeypatch.setattr(canary, "load_pristine_common_ir", load_input)
    monkeypatch.setattr(
        canary,
        "GOLD_FREEZE_MANIFEST_SHA256",
        sha256((gold / "freeze_manifest.json").read_bytes()).hexdigest(),
    )
    attachment_counts = iter([0, 0, 1, 0, 0, 1])
    monkeypatch.setattr(canary, "_attachment_count", lambda _document: next(attachment_counts))


def test_plan_reads_only_pinned_pristine_common_ir_and_records_gold_constants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)

    documents, plan = canary.build_plan(input_zip=baseline)

    assert tuple(documents) == canary.CORRECTED_NOTICE_IDS
    assert plan["execution_status"] == "planned"
    assert plan["routing_retention_status"] == "not_run"
    assert plan["semantic_profile_status"] == "not_run"
    assert plan["calls"]["planned"] == 8
    assert plan["calls"]["by_task"] == {
        "announcement_section_scope_v1": 2,
        "announcement_block_router_v03": 6,
    }
    assert plan["dataset_version"] == canary.GOLD_DATASET_VERSION
    assert plan["gold_oracle"] == {
        "freeze_manifest_sha256": canary.GOLD_FREEZE_MANIFEST_SHA256,
        "read_phase": "post_openai_only",
    }
    serialized = json.dumps(plan, ensure_ascii=False)
    assert "source_blocks" not in serialized
    assert '"selection"' not in serialized


def test_plan_rejects_baseline_sha_pin_before_any_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)
    monkeypatch.setattr(
        canary, "load_pristine_common_ir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pristine_common_ir.PristineCommonIrError("pristine input ZIP SHA-256 pin mismatch")
        ),
    )
    monkeypatch.setattr(
        canary,
        "GOLD_FREEZE_MANIFEST_SHA256",
        sha256((gold / "freeze_manifest.json").read_bytes()).hexdigest(),
    )

    with pytest.raises(canary.ExistingARoutingCanaryError, match="pristine input ZIP SHA-256 pin"):
        canary.build_plan(input_zip=baseline)


@pytest.mark.parametrize(
    "injected_block",
    [
        {
            "block_id": "adj:synthetic:purpose",
            "kind": "text",
            "text": "must-not-appear-in-the-error",
            "provenance": {"method": "native"},
        },
        {
            "block_id": "pdf:b1",
            "kind": "text",
            "text": "must-not-appear-in-the-error",
            "provenance": {"method": "manual_gold_native_span_composition"},
        },
        {
            "block_id": "pdf:b1",
            "kind": "text",
            "text": "must-not-appear-in-the-error",
            "provenance": {
                "method": "pdf_inspector",
                "generator_version": "manual-gold-v1",
            },
        },
        {
            "block_id": "pdf:b1",
            "kind": "text",
            "text": "must-not-appear-in-the-error",
            "provenance": {"method": "pdf_inspector"},
            "llm_provenance": {"review_mode": "gold patch"},
        },
        {
            "block_id": "pdf:b1",
            "kind": "text",
            "text": "must-not-appear-in-the-error",
            "source_block_label": "manual gold repair",
            "provenance": {"method": "pdf_inspector"},
        },
        {
            "block_id": "pdf:b1",
            "kind": "text",
            "text": "must-not-appear-in-the-error",
            "occurrences": [{"occurrence_id": "adj:synthetic:o1"}],
            "provenance": {"method": "native"},
        },
    ],
)
def test_plan_rejects_adjudication_only_common_ir_before_model_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected_block: dict,
) -> None:
    baseline = _baseline(tmp_path, injected_block=injected_block)
    gold = _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)

    with pytest.raises(
        canary.ExistingARoutingCanaryError,
        match="manual adjudication provenance",
    ) as captured:
        canary.build_plan(input_zip=baseline)

    assert "must-not-appear-in-the-error" not in str(captured.value)


def test_adjudication_words_in_native_source_text_do_not_trigger_metadata_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline(
        tmp_path,
        injected_block={
            "block_id": "pdf:b1",
            "kind": "text",
            "text": "manual_gold and adjudication are ordinary source words here",
            "provenance": {"method": "pdf_inspector"},
        },
    )
    gold = _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)

    documents, _plan = canary.build_plan(input_zip=baseline)

    assert tuple(documents) == canary.CORRECTED_NOTICE_IDS


def test_metadata_guard_fails_closed_on_excessive_nesting() -> None:
    nested: dict = {}
    cursor = nested
    for _index in range(canary.MAX_METADATA_SCAN_DEPTH + 1):
        child: dict = {}
        cursor["nested"] = child
        cursor = child

    with pytest.raises(
        canary.ExistingARoutingCanaryError,
        match="metadata nesting exceeds limit",
    ):
        canary._require_automatic_common_ir(nested)


def test_main_rejects_contaminated_baseline_before_constructing_openai_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline(
        tmp_path,
        injected_block={
            "block_id": "pdf:b1",
            "kind": "text",
            "text": "must-not-reach-provider",
            "provenance": {
                "method": "pdf_inspector",
                "generator_version": "manual gold v1",
            },
        },
    )
    gold = _gold(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    _pin_synthetic(monkeypatch, baseline, gold)
    monkeypatch.setattr(canary, "_verify_prompt_pins", lambda: None)
    constructed = False

    def forbidden_client(**_kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("OpenAI client must not be constructed")

    monkeypatch.setattr(canary, "OpenAILLMClient", forbidden_client)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-secret")

    assert canary.main([
        "--input-zip", str(baseline),
        "--gold-root", str(gold),
        "--execute-openai",
        "--model", canary.PINNED_OPENAI_MODEL_ID,
        "--output-dir", str(output),
    ]) == 1
    assert constructed is False


def test_main_rejects_openai_debug_logging_before_constructing_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)
    output = tmp_path / "output"
    output.mkdir()
    constructed = False

    def forbidden_client(**_kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("OpenAI client must not be constructed")

    monkeypatch.setattr(canary, "OpenAILLMClient", forbidden_client)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setenv("OPENAI_LOG", "debug")

    assert canary.main([
        "--input-zip", str(baseline),
        "--gold-root", str(gold),
        "--execute-openai",
        "--model", canary.PINNED_OPENAI_MODEL_ID,
        "--output-dir", str(output),
    ]) == 1
    assert constructed is False


def test_main_dry_run_needs_no_gold_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)

    assert canary.main(["--input-zip", str(baseline)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution_status"] == "planned"
    assert plan["gold_oracle"]["read_phase"] == "post_openai_only"


def test_main_execute_requires_gold_before_constructing_openai_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)
    output = tmp_path / "output"
    output.mkdir()
    constructed = False

    def forbidden_client(**_kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("OpenAI client must not be constructed")

    monkeypatch.setattr(canary, "OpenAILLMClient", forbidden_client)
    assert canary.main([
        "--input-zip", str(baseline),
        "--execute-openai",
        "--model", canary.PINNED_OPENAI_MODEL_ID,
        "--output-dir", str(output),
    ]) == 1
    assert constructed is False


def test_execute_uses_public_routing_seam_and_never_selection_or_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)
    documents, plan = canary.build_plan(input_zip=baseline)
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

    def audit_after_calls(**_kwargs: object) -> dict[str, str]:
        assert len(calls) == canary.MAX_OPENAI_CALLS
        return {"status": "passed"}

    report = canary.execute_canary(
        documents=documents,
        plan=plan,
        llm_client=FakeLlm(),  # type: ignore[arg-type]
        model_id=canary.PINNED_OPENAI_MODEL_ID,
        timeout_seconds=1,
        gold_root=gold,
        audit_runner=audit_after_calls,
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
    _documents, plan = canary.build_plan(input_zip=baseline)
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
    assert canary.main(["--input-zip", "unused.zip", "--gold-root", "unused-gold"]) == 1
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


def test_stable_relation_survives_occurrence_regeneration_and_primary_cell_split() -> None:
    table_id = "hwp:t50"

    def block(block_id: str, text: str, cell_id: str) -> object:
        return SimpleNamespace(
            block_id=block_id,
            text=text,
            common_ir_block_id=table_id,
            common_ir_cell_id=cell_id,
        )

    def atom(role: str, occurrence_id: str, source_block_id: str, cell_id: str) -> object:
        return SimpleNamespace(
            role=role,
            occurrence_id=occurrence_id,
            source_block_id=source_block_id,
            common_ir_block_id=table_id,
            common_ir_cell_id=cell_id,
        )

    old_occurrences = frozenset({"old-primary", "old-row", "old-column"})
    new_pack = SimpleNamespace(blocks=(
        block("new-primary-a", "지원 금액", "c-value"),
        block("new-primary-b", "100 만원", "c-value"),
        block("new-row", "기업당", "c-row"),
        block("new-column", "지원 내용", "c-column"),
        block("new-extra", "구분", "c-extra"),
    ))
    new_candidate = SimpleNamespace(
        kind="table_axis_context",
        atoms=(
            atom("column_header", "new-extra", "new-extra", "c-extra"),
            atom("primary_value", "new-primary-b", "new-primary-b", "c-value"),
            atom("row_header", "new-row", "new-row", "c-row"),
            atom("primary_value", "new-primary-a", "new-primary-a", "c-value"),
            atom("column_header", "new-column", "new-column", "c-column"),
        ),
    )
    expected_atoms = tuple(sorted((
        (table_id, "c-value", "primary_value", canary._stable_text_hash("지원 금액 100 만원")),
        (table_id, "c-row", "row_header", canary._stable_text_hash("기업당")),
        (table_id, "c-column", "column_header", canary._stable_text_hash("지원 내용")),
    )))
    expectation = canary._StableRelationExpectation(
        "PBLN_1",
        expected_atoms,
        canary._stable_text_hash("기업당 지원 내용 지원 금액 100 만원"),
    )
    key = canary._stable_relation_key(
        expectation.notice_id,
        expectation.atoms,
        expectation.text_sha256,
    )

    assert canary._reachable_keys(
        {"old": ("PBLN_1", old_occurrences)},
        {"PBLN_1": (new_candidate,)},
    ) == set()
    assert canary._stable_reachable_keys(
        {key: expectation},
        {"PBLN_1": (new_candidate,)},
        {"PBLN_1": new_pack},
    ) == {key}


@pytest.mark.parametrize("tamper", ["cell", "role", "text", "order", "punctuation"])
def test_stable_relation_fails_closed_on_semantic_signature_drift(tamper: str) -> None:
    table_id = "hwp:t50"
    value_cell = "different-cell" if tamper == "cell" else "c-value"
    value_role = "row_header" if tamper == "role" else "primary_value"
    value_text = {
        "text": "200 만원",
        "order": "만원 100",
        "punctuation": "100만원",
    }.get(tamper, "100 만원")
    pack = SimpleNamespace(blocks=(
        SimpleNamespace(block_id="value", text=value_text, common_ir_block_id=table_id, common_ir_cell_id=value_cell),
        SimpleNamespace(block_id="row", text="기업당", common_ir_block_id=table_id, common_ir_cell_id="c-row"),
    ))
    candidate = SimpleNamespace(
        kind="table_axis_context",
        atoms=(
            SimpleNamespace(role=value_role, occurrence_id="value", source_block_id="value", common_ir_block_id=table_id, common_ir_cell_id=value_cell),
            SimpleNamespace(role="row_header", occurrence_id="row", source_block_id="row", common_ir_block_id=table_id, common_ir_cell_id="c-row"),
        ),
    )
    atoms = tuple(sorted((
        (table_id, "c-value", "primary_value", canary._stable_text_hash("100 만원")),
        (table_id, "c-row", "row_header", canary._stable_text_hash("기업당")),
    )))
    expectation = canary._StableRelationExpectation(
        "PBLN_1",
        atoms,
        canary._stable_text_hash("100 만원 기업당"),
    )
    key = canary._stable_relation_key("PBLN_1", atoms, expectation.text_sha256)

    assert canary._stable_reachable_keys(
        {key: expectation},
        {"PBLN_1": (candidate,)},
        {"PBLN_1": pack},
    ) == set()


def test_plan_never_opens_gold_before_model_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, gold = _baseline(tmp_path), _gold(tmp_path)
    _pin_synthetic(monkeypatch, baseline, gold)
    monkeypatch.setattr(
        canary,
        "_gold_verifier",
        lambda: (_ for _ in ()).throw(AssertionError("Gold verifier ran before model phase")),
    )
    monkeypatch.setattr(
        canary,
        "_load_gold_pins",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Gold pins were opened")),
    )
    monkeypatch.setattr(
        canary,
        "_gold_audit_inputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Gold artifacts were opened")),
    )
    canary.build_plan(input_zip=baseline)


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
