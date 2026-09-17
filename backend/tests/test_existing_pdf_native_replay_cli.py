from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "replay_existing_pdf_native.py"
SPEC = importlib.util.spec_from_file_location("replay_existing_pdf_native", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()


def _native(notice_id: str) -> dict:
    return {
        "notice_id": notice_id,
        "source_kind": "pdf",
        "artifact_role": "production",
        "method": "pdf_inspector",
        "version": "1.17.0",
        "source_path": "source.pdf",
        "process_result": {"page_count": 1},
        "pages_markdown_result": [],
        "text_items": [
            {
                "page": 1,
                "text": "지원 사업",
                "x": 1.0,
                "y": 2.0,
                "width": 3.0,
                "height": 4.0,
                "font_size": 10.0,
            }
        ],
        "structure_elements": [],
    }


def _common_ir(notice_id: str, source_hash: str) -> dict:
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": f"pdf:{notice_id}",
            "source_kind": "pdf",
            "artifact_role": "production",
            "page_count": 1,
            "pdf_semantic_eligibility": "eligible_native_text",
            "pdf_semantic_reason": "substantive_native_text_available",
            "native_text_page_count": 1,
            "raw_artifact_ids": ["native.json"],
            "provenance": {
                "method": "pdf_native_only",
                "page": None,
                "bbox": None,
                "coordinate_space": None,
                "source_location": "source.pdf",
                "source_sha256": source_hash,
                "generator": "common_ir_v1_adapters",
                "generator_version": "1.1.0",
                "schema_version": "common_ir_v1",
                "parser": "pdf_inspector",
                "parser_version": "1.17.0",
            },
        },
        "blocks": [],
        "conflicts": [],
        "relations": [],
    }


def _install_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate_source_after_adapter: bool = False,
    mutate_native_after_adapter: bool = False,
) -> list[tuple[list[str], Path, dict[str, str]]]:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def run(command, *, cwd, timeout_seconds, environment=None):
        command = list(command)
        calls.append((command, cwd, dict(environment or {})))
        assert stat.S_IMODE(cwd.stat().st_mode) == 0o700
        if MODULE.CAPTURE_MODULE in command:
            notice_id = command[command.index("--notice-id") + 1]
            (cwd / "native.json").write_bytes(_canonical(_native(notice_id)))
        elif MODULE.ADAPTER_MODULE in command:
            notice_id = command[command.index("--notice-id") + 1]
            source_hash = command[command.index("--source-sha256") + 1]
            common_ir = _common_ir(notice_id, source_hash)
            if "--surya-layout-artifact" in command:
                common_ir["document"]["raw_artifact_ids"] = [
                    MODULE.NATIVE_NAME,
                    MODULE.RENDER_MANIFEST_NAME,
                    MODULE.SURYA_LAYOUT_ARTIFACT_NAME,
                ]
            (cwd / "common_ir.json").write_bytes(_canonical(common_ir))
            if mutate_source_after_adapter:
                (cwd / "source.pdf").write_bytes(b"%PDF-1.7\nmutated by adapter child\n")
            if mutate_native_after_adapter:
                (cwd / "native.json").write_bytes(b'{"mutated":true}')
        else:  # pragma: no cover - protects the fixed command inventory.
            raise AssertionError(command)
        return "", ""

    capture_api = SimpleNamespace(
        DEFAULT_LIMITS={
            "max_source_bytes": 1024 * 1024,
            "max_capture_bytes": 1024 * 1024,
            "max_text_items": 20_000,
            "max_pages": 2_000,
        },
        NativeCaptureError=ValueError,
        PINNED_PDF_INSPECTOR_VERSION="1.17.0",
        canonical_json_bytes=_canonical,
        validate_native_capture=lambda payload, **_kwargs: payload,
    )
    monkeypatch.setattr(MODULE, "_run_command", run)
    monkeypatch.setattr(MODULE, "_load_native_capture_api", lambda: capture_api)
    return calls


