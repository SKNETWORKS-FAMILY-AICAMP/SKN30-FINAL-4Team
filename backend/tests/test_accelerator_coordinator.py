"""Offline tests for the Existing-PDF-only accelerator coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    PdfCoordinateManifest,
    build_sidecar_binding,
)
from common_ir_pipeline.pdf_fusion.render_manifest import PdfRenderManifest, RenderedPage
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact,
    SuryaLayoutPage,
    SuryaLayoutRegion,
    SuryaProducerIdentity as PortableProducer,
)
from worker.accelerator_coordinator import (
    ArtifactRead,
    ArtifactReadLimitExceeded,
    ArtifactNotFound,
    CoordinatorDisposition,
    ExistingPdfSuryaCoordinator,
    ExistingPdfSuryaOutcome,
)
from worker.contracts.accelerator import (
    AcceleratorContractError,
    AcceleratorCredentialError,
    AcceleratorJobState,
    AcceleratorJobStatus,
    AcceleratorResourceCaps,
    ArtifactDescriptor,
    LayoutComputeIdentity,
    PageCoordinateBinding,
    PageImageInput,
    PageRange,
    RenderManifestInput,
    SignedStorageCapability,
    StorageResourceCaps,
    SuryaLayoutReconciliationHandle,
    SuryaLayoutResultArtifactManifest,
    SuryaLayoutRequest,
    SuryaProducerIdentity,
    build_surya_layout_result_object_key,
)
from worker.ports.accelerator import AcceleratorJobNotFoundError


NOW = datetime(2026, 9, 16, tzinfo=UTC)


def _digest(value: str | bytes) -> str:
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _capability(*, method: str, key: str, expected: str | None, max_bytes: int) -> SignedStorageCapability:
    path = f"/storage/v1/object/sign/request-temp/{key}"
    return SignedStorageCapability(
        method=method,  # type: ignore[arg-type]
        access_mode="read_only" if method == "GET" else "create_only",
        url=f"https://storage.example.test{path}?signature=test-signature",
        storage_host="storage.example.test",
        bucket="request-temp",
        path=path,
        object_key=key,
        expected_sha256=expected,
        expires_at=NOW + timedelta(minutes=5),
        resource_caps=StorageResourceCaps(
            max_bytes=max_bytes,
            allowed_mime_types=("image/png",) if method == "GET" and key.endswith(".png") else ("application/json",),
        ),
    )


def _trusted_render(*, source: str = "trusted-pdf", page_count: int = 1) -> PdfRenderManifest:
    transform = AffineTransform.from_sequence([2, 0, 0, -2, 0, 400])
    pages = []
    for page_number in range(1, page_count + 1):
        image_hash = _digest(f"trusted-page-{page_number}")
        coordinate = PdfCoordinateManifest(
            source_sha256=_digest(source), page=page_number, page_count=page_count,
            media_box=(0, 0, 100, 200), crop_box=(0, 0, 100, 200), rotation=0,
            user_unit=1.0, canonical_width_pt=100.0, canonical_height_pt=200.0,
            render_scale_px_per_point=2.0, rendered_width_px=200, rendered_height_px=400,
            pdf_origin="bottom_left", pdf_x_axis="right", pdf_y_axis="up",
            pixel_origin="top_left", pixel_x_axis="right", pixel_y_axis="down",
            user_to_pixel=transform, pixel_to_user=transform.inverse(), renderer="pdfium",
            renderer_version="153.0", renderer_config_sha256=_digest("render-config"),
            page_image_sha256=image_hash,
        )
        pages.append(RenderedPage(
            page=page_number, image_relative_path=f"rendered/page-{page_number:03d}.png", image_sha256=image_hash,
            image_size_bytes=100 + page_number, coordinate_manifest=coordinate,
            coordinate_manifest_sha256=coordinate.manifest_sha256(),
        ))
    return PdfRenderManifest(
        source_pdf_relative_path="source/notice.pdf", source_pdf_sha256=_digest(source),
        source_pdf_size_bytes=99, page_count=page_count, renderer="pdfium", renderer_version="153.0",
        renderer_config_sha256=_digest("render-config"), pages=tuple(pages),
    )


def _request(
    render: PdfRenderManifest,
    *,
    subset: bool = False,
    output_cap: int = 1_000_000,
) -> SuryaLayoutRequest:
    selected = render.pages[:1] if subset else render.pages
    producer = SuryaProducerIdentity(
        engine_id="surya", engine_version="0.15.0", model_id="surya-layout",
        model_revision="1" * 40, model_weights_sha256=_digest("weights"),
        pipeline_revision="pipeline-r1", config_sha256=_digest("config"),
        worker_image_digest=f"sha256:{_digest('image')}",
    )
    identity = LayoutComputeIdentity(
        source_sha256=render.source_pdf_sha256,
        source_page_count=render.page_count,
        page_range=PageRange(start_page=1, end_page=len(selected)),
        render_manifest=RenderManifestInput(
            schema_version=render.schema_version,
            render_manifest_sha256=render.manifest_sha256(),
            size_bytes=len(render.canonical_json()),
            mime_type="application/json",
            capability=_capability(
                method="GET",
                key="run/render-manifest.json",
                expected=render.manifest_sha256(),
                max_bytes=20_000,
            ),
        ),
        page_images=tuple(
            PageImageInput(
                page_number=rendered.page,
                page_image_sha256=rendered.image_sha256,
                size_bytes=rendered.image_size_bytes,
                mime_type="image/png",
                pixel_width=200,
                pixel_height=400,
                sidecar_binding=PageCoordinateBinding(
                    **build_sidecar_binding(rendered.coordinate_manifest)
                ),
                capability=_capability(
                    method="GET",
                    key=f"run/page-{rendered.page:03d}.png",
                    expected=rendered.image_sha256,
                    max_bytes=20_000,
                ),
            )
            for rendered in selected
        ),
        workload_scope="existing_pdf_shadow",
        mode="layout",
        producer=producer,
    )
    return SuryaLayoutRequest(
        identity=identity,
        result_upload_capability=_capability(
            method="PUT",
            key=build_surya_layout_result_object_key(identity.logical_compute_key),
            expected=None,
            max_bytes=output_cap,
        ),
        resource_caps=AcceleratorResourceCaps(
            max_page_count=len(selected), max_total_input_bytes=30_000, max_total_rendered_pixels=100_000 * len(selected),
            max_output_bytes=output_cap, execution_timeout_seconds=60, ttl_seconds=120,
        ),
    )


def test_request_fixture_binds_result_key_to_its_logical_compute_key() -> None:
    request = _request(_trusted_render())

    assert request.result_upload_capability.object_key == (
        f"accelerator/surya-layout/{request.logical_compute_key}/result.json"
    )


def _reconciliation_handle(
    request: SuryaLayoutRequest,
    render: PdfRenderManifest,
) -> SuryaLayoutReconciliationHandle:
    return SuryaLayoutReconciliationHandle.from_request(request, render)


def _artifact(request: SuryaLayoutRequest, render: PdfRenderManifest) -> SuryaLayoutArtifact:
    producer = request.identity.producer
    portable = PortableProducer(
        engine_id=producer.engine_id, engine_version=producer.engine_version,
        model_id=producer.model_id, model_revision=producer.model_revision,
        model_weights_sha256=producer.model_weights_sha256, pipeline_revision=producer.pipeline_revision,
        config_sha256=producer.config_sha256, worker_image_digest=producer.worker_image_digest,
    )
    region = SuryaLayoutRegion(
        region_id="p0001-text-0001", label="text", bbox_px=(10, 10, 100, 100),
        polygon_px=((10, 10), (100, 10), (100, 100), (10, 100)), confidence=0.9,
        reading_order=0,
    )
    return SuryaLayoutArtifact(
        logical_compute_key=request.logical_compute_key, source_sha256=render.source_pdf_sha256,
        page_count=1, render_manifest_schema_version=render.schema_version,
        render_manifest_sha256=render.manifest_sha256(), producer=portable, requested_pages=(1,),
        pages=(SuryaLayoutPage(
            page=1, sidecar_binding=build_sidecar_binding(render.pages[0].coordinate_manifest),
            pixel_width=200, pixel_height=400, rendered_page_px=(0, 0, 200, 400), regions=(region,),
        ),),
    )


def _status(
    request: SuryaLayoutRequest,
    raw: bytes | None = None,
    *,
    state: AcceleratorJobState = AcceleratorJobState.SUCCEEDED,
    external_job_id: str = "provider-job-1",
    logical_compute_key: str | None = None,
    request_digest: str | None = None,
    artifact_size_bytes: int | None = None,
    artifact_key: str | None = None,
) -> AcceleratorJobStatus:
    descriptor = None
    if state is AcceleratorJobState.SUCCEEDED:
        assert raw is not None
        descriptor = ArtifactDescriptor(
            object_key=artifact_key or request.result_upload_capability.object_key, sha256=_digest(raw),
            size_bytes=artifact_size_bytes if artifact_size_bytes is not None else len(raw),
            mime_type="application/json", schema_version="surya_layout_artifact/v1",
        )
    logical_compute_key = logical_compute_key or request.logical_compute_key
    request_digest = request_digest or request.request_digest
    failure = state in {AcceleratorJobState.CONTENT_FAILED, AcceleratorJobState.INFRA_RETRYABLE}
    manifest = SuryaLayoutResultArtifactManifest(
        external_job_id=external_job_id, status=state.value, logical_compute_key=logical_compute_key,
        request_digest=request_digest, result_artifact=descriptor,
        source_sha256=request.identity.source_sha256, source_page_count=1,
        page_range=request.identity.page_range,
        render_manifest_schema_version=request.identity.render_manifest.schema_version,
        render_manifest_sha256=request.identity.render_manifest.render_manifest_sha256,
        page_images=tuple(image.sidecar_binding for image in request.identity.page_images),
        producer=request.identity.producer, started_at=NOW, completed_at=NOW + timedelta(seconds=1),
        reason_code="provider_error" if failure else None,
    )
    return AcceleratorJobStatus(
        external_job_id=external_job_id, state=state, logical_compute_key=logical_compute_key,
        request_digest=request_digest, result_manifest=manifest,
        reason_code="provider_error" if failure else None,
    )


@dataclass
class _Fence:
    values: list[bool]
    calls: int = 0
    lost: bool = False

    def is_current(self) -> bool:
        self.calls += 1
        if self.lost:
            return False
        current = self.values.pop(0) if self.values else True
        self.lost = current is not True
        return current


@dataclass
class _FlappingFence:
    """Adversarial probe that can recover after false or an exception."""

    values: list[bool]

    def is_current(self) -> bool:
        return self.values.pop(0) if self.values else True


class _Reader:
    def __init__(self, response: ArtifactRead | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str, int]] = []

    def read(self, bucket: str, object_key: str, *, max_bytes: int) -> ArtifactRead:
        self.calls.append((bucket, object_key, max_bytes))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _Accelerator:
    def __init__(self, *, submit: object | None = None, poll: object | None = None, cancel_error: bool = False) -> None:
        self.submit_result = submit
        self.poll_result = poll
        self.cancel_error = cancel_error
        self.submits = 0
        self.polls: list[str] = []
        self.cancels: list[str] = []

    def submit(self, request: SuryaLayoutRequest):
        self.submits += 1
        if isinstance(self.submit_result, Exception):
            raise self.submit_result
        return self.submit_result

    def get_status(self, external_job_id: str):
        self.polls.append(external_job_id)
        if isinstance(self.poll_result, Exception):
            raise self.poll_result
        return self.poll_result

    def cancel(self, external_job_id: str):
        self.cancels.append(external_job_id)
        if self.cancel_error:
            raise RuntimeError("cancel unavailable")
        return self.poll_result


def _coordinator(accelerator: _Accelerator, reader: _Reader, fence: _Fence) -> ExistingPdfSuryaCoordinator:
    return ExistingPdfSuryaCoordinator(accelerator=accelerator, artifact_reader=reader, fence=fence)


def test_happy_path_binds_render_reads_exact_cap_and_returns_portable_artifact() -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    reader = _Reader(ArtifactRead(content=raw, content_type="application/json"))
    accelerator = _Accelerator(submit=_status(request, raw))

    outcome = _coordinator(accelerator, reader, _Fence([])).advance(request, render)

    assert outcome.disposition is CoordinatorDisposition.SUCCEEDED
    assert outcome.artifact is not None
    assert reader.calls == [("request-temp", request.result_upload_capability.object_key, len(raw))]
    assert accelerator.submits == 1


def test_persisted_reconciliation_survives_restart_without_request_or_provider_io() -> None:
    render = _trusted_render()
    request = _request(render, output_cap=1_000_000)
    raw = _artifact(request, render).canonical_json()
    handle = _reconciliation_handle(request, render)
    persisted_json = json.dumps(
        handle.to_persistence_payload(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    lowered = persisted_json.lower()
    assert "https://" not in lowered
    assert "signature" not in lowered
    assert "capability" not in lowered
    assert "token" not in lowered
    restored = SuryaLayoutReconciliationHandle.model_validate_json(persisted_json)
    del handle, request

    reader = _Reader(ArtifactRead(content=raw, content_type="application/json"))
    accelerator = _Accelerator()

    outcome = _coordinator(accelerator, reader, _Fence([])).reconcile_result_artifact(
        restored,
        external_job_id=restored.logical_compute_key,
    )

    assert outcome.disposition is CoordinatorDisposition.SUCCEEDED
    assert outcome.external_job_id == restored.logical_compute_key
    assert outcome.provider_state is None
    assert outcome.artifact is not None
    assert reader.calls == [
        ("request-temp", restored.result_object_key, 1_000_000)
    ]
    assert accelerator.submits == 0
    assert accelerator.polls == []
    assert accelerator.cancels == []


def test_reconciliation_handle_recomputes_lineage_and_rejects_credentials() -> None:
    render = _trusted_render()
    request = _request(render)
    handle = _reconciliation_handle(request, render)
    assert handle.validate_external_job_id(request.logical_compute_key) == (
        request.logical_compute_key
    )
    with pytest.raises(AcceleratorContractError, match="external_job_id"):
        handle.validate_external_job_id(_digest("different-persistent-job"))

    payload = handle.to_persistence_payload()
    poisoned_key = _digest("poisoned-compute")
    payload["logical_compute_key"] = poisoned_key
    payload["result_object_key"] = build_surya_layout_result_object_key(poisoned_key)

    with pytest.raises(ValueError, match="trusted render lineage"):
        SuryaLayoutReconciliationHandle.from_persistence_payload(payload)

    clean = _reconciliation_handle(request, render).to_persistence_payload()
    clean["access_token"] = "must-not-cross-the-boundary"
    with pytest.raises(AcceleratorCredentialError):
        SuryaLayoutReconciliationHandle.from_persistence_payload(clean)


def test_mutated_reconciliation_handle_fails_before_storage_or_provider_io() -> None:
    render = _trusted_render()
    request = _request(render)
    handle = _reconciliation_handle(request, render)
    object.__setattr__(
        handle,
        "result_object_key",
        build_surya_layout_result_object_key(_digest("other-compute")),
    )
    reader = _Reader(ArtifactRead(b"unused", "application/json"))
    accelerator = _Accelerator()

    outcome = _coordinator(accelerator, reader, _Fence([])).reconcile_result_artifact(
        handle,
        external_job_id=request.logical_compute_key,
    )

    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "reconciliation_handle_invalid"
    assert reader.calls == []
    assert accelerator.submits == 0
    assert accelerator.polls == []
    assert accelerator.cancels == []


def test_reconciliation_missing_artifact_is_pending_and_storage_outage_is_retryable() -> None:
    render = _trusted_render()
    request = _request(render)
    missing_accelerator = _Accelerator()
    missing = _coordinator(
        missing_accelerator,
        _Reader(ArtifactNotFound("deterministic key absent")),
        _Fence([]),
    ).reconcile_result_artifact(
        _reconciliation_handle(request, render),
        external_job_id=request.logical_compute_key,
    )
    assert missing.disposition is CoordinatorDisposition.PENDING
    assert missing.reason_code == "artifact_not_found"
    assert missing.external_job_id == request.logical_compute_key
    assert missing_accelerator.submits == 0 and missing_accelerator.polls == []

    unavailable = _coordinator(
        _Accelerator(),
        _Reader(RuntimeError("storage unavailable")),
        _Fence([]),
    ).reconcile_result_artifact(
        _reconciliation_handle(request, render),
        external_job_id=request.logical_compute_key,
    )
    assert unavailable.disposition is CoordinatorDisposition.INFRA_RETRYABLE
    assert unavailable.reason_code == "artifact_read_failed"


@pytest.mark.parametrize("poison", ["content_type", "noncanonical", "wrong_lineage"])
def test_reconciliation_never_accepts_or_overwrites_a_poisoned_existing_object(poison: str) -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    content_type = "application/json"
    if poison == "content_type":
        content_type = "text/plain"
    elif poison == "noncanonical":
        raw += b"\n"
    else:
        payload = _artifact(request, render).to_dict()
        payload["logical_compute_key"] = _digest("other-compute")
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    accelerator = _Accelerator()
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(raw, content_type)),
        _Fence([]),
    ).reconcile_result_artifact(
        _reconciliation_handle(request, render),
        external_job_id=request.logical_compute_key,
    )

    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.external_job_id == request.logical_compute_key
    assert outcome.reason_code in {
        "artifact_content_type_mismatch",
        "artifact_parse_failed",
    }
    assert outcome.artifact is None
    assert accelerator.submits == 0 and accelerator.polls == [] and accelerator.cancels == []


def test_reconciliation_applies_fence_before_and_after_its_only_storage_io() -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()

    pre_accelerator = _Accelerator()
    before = _coordinator(
        pre_accelerator,
        _Reader(ArtifactRead(raw, "application/json")),
        _Fence([False]),
    ).reconcile_result_artifact(
        _reconciliation_handle(request, render),
        external_job_id=request.logical_compute_key,
    )
    assert before.disposition is CoordinatorDisposition.FENCE_LOST
    assert pre_accelerator.submits == 0 and pre_accelerator.polls == []

    post_accelerator = _Accelerator()
    post = _coordinator(
        post_accelerator,
        _Reader(ArtifactRead(raw, "application/json")),
        _Fence([True, False]),
    ).reconcile_result_artifact(
        _reconciliation_handle(request, render),
        external_job_id=request.logical_compute_key,
    )
    assert post.disposition is CoordinatorDisposition.FENCE_LOST
    assert post.artifact is None
    assert post_accelerator.submits == 0 and post_accelerator.polls == []
    assert post_accelerator.cancels == []


def test_reconciliation_rejects_job_id_mismatch_or_oversized_object_before_accepting() -> None:
    render = _trusted_render()
    request = _request(render, output_cap=100)
    raw = _artifact(request, render).canonical_json()
    invalid_reader = _Reader(ArtifactRead(raw, "application/json"))
    invalid = _coordinator(
        _Accelerator(), invalid_reader, _Fence([])
    ).reconcile_result_artifact(
        _reconciliation_handle(request, render),
        external_job_id=_digest("different-persistent-job"),
    )
    assert invalid.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert invalid.reason_code == "reconciliation_job_id_mismatch"
    assert invalid.external_job_id is None
    assert invalid_reader.calls == []

    oversized = _coordinator(
        _Accelerator(), _Reader(ArtifactRead(raw, "application/json")), _Fence([])
    ).reconcile_result_artifact(
        _reconciliation_handle(request, render),
        external_job_id=request.logical_compute_key,
    )
    assert oversized.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert oversized.reason_code == "artifact_size_cap_exceeded"


@pytest.mark.parametrize("state", [AcceleratorJobState.QUEUED, AcceleratorJobState.RUNNING])
def test_pending_returns_immediately_without_storage(state: AcceleratorJobState) -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    reader = _Reader(ArtifactRead(b"unused", "application/json"))
    pending = AcceleratorJobStatus(external_job_id="provider-job-1", state=state, logical_compute_key=request.logical_compute_key, request_digest=request.request_digest)
    outcome = _coordinator(_Accelerator(submit=pending), reader, _Fence([])).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.PENDING
    assert reader.calls == []


def test_reattach_polls_only_and_rejects_returned_external_id_mismatch() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    reader = _Reader(ArtifactRead(raw, "application/json"))
    accelerator = _Accelerator(poll=_status(request, raw, external_job_id="another-job"))
    outcome = _coordinator(accelerator, reader, _Fence([])).advance(request, render, "prior-job")
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_status_mismatch"
    assert accelerator.submits == 0 and reader.calls == []
    assert accelerator.cancels == []


def test_stale_provider_result_never_cancels_even_if_a_later_probe_recovers() -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    accelerator = _Accelerator(
        poll=_status(request, raw, external_job_id="another-job")
    )
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(raw, "application/json")),
        _FlappingFence([True, True, False, True]),
    ).advance(request, render, "prior-job")
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    assert outcome.external_job_id == "prior-job"
    assert accelerator.cancels == []


@pytest.mark.parametrize(
    ("state", "disposition"),
    [
        (AcceleratorJobState.CONTENT_FAILED, CoordinatorDisposition.CONTENT_FAILED),
        (AcceleratorJobState.CANCELLED, CoordinatorDisposition.CANCELLED),
    ],
)
def test_content_failure_and_cancellation_are_terminal_without_reads(state: AcceleratorJobState, disposition: CoordinatorDisposition) -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    reader = _Reader(ArtifactRead(b"unused", "application/json"))
    outcome = _coordinator(_Accelerator(submit=_status(request, state=state)), reader, _Fence([])).advance(request, render)
    assert outcome.disposition is disposition
    assert reader.calls == []


def test_infra_does_not_resubmit_without_explicit_authorization_and_replaces_once_when_allowed() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    retryable = _status(request, state=AcceleratorJobState.INFRA_RETRYABLE)
    reader = _Reader(ArtifactRead(raw, "application/json"))
    accelerator = _Accelerator(poll=retryable, submit=_status(request, raw, external_job_id="replacement-job"))
    coordinator = _coordinator(accelerator, reader, _Fence([]))

    no_replacement = coordinator.advance(request, render, "provider-job-1")
    assert no_replacement.disposition is CoordinatorDisposition.INFRA_RETRYABLE
    assert accelerator.submits == 0

    replacement = coordinator.advance(request, render, "provider-job-1", replacement_authorized=True)
    assert replacement.disposition is CoordinatorDisposition.SUCCEEDED
    assert accelerator.submits == 1
    assert accelerator.polls == ["provider-job-1"]
    assert accelerator.cancels == []


def test_missing_prior_job_is_retryable_and_uses_same_explicit_replacement_policy() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    reader = _Reader(ArtifactRead(raw, "application/json"))
    accelerator = _Accelerator(poll=AcceleratorJobNotFoundError("gone"), submit=_status(request, raw, external_job_id="new-job"))
    coordinator = _coordinator(accelerator, reader, _Fence([]))
    assert coordinator.advance(request, render, "gone").reason_code == "provider_job_not_found"
    assert accelerator.submits == 0
    assert coordinator.advance(request, render, "gone", replacement_authorized=True).disposition is CoordinatorDisposition.SUCCEEDED
    assert accelerator.submits == 1
    assert accelerator.polls == ["gone"]
    assert accelerator.cancels == []


def test_live_replacement_submit_validation_failure_does_not_cancel_prior_job() -> None:
    render = _trusted_render()
    request = _request(render)
    accelerator = _Accelerator(submit=SimpleNamespace(external_job_id="untrusted"))
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([]),
    ).advance(
        request,
        render,
        "prior-job",
        replacement_authorized=True,
    )
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_status_malformed"
    assert accelerator.submits == 1
    assert accelerator.polls == []
    assert accelerator.cancels == []


@pytest.mark.parametrize("response", [ArtifactRead(b"short", "application/json"), ArtifactRead(b"x" * 9999, "application/json")])
def test_declared_size_mismatches_are_content_failures(response: ArtifactRead) -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    outcome = _coordinator(_Accelerator(submit=_status(request, raw)), _Reader(response), _Fence([])).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "artifact_size_mismatch"


def test_invalid_artifact_reader_shape_is_a_content_failure() -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    outcome = _coordinator(
        _Accelerator(submit=_status(request, raw)),
        _Reader(SimpleNamespace(content=raw, content_type="application/json")),  # type: ignore[arg-type]
        _Fence([]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "artifact_read_invalid"


def test_transient_storage_read_failure_is_retryable() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    outcome = _coordinator(_Accelerator(submit=_status(request, raw)), _Reader(RuntimeError("storage down")), _Fence([])).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.INFRA_RETRYABLE


def test_submit_contract_failure_is_terminal_instead_of_retryable() -> None:
    render = _trusted_render()
    request = _request(render)
    outcome = _coordinator(
        _Accelerator(submit=AcceleratorContractError("dispatch rejected")),
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_submit_contract_invalid"


def test_storage_bounded_read_limit_is_a_terminal_content_failure() -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    outcome = _coordinator(
        _Accelerator(submit=_status(request, raw)),
        _Reader(ArtifactReadLimitExceeded("object exceeded max_bytes")),
        _Fence([]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "artifact_size_cap_exceeded"
    assert outcome.provider_state is AcceleratorJobState.SUCCEEDED


def test_storage_bounded_read_limit_with_concurrent_fence_loss_never_cancels_job() -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    accelerator = _Accelerator(submit=_status(request, raw))
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactReadLimitExceeded("object exceeded max_bytes")),
        _Fence([True, True, True, True, False]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    assert outcome.external_job_id == "provider-job-1"
    assert accelerator.cancels == []


@pytest.mark.parametrize("kind", ["hash", "content_type"])
def test_hash_and_content_type_are_verified_before_parse(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    monkeypatch.setattr(
        "worker.accelerator_coordinator.parse_surya_layout_artifact_bytes",
        lambda *args, **kwargs: pytest.fail("parser must not run before byte checks"),
    )
    response = ArtifactRead(raw if kind == "content_type" else raw[:-1] + b" ", "text/plain" if kind == "content_type" else "application/json")
    outcome = _coordinator(_Accelerator(submit=_status(request, raw)), _Reader(response), _Fence([])).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == (
        "artifact_digest_mismatch"
        if kind == "hash"
        else "artifact_content_type_mismatch"
    )


@pytest.mark.parametrize("mutation", ["lineage", "geometry"])
def test_parser_rejects_remote_artifact_lineage_and_geometry(mutation: str) -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    payload = _artifact(request, render).to_dict()
    if mutation == "lineage":
        payload["producer"]["engine_version"] = "untrusted"
    else:
        payload["pages"][0]["regions"][0]["bbox_px"] = [-1, 10, 100, 100]
        payload["pages"][0]["regions"][0]["polygon_px"] = [[-1, 10], [100, 10], [100, 100], [-1, 100]]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    outcome = _coordinator(_Accelerator(submit=_status(request, raw)), _Reader(ArtifactRead(raw, "application/json")), _Fence([])).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.artifact is None
    assert outcome.reason_code == "artifact_parse_failed"


def test_stale_fence_before_or_after_provider_never_cancels() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    accelerator = _Accelerator(submit=_status(request, raw))
    before = _coordinator(accelerator, _Reader(ArtifactRead(raw, "application/json")), _Fence([False])).advance(request, render)
    assert before.disposition is CoordinatorDisposition.FENCE_LOST
    assert accelerator.submits == 0

    stale_accelerator = _Accelerator(submit=_status(request, raw), cancel_error=True)
    after = _coordinator(stale_accelerator, _Reader(ArtifactRead(raw, "application/json")), _Fence([True, True, False])).advance(request, render)
    assert after.disposition is CoordinatorDisposition.FENCE_LOST
    assert stale_accelerator.cancels == []


def test_fence_loss_after_read_or_parse_discards_artifact_without_cancelling() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    after_read_accelerator = _Accelerator(submit=_status(request, raw))
    after_read = _coordinator(after_read_accelerator, _Reader(ArtifactRead(raw, "application/json")), _Fence([True, True, True, True, False])).advance(request, render)
    assert after_read.disposition is CoordinatorDisposition.FENCE_LOST and after_read.artifact is None
    assert after_read_accelerator.cancels == []

    after_parse_accelerator = _Accelerator(submit=_status(request, raw))
    after_parse = _coordinator(after_parse_accelerator, _Reader(ArtifactRead(raw, "application/json")), _Fence([True, True, True, True, True, False])).advance(request, render)
    assert after_parse.disposition is CoordinatorDisposition.FENCE_LOST and after_parse.artifact is None
    assert after_parse_accelerator.cancels == []


def test_untrusted_request_render_binding_never_contacts_provider_or_storage() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    accelerator = _Accelerator(submit=SimpleNamespace())
    reader = _Reader(ArtifactRead(b"unused", "application/json"))
    outcome = _coordinator(accelerator, reader, _Fence([])).advance(
        request,
        _trusted_render(source="other-pdf"),
    )
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert accelerator.submits == 0 and reader.calls == []


@pytest.mark.parametrize("state", [AcceleratorJobState.QUEUED, AcceleratorJobState.CONTENT_FAILED])
@pytest.mark.parametrize("field", ["logical_compute_key", "request_digest"])
def test_every_provider_status_binds_key_and_digest_before_any_disposition_or_io(
    state: AcceleratorJobState,
    field: str,
) -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    wrong = _digest("wrong")
    if state is AcceleratorJobState.QUEUED:
        status = AcceleratorJobStatus(
            external_job_id="provider-job-1", state=state,
            logical_compute_key=wrong if field == "logical_compute_key" else request.logical_compute_key,
            request_digest=wrong if field == "request_digest" else request.request_digest,
        )
    else:
        status = _status(
            request, state=state,
            logical_compute_key=wrong if field == "logical_compute_key" else None,
            request_digest=wrong if field == "request_digest" else None,
        )
    accelerator = _Accelerator(submit=status)
    reader = _Reader(ArtifactRead(b"unused", "application/json"))
    outcome = _coordinator(accelerator, reader, _Fence([])).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_status_mismatch"
    assert reader.calls == []


@pytest.mark.parametrize(
    "mutated_external_job_id",
    ["/tenant/private/result.json?signature=leaked", 123],
)
def test_provider_status_is_freshly_validated_before_exposing_external_id(
    mutated_external_job_id: object,
) -> None:
    render = _trusted_render()
    request = _request(render)
    status = AcceleratorJobStatus(
        external_job_id="provider-job-1",
        state=AcceleratorJobState.QUEUED,
        logical_compute_key=request.logical_compute_key,
        request_digest=request.request_digest,
    )
    object.__setattr__(status, "external_job_id", mutated_external_job_id)
    outcome = _coordinator(
        _Accelerator(submit=status),
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_status_malformed"
    assert outcome.external_job_id is None


def test_full_document_only_rejects_a_trusted_render_subset_without_io() -> None:
    render = _trusted_render(page_count=2)
    request = _request(render, subset=True)
    accelerator = _Accelerator(submit=SimpleNamespace())
    reader = _Reader(ArtifactRead(b"unused", "application/json"))
    outcome = _coordinator(accelerator, reader, _Fence([])).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "request_render_binding_invalid"
    assert accelerator.submits == 0 and reader.calls == []


def test_normalized_json_content_type_is_accepted_and_control_characters_are_rejected() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    accepted = _coordinator(
        _Accelerator(submit=_status(request, raw)),
        _Reader(ArtifactRead(raw, "Application/Json; charset=utf-8")),
        _Fence([]),
    ).advance(request, render)
    assert accepted.disposition is CoordinatorDisposition.SUCCEEDED

    rejected = _coordinator(
        _Accelerator(submit=_status(request, raw)),
        _Reader(ArtifactRead(raw, "application/json\r\ntext/plain")),
        _Fence([]),
    ).advance(request, render)
    assert rejected.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert rejected.reason_code == "artifact_content_type_mismatch"


def test_oversized_descriptor_fails_before_storage_read() -> None:
    render, request = _trusted_render(), None
    request = _request(render, output_cap=20_000_000)
    raw = _artifact(request, render).canonical_json()
    reader = _Reader(ArtifactRead(raw, "application/json"))
    outcome = _coordinator(
        _Accelerator(submit=_status(request, raw, artifact_size_bytes=17 * 1024 * 1024)),
        reader,
        _Fence([]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "artifact_size_cap_exceeded"
    assert reader.calls == []


def test_prior_succeeded_read_failure_polls_same_id_and_never_submits() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    accelerator = _Accelerator(
        poll=_status(request, raw, external_job_id="prior-job"),
        submit=_status(request, raw, external_job_id="replacement-job"),
    )
    outcome = _coordinator(accelerator, _Reader(RuntimeError("network")), _Fence([])).advance(
        request,
        render,
        "prior-job",
    )
    assert outcome.disposition is CoordinatorDisposition.INFRA_RETRYABLE
    assert accelerator.polls == ["prior-job"]
    assert accelerator.submits == 0


def test_poll_fence_loss_before_and_after_provider_action_is_safe() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    accelerator = _Accelerator(poll=_status(request, raw, external_job_id="prior-job"))
    before = _coordinator(accelerator, _Reader(ArtifactRead(raw, "application/json")), _Fence([False])).advance(request, render, "prior-job")
    assert before.disposition is CoordinatorDisposition.FENCE_LOST
    assert before.external_job_id == "prior-job"
    assert accelerator.polls == []
    assert accelerator.cancels == []

    stale_accelerator = _Accelerator(poll=_status(request, raw, external_job_id="prior-job"))
    after = _coordinator(stale_accelerator, _Reader(ArtifactRead(raw, "application/json")), _Fence([True, True, False])).advance(request, render, "prior-job")
    assert after.disposition is CoordinatorDisposition.FENCE_LOST
    assert stale_accelerator.polls == ["prior-job"]
    assert stale_accelerator.cancels == []


def test_poll_helper_fence_loss_after_initial_probe_never_cancels_known_job() -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    accelerator = _Accelerator(poll=_status(request, raw, external_job_id="prior-job"))
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(raw, "application/json")),
        _Fence([True, False]),
    ).advance(request, render, "prior-job")
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    assert accelerator.polls == []
    assert accelerator.cancels == []


def test_authorized_replacement_does_not_mutate_known_job_if_fence_is_lost_before_submit() -> None:
    render = _trusted_render()
    request = _request(render)
    accelerator = _Accelerator(submit=SimpleNamespace())
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([True, False]),
    ).advance(
        request,
        render,
        "prior-job",
        replacement_authorized=True,
    )
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    assert outcome.external_job_id == "prior-job"
    assert accelerator.submits == 0
    assert accelerator.cancels == []


def test_malformed_provider_result_with_post_call_fence_loss_never_crashes_or_reads() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    reader = _Reader(ArtifactRead(b"unused", "application/json"))
    accelerator = _Accelerator(poll=SimpleNamespace(external_job_id="not-used"))
    outcome = _coordinator(
        accelerator,
        reader,
        _Fence([True, True, False]),
    ).advance(request, render, "prior-job")
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    assert outcome.reason_code == "fence_lost"
    assert reader.calls == []
    assert accelerator.cancels == []


def test_malformed_live_poll_never_uses_non_atomic_provider_cancel() -> None:
    render = _trusted_render()
    request = _request(render)
    accelerator = _Accelerator(poll=SimpleNamespace(external_job_id="not-used"))
    fence = _Fence([True, True, True, True, True])

    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(b"unused", "application/json")),
        fence,
    ).advance(request, render, "prior-job")

    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_status_malformed"
    assert accelerator.cancels == []
    # Advance, poll, and the mandatory post-provider validation sample.
    assert fence.calls == 3


def test_malformed_live_poll_does_not_probe_for_or_attempt_cleanup_cancel() -> None:
    render = _trusted_render()
    request = _request(render)
    accelerator = _Accelerator(poll=SimpleNamespace(external_job_id="not-used"))

    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([True, True, True, False]),
    ).advance(request, render, "prior-job")

    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_status_malformed"
    assert accelerator.cancels == []


def test_safe_provider_reason_is_preserved_for_terminal_content_failure() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    outcome = _coordinator(
        _Accelerator(submit=_status(request, state=AcceleratorJobState.CONTENT_FAILED)),
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_error"


@pytest.mark.parametrize(
    "unsafe_reason",
    ["https://storage.example.test/sign?sig=secret", "Provider Error", "a" * 100_000, 123],
)
def test_untrusted_provider_reason_never_reaches_the_outcome(unsafe_reason: object) -> None:
    render = _trusted_render()
    request = _request(render)
    status = _status(request, state=AcceleratorJobState.CONTENT_FAILED)
    object.__setattr__(status, "reason_code", unsafe_reason)
    outcome = _coordinator(
        _Accelerator(submit=status),
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_status_malformed"
    assert outcome.reason_code != unsafe_reason


def test_metadata_mismatch_rechecks_fence_and_prioritizes_fence_loss() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    accelerator = _Accelerator(
        submit=_status(request, raw, artifact_key="run/surya/other-result.json"),
    )
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(raw, "application/json")),
        _Fence([True, True, True, False]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    assert outcome.provider_state is None
    assert accelerator.cancels == []


def test_rejected_succeeded_metadata_preserves_provider_state_while_fence_is_live() -> None:
    render = _trusted_render()
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    outcome = _coordinator(
        _Accelerator(
            submit=_status(
                request,
                raw,
                artifact_key="run/surya/other-result.json",
            ),
        ),
        _Reader(ArtifactRead(raw, "application/json")),
        _Fence([]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "provider_result_mismatch"
    assert outcome.provider_state is AcceleratorJobState.SUCCEEDED


def test_stale_provider_result_skips_cancel_and_its_fence_probes() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    fence = _Fence([True, True, False])
    accelerator = _Accelerator(submit=_status(request, raw), cancel_error=True)
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(raw, "application/json")),
        fence,
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    # Initial probe and provider pre/post probes; stale paths do not cancel.
    assert fence.calls == 3
    assert accelerator.cancels == []


class _TruthyAuthorization:
    def __bool__(self) -> bool:
        return True


@pytest.mark.parametrize("authorization", ["true", 1, _TruthyAuthorization()])
def test_replacement_authorization_requires_an_exact_bool_before_provider_io(authorization: object) -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    accelerator = _Accelerator(submit=SimpleNamespace(), poll=SimpleNamespace())
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([]),
    ).advance(request, render, "prior-job", replacement_authorized=authorization)  # type: ignore[arg-type]
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "replacement_authorization_invalid"
    assert outcome.provider_state is None
    assert accelerator.polls == [] and accelerator.submits == 0


def test_replacement_authorization_requires_a_prior_job_id() -> None:
    render = _trusted_render()
    request = _request(render)
    accelerator = _Accelerator(submit=SimpleNamespace())
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([]),
    ).advance(request, render, replacement_authorized=True)
    assert outcome.disposition is CoordinatorDisposition.CONTENT_FAILED
    assert outcome.reason_code == "replacement_authorization_invalid"
    assert accelerator.submits == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"external_job_id": "/storage/private/result.json?signature=secret"},
        {"reason_code": "Provider Error"},
    ],
)
def test_outcome_boundary_rejects_unsafe_identifiers(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ExistingPdfSuryaOutcome(
            disposition=CoordinatorDisposition.CONTENT_FAILED,
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("operation", ["submit", "poll"])
def test_provider_exception_with_concurrent_fence_loss_returns_fence_lost(operation: str) -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    accelerator = _Accelerator(
        submit=RuntimeError("provider") if operation == "submit" else None,
        poll=RuntimeError("provider") if operation == "poll" else None,
    )
    outcome = _coordinator(
        accelerator,
        _Reader(ArtifactRead(b"unused", "application/json")),
        _Fence([True, True, False]),
    ).advance(request, render, "prior-job" if operation == "poll" else None)
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    assert outcome.provider_state is None
    if operation == "poll":
        assert outcome.external_job_id == "prior-job"
        assert accelerator.cancels == []


def test_storage_exception_with_concurrent_fence_loss_returns_fence_lost() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    outcome = _coordinator(
        _Accelerator(submit=_status(request, raw)),
        _Reader(RuntimeError("storage")),
        _Fence([True, True, True, True, False]),
    ).advance(request, render)
    assert outcome.disposition is CoordinatorDisposition.FENCE_LOST
    assert outcome.provider_state is None


def test_valid_provider_statuses_preserve_their_original_state() -> None:
    render, request = _trusted_render(), None
    request = _request(render)
    raw = _artifact(request, render).canonical_json()
    pending = AcceleratorJobStatus(
        external_job_id="provider-job-1", state=AcceleratorJobState.QUEUED,
        logical_compute_key=request.logical_compute_key, request_digest=request.request_digest,
    )
    assert _coordinator(_Accelerator(submit=pending), _Reader(ArtifactRead(raw, "application/json")), _Fence([])).advance(request, render).provider_state is AcceleratorJobState.QUEUED
    assert _coordinator(_Accelerator(submit=_status(request, state=AcceleratorJobState.CONTENT_FAILED)), _Reader(ArtifactRead(raw, "application/json")), _Fence([])).advance(request, render).provider_state is AcceleratorJobState.CONTENT_FAILED
    assert _coordinator(_Accelerator(submit=_status(request, raw)), _Reader(ArtifactRead(raw, "application/json")), _Fence([])).advance(request, render).provider_state is AcceleratorJobState.SUCCEEDED
