"""Offline contract tests for the PDF-free RunPod Surya handler core."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import struct
from typing import Any
import zlib

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    PdfCoordinateManifest,
    build_sidecar_binding,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    MAX_PNG_FILE_BYTES,
    PdfRenderManifest,
    RenderedPage,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact,
    SuryaProducerIdentity as PortableSuryaProducerIdentity,
    parse_surya_layout_artifact_bytes,
)
from prereview_runpod_worker.surya_layout_worker.handler import (
    RunPodSuryaLayoutHandler,
    _artifact_empty_pages_size,
)
from prereview_runpod_worker.surya_layout_worker.handler import (
    InferenceContentFailure,
    InferenceInfrastructureFailure,
    RunPodSuryaExecutionPolicy,
    StorageGetContentFailure,
    StorageInfrastructureFailure,
    StoragePutConflictFailure,
    SuryaLayoutPageResult,
    SuryaModelConfidenceMarker,
)
from worker.contracts.accelerator import (
    AcceleratorDispatchPolicy,
    AcceleratorJobState,
    AcceleratorJobStatus,
    AcceleratorResourceCaps,
    AcceleratorStorageScope,
    LayoutComputeIdentity,
    PageCoordinateBinding,
    PageImageInput,
    PageRange,
    RenderManifestInput,
    SignedStorageCapability,
    StorageResourceCaps,
    SuryaLayoutRequest,
    SuryaProducerIdentity,
    build_surya_layout_result_object_key,
)


NOW = datetime(2026, 9, 16, tzinfo=UTC)


def _digest(value: str | bytes) -> str:
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _png(width: int = 2, height: int = 2) -> bytes:
    """Tiny non-interlaced RGB PNG accepted by the portable byte validator."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + (b"\x12\x34\x56" * width) for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _capability(*, method: str, key: str, expected: str | None, max_bytes: int) -> SignedStorageCapability:
    operation = "sign" if method == "GET" else "upload/sign"
    path = f"/storage/v1/object/{operation}/request-temp/{key}"
    return SignedStorageCapability(
        method=method,  # type: ignore[arg-type]
        access_mode="read_only" if method == "GET" else "create_only",
        url=f"https://storage.example.test{path}?signature=secret-not-for-logs",
        storage_host="storage.example.test",
        bucket="request-temp",
        path=path,
        object_key=key,
        expected_sha256=expected,
        expires_at=NOW + timedelta(minutes=5),
        resource_caps=StorageResourceCaps(
            max_bytes=max_bytes,
            allowed_mime_types=("image/png",) if key.endswith(".png") else ("application/json",),
        ),
    )


def _render_and_request() -> tuple[PdfRenderManifest, SuryaLayoutRequest, bytes]:
    png = _png()
    image_hash = _digest(png)
    source_hash = _digest("source-pdf")
    transform = AffineTransform.from_sequence([1, 0, 0, -1, 0, 2])
    coordinate = PdfCoordinateManifest(
        source_sha256=source_hash,
        page=1,
        page_count=1,
        media_box=(0, 0, 2, 2),
        crop_box=(0, 0, 2, 2),
        rotation=0,
        user_unit=1.0,
        canonical_width_pt=2.0,
        canonical_height_pt=2.0,
        render_scale_px_per_point=1.0,
        rendered_width_px=2,
        rendered_height_px=2,
        pdf_origin="bottom_left",
        pdf_x_axis="right",
        pdf_y_axis="up",
        pixel_origin="top_left",
        pixel_x_axis="right",
        pixel_y_axis="down",
        user_to_pixel=transform,
        pixel_to_user=transform.inverse(),
        renderer="pdfium",
        renderer_version="1",
        renderer_config_sha256=_digest("renderer-config"),
        page_image_sha256=image_hash,
    )
    render = PdfRenderManifest(
        source_pdf_relative_path="source/notice.pdf",
        source_pdf_sha256=source_hash,
        source_pdf_size_bytes=1,
        page_count=1,
        renderer="pdfium",
        renderer_version="1",
        renderer_config_sha256=_digest("renderer-config"),
        pages=(RenderedPage(
            page=1,
            image_relative_path="rendered/page-001.png",
            image_sha256=image_hash,
            image_size_bytes=len(png),
            coordinate_manifest=coordinate,
            coordinate_manifest_sha256=coordinate.manifest_sha256(),
        ),),
    )
    producer = SuryaProducerIdentity(
        engine_id="surya",
        engine_version="0.22.1",
        model_id="surya-layout",
        model_revision="1" * 40,
        model_weights_sha256=_digest("weights"),
        pipeline_revision="pipeline-r1",
        config_sha256=_digest("config"),
        worker_image_digest=f"sha256:{_digest('image')}",
    )
    identity = LayoutComputeIdentity.model_validate(
        {
            "source_sha256": source_hash,
            "source_page_count": 1,
            "page_range": PageRange(start_page=1, end_page=1),
            "render_manifest": RenderManifestInput(
                schema_version=render.schema_version,
                render_manifest_sha256=render.manifest_sha256(),
                size_bytes=len(render.canonical_json()),
                mime_type="application/json",
                capability=_capability(method="GET", key="input/render-manifest.json", expected=render.manifest_sha256(), max_bytes=100_000),
            ),
            "page_images": (PageImageInput(
                page_number=1,
                page_image_sha256=image_hash,
                size_bytes=len(png),
                mime_type="image/png",
                pixel_width=2,
                pixel_height=2,
                sidecar_binding=PageCoordinateBinding(**build_sidecar_binding(coordinate)),
                capability=_capability(method="GET", key="input/page-001.png", expected=image_hash, max_bytes=100_000),
            ),),
            "workload_scope": "existing_pdf_shadow",
            "mode": "layout",
            "producer": producer,
        }
    )
    request = SuryaLayoutRequest(
        identity=identity,
        result_upload_capability=_capability(
            method="PUT",
            key=build_surya_layout_result_object_key(identity.logical_compute_key),
            expected=None,
            max_bytes=100_000,
        ),
        resource_caps=AcceleratorResourceCaps(
            max_page_count=1,
            max_total_input_bytes=200_000,
            max_total_rendered_pixels=100,
            max_output_bytes=100_000,
            execution_timeout_seconds=30,
            ttl_seconds=60,
        ),
    )
    return render, request, png


