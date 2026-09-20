"""Safety checks for the public-primary-corpus Surya export command."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import zlib

import pytest

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    PdfCoordinateManifest,
    build_sidecar_binding,
)
from common_ir_pipeline.pdf_fusion.native_capture import canonical_json_bytes
from common_ir_pipeline.pdf_fusion.primary_corpus_split import (
    PrimaryCorpusSplitError,
    canonical_primary_corpus_split_json,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    RenderedPageInput,
    assemble_render_manifest,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact,
    SuryaLayoutPage,
    SuryaProducerIdentity as PortableProducer,
)
from scripts import export_pdf_primary_corpus_surya_artifact as export
from scripts import run_persistent_surya_storage_e2e as e2e
from worker.accelerator_coordinator import CoordinatorDisposition
from worker.contracts.accelerator import (
    SuryaProducerIdentity as ContractProducer,
    build_surya_layout_reconciliation_logical_compute_key,
)


def _digest(value: bytes | str) -> str:
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30\x10\x20\x30"))
        + chunk(b"IEND", b"")
    )


def _native(source: Path, notice_id: str) -> dict[str, object]:
    return {
        "capture_schema_version": "pdf_inspector_native_capture/v1",
        "notice_id": notice_id,
        "source_kind": "pdf",
        "artifact_role": "production",
        "method": "pdf_inspector",
        "version": "1.17.0",
        "extraction_scope": "full_document",
        "source_path": "source.pdf",
        "source_sha256": _digest(source.read_bytes()),
        "source_size_bytes": source.stat().st_size,
        "process_result": {
            "pdf_type": "text_based", "markdown": "fixture", "page_count": 1,
            "pages_needing_ocr": [], "ocr_reasons_by_page": [], "title": None,
            "confidence": 1.0, "is_complex_layout": False,
            "pages_with_tables": [], "pages_with_columns": [], "has_encoding_issues": False,
        },
        "pages_markdown_result": {
            "pages": [{"page": 0, "markdown": "fixture", "needs_ocr": False, "ocr_reason": None}],
            "pages_with_tables": [], "pages_with_columns": [], "pages_needing_ocr": [],
            "ocr_reasons_by_page": [], "is_complex": False,
        },
        "text_items": [{
            "page": 1, "text": "fixture", "x": 0.0, "y": 1.0,
            "width": 2.0, "height": 1.0, "font": "Fixture", "font_tag": "F1",
            "font_size": 10.0, "is_bold": False, "is_italic": False,
            "is_underline": False, "is_strikeout": False, "item_type": "text",
            "mcid": None,
        }], "structure_elements": [],
    }


def _producer() -> ContractProducer:
    return ContractProducer(
        engine_id="surya", engine_version="0.1", model_id="surya-layout",
        model_revision="1" * 40, model_weights_sha256=_digest("weights"),
        pipeline_revision="pipeline-r1", config_sha256=_digest("config"),
        worker_image_digest=f"sha256:{_digest('image')}",
    )


def _common_ir(notice_id: str, source_sha256: str) -> dict[str, object]:
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": f"pdf:{notice_id}", "source_kind": "pdf",
            "artifact_role": "production", "page_count": 1,
            "pdf_semantic_eligibility": "eligible_native_text",
            "pdf_semantic_reason": "substantive_native_text_available",
            "native_text_page_count": 1, "raw_artifact_ids": ["native.json"],
            "provenance": {
                "method": "pdf_native_only", "page": None, "bbox": None,
                "coordinate_space": None, "source_location": "source.pdf",
                "source_sha256": source_sha256, "generator": "common_ir_v1_adapters",
                "generator_version": "1.1.0", "schema_version": "common_ir_v1",
                "parser": "pdf_inspector", "parser_version": "1.17.0",
            },
        },
        "blocks": [], "conflicts": [], "relations": [],
    }


def _case_root(root: Path, notice_id: str) -> None:
    native = root / "native"
    render = root / "render"
    native.mkdir(parents=True)
    render.mkdir()
    source = b"%PDF-1.7\nfixture\n"
    (native / "source.pdf").write_bytes(source)
    (render / "source.pdf").write_bytes(source)
    (native / "native.json").write_bytes(canonical_json_bytes(_native(native / "source.pdf", notice_id)))
    image = render / "rendered" / "page-0001.png"
    image.parent.mkdir()
    image.write_bytes(_png())
    coordinate = PdfCoordinateManifest(
        source_sha256=_digest(source), page=1, page_count=1,
        media_box=(0, 0, 2, 1), crop_box=(0, 0, 2, 1), rotation=0, user_unit=1.0,
        canonical_width_pt=2.0, canonical_height_pt=1.0, render_scale_px_per_point=1.0,
        rendered_width_px=2, rendered_height_px=1, pdf_origin="bottom_left",
        pdf_x_axis="right", pdf_y_axis="up", pixel_origin="top_left",
        pixel_x_axis="right", pixel_y_axis="down",
        user_to_pixel=AffineTransform.from_sequence([1, 0, 0, -1, 0, 1]),
        pixel_to_user=AffineTransform.from_sequence([1, 0, 0, -1, 0, 1]),
        renderer="pdfium", renderer_version="1", renderer_config_sha256=_digest("config"),
        page_image_sha256=_digest(image.read_bytes()),
    )
    manifest = assemble_render_manifest(
        artifact_root=render, source_pdf_path=render / "source.pdf",
        pages=(RenderedPageInput(image_path=image, coordinate_manifest=coordinate),),
    )
    (render / "render_manifest.json").write_bytes(manifest.canonical_json())
    common = canonical_json_bytes(_common_ir(notice_id, _digest(source)))
    (native / "common_ir.json").write_bytes(common)
    native_bytes = (native / "native.json").read_bytes()
    native_manifest = {
        "schema_version": "existing_pdf_native_replay/v1",
        "scope": "existing_kb_offline_only",
        "notice_id": notice_id,
        "whole_document": True,
        "pipeline": {
            "capture_module": "common_ir_pipeline.workers.pdf_inspector_capture",
            "pdf_inspector_version": "1.17.0", "capture_limits": {},
            "adapter_module": "common_ir_pipeline.adapters.pdf_native",
        },
        "artifacts": {
            "source_pdf": {"path": "source.pdf", "sha256": _digest((native / "source.pdf").read_bytes()), "size_bytes": (native / "source.pdf").stat().st_size},
            "native_capture": {"path": "native.json", "sha256": _digest(native_bytes), "size_bytes": len(native_bytes), "method": "pdf_inspector", "version": "1.17.0", "page_count": 1},
            "common_ir": {"path": "common_ir.json", "sha256": _digest(common), "size_bytes": len(common), "generator": "common_ir_v1_adapters", "generator_version": "1.1.0", "schema_version": "common_ir_v1", "page_count": 1},
        },
        "coverage": {
            "document_page_count": 1, "native_text_item_count": 1,
            "native_text_pages": [1], "common_ir_block_count": 0, "common_ir_pages": [],
        },
    }
    (native / "manifest.json").write_text(json.dumps(native_manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    root = tmp_path / "case"
    notice_id = "PBLN_000000000000001"
    _case_root(root, notice_id)
    payload = json.loads((Path(__file__).resolve().parents[1] / "baselines/pdf_reconstruction/primary_corpus_split_a45.v1.json").read_text())
    payload["cases"][0].update({
        "case_id": "public-case", "notice_id": notice_id,
        "source_pdf_sha256": _digest((root / "render/source.pdf").read_bytes()),
    })
    split = tmp_path / "split.json"
    split.write_bytes(canonical_primary_corpus_split_json(payload))
    expected = _digest(split.read_bytes())
    monkeypatch.setattr(e2e, "_load_runpod_settings", lambda _: SimpleNamespace(producer=_producer()))
    monkeypatch.setattr(export, "validate_split_source_baseline_file", lambda *_a, **_k: "baseline")
    return export.build_parser().parse_args([
        "--split", str(split), "--expected-split-sha256", expected,
        "--source-baseline", str(Path(__file__).resolve().parents[1] / "baselines/pdf_fusion/bizinfo_existing_100.v1.json"),
        "--case-id", "public-case", "--case-root", str(root),
        "--runpod-config-json", str(tmp_path / "config.json"),
        "--runpod-api-client-config", str(tmp_path / "client.json"),
        "--runpod-bearer-token-file", str(tmp_path / "token"),
        "--storage-bucket", "request-temp", "--supabase-url", "http://127.0.0.1:8000",
        "--supabase-service-role-key-file", str(tmp_path / "service-key"),
    ])


def _artifact(args: object) -> SuryaLayoutArtifact:
    manifest = e2e._load_render_manifest(args.case_root / "render")
    producer = _producer()
    portable = PortableProducer.from_dict(producer.model_dump(mode="json"))
    page = manifest.pages[0]
    return SuryaLayoutArtifact(
        logical_compute_key=build_surya_layout_reconciliation_logical_compute_key(manifest, producer),
        source_sha256=manifest.source_pdf_sha256, page_count=manifest.page_count,
        render_manifest_schema_version=manifest.schema_version,
        render_manifest_sha256=manifest.manifest_sha256(), producer=portable,
        requested_pages=(1,), pages=(SuryaLayoutPage(
            page=1, sidecar_binding=build_sidecar_binding(page.coordinate_manifest),
            pixel_width=2, pixel_height=1, rendered_page_px=(0, 0, 2, 1), regions=(),
        ),),
    )


def _success(args: object, **kwargs: object) -> e2e.SafeE2EOutcome:
    assert args.artifact_root == args.case_root / "render"
    assert kwargs["expected_source_sha256"] == _digest((args.case_root / "render/source.pdf").read_bytes())
    kwargs["accepted_artifact"](_artifact(args))
    return e2e.SafeE2EOutcome(
        disposition=CoordinatorDisposition.SUCCEEDED.value, reason_code="completed"
    )


def test_success_publishes_exact_canonical_private_single_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path, monkeypatch)
    export.run_export(args, e2e_runner=_success)
    target = args.case_root / "surya/surya_layout_artifact.json"
    assert target.read_bytes() == _artifact(args).canonical_json()
    info = os.stat(target)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_nlink == 1
    assert stat.S_IMODE((args.case_root / "surya").stat().st_mode) == 0o700


@pytest.mark.parametrize("kind", ["anchor", "case", "source", "sealed"])
def test_anchor_case_source_and_sealed_reject_before_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    args = _args(tmp_path, monkeypatch)
    if kind == "anchor":
        args.expected_split_sha256 = "0" * 64
    elif kind == "case":
        args.case_id = "unknown-case"
    elif kind == "sealed":
        args.case_id = "blind-x-01"
    else:
        (args.case_root / "native/source.pdf").write_bytes(b"%PDF-1.7\nchanged\n")
    called = False

    def runner(*_: object, **__: object) -> e2e.SafeE2EOutcome:
        nonlocal called
        called = True
        raise AssertionError("network path must not run")

    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(args, e2e_runner=runner)
    assert not called
    assert not (args.case_root / "surya/surya_layout_artifact.json").exists()


@pytest.mark.parametrize("kind", ["existing", "symlink", "unsafe"])
def test_existing_symlink_and_unsafe_output_reject_before_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    args = _args(tmp_path, monkeypatch)
    parent = args.case_root / "surya"
    parent.mkdir()
    target = parent / "surya_layout_artifact.json"
    if kind == "existing":
        target.write_bytes(b"existing")
    elif kind == "symlink":
        target.symlink_to(args.case_root / "render/render_manifest.json")
    else:
        parent.chmod(0o775)
    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(args, e2e_runner=lambda *_a, **_k: pytest.fail("must not run E2E"))


@pytest.mark.parametrize("relative", ["native/source.pdf", "native/native.json", "render/render_manifest.json", "render/rendered/page-0001.png"])
def test_group_writable_evidence_rejects_before_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    args = _args(tmp_path, monkeypatch)
    (args.case_root / relative).chmod(0o620)
    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(args, e2e_runner=lambda *_a, **_k: pytest.fail("must not run E2E"))


def test_no_or_multiple_callback_never_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path, monkeypatch)

    def none(*_: object, **__: object) -> e2e.SafeE2EOutcome:
        return e2e.SafeE2EOutcome(disposition=CoordinatorDisposition.SUCCEEDED.value, reason_code="completed")

    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(args, e2e_runner=none)
    assert not (args.case_root / "surya/surya_layout_artifact.json").exists()

    # Use a new, otherwise-valid case.  The runner deliberately swallows the
    # second callback's failure; the exporter must still observe count=2 and
    # refuse publication after the E2E returns a nominal success.
    multiple_args = _args(tmp_path / "multiple", monkeypatch)

    def multiple(run_args: object, **kwargs: object) -> e2e.SafeE2EOutcome:
        kwargs["accepted_artifact"](_artifact(run_args))
        with pytest.raises(export.PrimaryCorpusSuryaExportError):
            kwargs["accepted_artifact"](_artifact(run_args))
        return e2e.SafeE2EOutcome(disposition=CoordinatorDisposition.SUCCEEDED.value, reason_code="completed")

    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(multiple_args, e2e_runner=multiple)
    assert not (multiple_args.case_root / "surya/surya_layout_artifact.json").exists()


def test_case_root_swap_before_publish_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path, monkeypatch)

    def swapped(run_args: object, **kwargs: object) -> e2e.SafeE2EOutcome:
        kwargs["accepted_artifact"](_artifact(run_args))
        original = run_args.case_root
        original.rename(original.with_name("old-case"))
        original.mkdir(mode=0o700)
        return e2e.SafeE2EOutcome(disposition=CoordinatorDisposition.SUCCEEDED.value, reason_code="completed")

    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(args, e2e_runner=swapped)
    assert not (args.case_root / "surya/surya_layout_artifact.json").exists()
    assert not (args.case_root.with_name("old-case") / "surya/surya_layout_artifact.json").exists()


def test_post_link_cleanup_fault_is_committed_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path, monkeypatch)
    real_unlink = export.os.unlink

    def fail_temporary_unlink(path: object, *args: object, **kwargs: object) -> None:
        if isinstance(path, str) and path.startswith(".surya_layout_artifact.json."):
            raise OSError("injected cleanup fault")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(export.os, "unlink", fail_temporary_unlink)
    outcome = export.run_export(args, e2e_runner=_success)
    assert outcome.disposition == CoordinatorDisposition.SUCCEEDED.value
    assert (args.case_root / "surya/surya_layout_artifact.json").is_file()


@pytest.mark.parametrize("fault", ["directory_fsync", "post_commit_close"])
def test_post_link_durability_faults_remain_committed_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    args = _args(tmp_path, monkeypatch)
    if fault == "directory_fsync":
        real_fsync = export.os.fsync
        calls = 0

        def fail_second_fsync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected directory fsync fault")
            real_fsync(fd)

        monkeypatch.setattr(export.os, "fsync", fail_second_fsync)
    else:
        real_close = export.os.close
        calls = 0

        def fail_after_commit_close(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise OSError("injected post-commit close fault")
            real_close(fd)

        monkeypatch.setattr(export.os, "close", fail_after_commit_close)
    outcome = export.run_export(args, e2e_runner=_success)
    assert outcome.disposition == CoordinatorDisposition.SUCCEEDED.value
    assert (args.case_root / "surya/surya_layout_artifact.json").is_file()


def test_source_baseline_rejects_before_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path, monkeypatch)
    monkeypatch.setattr(
        export,
        "validate_split_source_baseline_file",
        lambda *_a, **_k: (_ for _ in ()).throw(PrimaryCorpusSplitError("baseline mismatch")),
    )
    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(args, e2e_runner=lambda *_a, **_k: pytest.fail("must not run E2E"))


@pytest.mark.parametrize("mutation", ["coverage", "scope", "common_descriptor"])
def test_replay_receipt_lineage_mutations_reject_before_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    args = _args(tmp_path, monkeypatch)
    receipt_path = args.case_root / "native/manifest.json"
    receipt = json.loads(receipt_path.read_text())
    if mutation == "coverage":
        receipt["coverage"]["native_text_pages"] = []
    elif mutation == "scope":
        receipt["scope"] = "other"
    else:
        receipt["artifacts"]["common_ir"]["schema_version"] = "other"
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(args, e2e_runner=lambda *_a, **_k: pytest.fail("must not run E2E"))

@pytest.mark.parametrize("mutation", ["source", "pages", "producer", "logical_key"])
def test_callback_mutations_are_reparsed_and_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    args = _args(tmp_path, monkeypatch)

    def mutated(run_args: object, **kwargs: object) -> e2e.SafeE2EOutcome:
        artifact = _artifact(run_args)
        if mutation == "source":
            artifact = replace(artifact, source_sha256="0" * 64)
        elif mutation == "pages":
            artifact = replace(artifact, page_count=2)
        elif mutation == "producer":
            artifact = replace(artifact, producer=replace(artifact.producer, model_id="other-model"))
        else:
            artifact = replace(artifact, logical_compute_key="0" * 64)
        kwargs["accepted_artifact"](artifact)
        raise AssertionError("mutated callback must fail")

    with pytest.raises(export.PrimaryCorpusSuryaExportError):
        export.run_export(args, e2e_runner=mutated)
    assert not (args.case_root / "surya/surya_layout_artifact.json").exists()


def test_main_redacts_secret_exception_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(export, "run_export", lambda _: (_ for _ in ()).throw(RuntimeError("token=secret signed-url=https://private")))
    assert export.main([
        "--split", str(tmp_path / "split"), "--expected-split-sha256", "0" * 64,
        "--source-baseline", str(tmp_path / "baseline"),
        "--case-id", "blind-x-01", "--case-root", str(tmp_path / "case"),
        "--runpod-config-json", str(tmp_path / "config"), "--runpod-api-client-config", str(tmp_path / "client"),
        "--runpod-bearer-token-file", str(tmp_path / "token"), "--storage-bucket", "bucket",
        "--supabase-url", "http://127.0.0.1:8000", "--supabase-service-role-key-file", str(tmp_path / "key"),
    ]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output and "signed-url" not in output and "blind-x-01" not in output
