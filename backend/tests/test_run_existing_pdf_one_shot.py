"""Offline orchestration checks for the Existing PDF one-shot operator."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
from types import SimpleNamespace
import zlib

import pytest

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    PdfCoordinateManifest,
    build_sidecar_binding,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    RenderedPageInput,
    assemble_render_manifest,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact,
    SuryaLayoutPage,
)
from scripts import run_existing_pdf_one_shot as operator
from worker.contracts.accelerator import (
    SuryaProducerIdentity,
    build_surya_layout_reconciliation_logical_compute_key,
)


NOTICE_ID = "PBLN_123456"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_existing_pdf_one_shot.py"
REPOSITORY_ROOT = SCRIPT.parents[2]


def _digest(value: bytes | str) -> str:
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30\x10\x20\x30"))
        + chunk(b"IEND", b"")
    )


def _private(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    pdf = tmp_path / "notice.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfixture\n")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "notice_id": NOTICE_ID,
                "pblanc_id": NOTICE_ID,
                "title": "fixture",
                "apply_period": "2026-01-01 ~ 2026-01-31",
                "ministry": "ministry",
                "executing_agency": "agency",
                "registered_at": "2026-01-01",
                "detail_url": "https://example.test/notice",
            }
        ),
        encoding="utf-8",
    )
    runpod = _private(tmp_path / "runpod.json", '{"config":"fixture"}\n')
    api = _private(tmp_path / "api.json", '{"origin":"fixture"}\n')
    token = _private(tmp_path / "bearer.token", "not-a-real-token\n")
    backend_env = _private(
        tmp_path / "backend.env",
        "PREREVIEW_LLM_PROVIDER=openai\n"
        "OPENAI_API_KEY=not-a-real-key\n"
        "OPENAI_LLM_MODEL=gpt-5.6-terra\n",
    )
    return pdf, metadata, runpod, api, token, backend_env


def _args(
    tmp_path: Path,
    *,
    output: Path,
    resume: bool = False,
    no_ingest: bool = True,
) -> object:
    pdf, metadata, runpod, api, token, backend_env = _inputs(tmp_path)
    supabase = _private(tmp_path / "supabase.env", "fixture=true\n")
    argv = [
        "--pdf",
        str(pdf),
        "--metadata",
        str(metadata),
        "--output-dir",
        str(output),
        "--runpod-config-json",
        str(runpod),
        "--runpod-api-client-config",
        str(api),
        "--runpod-bearer-token-file",
        str(token),
        "--supabase-runtime-env",
        str(supabase),
        "--backend-env",
        str(backend_env),
        "--generated-at",
        "2026-09-17T00:00:00Z",
    ]
    if resume:
        argv.append("--resume")
    if no_ingest:
        argv.append("--no-ingest")
    return operator.build_parser().parse_args(argv)


def _render_pages(source: Path, output: Path, *, page_count: int) -> None:
    output.mkdir()
    staged_source = output / "source.pdf"
    shutil.copyfile(source, staged_source)
    source_hash = _digest(source.read_bytes())
    inputs: list[RenderedPageInput] = []
    for page_number in range(1, page_count + 1):
        image = output / "rendered" / f"page-{page_number:04d}.png"
        image.parent.mkdir(exist_ok=True)
        image.write_bytes(_png())
        coordinate = PdfCoordinateManifest(
            source_sha256=source_hash,
            page=page_number,
            page_count=page_count,
            media_box=(0, 0, 2, 1),
            crop_box=(0, 0, 2, 1),
            rotation=0,
            user_unit=1.0,
            canonical_width_pt=2.0,
            canonical_height_pt=1.0,
            render_scale_px_per_point=1.0,
            rendered_width_px=2,
            rendered_height_px=1,
            pdf_origin="bottom_left",
            pdf_x_axis="right",
            pdf_y_axis="up",
            pixel_origin="top_left",
            pixel_x_axis="right",
            pixel_y_axis="down",
            user_to_pixel=AffineTransform.from_sequence([1, 0, 0, -1, 0, 1]),
            pixel_to_user=AffineTransform.from_sequence([1, 0, 0, -1, 0, 1]),
            renderer="pdfium",
            renderer_version="fixture",
            renderer_config_sha256=_digest("renderer-config"),
            page_image_sha256=_digest(image.read_bytes()),
        )
        inputs.append(
            RenderedPageInput(image_path=image, coordinate_manifest=coordinate)
        )
    manifest = assemble_render_manifest(
        artifact_root=output,
        source_pdf_path=staged_source,
        pages=tuple(inputs),
    )
    (output / "render_manifest.json").write_bytes(manifest.canonical_json())


def _render(source: Path, output: Path, _: float) -> None:
    _render_pages(source, output, page_count=1)


def _producer() -> SuryaProducerIdentity:
    return SuryaProducerIdentity(
        engine_id="surya",
        engine_version="fixture",
        model_id="layout",
        model_revision="1" * 40,
        model_weights_sha256=_digest("weights"),
        pipeline_revision="fixture",
        config_sha256=_digest("config"),
        worker_image_digest=f"sha256:{_digest('image')}",
    )


def _artifact(
    render_root: Path, producer_contract: SuryaProducerIdentity
) -> SuryaLayoutArtifact:
    manifest = operator._load_render(render_root)
    producer = operator.PortableSuryaProducerIdentity.from_dict(
        producer_contract.model_dump(mode="json")
    )
    return SuryaLayoutArtifact(
        logical_compute_key=build_surya_layout_reconciliation_logical_compute_key(
            manifest, producer_contract
        ),
        source_sha256=manifest.source_pdf_sha256,
        page_count=manifest.page_count,
        render_manifest_schema_version=manifest.schema_version,
        render_manifest_sha256=manifest.manifest_sha256(),
        producer=producer,
        requested_pages=tuple(range(1, manifest.page_count + 1)),
        pages=tuple(
            SuryaLayoutPage(
                page=page.page,
                sidecar_binding=build_sidecar_binding(page.coordinate_manifest),
                pixel_width=page.coordinate_manifest.rendered_width_px,
                pixel_height=page.coordinate_manifest.rendered_height_px,
                rendered_page_px=(
                    0,
                    0,
                    page.coordinate_manifest.rendered_width_px,
                    page.coordinate_manifest.rendered_height_px,
                ),
                regions=(),
            )
            for page in manifest.pages
        ),
    )


def _write_replay(
    notice_id: str,
    source: Path,
    output: Path,
    *,
    artifact: Path | None = None,
    render_manifest: Path | None = None,
    page_count: int = 1,
    native_text_page_count: int = 1,
    pdf_semantic_eligibility: str = "eligible_native_text",
) -> None:
    output.mkdir()
    source_hash = _digest(source.read_bytes())
    shutil.copyfile(source, output / "source.pdf")
    if artifact is not None and render_manifest is not None:
        shutil.copyfile(render_manifest, output / "render_manifest.json")
        shutil.copyfile(artifact, output / "surya_layout_artifact.json")
    (output / "native.json").write_text('{"native":"fixture"}\n', encoding="utf-8")
    (output / "common_ir.json").write_text(
        json.dumps(
            {
                "schema_version": "common_ir_v1",
                "document": {
                    "document_id": f"pdf:{notice_id}",
                    "page_count": page_count,
                    "pdf_semantic_eligibility": pdf_semantic_eligibility,
                    "native_text_page_count": native_text_page_count,
                    "provenance": {"source_sha256": source_hash},
                },
                "blocks": [],
            }
        ),
        encoding="utf-8",
    )
    artifacts: dict[str, dict[str, object]] = {}
    artifact_names = {
        "source_pdf": "source.pdf",
        "native_capture": "native.json",
        "common_ir": "common_ir.json",
    }
    if artifact is not None and render_manifest is not None:
        artifact_names.update(
            {
                "render_manifest": "render_manifest.json",
                "surya_layout_artifact": "surya_layout_artifact.json",
            }
        )
    for key, name in artifact_names.items():
        path = output / name
        artifacts[key] = {
            "path": name,
            "sha256": _digest(path.read_bytes()),
            "size_bytes": path.stat().st_size,
        }
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "existing_pdf_native_replay/v1",
                "notice_id": notice_id,
                "whole_document": True,
                "artifacts": artifacts,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _native_preflight(
    notice_id: str,
    source: Path,
    output: Path,
    _: float,
) -> None:
    _write_replay(notice_id, source, output)


def _replay(
    notice_id: str,
    source: Path,
    artifact: Path,
    render_manifest: Path,
    output: Path,
    _: float,
) -> None:
    _write_replay(
        notice_id,
        source,
        output,
        artifact=artifact,
        render_manifest=render_manifest,
    )


def _structure(_: dict[str, object], config: object) -> tuple[dict[str, object], dict[str, object]]:
    assert getattr(config, "llm_model") == "gpt-5.6-terra"
    return (
        {
            "schema_version": "existing_program_profile/v0.2",
            "notice_id": f"bizinfo:{NOTICE_ID}",
        },
        {"schema_version": "source_selection/v0.2"},
    )


def _ports(calls: list[str], *, ingest_status: str = "ingested") -> operator.PipelinePorts:
    producer = _producer()

    def render(source: Path, output: Path, timeout: float) -> None:
        calls.append("render")
        _render(source, output, timeout)

    def native_preflight(
        notice_id: str, source: Path, output: Path, timeout: float
    ) -> None:
        calls.append("native_preflight")
        _native_preflight(notice_id, source, output, timeout)

    def load_producer(_: Path) -> SuryaProducerIdentity:
        return producer

    def dispatch(_: object, render_root: Path) -> SuryaLayoutArtifact:
        calls.append("dispatch")
        return _artifact(render_root, producer)

    def replay(*args: object) -> None:
        calls.append("replay")
        _replay(*args)  # type: ignore[arg-type]

    def structure(common_ir: dict[str, object], config: object) -> tuple[dict[str, object], dict[str, object]]:
        calls.append("structure")
        return _structure(common_ir, config)

    def validate(package: Path) -> None:
        calls.append("validate")
        record = json.loads(
            (package / "pipeline" / "ingestion_record.v0.1.json").read_text()
        )
        descriptor = record["analysis"]["pdf_fusion"]
        assert descriptor == {
            "native_capture_path": "pipeline/pdf_fusion/native_capture.json",
            "render_manifest_path": "pipeline/pdf_fusion/render_manifest.json",
            "surya_layout_artifact_path": "pipeline/pdf_fusion/surya_layout_artifact.json",
            "replay_manifest_path": "pipeline/pdf_fusion/replay_manifest.json",
            "rendered_pages_path": "pipeline/pdf_fusion/rendered",
        }
        for relative in (*descriptor.values(), "pipeline/pdf_fusion/source.pdf"):
            assert (package / relative).exists()
        assert (package / "pipeline/pdf_fusion/rendered/page-0001.png").is_file()

    def ingest(_: Path, settings: object) -> dict[str, str]:
        calls.append("ingest")
        assert settings == {"fixture": "settings"}
        return {
            "status": ingest_status,
            "notice_id": f"bizinfo:{NOTICE_ID}",
            "profile_version_pk": "00000000-0000-4000-8000-000000000001",
        }

    return operator.PipelinePorts(
        render=render,
        native_preflight=native_preflight,
        load_producer=load_producer,
        dispatch=dispatch,
        replay=replay,
        structure=structure,
        validate_package=validate,
        ingest=ingest,
    )


def test_no_ingest_builds_complete_resumable_pdf_package(tmp_path: Path) -> None:
    output = tmp_path / "output"
    calls: list[str] = []
    result = operator.run_pipeline(
        _args(tmp_path, output=output), ports=_ports(calls)
    )

    assert result.ingested is False
    assert calls == [
        "render",
        "native_preflight",
        "dispatch",
        "replay",
        "structure",
        "validate",
    ]
    assert not (output / "ingest_result.json").exists()
    invocation = json.loads((output / "invocation.json").read_text())
    assert invocation["structuring_policy"] == {
        "composite_candidate_mode": "shadow",
        "native_exact_candidate_mode": "lines+continuations",
        "source_selection_attempts": 2,
        "reasoning_effort": "medium",
        "task_completion_token_caps": {
            "announcement_section_scope_v1": 4096,
            "announcement_block_router_v03": 16384,
            "announcement_source_selection_v02": 32768,
            "announcement_anchor_correction_v1": 4096,
        },
        "timeout_seconds": 120.0,
        "prompt_bundle_version": operator.announcement_profiles.PROMPT_BUNDLE_VERSION,
    }
    assert invocation["operator_contract_version"] == operator.PIPELINE_SCHEMA
    assert invocation["operator_code_version"] == operator.OPERATOR_CODE_VERSION
    assert invocation["native_preflight_policy"] == {
        "mode": "native_only_replay",
        "capture_reused_by_fusion": False,
    }
    assert invocation["surya_producer"] == _producer().model_dump(mode="json")
    assert invocation["execution_policy"] == {
        "render_timeout_seconds": 300.0,
        "replay_timeout_seconds": 300.0,
        "storage_timeout_seconds": 30.0,
        "http_timeout_seconds": 30.0,
        "poll_timeout_seconds": 180.0,
        "poll_interval_seconds": 2.0,
    }

    resume_calls: list[str] = []
    resumed = operator.run_pipeline(
        _args(tmp_path, output=output, resume=True), ports=_ports(resume_calls)
    )
    assert resumed.ingested is False
    assert resume_calls == ["validate"]


def test_resume_reverifies_prior_ingest_receipt_with_idempotent_importer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    monkeypatch.setattr(
        operator,
        "load_local_supabase_settings",
        lambda *args, **kwargs: {"fixture": "settings"},
    )
    calls: list[str] = []
    result = operator.run_pipeline(
        _args(tmp_path, output=output, no_ingest=False), ports=_ports(calls)
    )
    assert result.ingested is True
    assert calls[-2:] == ["validate", "ingest"]

    resume_calls: list[str] = []
    resumed = operator.run_pipeline(
        _args(tmp_path, output=output, resume=True, no_ingest=False),
        ports=_ports(resume_calls),
    )
    assert resumed.ingested is True
    assert resume_calls == ["validate", "ingest"]


def test_forged_ingest_receipt_is_checked_after_idempotent_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    monkeypatch.setattr(
        operator,
        "load_local_supabase_settings",
        lambda *args, **kwargs: {"fixture": "settings"},
    )
    operator.run_pipeline(
        _args(tmp_path, output=output, no_ingest=False), ports=_ports([])
    )
    (output / "ingest_result.json").write_text(
        json.dumps(
            {
                "status": "already_ingested",
                "notice_id": f"bizinfo:{NOTICE_ID}",
                "profile_version_pk": "00000000-0000-4000-8000-000000000099",
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    with pytest.raises(
        operator.ExistingPdfOneShotError, match="ingest_result_invalid"
    ):
        operator.run_pipeline(
            _args(tmp_path, output=output, resume=True, no_ingest=False),
            ports=_ports(calls),
        )

    assert calls == ["validate", "ingest"]


def test_existing_output_requires_explicit_resume(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(operator.ExistingPdfOneShotError, match="output_conflict"):
        operator.run_pipeline(_args(tmp_path, output=output), ports=_ports([]))


def test_resume_rejects_output_root_that_is_not_private(tmp_path: Path) -> None:
    output = tmp_path / "output"
    operator.run_pipeline(_args(tmp_path, output=output), ports=_ports([]))
    output.chmod(0o755)

    with pytest.raises(operator.ExistingPdfOneShotError, match="output_conflict"):
        operator.run_pipeline(
            _args(tmp_path, output=output, resume=True), ports=_ports([])
        )


def test_invalid_pdf_is_rejected_without_reading_the_whole_file(tmp_path: Path) -> None:
    args = _args(tmp_path, output=tmp_path / "output")
    args.pdf.write_bytes(b"not-pdf")
    with pytest.raises(operator.ExistingPdfOneShotError, match="input_invalid"):
        operator.run_pipeline(args, ports=_ports([]))


def test_native_preflight_failure_stops_before_gpu_dispatch(tmp_path: Path) -> None:
    calls: list[str] = []
    ports = _ports(calls)

    def fail_native(*args: object) -> None:
        del args
        calls.append("native_preflight_failed")
        raise RuntimeError("fixture parser failure")

    with pytest.raises(RuntimeError, match="fixture parser failure"):
        operator.run_pipeline(
            _args(tmp_path, output=tmp_path / "output"),
            ports=replace(ports, native_preflight=fail_native),
        )

    assert calls == ["render", "native_preflight_failed"]


@pytest.mark.parametrize(
    "reason",
    ["storage_upload_failed", "storage_capability_issue_failed"],
)
def test_default_dispatch_preserves_safe_storage_failure_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    args = _args(tmp_path, output=tmp_path / "output")

    def fail(*_: object, **__: object) -> object:
        raise operator.surya_e2e.PersistentSuryaE2EError(reason)

    monkeypatch.setattr(operator.surya_e2e, "run_e2e", fail)

    with pytest.raises(operator.ExistingPdfOneShotError, match=reason):
        operator._default_dispatch(args, tmp_path)


@pytest.mark.parametrize(
    ("page_count", "native_text_page_count", "eligibility"),
    [
        (1, 0, "excluded_image_only"),
        (2, 1, "eligible_native_text"),
        (2, 2, "eligible_native_text"),
    ],
)
def test_native_preflight_requires_native_text_on_every_page_before_dispatch(
    tmp_path: Path,
    page_count: int,
    native_text_page_count: int,
    eligibility: str,
) -> None:
    calls: list[str] = []
    ports = _ports(calls)

    def ineligible_native(
        notice_id: str, source: Path, output: Path, _: float
    ) -> None:
        calls.append("native_preflight")
        _write_replay(
            notice_id,
            source,
            output,
            page_count=page_count,
            native_text_page_count=native_text_page_count,
            pdf_semantic_eligibility=eligibility,
        )

    with pytest.raises(
        operator.ExistingPdfOneShotError, match="native_preflight_ineligible"
    ):
        operator.run_pipeline(
            _args(tmp_path, output=tmp_path / "output"),
            ports=replace(ports, native_preflight=ineligible_native),
        )

    assert calls == ["render", "native_preflight"]


def test_openai_debug_logging_is_rejected_before_client_or_pipeline_work(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    args = _args(tmp_path, output=tmp_path / "output")
    args.backend_env.write_text(
        args.backend_env.read_text(encoding="utf-8") + "OPENAI_LOG=DeBuG\n",
        encoding="utf-8",
    )

    with pytest.raises(
        operator.ExistingPdfOneShotError, match="openai_configuration_invalid"
    ):
        operator.run_pipeline(args, ports=_ports(calls))

    assert calls == []


def test_ambient_openai_debug_logging_is_rejected_before_pipeline_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    args = _args(tmp_path, output=tmp_path / "output")
    monkeypatch.setenv("OPENAI_LOG", "DeBuG")

    with pytest.raises(
        operator.ExistingPdfOneShotError, match="openai_configuration_invalid"
    ):
        operator.run_pipeline(args, ports=_ports(calls))

    assert calls == []


def test_direct_cli_help_bootstraps_the_pinned_vendor_package() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--runpod-config-json" in completed.stdout


def test_default_structure_pins_reviewed_task_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            constructed.append(kwargs)

    def fake_structure(
        document: dict[str, object], client: object, **kwargs: object
    ) -> object:
        assert document == {"fixture": True}
        assert set(client._delegates) == set(operator.TASK_COMPLETION_TOKEN_CAPS)  # type: ignore[attr-defined]
        assert kwargs == {
            "model_profile": "existing_profile",
            "composite_candidate_mode": "shadow",
            "native_exact_candidate_mode": "lines+continuations",
            "source_selection_attempts": 2,
        }
        return SimpleNamespace(profile={"profile": True}, source_selection={"selection": True})

    monkeypatch.setattr(operator, "OpenAILLMClient", FakeClient)
    monkeypatch.setattr(
        operator.announcement_profiles,
        "structure_announcement_profile_artifacts",
        fake_structure,
    )
    config = operator.OpenAIConfig(
        api_key="not-a-real-key",
        llm_model="gpt-5.6-terra",
        timeout_seconds=42.0,
    )

    assert operator._default_structure({"fixture": True}, config) == (
        {"profile": True},
        {"selection": True},
    )
    assert {item["max_completion_tokens"] for item in constructed} == {
        4096,
        16384,
        32768,
    }
    assert all(item["reasoning_effort"] == "medium" for item in constructed)
    assert all(item["timeout_seconds"] == 42.0 for item in constructed)
    assert all(
        item["model_profiles"] == {"existing_profile": "gpt-5.6-terra"}
        for item in constructed
    )


def test_default_replay_passes_both_trusted_surya_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import replay_existing_pdf_native as replay

    captured: dict[str, object] = {}

    def fake_replay_existing_pdf(
        *,
        notice_id: str,
        source_pdf: Path,
        output_directory: Path,
        timeout_seconds: float,
        surya_layout_artifact: Path | None = None,
        render_manifest: Path | None = None,
    ) -> None:
        captured.update(
            {
                "notice_id": notice_id,
                "source_pdf": source_pdf,
                "output_directory": output_directory,
                "timeout_seconds": timeout_seconds,
                "surya_layout_artifact": surya_layout_artifact,
                "render_manifest": render_manifest,
            }
        )

    monkeypatch.setattr(replay, "replay_existing_pdf", fake_replay_existing_pdf)
    source = tmp_path / "source.pdf"
    artifact = tmp_path / "surya.json"
    render_manifest = tmp_path / "render.json"
    output = tmp_path / "replay"

    operator._default_replay(
        NOTICE_ID,
        source,
        artifact,
        render_manifest,
        output,
        30.0,
    )

    assert captured == {
        "notice_id": NOTICE_ID,
        "source_pdf": source,
        "output_directory": output,
        "timeout_seconds": 30.0,
        "surya_layout_artifact": artifact,
        "render_manifest": render_manifest,
    }


@pytest.mark.parametrize("tamper", ["logical_key", "producer", "page_subset"])
def test_surya_resume_rebinds_to_current_manifest_and_producer(
    tmp_path: Path, tamper: str
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    render_root = tmp_path / "render"
    _render_pages(source, render_root, page_count=2)
    producer = _producer()
    payload = _artifact(render_root, producer).to_dict()
    if tamper == "logical_key":
        payload["logical_compute_key"] = _digest("other-key")
    elif tamper == "producer":
        payload["producer"]["pipeline_revision"] = "other-revision"
    else:
        payload["requested_pages"] = [1]
        payload["pages"] = payload["pages"][:1]
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(
        operator.ExistingPdfOneShotError, match="surya_artifact_invalid"
    ):
        operator._load_surya(
            artifact_path,
            operator._load_render(render_root),
            producer,
        )