def _request_with_declared_input_sizes(
    request: SuryaLayoutRequest,
    *,
    render_manifest_size: int | None = None,
    page_png_size: int | None = None,
    max_total_input_bytes: int | None = None,
) -> SuryaLayoutRequest:
    """Build a fresh integrity-bound request with controlled declared sizes."""

    identity = request.identity
    render_input = identity.render_manifest
    page = identity.page_images[0]
    declared_render_size = render_manifest_size or render_input.size_bytes
    declared_png_size = page_png_size or page.size_bytes
    rebuilt_page = PageImageInput(
        page_number=page.page_number,
        page_image_sha256=page.page_image_sha256,
        size_bytes=declared_png_size,
        mime_type=page.mime_type,
        pixel_width=page.pixel_width,
        pixel_height=page.pixel_height,
        sidecar_binding=page.sidecar_binding,
        capability=_capability(
            method="GET",
            key=page.capability.object_key,
            expected=page.capability.expected_sha256,
            max_bytes=declared_png_size,
        ),
    )
    rebuilt_identity = LayoutComputeIdentity.model_validate(
        {
            "source_sha256": identity.source_sha256,
            "source_page_count": identity.source_page_count,
            "page_range": identity.page_range,
            "render_manifest": RenderManifestInput(
                schema_version=render_input.schema_version,
                render_manifest_sha256=render_input.render_manifest_sha256,
                size_bytes=declared_render_size,
                mime_type=render_input.mime_type,
                capability=_capability(
                    method="GET",
                    key=render_input.capability.object_key,
                    expected=render_input.capability.expected_sha256,
                    max_bytes=declared_render_size,
                ),
            ),
            "page_images": (rebuilt_page,),
            "workload_scope": identity.workload_scope,
            "mode": identity.mode,
            "producer": identity.producer,
        }
    )
    return SuryaLayoutRequest(
        identity=rebuilt_identity,
        result_upload_capability=_capability(
            method="PUT",
            key=build_surya_layout_result_object_key(
                rebuilt_identity.logical_compute_key
            ),
            expected=None,
            max_bytes=request.result_upload_capability.resource_caps.max_bytes,
        ),
        resource_caps=AcceleratorResourceCaps(
            max_page_count=request.resource_caps.max_page_count,
            max_total_input_bytes=(
                max_total_input_bytes or request.resource_caps.max_total_input_bytes
            ),
            max_total_rendered_pixels=request.resource_caps.max_total_rendered_pixels,
            max_output_bytes=request.resource_caps.max_output_bytes,
            execution_timeout_seconds=request.resource_caps.execution_timeout_seconds,
            ttl_seconds=request.resource_caps.ttl_seconds,
        ),
    )


