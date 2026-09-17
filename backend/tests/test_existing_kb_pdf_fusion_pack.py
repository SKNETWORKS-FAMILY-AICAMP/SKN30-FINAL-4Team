from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import struct
import subprocess
import sys
import zlib

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = BACKEND_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    PdfCoordinateManifest,
    build_sidecar_binding,
)
from common_ir_pipeline.pdf_fusion.native_capture import canonical_json_bytes
from common_ir_pipeline.pdf_fusion.render_manifest import (
    RenderedPageInput,
    assemble_render_manifest,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact,
    SuryaLayoutPage,
    SuryaProducerIdentity,
)
from existing_kb_pack import PackValidationError, validate_notice_directory
from test_existing_kb_bootstrap import _write_pack


PDF_FUSION_DESCRIPTOR = {
    "native_capture_path": "pipeline/pdf_fusion/native_capture.json",
    "render_manifest_path": "pipeline/pdf_fusion/render_manifest.json",
    "surya_layout_artifact_path": "pipeline/pdf_fusion/surya_layout_artifact.json",
    "replay_manifest_path": "pipeline/pdf_fusion/replay_manifest.json",
    "rendered_pages_path": "pipeline/pdf_fusion/rendered",
}


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

    pixels = b"\x00" + (b"\x10\x20\x30" * 2)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _bound_native_capture(
    source: Path,
    *,
    notice_id: str,
) -> dict[str, object]:
    text = "수행기관A"
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
            "pdf_type": "text_based",
            "markdown": text,
            "page_count": 1,
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "title": None,
            "confidence": 1.0,
            "is_complex_layout": False,
            "pages_with_tables": [],
            "pages_with_columns": [],
            "has_encoding_issues": False,
        },
        "pages_markdown_result": {
            "pages": [
                {
                    "page": 0,
                    "markdown": text,
                    "needs_ocr": False,
                    "ocr_reason": None,
                }
            ],
            "pages_with_tables": [],
            "pages_with_columns": [],
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "is_complex": False,
        },
        "text_items": [
            {
                "page": 1,
                "text": text,
                "x": 0.0,
                "y": 1.0,
                "width": 2.0,
                "height": 1.0,
                "font": "Fixture",
                "font_tag": "F1",
                "font_size": 10.0,
                "is_bold": False,
                "is_italic": False,
                "is_underline": False,
                "is_strikeout": False,
                "item_type": "text",
                "mcid": None,
            }
        ],
        "structure_elements": [],
    }


