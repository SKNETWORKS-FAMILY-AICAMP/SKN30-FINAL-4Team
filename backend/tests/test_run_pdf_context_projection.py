from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

import pytest


BACKEND_ROOT = Path(__file__).parents[1]
SCRIPT = BACKEND_ROOT / "scripts" / "run_pdf_context_projection.py"
SPEC = importlib.util.spec_from_file_location("run_pdf_context_projection", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# Reuse the vendored reconstruction fixture to exercise the real core rather
# than replacing the projection contract with a mock.
VENDOR_TESTS = BACKEND_ROOT / "vendor" / "common_ir_pipeline" / "tests"
if str(VENDOR_TESTS) not in sys.path:
    sys.path.insert(0, str(VENDOR_TESTS))
import test_reconstruction_plan as reconstruction_fixtures  # noqa: E402

from common_ir_pipeline.pdf_fusion.context_groups import (  # noqa: E402
    build_pdf_context_groups,
    canonical_pdf_context_groups_json,
)
from common_ir_pipeline.pdf_fusion.reconstruction_plan import (  # noqa: E402
    canonical_reconstruction_plan_json,
)


@pytest.fixture
def input_artifacts(tmp_path: Path):
    fixture = reconstruction_fixtures.PdfReconstructionPlanTests()
    fixture.setUp()
    try:
        plan = fixture.plan()
        fragments = {
            "schema_version": "pdf_fragment_groups/v1",
            "evaluation_only": True,
            "non_promotable": True,
            "standalone_validation_scope": "internal_consistency_only",
            "notice_id": plan["notice_id"],
            "source_pdf_sha256": plan["source_pdf_sha256"],
            "page_scope": list(plan["page_scope"]),
            "input_artifacts": {
                "reconstruction_plan_schema_version": plan["schema_version"],
                "reconstruction_plan_sha256": sha256(
                    canonical_reconstruction_plan_json(plan)
                ).hexdigest(),
                "surya_layout_artifact_schema_version": "surya_layout_artifact/v1",
                "surya_layout_artifact_sha256": sha256(b"empty-surya-fixture").hexdigest(),
            },
            "fragment_groups": [],
            "rejected_proposals": [],
            "metrics": {
                "eligible_odl_paragraph_count": 0,
                "accepted_fragment_group_count": 0,
                "rejected_proposal_count": 0,
            },
        }
        plan_path = tmp_path / "plan.json"
        fragments_path = tmp_path / "fragments.json"
        plan_path.write_bytes(canonical_reconstruction_plan_json(plan))
        fragments_path.write_text(
            json.dumps(fragments, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        yield plan, fragments, plan_path, fragments_path
    finally:
        fixture.tearDown()


def _arguments(plan: Path, fragments: Path, output: Path) -> list[str]:
    return [
        "--reconstruction-plan",
        str(plan),
        "--fragment-groups",
        str(fragments),
        "--output",
        str(output),
        "--acknowledge-upstream-replay",
    ]


def test_real_core_projection_is_canonical_private_and_summarized(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    plan, fragments, plan_path, fragments_path = input_artifacts
    output = tmp_path / "context-groups.json"

    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    summary = json.loads(captured.out)
    encoded = output.read_bytes()
    expected = build_pdf_context_groups(plan, fragments)
    assert encoded == canonical_pdf_context_groups_json(expected)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert summary == {
        "context_policy_version": expected["context_policy_version"],
        "metrics": expected["metrics"],
        "output_sha256": sha256(encoded).hexdigest(),
        "output_size_bytes": len(encoded),
        "schema_version": expected["schema_version"],
    }
    assert captured.out.rstrip("\n") == json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def test_acknowledgement_is_mandatory(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    with pytest.raises(SystemExit) as raised:
        MODULE.main(
            _arguments(plan_path, fragments_path, tmp_path / "output.json")[:-1]
        )
    assert raised.value.code == 2
    assert "--acknowledge-upstream-replay" in capfd.readouterr().err


def test_non_posix_platform_fails_closed(
    input_artifacts,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    output = tmp_path / "output.json"
    monkeypatch.setattr(MODULE.os, "name", "nt")
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: platform_not_supported\n"
    assert not output.exists()


def test_duplicate_key_is_rejected_without_semantic_leak(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _plan, _fragments, _plan_path, fragments_path = input_artifacts
    secret = "NEVER_PRINT_THIS_SEMANTIC_TEXT"
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"semantic":"' + secret + '","semantic":"' + secret + '"}',
        encoding="utf-8",
    )
    output = tmp_path / "output.json"
    assert MODULE.main(_arguments(duplicate, fragments_path, output)) == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    assert captured.err == "error: input_json_invalid\n"
    assert not output.exists()


def test_input_symlink_is_rejected(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    link = tmp_path / "plan-link.json"
    link.symlink_to(plan_path)
    output = tmp_path / "output.json"
    assert MODULE.main(_arguments(link, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: input_file_invalid\n"
    assert not output.exists()


def test_oversize_input_is_rejected(
    input_artifacts,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    monkeypatch.setattr(MODULE, "MAX_INPUT_BYTES", 8)
    output = tmp_path / "output.json"
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: input_file_invalid\n"
    assert not output.exists()


def test_existing_regular_or_symlink_output_is_never_overwritten(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    existing = tmp_path / "existing.json"
    existing.write_text("operator-data", encoding="utf-8")
    assert MODULE.main(_arguments(plan_path, fragments_path, existing)) == 1
    assert existing.read_text(encoding="utf-8") == "operator-data"
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"

    external = tmp_path / "external.json"
    external.write_text("external-data", encoding="utf-8")
    link = tmp_path / "output-link.json"
    link.symlink_to(external)
    assert MODULE.main(_arguments(plan_path, fragments_path, link)) == 1
    assert external.read_text(encoding="utf-8") == "external-data"
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"


def test_json_shape_limits_and_nonfinite_numbers_are_rejected(
    input_artifacts,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _fragments, _plan_path, fragments_path = input_artifacts
    invalid = tmp_path / "invalid.json"
    output = tmp_path / "output.json"

    invalid.write_text('{"value":1e999}', encoding="utf-8")
    assert MODULE.main(_arguments(invalid, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: input_json_invalid\n"

    invalid.write_text('{"value":"four-bytes"}', encoding="utf-8")
    monkeypatch.setattr(MODULE, "MAX_JSON_STRING_BYTES", 3)
    assert MODULE.main(_arguments(invalid, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: input_json_limits_exceeded\n"
    assert not output.exists()


def test_nonexistent_output_parent_is_rejected(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    output = tmp_path / "missing" / "output.json"
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"
    assert not output.exists()


def test_output_name_swap_is_detected_without_deleting_replacement(
    input_artifacts,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    output = tmp_path / "output.json"
    moved = tmp_path / "moved-created-output.json"
    attacker_content = b"attacker-owned-replacement"
    real_fsync = MODULE.os.fsync
    swapped = False

    def swap_after_output_fsync(descriptor: int) -> None:
        nonlocal swapped
        real_fsync(descriptor)
        if not swapped and output.exists():
            output.rename(moved)
            output.write_bytes(attacker_content)
            swapped = True

    monkeypatch.setattr(MODULE.os, "fsync", swap_after_output_fsync)
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert swapped
    assert output.read_bytes() == attacker_content
    assert moved.exists()
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"


def test_restrictive_umask_still_creates_mode_0600_output(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    output = tmp_path / "output.json"
    previous = os.umask(0o777)
    try:
        assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 0
    finally:
        os.umask(previous)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert capfd.readouterr().err == ""


def test_output_parent_swap_is_detected_without_touching_new_parent(
    input_artifacts,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    parent = tmp_path / "target-parent"
    parent.mkdir()
    moved_parent = tmp_path / "moved-parent"
    output = parent / "output.json"
    attacker_content = b"new-parent-attacker-file"
    real_fsync = MODULE.os.fsync
    swapped = False

    def swap_parent_after_output_fsync(descriptor: int) -> None:
        nonlocal swapped
        real_fsync(descriptor)
        if not swapped and output.exists():
            parent.rename(moved_parent)
            parent.mkdir()
            output.write_bytes(attacker_content)
            swapped = True

    monkeypatch.setattr(MODULE.os, "fsync", swap_parent_after_output_fsync)
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert swapped
    assert output.read_bytes() == attacker_content
    assert not (moved_parent / "output.json").exists()
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"


def test_output_swap_during_parent_fsync_is_detected_without_deleting_replacement(
    input_artifacts,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    output = tmp_path / "output.json"
    moved = tmp_path / "moved-created-output.json"
    attacker_content = b"attacker-owned-during-parent-fsync"
    real_fsync = MODULE.os.fsync
    swapped = False

    def swap_during_parent_fsync(descriptor: int) -> None:
        nonlocal swapped
        real_fsync(descriptor)
        descriptor_stat = os.fstat(descriptor)
        if not swapped and stat.S_ISDIR(descriptor_stat.st_mode) and output.exists():
            output.rename(moved)
            output.write_bytes(attacker_content)
            swapped = True

    monkeypatch.setattr(MODULE.os, "fsync", swap_during_parent_fsync)
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert swapped
    assert output.read_bytes() == attacker_content
    assert moved.exists()
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"


def test_group_writable_output_parent_is_rejected(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    parent = tmp_path / "shared-parent"
    parent.mkdir(mode=0o770)
    parent.chmod(0o770)
    output = parent / "output.json"
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"
    assert not output.exists()


def test_fchmod_failure_removes_only_the_created_output(
    input_artifacts,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    output = tmp_path / "output.json"

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("injected fchmod failure")

    monkeypatch.setattr(MODULE.os, "fchmod", fail_fchmod)
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"
    assert not output.exists()


def test_symlink_output_parent_is_rejected(
    input_artifacts, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _plan, _fragments, plan_path, fragments_path = input_artifacts
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    output = link_parent / "output.json"
    assert MODULE.main(_arguments(plan_path, fragments_path, output)) == 1
    assert capfd.readouterr().err == "error: output_target_invalid_or_exists\n"
    assert not (real_parent / "output.json").exists()
