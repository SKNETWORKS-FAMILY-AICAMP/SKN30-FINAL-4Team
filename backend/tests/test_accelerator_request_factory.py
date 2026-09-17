"""Unit tests for trusted local-render to Surya-request preparation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import zlib

import pytest

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    PdfCoordinateManifest,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    PdfRenderManifestError,
    RenderedPageInput,
    assemble_render_manifest,
)
from worker import accelerator_request_factory as factory
from worker.accelerator_request_factory import prepare_surya_layout_request
from worker.contracts.accelerator import (
    AcceleratorDispatchPolicy,
    AcceleratorResourceCaps,
    AcceleratorStorageScope,
    SignedStorageCapability,
    StorageResourceCaps,
    SuryaLayoutReconciliationHandle,
    SuryaProducerIdentity,
    build_surya_layout_result_object_key,
)


NOW = datetime(2026, 9, 16, tzinfo=UTC)
BUCKET = "request-temp"


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

    raw = b"\x00" + (b"\x10\x20\x30" * 32)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _render_manifest(root: Path, *, page_count: int = 1) -> PdfRenderManifest:
    source = root / "source" / "input.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-1.7\ntrusted fixture\n")
    png = _png()
    source_hash = _digest(source.read_bytes())
    image_hash = _digest(png)
    pages: list[RenderedPageInput] = []
    for page_number in range(1, page_count + 1):
        image = root / "rendered" / f"page-{page_number:03d}.png"
        image.parent.mkdir(exist_ok=True)
        image.write_bytes(png)
        coordinate = PdfCoordinateManifest(
            source_sha256=source_hash,
            page=page_number,
            page_count=page_count,
            media_box=(0, 0, 32, 1),
            crop_box=(0, 0, 32, 1),
            rotation=0,
            user_unit=1.0,
            canonical_width_pt=32.0,
            canonical_height_pt=1.0,
            render_scale_px_per_point=1.0,
            rendered_width_px=32,
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
        pages.append(RenderedPageInput(image_path=image, coordinate_manifest=coordinate))
    return assemble_render_manifest(
        artifact_root=root,
        source_pdf_path=source,
        pages=tuple(pages),
    )


def _producer() -> SuryaProducerIdentity:
    return SuryaProducerIdentity(
        engine_id="surya",
        engine_version="0.22.1",
        model_id="surya-layout",
        model_revision="1" * 40,
        model_weights_sha256=_digest("weights"),
        pipeline_revision="pipeline-r1",
        config_sha256=_digest("config"),
        worker_image_digest=f"sha256:{_digest('image')}",
    )


def _caps(**overrides: int) -> AcceleratorResourceCaps:
    values = {
        "max_page_count": 1,
        "max_total_input_bytes": 100_000,
        "max_total_rendered_pixels": 1_000_000,
        "max_output_bytes": 100_000,
        "execution_timeout_seconds": 60,
        "ttl_seconds": 300,
    }
    values.update(overrides)
    return AcceleratorResourceCaps(**values)


def _policy(**overrides: object) -> AcceleratorDispatchPolicy:
    values: dict[str, object] = {
        "allowed_scopes": tuple(
            AcceleratorStorageScope(
                method=method,  # type: ignore[arg-type]
                origin="https://storage.example.test",
                endpoint_path_template=(
                    "/storage/v1/object/sign/{bucket}/{object_key}"
                    if method == "GET"
                    else "/storage/v1/object/upload/sign/{bucket}/{object_key}"
                ),
                bucket=BUCKET,
                object_key_prefix=(
                    "accelerator/input/"
                    if method == "GET"
                    else "accelerator/surya-layout/"
                ),
            )
            for method in ("GET", "PUT")
        ),
        "max_ttl_seconds": 600,
        "max_execution_timeout_seconds": 300,
        "max_page_count": 100,
        "max_total_input_bytes": 1_000_000,
        "max_total_rendered_pixels": 100_000_000,
        "max_capability_bytes": 1_000_000,
        "allowed_mime_types": ("application/json", "image/png"),
    }
    values.update(overrides)
    return AcceleratorDispatchPolicy(**values)


class _Uploader:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.uploads: list[tuple[str, str, bytes, str]] = []

    def put_if_absent(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> bool:
        self.events.append(f"upload:{object_key}")
        self.uploads.append((bucket, object_key, content, content_type))
        return True


class _Issuer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.reads: list[tuple[str, str, str, int, str, int]] = []
        self.result: tuple[str, str, int, str] | None = None

    def issue_read(
        self,
        *,
        bucket: str,
        object_key: str,
        expected_sha256: str,
        size_bytes: int,
        mime_type: str,
        ttl_seconds: int,
    ) -> SignedStorageCapability:
        self.events.append(f"read:{object_key}")
        self.reads.append(
            (bucket, object_key, expected_sha256, size_bytes, mime_type, ttl_seconds)
        )
        return _capability(
            method="GET",
            bucket=bucket,
            object_key=object_key,
            expected_sha256=expected_sha256,
            max_bytes=size_bytes,
            mime_type=mime_type,
        )

    def issue_surya_layout_result_upload(
        self,
        *,
        bucket: str,
        logical_compute_key: str,
        max_bytes: int,
        mime_type: str = "application/json",
    ) -> SignedStorageCapability:
        self.events.append("result")
        self.result = (bucket, logical_compute_key, max_bytes, mime_type)
        return _capability(
            method="PUT",
            bucket=bucket,
            object_key=build_surya_layout_result_object_key(logical_compute_key),
            expected_sha256=None,
            max_bytes=max_bytes,
            mime_type=mime_type,
        )


def _capability(
    *,
    method: str,
    bucket: str,
    object_key: str,
    expected_sha256: str | None,
    max_bytes: int,
    mime_type: str,
) -> SignedStorageCapability:
    operation = "sign" if method == "GET" else "upload/sign"
    return SignedStorageCapability(
        method=method,  # type: ignore[arg-type]
        access_mode="read_only" if method == "GET" else "create_only",
        url=(
            f"https://storage.example.test/storage/v1/object/{operation}/"
            f"{bucket}/{object_key}?signature=test"
        ),
        storage_host="storage.example.test",
        bucket=bucket,
        path=f"/storage/v1/object/{operation}/{bucket}/{object_key}",
        object_key=object_key,
        expected_sha256=expected_sha256,
        expires_at=NOW + timedelta(seconds=600),
        resource_caps=StorageResourceCaps(
            max_bytes=max_bytes,
            allowed_mime_types=(mime_type,),
        ),
    )


def test_prepares_bound_request_after_immutable_uploads(tmp_path: Path) -> None:
    manifest = _render_manifest(tmp_path)
    events: list[str] = []
    uploader = _Uploader(events)
    issuer = _Issuer(events)

    prepared = prepare_surya_layout_request(
        render_manifest=manifest,
        artifact_root=tmp_path,
        input_bucket=BUCKET,
        uploader=uploader,
        issuer=issuer,
        producer=_producer(),
        resource_caps=_caps(),
        read_capability_ttl_seconds=300,
        dispatch_policy=_policy(),
    )

    manifest_hash = manifest.manifest_sha256()
    namespace = f"accelerator/input/{manifest_hash}"
    page = manifest.pages[0]
    expected_page_key = f"{namespace}/page-0001-{page.image_sha256}.png"
    assert [upload.object_key for upload in prepared.input_uploads] == [
        f"{namespace}/render-manifest.json",
        expected_page_key,
    ]
    assert [(bucket, key, mime) for bucket, key, _, mime in uploader.uploads] == [
        (BUCKET, f"{namespace}/render-manifest.json", "application/json"),
        (BUCKET, expected_page_key, "image/png"),
    ]
    assert uploader.uploads[0][2] == manifest.canonical_json()
    assert uploader.uploads[1][2] == (tmp_path / page.image_relative_path).read_bytes()
    assert [call[1] for call in issuer.reads] == [
        f"{namespace}/render-manifest.json",
        expected_page_key,
    ]
    assert all(call[-1] == 300 for call in issuer.reads)
    assert issuer.result == (
        BUCKET,
        prepared.request.logical_compute_key,
        _caps().max_output_bytes,
        "application/json",
    )
    assert prepared.request.result_upload_capability.object_key == (
        build_surya_layout_result_object_key(prepared.request.logical_compute_key)
    )
    assert events[-1] == "result"
    assert prepared.trusted_render_manifest == manifest
    assert prepared.reconciliation_handle.logical_compute_key == (
        prepared.request.logical_compute_key
    )
    assert prepared.reconciliation_handle.result_object_key == (
        prepared.request.result_upload_capability.object_key
    )
    persisted = json.dumps(prepared.reconciliation_handle.to_persistence_payload())
    assert SuryaLayoutReconciliationHandle.model_validate_json(persisted) == (
        prepared.reconciliation_handle
    )
    assert "https://" not in persisted
    assert "signature" not in persisted.lower()
    assert "capability" not in persisted.lower()


def test_rejects_local_page_manifest_mismatch_before_upload(tmp_path: Path) -> None:
    manifest = _render_manifest(tmp_path)
    (tmp_path / manifest.pages[0].image_relative_path).write_bytes(_png() + b"changed")
    events: list[str] = []

    with pytest.raises(PdfRenderManifestError):
        prepare_surya_layout_request(
            render_manifest=manifest,
            artifact_root=tmp_path,
            input_bucket=BUCKET,
            uploader=_Uploader(events),
            issuer=_Issuer(events),
            producer=_producer(),
            resource_caps=_caps(),
            read_capability_ttl_seconds=300,
            dispatch_policy=_policy(),
        )
    assert events == []


def test_rejects_symlinked_local_page_before_upload(tmp_path: Path) -> None:
    manifest = _render_manifest(tmp_path)
    image = tmp_path / manifest.pages[0].image_relative_path
    target = tmp_path / "real-page.png"
    image.rename(target)
    os.symlink("../real-page.png", image)
    events: list[str] = []

    with pytest.raises(PdfRenderManifestError):
        prepare_surya_layout_request(
            render_manifest=manifest,
            artifact_root=tmp_path,
            input_bucket=BUCKET,
            uploader=_Uploader(events),
            issuer=_Issuer(events),
            producer=_producer(),
            resource_caps=_caps(),
            read_capability_ttl_seconds=300,
            dispatch_policy=_policy(),
        )
    assert events == []


def test_rechecks_page_bytes_after_local_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _render_manifest(tmp_path)
    (tmp_path / manifest.pages[0].image_relative_path).write_bytes(_png() + b"changed")
    monkeypatch.setattr(
        factory,
        "validate_render_manifest_files",
        lambda *_args, **_kwargs: manifest,
    )
    events: list[str] = []

    with pytest.raises(PdfRenderManifestError, match="page image bytes"):
        prepare_surya_layout_request(
            render_manifest=manifest,
            artifact_root=tmp_path,
            input_bucket=BUCKET,
            uploader=_Uploader(events),
            issuer=_Issuer(events),
            producer=_producer(),
            resource_caps=_caps(),
            read_capability_ttl_seconds=300,
            dispatch_policy=_policy(),
        )
    assert events == []


@pytest.mark.parametrize(
    ("page_count", "caps", "message"),
    (
        (2, {"max_page_count": 1}, "page count"),
        (1, {"max_total_input_bytes": 1}, "byte cap"),
        (1, {"max_total_rendered_pixels": 1}, "pixels"),
    ),
)
def test_resource_caps_are_checked_before_storage_side_effects(
    tmp_path: Path,
    page_count: int,
    caps: dict[str, int],
    message: str,
) -> None:
    events: list[str] = []

    with pytest.raises(ValueError, match=message):
        prepare_surya_layout_request(
            render_manifest=_render_manifest(tmp_path, page_count=page_count),
            artifact_root=tmp_path,
            input_bucket=BUCKET,
            uploader=_Uploader(events),
            issuer=_Issuer(events),
            producer=_producer(),
            resource_caps=_caps(**caps),
            read_capability_ttl_seconds=300,
            dispatch_policy=_policy(),
        )
    assert events == []


@pytest.mark.parametrize(
    ("caps", "policy_overrides", "read_ttl", "message"),
    (
        ({"ttl_seconds": 601}, {}, 601, "request TTL"),
        ({}, {}, 601, "read capability TTL"),
        ({"execution_timeout_seconds": 301, "ttl_seconds": 301}, {}, 301, "execution timeout"),
        ({"max_page_count": 101}, {}, 300, "page cap"),
        ({"max_total_input_bytes": 1_000_001}, {}, 300, "input byte cap"),
        ({"max_total_rendered_pixels": 100_000_001}, {}, 300, "pixel cap"),
        ({"max_output_bytes": 1_000_001}, {}, 300, "output cap"),
        ({"max_page_count": 2}, {"max_page_count": 1}, 300, "page cap"),
        ({}, {"max_total_input_bytes": 1}, 300, "input byte cap"),
        ({}, {"max_total_rendered_pixels": 1}, 300, "pixel cap"),
        ({"max_output_bytes": 1}, {"max_capability_bytes": 100}, 300, "input capability bytes"),
        ({}, {"allowed_mime_types": ("application/json",)}, 300, "MIME"),
    ),
)
def test_deployment_policy_violations_prevent_every_storage_side_effect(
    tmp_path: Path,
    caps: dict[str, int],
    policy_overrides: dict[str, object],
    read_ttl: int,
    message: str,
) -> None:
    events: list[str] = []

    with pytest.raises(ValueError, match=message):
        prepare_surya_layout_request(
            render_manifest=_render_manifest(tmp_path, page_count=2 if message == "page cap" else 1),
            artifact_root=tmp_path,
            input_bucket=BUCKET,
            uploader=_Uploader(events),
            issuer=_Issuer(events),
            producer=_producer(),
            resource_caps=_caps(**caps),
            read_capability_ttl_seconds=read_ttl,
            dispatch_policy=_policy(**policy_overrides),
        )

    assert events == []


@pytest.mark.parametrize("method", ("GET", "PUT"))
def test_missing_deployment_storage_scope_prevents_storage_side_effects(
    tmp_path: Path,
    method: str,
) -> None:
    events: list[str] = []
    scopes = tuple(scope for scope in _policy().allowed_scopes if scope.method != method)

    with pytest.raises(ValueError, match="does not permit"):
        prepare_surya_layout_request(
            render_manifest=_render_manifest(tmp_path),
            artifact_root=tmp_path,
            input_bucket=BUCKET,
            uploader=_Uploader(events),
            issuer=_Issuer(events),
            producer=_producer(),
            resource_caps=_caps(),
            read_capability_ttl_seconds=300,
            dispatch_policy=_policy(allowed_scopes=scopes),
        )

    assert events == []


@pytest.mark.parametrize(
    ("method", "wrong_template"),
    (
        ("GET", "/storage/v1/object/public/{bucket}/{object_key}"),
        ("PUT", "/storage/v1/object/sign/{bucket}/{object_key}"),
    ),
)
def test_non_exact_storage_endpoint_template_prevents_storage_side_effects(
    tmp_path: Path,
    method: str,
    wrong_template: str,
) -> None:
    events: list[str] = []
    scopes = tuple(
        AcceleratorStorageScope(
            method=scope.method,
            origin=scope.origin,
            endpoint_path_template=(
                wrong_template if scope.method == method else scope.endpoint_path_template
            ),
            bucket=scope.bucket,
            object_key_prefix=scope.object_key_prefix,
        )
        for scope in _policy().allowed_scopes
    )

    with pytest.raises(ValueError, match="does not permit"):
        prepare_surya_layout_request(
            render_manifest=_render_manifest(tmp_path),
            artifact_root=tmp_path,
            input_bucket=BUCKET,
            uploader=_Uploader(events),
            issuer=_Issuer(events),
            producer=_producer(),
            resource_caps=_caps(),
            read_capability_ttl_seconds=300,
            dispatch_policy=_policy(allowed_scopes=scopes),
        )

    assert events == []