def _add_pdf_fusion(notice: Path) -> None:
    notice_id = notice.name
    source = next((notice / "attachments").iterdir())
    fusion = notice / "pipeline" / "pdf_fusion"
    source_copy = fusion / "source.pdf"
    source_copy.parent.mkdir(parents=True)
    source_copy.write_bytes(source.read_bytes())
    image = fusion / "rendered" / "page-0001.png"
    image.parent.mkdir()
    image.write_bytes(_png())

    source_hash = _digest(source.read_bytes())
    image_hash = _digest(image.read_bytes())
    coordinate = PdfCoordinateManifest(
        source_sha256=source_hash,
        page=1,
        page_count=1,
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
        renderer_version="1",
        renderer_config_sha256=_digest("render-config"),
        page_image_sha256=image_hash,
    )
    render = assemble_render_manifest(
        artifact_root=fusion,
        source_pdf_path=source_copy,
        pages=(RenderedPageInput(image_path=image, coordinate_manifest=coordinate),),
    )
    render_path = fusion / "render_manifest.json"
    render_path.write_bytes(render.canonical_json())

    producer = SuryaProducerIdentity(
        engine_id="surya",
        engine_version="0.22.1",
        model_id="datalab-to/surya-ocr-2",
        model_revision="1" * 40,
        model_weights_sha256=_digest("weights"),
        pipeline_revision="pipeline-r1",
        config_sha256=_digest("surya-config"),
        worker_image_digest=f"sha256:{_digest('image')}",
    )
    surya = SuryaLayoutArtifact(
        logical_compute_key=_digest("logical-compute"),
        source_sha256=source_hash,
        page_count=1,
        render_manifest_schema_version=render.schema_version,
        render_manifest_sha256=render.manifest_sha256(),
        producer=producer,
        requested_pages=(1,),
        pages=(
            SuryaLayoutPage(
                page=1,
                sidecar_binding=build_sidecar_binding(coordinate),
                pixel_width=2,
                pixel_height=1,
                rendered_page_px=(0, 0, 2, 1),
                regions=(),
            ),
        ),
    )
    surya_path = fusion / "surya_layout_artifact.json"
    surya_path.write_bytes(surya.canonical_json())

    native = _bound_native_capture(source, notice_id=notice_id)
    native_path = fusion / "native_capture.json"
    native_path.write_bytes(canonical_json_bytes(native))

    common_ir_path = (
        notice / "pipeline" / "common_ir_v1" / f"{notice_id}.pdf.json"
    )
    common_ir = json.loads(common_ir_path.read_text(encoding="utf-8"))
    common_ir["document"].update(
        {
            "page_count": 1,
            "raw_artifact_ids": [
                "native.json",
                "render_manifest.json",
                "surya_layout_artifact.json",
            ],
        }
    )
    common_ir["document"]["provenance"].update(
        {
            "generator": "common_ir_v1_adapters",
            "generator_version": "1.1.0",
        }
    )
    common_ir_path.write_text(
        json.dumps(common_ir, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    replay = {
        "schema_version": "existing_pdf_native_replay/v1",
        "scope": "existing_kb_offline_only",
        "notice_id": notice_id,
        "whole_document": True,
        "artifacts": {
            "source_pdf": {
                "path": "source.pdf",
                "sha256": source_hash,
                "size_bytes": source.stat().st_size,
            },
            "native_capture": {
                "path": "native.json",
                "sha256": _digest(native_path.read_bytes()),
                "size_bytes": native_path.stat().st_size,
                "method": "pdf_inspector",
                "version": "1.17.0",
                "page_count": 1,
            },
            "common_ir": {
                "path": "common_ir.json",
                "sha256": _digest(common_ir_path.read_bytes()),
                "size_bytes": common_ir_path.stat().st_size,
                "schema_version": "common_ir_v1",
                "generator": "common_ir_v1_adapters",
                "generator_version": "1.1.0",
                "page_count": 1,
            },
            "render_manifest": {
                "path": "render_manifest.json",
                "sha256": render.manifest_sha256(),
                "size_bytes": render_path.stat().st_size,
                "schema_version": render.schema_version,
                "source_pdf_sha256": source_hash,
                "page_count": 1,
            },
            "surya_layout_artifact": {
                "path": "surya_layout_artifact.json",
                "sha256": surya.artifact_sha256(),
                "size_bytes": surya_path.stat().st_size,
                "schema_version": surya.schema_version,
                "source_sha256": source_hash,
                "render_manifest_sha256": render.manifest_sha256(),
                "logical_compute_key": surya.logical_compute_key,
                "producer": producer.to_dict(),
                "requested_pages": [1],
            },
        },
        "pipeline": {
            "capture_module": "common_ir_pipeline.workers.pdf_inspector_capture",
            "adapter_module": "common_ir_pipeline.adapters.pdf_native",
            "pdf_inspector_version": "1.17.0",
            "capture_limits": {},
        },
        "coverage": {
            "document_page_count": 1,
            "native_text_item_count": 1,
            "native_text_pages": [1],
            "common_ir_block_count": 0,
            "common_ir_pages": [],
        },
    }
    replay_path = fusion / "replay_manifest.json"
    replay_path.write_text(
        json.dumps(replay, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    record_path = notice / "pipeline" / "ingestion_record.v0.1.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["analysis"]["pdf_fusion"] = dict(PDF_FUSION_DESCRIPTOR)
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def _convert_legacy_source_format(notice: Path, source_format: str) -> None:
    notice_id = notice.name
    old_source = next((notice / "attachments").iterdir())
    new_source = old_source.with_suffix(f".{source_format}")
    prefix = (
        bytes.fromhex("d0cf11e0a1b11ae1")
        if source_format == "hwp"
        else b"PK\x03\x04"
    )
    old_source.unlink()
    new_source.write_bytes(prefix + b"legacy fixture")
    source_hash = _digest(new_source.read_bytes())

    profile_path = notice / "pipeline" / "structured_profile.v0.2.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["source_profile_id"] = f"{source_format}:{notice_id}"
    document = profile["source_documents"][0]
    document["format"] = source_format
    document["common_ir"].update(
        {
            "document_id": f"{source_format}:{notice_id}",
            "source_kind": source_format,
            "source_sha256": source_hash,
        }
    )
    profile["processing_metadata"]["candidate_pack"].update(
        {
            "common_ir_document_id": f"{source_format}:{notice_id}",
            "common_ir_source_sha256": source_hash,
        }
    )
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    selection_path = notice / "pipeline" / "source_selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selection["common_ir_identity"].update(
        {
            "document_id": f"{source_format}:{notice_id}",
            "source_kind": source_format,
            "source_sha256": source_hash,
        }
    )
    selection_path.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")

    old_common_ir = (
        notice / "pipeline" / "common_ir_v1" / f"{notice_id}.pdf.json"
    )
    common_ir = json.loads(old_common_ir.read_text(encoding="utf-8"))
    common_ir["document"].update(
        {
            "document_id": f"{source_format}:{notice_id}",
            "source_kind": source_format,
        }
    )
    common_ir["document"]["provenance"]["source_sha256"] = source_hash
    old_common_ir.unlink()
    new_common_ir = old_common_ir.with_name(f"{notice_id}.{source_format}.json")
    new_common_ir.write_text(json.dumps(common_ir, ensure_ascii=False), encoding="utf-8")

    record_path = notice / "pipeline" / "ingestion_record.v0.1.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["analysis"] = {
        "input_path": new_source.name,
        "common_ir_path": f"pipeline/common_ir_v1/{notice_id}.{source_format}.json",
    }
    record["structured_profile"] = profile
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def test_pdf_fusion_pack_accepts_fully_bound_artifacts(tmp_path: Path) -> None:
    notice = _write_pack(tmp_path / "pack")
    _add_pdf_fusion(notice)

    validate_notice_directory(notice)
    assert (notice / "pipeline" / "pdf_fusion" / "source.pdf").is_file()


def test_pdf_fusion_pack_rejects_nested_noncanonical_source_copy(
    tmp_path: Path,
) -> None:
    notice = _write_pack(tmp_path / "pack")
    _add_pdf_fusion(notice)
    fusion = notice / "pipeline" / "pdf_fusion"
    nested = fusion / "source" / "source.pdf"
    nested.parent.mkdir()
    (fusion / "source.pdf").rename(nested)

    with pytest.raises(PackValidationError, match="fusion source copy"):
        validate_notice_directory(notice)


def test_pdf_fusion_pack_direct_cli_prefers_checked_in_vendor(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    notice = _write_pack(pack)
    _add_pdf_fusion(notice)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "existing_kb_pack.py"),
            str(pack),
            "--expected-count",
            "1",
        ],
        cwd=tmp_path,
        env={"PYTHONNOUSERSITE": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "valid"


@pytest.mark.parametrize("source_format", ("hwp", "hwpx"))
def test_legacy_non_pdf_pack_remains_valid(
    tmp_path: Path,
    source_format: str,
) -> None:
    notice = _write_pack(tmp_path / "pack")
    _convert_legacy_source_format(notice, source_format)

    validate_notice_directory(notice)


def test_pdf_fusion_pack_rejects_tampered_rendered_page(tmp_path: Path) -> None:
    notice = _write_pack(tmp_path / "pack")
    _add_pdf_fusion(notice)
    page = notice / "pipeline" / "pdf_fusion" / "rendered" / "page-0001.png"
    page.write_bytes(_png() + b"tampered")

    with pytest.raises(PackValidationError, match="render manifest/file binding"):
        validate_notice_directory(notice)


def test_pdf_fusion_pack_rejects_replay_producer_mismatch(tmp_path: Path) -> None:
    notice = _write_pack(tmp_path / "pack")
    _add_pdf_fusion(notice)
    replay_path = notice / "pipeline" / "pdf_fusion" / "replay_manifest.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["artifacts"]["surya_layout_artifact"]["producer"]["model_id"] = (
        "untrusted-model"
    )
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    with pytest.raises(PackValidationError, match="Surya producer/page binding"):
        validate_notice_directory(notice)


def test_pdf_fusion_pack_rejects_extra_rendered_page(tmp_path: Path) -> None:
    notice = _write_pack(tmp_path / "pack")
    _add_pdf_fusion(notice)
    extra = notice / "pipeline" / "pdf_fusion" / "rendered" / "page-0002.png"
    extra.write_bytes(_png())

    with pytest.raises(PackValidationError, match="rendered page set mismatch"):
        validate_notice_directory(notice)


def test_pdf_fusion_contract_rejects_noncanonical_descriptor_path(
    tmp_path: Path,
) -> None:
    notice = _write_pack(tmp_path / "pack")
    _add_pdf_fusion(notice)
    record_path = notice / "pipeline" / "ingestion_record.v0.1.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["analysis"]["pdf_fusion"]["native_capture_path"] = (
        "pipeline/pdf_fusion/elsewhere.json"
    )
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(PackValidationError, match="schema violation"):
        validate_notice_directory(notice)