@pytest.mark.parametrize("target", ["source", "native"])
def test_adapter_cannot_mutate_published_source_or_native(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\nfixed test bytes\n")
    _install_fake_pipeline(
        monkeypatch,
        mutate_source_after_adapter=target == "source",
        mutate_native_after_adapter=target == "native",
    )
    output = tmp_path / "output"

    with pytest.raises(MODULE.ExistingPdfReplayError, match="changed after the adapter"):
        MODULE.replay_existing_pdf(
            notice_id="PBLN_123",
            source_pdf=source,
            output_directory=output,
        )

    assert not output.exists()


def test_replay_publishes_only_relative_deterministic_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\nfixed test bytes\n")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    monkeypatch.setenv("DATABASE_URL", "must-not-cross")
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/untrusted.so")
    calls = _install_fake_pipeline(monkeypatch)

    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = MODULE.replay_existing_pdf(
        notice_id="PBLN_123", source_pdf=source, output_directory=first
    )
    second_manifest = MODULE.replay_existing_pdf(
        notice_id="PBLN_123", source_pdf=source, output_directory=second
    )

    assert sorted(path.name for path in first.iterdir()) == sorted(MODULE.PUBLISHED_NAMES)
    assert first_manifest == second_manifest
    for name in MODULE.PUBLISHED_NAMES:
        assert (first / name).read_bytes() == (second / name).read_bytes()
        assert stat.S_IMODE((first / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    manifest = json.loads((first / "manifest.json").read_text())
    assert manifest["scope"] == "existing_kb_offline_only"
    assert manifest["whole_document"] is True
    assert manifest["artifacts"]["source_pdf"]["path"] == "source.pdf"
    assert manifest["artifacts"]["native_capture"]["path"] == "native.json"
    assert manifest["artifacts"]["common_ir"]["path"] == "common_ir.json"
    assert manifest["artifacts"]["native_capture"]["size_bytes"] > 0
    assert manifest["artifacts"]["common_ir"]["size_bytes"] > 0
    assert manifest["pipeline"]["capture_limits"]["max_text_items"] == 20_000
    assert manifest["coverage"]["native_text_pages"] == [1]
    assert len(calls) == 4
    for command, cwd, environment in calls:
        assert command[0] == os.sys.executable
        assert cwd.name.startswith(".existing-pdf-replay-")
        assert "OPENAI_API_KEY" not in environment
        assert "DATABASE_URL" not in environment
        assert "PYTHONPATH" not in environment
        assert "LD_PRELOAD" not in environment
    assert "--source-relative-path" in calls[0][0]
    assert not any(part.startswith("--page") for command, _, _ in calls for part in command)


def test_replay_stages_optional_surya_sidecar_and_records_its_lineage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\nfixed test bytes\n")
    render_manifest = tmp_path / "render.json"
    render_manifest.write_bytes(_canonical({"schema_version": "pdf_render_manifest/v1"}))
    artifact = tmp_path / "surya.json"
    artifact.write_bytes(_canonical({
        "schema_version": "surya_layout_artifact/v1",
        "source_sha256": "a" * 64,
        "render_manifest_sha256": "b" * 64,
        "logical_compute_key": "c" * 64,
        "producer": {"engine_id": "surya"},
        "requested_pages": [1],
    }))
    calls = _install_fake_pipeline(monkeypatch)
    output = tmp_path / "output"

    manifest = MODULE.replay_existing_pdf(
        notice_id="PBLN_123", source_pdf=source, output_directory=output,
        render_manifest=render_manifest, surya_layout_artifact=artifact,
    )

    assert sorted(path.name for path in output.iterdir()) == sorted((
        MODULE.SOURCE_NAME, MODULE.NATIVE_NAME, MODULE.RENDER_MANIFEST_NAME,
        MODULE.SURYA_LAYOUT_ARTIFACT_NAME, MODULE.COMMON_IR_NAME, MODULE.MANIFEST_NAME,
    ))
    adapter_command = next(command for command, _, _ in calls if MODULE.ADAPTER_MODULE in command)
    assert adapter_command[adapter_command.index("--render-manifest") + 1] == MODULE.RENDER_MANIFEST_NAME
    assert adapter_command[adapter_command.index("--surya-layout-artifact") + 1] == MODULE.SURYA_LAYOUT_ARTIFACT_NAME
    assert str(render_manifest) not in adapter_command
    assert str(artifact) not in adapter_command
    lineage = manifest["artifacts"]["surya_layout_artifact"]
    assert lineage["path"] == MODULE.SURYA_LAYOUT_ARTIFACT_NAME
    assert lineage["logical_compute_key"] == "c" * 64
    assert lineage["producer"] == {"engine_id": "surya"}
    assert stat.S_IMODE((output / MODULE.SURYA_LAYOUT_ARTIFACT_NAME).stat().st_mode) == 0o600


def test_replay_requires_render_manifest_with_surya_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    sidecar = tmp_path / "surya.json"
    sidecar.write_bytes(b"{}")
    with pytest.raises(MODULE.ExistingPdfReplayError, match="provided together"):
        MODULE.replay_existing_pdf(
            notice_id="PBLN_123", source_pdf=source, output_directory=tmp_path / "output",
            surya_layout_artifact=sidecar,
        )


def test_existing_output_is_never_clobbered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("operator data")
    monkeypatch.setattr(
        MODULE,
        "_run_command",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not start"),
    )

    with pytest.raises(MODULE.ExistingPdfReplayError, match="never overwrites"):
        MODULE.replay_existing_pdf(
            notice_id="PBLN_123", source_pdf=source, output_directory=output
        )
    assert marker.read_text() == "operator data"


def test_partial_publish_is_left_fail_closed_without_recursive_cleanup(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    (staging / MODULE.SOURCE_NAME).write_bytes(b"source")
    output = tmp_path / "published"

    with pytest.raises(MODULE.ExistingPdfReplayError, match="cannot publish"):
        MODULE._publish(staging, output)

    assert output.is_dir()
    assert (output / MODULE.SOURCE_NAME).read_bytes() == b"source"
    assert not (output / MODULE.MANIFEST_NAME).exists()


def test_publish_never_replaces_a_concurrently_created_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    for name in MODULE.PUBLISHED_NAMES:
        (staging / name).write_bytes(name.encode("utf-8"))
    output = tmp_path / "published"
    real_link = MODULE.os.link
    raced = False

    def racing_link(source, destination, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            (output / str(destination)).write_bytes(b"concurrent-owner-data")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(MODULE.os, "link", racing_link)
    with pytest.raises(MODULE.ExistingPdfReplayError, match="cannot publish"):
        MODULE._publish(staging, output)

    assert (output / MODULE.SOURCE_NAME).read_bytes() == b"concurrent-owner-data"
    assert not (output / MODULE.MANIFEST_NAME).exists()


def test_input_must_be_a_non_symlink_regular_pdf(tmp_path: Path) -> None:
    real = tmp_path / "real.pdf"
    real.write_bytes(b"%PDF-1.7\n")
    linked = tmp_path / "linked.pdf"
    linked.symlink_to(real)
    with pytest.raises(MODULE.ExistingPdfReplayError, match="symlink"):
        MODULE._copy_source_pdf(linked, tmp_path / "copy.pdf", max_bytes=1024)
    with pytest.raises(MODULE.ExistingPdfReplayError, match="regular file"):
        MODULE._copy_source_pdf(tmp_path, tmp_path / "other.pdf", max_bytes=1024)

    with pytest.raises(MODULE.ExistingPdfReplayError, match="size limit"):
        MODULE._copy_source_pdf(real, tmp_path / "limited.pdf", max_bytes=5)


def test_subprocess_is_shell_free_and_timeout_kills_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class HungProcess:
        pid = 4242
        returncode = None
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["python", "-m", "worker"], timeout)
            self.returncode = -signal.SIGKILL
            return "", ""

    process = HungProcess()
    popen_kwargs = {}
    killed = []

    def popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(MODULE.subprocess, "Popen", popen)
    monkeypatch.setattr(MODULE.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(MODULE.ExistingPdfReplayError, match="exceeded"):
        MODULE._run_command(
            ["python", "-m", "worker"], cwd=tmp_path, timeout_seconds=0.01
        )
    assert popen_kwargs["shell"] is False
    assert popen_kwargs["start_new_session"] is True
    assert popen_kwargs["stdin"] is subprocess.DEVNULL
    assert popen_kwargs["stdout"] is subprocess.DEVNULL
    assert popen_kwargs["stderr"] is subprocess.DEVNULL
    assert callable(popen_kwargs["preexec_fn"])
    assert killed == [(4242, signal.SIGKILL)]
    assert process.calls == 2


def test_successful_subprocess_also_cleans_its_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = SimpleNamespace(
        pid=4243,
        returncode=0,
        communicate=lambda timeout=None: (None, None),
        kill=lambda: None,
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(MODULE.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(MODULE.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    assert MODULE._run_command(
        ["python", "-m", "worker"], cwd=tmp_path, timeout_seconds=1
    ) == ("", "")
    assert killed == [(4243, signal.SIGKILL)]


def test_canonical_replacement_never_follows_an_existing_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external.txt"
    external.write_bytes(b"external-owner-data")
    external.chmod(0o644)
    artifact = tmp_path / "artifact.json"
    artifact.symlink_to(external)

    MODULE._replace_with_canonical_file(artifact, b'{"safe":true}')

    assert not artifact.is_symlink()
    assert artifact.read_bytes() == b'{"safe":true}'
    assert external.read_bytes() == b"external-owner-data"
    assert stat.S_IMODE(external.stat().st_mode) == 0o644


def test_child_resource_limiter_sets_hard_process_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    fake_resource = SimpleNamespace(
        RLIM_INFINITY=-1,
        RLIMIT_AS=1,
        RLIMIT_DATA=2,
        RLIMIT_FSIZE=3,
        RLIMIT_CPU=4,
        RLIMIT_NOFILE=5,
        RLIMIT_CORE=6,
        getrlimit=lambda _limit: (-1, -1),
        setrlimit=lambda limit, values: calls.append((limit, values)),
    )
    monkeypatch.setattr(MODULE, "resource", fake_resource)

    MODULE._child_resource_limiter(10.2)()

    assert calls == [
        (fake_resource.RLIMIT_AS, (MODULE.CHILD_ADDRESS_SPACE_BYTES,) * 2),
        (fake_resource.RLIMIT_DATA, (MODULE.CHILD_ADDRESS_SPACE_BYTES,) * 2),
        (fake_resource.RLIMIT_FSIZE, (MODULE.CHILD_FILE_SIZE_BYTES,) * 2),
        (fake_resource.RLIMIT_CPU, (16, 16)),
        (fake_resource.RLIMIT_NOFILE, (MODULE.CHILD_OPEN_FILES,) * 2),
        (fake_resource.RLIMIT_CORE, (0, 0)),
    ]


def test_common_ir_must_not_leak_absolute_or_extra_artifact_paths(tmp_path: Path) -> None:
    payload = _common_ir("PBLN_123", "0" * 64)
    payload["document"]["provenance"]["source_location"] = str(tmp_path / "source.pdf")
    with pytest.raises(MODULE.ExistingPdfReplayError, match="relative source.pdf"):
        MODULE._require_relative_artifact_references(payload)

    payload = _common_ir("PBLN_123", "0" * 64)
    payload["document"]["raw_artifact_ids"].append("sidecar.json")
    with pytest.raises(MODULE.ExistingPdfReplayError, match="do not match staged artifacts"):
        MODULE._require_relative_artifact_references(payload)


def test_common_ir_output_is_revalidated_against_source_binding() -> None:
    source_hash = "a" * 64
    payload = _common_ir("PBLN_123", source_hash)
    MODULE._validate_common_ir_output(
        payload,
        notice_id="PBLN_123",
        source_sha256=source_hash,
        page_count=1,
        pinned_inspector_version="1.17.0",
    )

    tampered = json.loads(json.dumps(payload))
    tampered["document"]["provenance"]["source_sha256"] = "b" * 64
    with pytest.raises(MODULE.ExistingPdfReplayError, match="source_sha256"):
        MODULE._validate_common_ir_output(
            tampered,
            notice_id="PBLN_123",
            source_sha256=source_hash,
            page_count=1,
            pinned_inspector_version="1.17.0",
        )

    wrong_notice = json.loads(json.dumps(payload))
    wrong_notice["document"]["document_id"] = "pdf:PBLN_OTHER"
    with pytest.raises(MODULE.ExistingPdfReplayError, match="document_id"):
        MODULE._validate_common_ir_output(
            wrong_notice,
            notice_id="PBLN_123",
            source_sha256=source_hash,
            page_count=1,
            pinned_inspector_version="1.17.0",
        )


def test_json_artifact_is_bounded_and_symlinks_are_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"ok":true}', encoding="utf-8")
    assert MODULE._json_object(artifact, max_bytes=artifact.stat().st_size) == {"ok": True}

    with pytest.raises(MODULE.ExistingPdfReplayError, match="size limit"):
        MODULE._json_object(artifact, max_bytes=1)

    linked = tmp_path / "linked.json"
    linked.symlink_to(artifact)
    with pytest.raises(MODULE.ExistingPdfReplayError, match="cannot read"):
        MODULE._json_object(linked, max_bytes=1024)