def _request_with_output_cap(
    request: SuryaLayoutRequest,
    *,
    max_output_bytes: int,
) -> SuryaLayoutRequest:
    return SuryaLayoutRequest(
        identity=request.identity,
        result_upload_capability=_capability(
            method="PUT",
            key=build_surya_layout_result_object_key(request.logical_compute_key),
            expected=None,
            max_bytes=max_output_bytes,
        ),
        resource_caps=AcceleratorResourceCaps(
            max_page_count=request.resource_caps.max_page_count,
            max_total_input_bytes=request.resource_caps.max_total_input_bytes,
            max_total_rendered_pixels=request.resource_caps.max_total_rendered_pixels,
            max_output_bytes=max_output_bytes,
            execution_timeout_seconds=request.resource_caps.execution_timeout_seconds,
            ttl_seconds=request.resource_caps.ttl_seconds,
        ),
    )


class _Storage:
    def __init__(
        self,
        values: dict[str, bytes],
        *,
        fail_put: bool = False,
        get_error: Exception | None = None,
        put_error: Exception | None = None,
    ) -> None:
        self.values = values
        self.fail_put = fail_put
        self.get_error = get_error
        self.put_error = put_error
        self.uploads: dict[str, bytes] = {}
        self.get_calls = 0
        self.put_calls = 0
        self.get_deadlines: list[float | None] = []
        self.put_deadlines: list[float | None] = []

    def get(
        self,
        capability: SignedStorageCapability,
        *,
        max_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> bytes:
        self.get_calls += 1
        self.get_deadlines.append(deadline_monotonic)
        if self.get_error is not None:
            raise self.get_error
        value = self.values[capability.object_key]
        assert len(value) <= max_bytes
        return value

    def put_create_only(
        self,
        capability: SignedStorageCapability,
        *,
        content: bytes,
        content_type: str,
        deadline_monotonic: float | None = None,
    ) -> None:
        self.put_calls += 1
        self.put_deadlines.append(deadline_monotonic)
        assert content_type == "application/json"
        if self.put_error is not None:
            raise self.put_error
        if self.fail_put:
            raise RuntimeError("upstream temporary failure with https://signed.example/never-return")
        assert capability.object_key not in self.uploads
        self.uploads[capability.object_key] = content


class _Inference:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def layout(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _MonotonicClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _AdvancingStorage(_Storage):
    def __init__(self, *args: object, clock: _MonotonicClock, advance_get: float = 0.0, advance_put: float = 0.0, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._clock = clock
        self._advance_get = advance_get
        self._advance_put = advance_put

    def get(
        self,
        capability: SignedStorageCapability,
        *,
        max_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> bytes:
        result = super().get(
            capability,
            max_bytes=max_bytes,
            deadline_monotonic=deadline_monotonic,
        )
        self._clock.advance(self._advance_get)
        return result

    def put_create_only(
        self,
        capability: SignedStorageCapability,
        *,
        content: bytes,
        content_type: str,
        deadline_monotonic: float | None = None,
    ) -> None:
        super().put_create_only(
            capability,
            content=content,
            content_type=content_type,
            deadline_monotonic=deadline_monotonic,
        )
        self._clock.advance(self._advance_put)


class _AdvancingInference(_Inference):
    def __init__(self, result: object, *, clock: _MonotonicClock, advance: float) -> None:
        super().__init__(result)
        self._clock = clock
        self._advance = advance

    def layout(self, **kwargs: object) -> object:
        result = super().layout(**kwargs)
        self._clock.advance(self._advance)
        return result


def _event(request: SuryaLayoutRequest, event_id: str = "runpod-01") -> dict[str, object]:
    return {"id": event_id, "input": request.to_wire_payload()}


def _policy(request: SuryaLayoutRequest, *, terminal_bytes: int = 100_000) -> RunPodSuryaExecutionPolicy:
    return RunPodSuryaExecutionPolicy(
        dispatch_policy=AcceleratorDispatchPolicy(
            allowed_scopes=(
                AcceleratorStorageScope(
                    method="GET",
                    origin="https://storage.example.test",
                    endpoint_path_template="/storage/v1/object/sign/{bucket}/{object_key}",
                    bucket="request-temp",
                    object_key_prefix="input/",
                ),
                AcceleratorStorageScope(
                    method="PUT",
                    origin="https://storage.example.test",
                    endpoint_path_template="/storage/v1/object/upload/sign/{bucket}/{object_key}",
                    bucket="request-temp",
                    object_key_prefix="accelerator/surya-layout/",
                ),
            ),
            max_ttl_seconds=300,
            max_execution_timeout_seconds=60,
            max_page_count=4,
            max_total_input_bytes=200_000,
            max_total_rendered_pixels=1_000,
            max_capability_bytes=100_000,
            allowed_mime_types=("image/png", "application/json"),
        ),
        producer=request.identity.producer,
        max_output_bytes=100_000,
        max_terminal_output_bytes=terminal_bytes,
        max_regions_per_page=20,
    )


def _page_result(
    request: SuryaLayoutRequest,
    regions: object,
    *,
    error: object = False,
    image_bbox: object = (0, 0, 2, 2),
    with_confidence_marker: bool = False,
) -> SuryaLayoutPageResult:
    return SuryaLayoutPageResult(
        error=error,
        image_bbox=image_bbox,
        regions=regions,
        confidence_marker=(
            SuryaModelConfidenceMarker(producer=request.identity.producer)
            if with_confidence_marker
            else None
        ),
    )


def test_handler_uploads_canonical_textless_artifact_and_returns_bound_terminal_status() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    inference = _Inference(_page_result(request, [
        # Input order is Surya's reading order, even though this region is
        # lower on the page than the following Table region.
        {"label": "Text", "polygon": [[0, 1], [2, 1], [2, 2], [0, 2]], "bbox": [0, 1, 2, 2], "confidence": 0.99, "position": 0},
        {"label": "Table", "bbox": [0, 0, 2, 1], "text": "must never persist", "position": 1},
    ], with_confidence_marker=True))

    result = RunPodSuryaLayoutHandler(storage=storage, inference=inference, policy=_policy(request), now=lambda: NOW).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.SUCCEEDED
    assert status.external_job_id == "runpod-01"
    assert status.result_manifest is not None
    raw = storage.uploads[request.result_upload_capability.object_key]
    artifact = parse_surya_layout_artifact_bytes(
        raw,
        render_manifest=render,
        expected_logical_compute_key=request.logical_compute_key,
        expected_producer=PortableSuryaProducerIdentity(
            **status.result_manifest.producer.model_dump()
        ),
        expected_requested_pages=(1,),
    )
    assert [region.label for region in artifact.pages[0].regions] == ["text", "table"]
    assert [region.confidence for region in artifact.pages[0].regions] == [0.99, None]
    assert b"must never persist" not in raw
    assert b"confidence" in raw  # canonical field is present; it is never estimated.
    assert all("pdf" not in call for call in inference.calls)
    assert len(storage.get_deadlines) == 2
    assert all(isinstance(deadline, float) for deadline in storage.get_deadlines)
    assert inference.calls[0]["deadline_monotonic"] == storage.get_deadlines[0]
    assert storage.put_deadlines == [storage.get_deadlines[0]]


def test_handler_rejects_incremental_page_bytes_before_full_artifact_serialization(
    monkeypatch: Any,
) -> None:
    render, original_request, png = _render_and_request()
    base_size = _artifact_empty_pages_size(
        request=original_request,
        requested_pages=original_request.identity.page_range.page_numbers,
        producer=original_request.identity.producer,
    )
    request = _request_with_output_cap(
        original_request,
        max_output_bytes=base_size + 32,
    )
    storage = _Storage(
        {
            request.identity.render_manifest.capability.object_key: render.canonical_json(),
            request.identity.page_images[0].capability.object_key: png,
        }
    )
    base_policy = _policy(request)
    policy = RunPodSuryaExecutionPolicy(
        dispatch_policy=base_policy.dispatch_policy,
        producer=base_policy.producer,
        max_output_bytes=base_size + 32,
        max_terminal_output_bytes=base_policy.max_terminal_output_bytes,
        max_regions_per_page=base_policy.max_regions_per_page,
    )
    # If the incremental bound regresses, this sentinel proves the handler
    # reached the prohibited full-artifact serialization step.
    monkeypatch.setattr(
        SuryaLayoutArtifact,
        "canonical_json",
        lambda self: (_ for _ in ()).throw(AssertionError("full serialization reached")),
    )

    result = _handler(
        request,
        storage,
        _Inference(
            _page_result(
                request,
                [{"label": "Text", "bbox": [0, 0, 2, 2], "position": 0}],
            )
        ),
        policy=policy,
    ).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.CONTENT_FAILED
    assert status.reason_code == "result_artifact_exceeds_cap"
    assert storage.put_calls == 0


def test_handler_rejects_oversized_render_manifest_before_storage_download() -> None:
    render, request, png = _render_and_request()
    oversized_request = _request_with_declared_input_sizes(
        request,
        render_manifest_size=100_000,
    )
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    # The deployment policy permits this declared input size, but render
    # manifests have their own much smaller page-count-proportional cap.
    result = _handler(
        oversized_request,
        storage,
        _Inference(_page_result(oversized_request, [])),
    ).handle(_event(oversized_request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.CONTENT_FAILED
    assert status.reason_code == "render_manifest_exceeds_download_cap"
    assert storage.get_calls == storage.put_calls == 0


def test_handler_rejects_oversized_png_before_storage_download() -> None:
    render, request, png = _render_and_request()
    oversized = MAX_PNG_FILE_BYTES + 1
    oversized_request = _request_with_declared_input_sizes(
        request,
        page_png_size=oversized,
        max_total_input_bytes=oversized + 200_000,
    )
    storage = _Storage({
        oversized_request.identity.render_manifest.capability.object_key: render.canonical_json(),
        oversized_request.identity.page_images[0].capability.object_key: png,
    })

    base_policy = _policy(request)
    dispatch_policy = AcceleratorDispatchPolicy(
        **{
            **base_policy.dispatch_policy.model_dump(),
            "max_total_input_bytes": oversized + 200_000,
            "max_capability_bytes": oversized + 1,
        }
    )
    policy = RunPodSuryaExecutionPolicy(
        dispatch_policy=dispatch_policy,
        producer=base_policy.producer,
        max_output_bytes=base_policy.max_output_bytes,
        max_terminal_output_bytes=base_policy.max_terminal_output_bytes,
        max_regions_per_page=base_policy.max_regions_per_page,
    )

    result = _handler(
        oversized_request,
        storage,
        _Inference(_page_result(oversized_request, [])),
        policy=policy,
    ).handle(_event(oversized_request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.CONTENT_FAILED
    assert status.reason_code == "page_png_exceeds_download_cap"
    # Render manifest is loaded first; the untrusted PNG URL is never used.
    assert storage.get_calls == 1
    assert storage.put_calls == 0


def test_handler_unknown_raw_label_is_content_failure_without_upload() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({request.identity.render_manifest.capability.object_key: render.canonical_json(), request.identity.page_images[0].capability.object_key: png})
    result = RunPodSuryaLayoutHandler(storage=storage, inference=_Inference(_page_result(request, [{"label": "Unreviewed", "bbox": [0, 0, 2, 2], "position": 0}])), policy=_policy(request), now=lambda: NOW).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.CONTENT_FAILED
    assert status.reason_code == "layout_label_unsupported"
    assert storage.uploads == {}


def test_handler_duplicate_layout_geometry_is_content_failure_without_upload() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({request.identity.render_manifest.capability.object_key: render.canonical_json(), request.identity.page_images[0].capability.object_key: png})
    result = RunPodSuryaLayoutHandler(
        storage=storage,
        inference=_Inference(_page_result(request, [
            {"label": "Text", "bbox": [0, 0, 2, 2], "position": 0},
            {"label": "Table", "bbox": [0, 0, 2, 2], "position": 1},
        ])),
        policy=_policy(request),
        now=lambda: NOW,
    ).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.CONTENT_FAILED
    assert status.reason_code == "layout_geometry_invalid"
    assert storage.uploads == {}


def test_handler_storage_upload_failure_is_redacted_retryable_infrastructure_failure() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({request.identity.render_manifest.capability.object_key: render.canonical_json(), request.identity.page_images[0].capability.object_key: png}, fail_put=True)
    result = RunPodSuryaLayoutHandler(storage=storage, inference=_Inference(_page_result(request, [])), policy=_policy(request), now=lambda: NOW).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.INFRA_RETRYABLE
    assert status.reason_code == "result_upload_failed"
    assert "signed.example" not in str(result)


def test_handler_rejects_noncanonical_event_id_before_treating_it_as_provider_job_id() -> None:
    _, request, _ = _render_and_request()
    result = RunPodSuryaLayoutHandler(storage=_Storage({}), inference=_Inference(_page_result(request, [])), policy=_policy(request), now=lambda: NOW).handle(_event(request, event_id="bad id"))

    assert result == {"state": "content_failed", "reason_code": "event_invalid"}


def _recomputed_event_payload(request: SuryaLayoutRequest) -> dict[str, object]:
    """Mutable JSON-shaped request wire with derived envelope digest reset."""

    payload = json.loads(json.dumps(request.to_wire_payload()))
    payload["request_digest"] = ""
    return payload


def _handler(
    request: SuryaLayoutRequest,
    storage: _Storage,
    inference: _Inference,
    *,
    policy: RunPodSuryaExecutionPolicy | None = None,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> RunPodSuryaLayoutHandler:
    return RunPodSuryaLayoutHandler(
        storage=storage,
        inference=inference,
        policy=policy or _policy(request),
        now=now or (lambda: NOW),
        monotonic=monotonic,
    )


def test_execution_policy_rejects_unapproved_scope_without_port_calls() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    inference = _Inference(_page_result(request, []))
    payload = _recomputed_event_payload(request)
    capability = payload["identity"]["page_images"][0]["capability"]  # type: ignore[index]
    capability["object_key"] = "other/page-001.png"  # type: ignore[index]
    capability["path"] = "/storage/v1/object/sign/request-temp/other/page-001.png"  # type: ignore[index]
    capability["url"] = "https://storage.example.test/storage/v1/object/sign/request-temp/other/page-001.png?signature=unapproved"  # type: ignore[index]

    result = _handler(request, storage, inference).handle({"id": "runpod-01", "input": payload})

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.CONTENT_FAILED
    assert status.reason_code == "execution_policy_rejected"
    assert storage.get_calls == storage.put_calls == 0
    assert inference.calls == []
    assert "unapproved" not in str(result)


def test_execution_policy_admits_queue_delayed_capability_with_execution_window_only() -> None:
    render, request, png = _render_and_request()
    started_after_queue = NOW + timedelta(seconds=30)
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })

    # The original 60-second dispatch TTL has already spent 30 seconds in a
    # queue.  The worker must not restart that full TTL at invocation time;
    # exactly the 30-second execution window remains and is admissible.
    payload = _recomputed_event_payload(request)
    expiry = (NOW + timedelta(seconds=60)).isoformat()
    payload["identity"]["render_manifest"]["capability"]["expires_at"] = expiry  # type: ignore[index]
    payload["identity"]["page_images"][0]["capability"]["expires_at"] = expiry  # type: ignore[index]
    payload["result_upload_capability"]["expires_at"] = expiry
    result = _handler(
        request,
        storage,
        _Inference(_page_result(request, [])),
        now=lambda: started_after_queue,
    ).handle({"id": "runpod-01", "input": payload})

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.SUCCEEDED
    assert storage.get_calls == 2
    assert storage.put_calls == 1


def test_execution_policy_rejects_insufficient_remaining_or_excessive_lifetime_without_port_calls() -> None:
    render, request, png = _render_and_request()
    for lifetime_seconds in (29, 301):
        storage = _Storage({
            request.identity.render_manifest.capability.object_key: render.canonical_json(),
            request.identity.page_images[0].capability.object_key: png,
        })
        inference = _Inference(_page_result(request, []))
        payload = _recomputed_event_payload(request)
        expiry = (NOW + timedelta(seconds=lifetime_seconds)).isoformat()
        payload["identity"]["render_manifest"]["capability"]["expires_at"] = expiry  # type: ignore[index]
        payload["identity"]["page_images"][0]["capability"]["expires_at"] = expiry  # type: ignore[index]
        payload["result_upload_capability"]["expires_at"] = expiry  # type: ignore[index]

        result = _handler(request, storage, inference).handle({"id": "runpod-01", "input": payload})

        status = AcceleratorJobStatus.model_validate(result)
        assert status.state is AcceleratorJobState.CONTENT_FAILED
        assert status.reason_code == "execution_policy_rejected"
        assert storage.get_calls == storage.put_calls == 0
        assert inference.calls == []


def test_execution_timeout_stops_before_storage_io_at_deadline() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    clock = _MonotonicClock()
    # Deadline setup sees 0.0; the first pre-GET check sees the exact deadline.
    clock_values = iter((0.0, 30.0))
    result = _handler(
        request,
        storage,
        _Inference(_page_result(request, [])),
        monotonic=lambda: next(clock_values),
    ).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.INFRA_RETRYABLE
    assert status.reason_code == "execution_timeout"
    assert storage.get_calls == storage.put_calls == 0


def test_execution_timeout_after_storage_get_is_retryable_without_inference_or_upload() -> None:
    render, request, png = _render_and_request()
    clock = _MonotonicClock()
    storage = _AdvancingStorage(
        {
            request.identity.render_manifest.capability.object_key: render.canonical_json(),
            request.identity.page_images[0].capability.object_key: png,
        },
        clock=clock,
        advance_get=30.0,
    )

    result = _handler(
        request,
        storage,
        _Inference(_page_result(request, [])),
        monotonic=clock,
    ).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.INFRA_RETRYABLE
    assert status.reason_code == "execution_timeout"
    assert storage.get_calls == 1
    assert storage.put_calls == 0


def test_execution_timeout_after_inference_is_retryable_without_upload() -> None:
    render, request, png = _render_and_request()
    clock = _MonotonicClock()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    inference = _AdvancingInference(
        _page_result(request, []),
        clock=clock,
        advance=30.0,
    )

    result = _handler(
        request,
        storage,
        inference,
        monotonic=clock,
    ).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.INFRA_RETRYABLE
    assert status.reason_code == "execution_timeout"
    assert len(inference.calls) == 1
    assert storage.put_calls == 0


def test_execution_timeout_after_create_only_put_is_retryable_for_reconciliation() -> None:
    render, request, png = _render_and_request()
    clock = _MonotonicClock()
    storage = _AdvancingStorage(
        {
            request.identity.render_manifest.capability.object_key: render.canonical_json(),
            request.identity.page_images[0].capability.object_key: png,
        },
        clock=clock,
        advance_put=30.0,
    )

    result = _handler(
        request,
        storage,
        _Inference(_page_result(request, [])),
        monotonic=clock,
    ).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(result)
    assert status.state is AcceleratorJobState.INFRA_RETRYABLE
    assert status.reason_code == "execution_timeout"
    assert storage.put_calls == 1
    assert request.result_upload_capability.object_key in storage.uploads


def test_raw_preflight_rejects_huge_page_range_and_cap_without_port_calls() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    inference = _Inference(_page_result(request, []))
    payload = _recomputed_event_payload(request)
    payload["identity"]["page_range"]["end_page"] = 10**9  # type: ignore[index]
    payload["resource_caps"]["max_total_input_bytes"] = 10**18  # type: ignore[index]

    result = _handler(request, storage, inference).handle({"id": "runpod-01", "input": payload})

    assert result == {
        "state": "content_failed",
        "reason_code": "input_invalid",
    }
    assert storage.get_calls == storage.put_calls == 0
    assert inference.calls == []


def test_raw_preflight_rejects_oversized_page_list_without_port_calls() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    inference = _Inference(_page_result(request, []))
    payload = _recomputed_event_payload(request)
    payload["identity"]["page_range"]["end_page"] = 4  # type: ignore[index]
    payload["identity"]["page_images"] *= 5  # type: ignore[index]

    result = _handler(request, storage, inference).handle({"id": "runpod-01", "input": payload})

    assert result == {
        "state": "content_failed",
        "reason_code": "input_invalid",
    }
    assert storage.get_calls == storage.put_calls == 0
    assert inference.calls == []


def test_execution_policy_rejects_oversized_capability_and_producer_mismatch_without_port_calls() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    inference = _Inference(_page_result(request, []))
    payload = _recomputed_event_payload(request)
    payload["identity"]["page_images"][0]["capability"]["resource_caps"]["max_bytes"] = 100_001  # type: ignore[index]

    oversized = _handler(request, storage, inference).handle({"id": "runpod-01", "input": payload})
    assert oversized == {
        "state": "content_failed",
        "reason_code": "input_invalid",
    }
    assert storage.get_calls == storage.put_calls == 0
    assert inference.calls == []

    different_producer = SuryaProducerIdentity(
        **(request.identity.producer.model_dump() | {"engine_version": "0.22.2"})
    )
    mismatch_storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    mismatch_inference = _Inference(_page_result(request, []))
    mismatch_policy = RunPodSuryaExecutionPolicy(
        dispatch_policy=_policy(request).dispatch_policy,
        producer=different_producer,
        max_output_bytes=100_000,
        max_terminal_output_bytes=100_000,
        max_regions_per_page=20,
    )
    mismatch = _handler(
        request,
        mismatch_storage,
        mismatch_inference,
        policy=mismatch_policy,
    ).handle(_event(request))

    status = AcceleratorJobStatus.model_validate(mismatch)
    assert status.state is AcceleratorJobState.CONTENT_FAILED
    assert status.reason_code == "producer_identity_mismatch"
    assert mismatch_storage.get_calls == mismatch_storage.put_calls == 0
    assert mismatch_inference.calls == []


def test_official_surya_page_error_is_retryable_but_canvas_and_position_are_content_failures() -> None:
    render, request, png = _render_and_request()

    for page_result, state, reason in (
        (_page_result(request, [], error=True), AcceleratorJobState.INFRA_RETRYABLE, "layout_page_error"),
        (_page_result(request, [], image_bbox=(0, 0, 1, 2)), AcceleratorJobState.CONTENT_FAILED, "layout_canvas_mismatch"),
        (_page_result(request, [{"label": "Text", "bbox": [0, 0, 2, 2]}]), AcceleratorJobState.CONTENT_FAILED, "layout_position_invalid"),
    ):
        storage = _Storage({
            request.identity.render_manifest.capability.object_key: render.canonical_json(),
            request.identity.page_images[0].capability.object_key: png,
        })
        result = _handler(request, storage, _Inference(page_result)).handle(_event(request))
        status = AcceleratorJobStatus.model_validate(result)
        assert status.state is state
        assert status.reason_code == reason
        assert storage.uploads == {}


def test_typed_port_failures_choose_content_or_infrastructure_without_detail_leakage() -> None:
    render, request, png = _render_and_request()
    values = {
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    }

    get_content = _handler(
        request,
        _Storage(values, get_error=StorageGetContentFailure("https://secret.example/get")),
        _Inference(_page_result(request, [])),
    ).handle(_event(request))
    get_infra = _handler(
        request,
        _Storage(values, get_error=StorageInfrastructureFailure("upstream 503 https://secret.example/get")),
        _Inference(_page_result(request, [])),
    ).handle(_event(request))
    put_conflict = _handler(
        request,
        _Storage(values, put_error=StoragePutConflictFailure("already exists https://secret.example/put")),
        _Inference(_page_result(request, [])),
    ).handle(_event(request))

    content_status = AcceleratorJobStatus.model_validate(get_content)
    infra_status = AcceleratorJobStatus.model_validate(get_infra)
    conflict_status = AcceleratorJobStatus.model_validate(put_conflict)
    assert content_status.state is AcceleratorJobState.CONTENT_FAILED
    assert content_status.reason_code == "input_download_rejected"
    assert infra_status.state is AcceleratorJobState.INFRA_RETRYABLE
    assert infra_status.reason_code == "input_download_failed"
    assert conflict_status.state is AcceleratorJobState.INFRA_RETRYABLE
    assert conflict_status.reason_code == "result_upload_conflict"
    assert "secret.example" not in str((get_content, get_infra, put_conflict))


def test_typed_inference_failures_choose_content_or_infrastructure_without_detail_leakage() -> None:
    render, request, png = _render_and_request()
    values = {
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    }
    content = _handler(
        request,
        _Storage(values),
        _Inference(InferenceContentFailure("fallback rejected https://secret.example/model")),
    ).handle(_event(request))
    infrastructure = _handler(
        request,
        _Storage(values),
        _Inference(InferenceInfrastructureFailure("gpu 503 https://secret.example/model")),
    ).handle(_event(request))

    content_status = AcceleratorJobStatus.model_validate(content)
    infrastructure_status = AcceleratorJobStatus.model_validate(infrastructure)
    assert content_status.state is AcceleratorJobState.CONTENT_FAILED
    assert content_status.reason_code == "layout_inference_rejected"
    assert infrastructure_status.state is AcceleratorJobState.INFRA_RETRYABLE
    assert infrastructure_status.reason_code == "layout_inference_failed"
    assert "secret.example" not in str((content, infrastructure))


def test_terminal_output_cap_returns_compact_safe_envelope_without_upload() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })

    result = _handler(
        request,
        storage,
        _Inference(_page_result(request, [])),
        policy=_policy(request, terminal_bytes=192),
    ).handle(_event(request))

    assert result == {
        "state": "content_failed",
        "reason_code": "terminal_output_exceeds_cap",
    }
    assert storage.uploads == {}


def test_compact_terminal_fallback_omits_max_length_external_id_and_fits_policy() -> None:
    render, request, png = _render_and_request()
    storage = _Storage({
        request.identity.render_manifest.capability.object_key: render.canonical_json(),
        request.identity.page_images[0].capability.object_key: png,
    })
    policy = _policy(request, terminal_bytes=192)
    external_job_id = "r" + ("x" * 127)

    result = _handler(
        request,
        storage,
        _Inference(_page_result(request, [])),
        policy=policy,
    ).handle(_event(request, event_id=external_job_id))

    assert result == {
        "state": "content_failed",
        "reason_code": "terminal_output_exceeds_cap",
    }
    assert "external_job_id" not in result
    assert len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")) <= policy.max_terminal_output_bytes
    assert storage.uploads == {}


def test_early_invalid_input_with_max_length_external_id_is_bounded_and_redacted() -> None:
    _, request, _ = _render_and_request()
    policy = _policy(request, terminal_bytes=192)
    external_job_id = "r" + ("x" * 127)

    result = _handler(
        request,
        _Storage({}),
        _Inference(_page_result(request, [])),
        policy=policy,
    ).handle({"id": external_job_id, "input": {}})

    assert result == {"state": "content_failed", "reason_code": "input_invalid"}
    assert "external_job_id" not in result
    assert len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")) <= policy.max_terminal_output_bytes
