"""GPU/network-free core for the RunPod Surya-layout invocation handler.

This boundary accepts an already-rendered page manifest and PNG capabilities.
It *never* accepts, downloads, or renders a PDF, and it never preserves OCR
text, HTML, or inference-provider diagnostic payloads.  The surrounding
deployment is responsible for adapting RunPod's SDK, bounded HTTPS storage,
and Surya's GPU predictor to the two narrow ports below.

No raw signed URL, storage body, or storage object path is included in a
raised error or a returned response.  The trusted EC2 worker still performs
the later result-artifact acceptance and database commit.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import math
import re
from time import monotonic as _system_monotonic
from typing import Any, Literal, Protocol

from common_ir_pipeline.pdf_fusion.coordinate_manifest import build_sidecar_binding
from common_ir_pipeline.pdf_fusion.render_manifest import (
    MAX_PNG_FILE_BYTES,
    PdfRenderManifest,
    PdfRenderManifestError,
    inspect_png_bytes,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    MAX_ARTIFACT_BYTES,
    MAX_REGIONS_PER_PAGE,
    MAX_REQUESTED_PAGES,
    MAX_TOTAL_REGIONS,
    SCHEMA_VERSION as SURYA_LAYOUT_ARTIFACT_SCHEMA_VERSION,
    SuryaLayoutArtifact,
    SuryaLayoutArtifactError,
    SuryaLayoutPage,
    SuryaLayoutRegion,
    SuryaProducerIdentity as PortableSuryaProducerIdentity,
)
from worker.contracts.accelerator import (
    AcceleratorContractError,
    AcceleratorDispatchPolicy,
    AcceleratorJobState,
    AcceleratorJobStatus,
    ArtifactDescriptor,
    SignedStorageCapability,
    SuryaLayoutRequest,
    SuryaLayoutResultArtifactManifest,
    SuryaProducerIdentity,
)

__all__ = [
    "ContentPortFailure",
    "InferencePort",
    "InferenceContentFailure",
    "InferenceInfrastructureFailure",
    "InfrastructurePortFailure",
    "PortContentFailure",
    "PortInfrastructureFailure",
    "RunPodSuryaLayoutHandler",
    "RunPodSuryaExecutionPolicy",
    "StorageContentFailure",
    "StorageGetContentFailure",
    "StorageInfrastructureFailure",
    "StoragePort",
    "StoragePutConflictFailure",
    "SuryaLayoutPageResult",
    "SuryaModelConfidenceMarker",
    "handle_event",
]


_EXTERNAL_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Render manifests are control-plane JSON, not arbitrary document payloads.
# This allows a little over 8 KiB of canonical metadata per requested source
# page plus a fixed document envelope.  It remains deliberately far below a
# page PNG and is checked before any remote GET or JSON allocation.
_RENDER_MANIFEST_BASE_MAX_BYTES = 64 * 1024
_RENDER_MANIFEST_PER_PAGE_MAX_BYTES = 8 * 1024


def _render_manifest_max_bytes(source_page_count: int) -> int:
    """Return the bounded canonical JSON budget for one source document."""

    if (
        isinstance(source_page_count, bool)
        or not isinstance(source_page_count, int)
        or source_page_count <= 0
    ):
        raise _ContentFailure("render_manifest_page_count_invalid")
    return _RENDER_MANIFEST_BASE_MAX_BYTES + (
        source_page_count * _RENDER_MANIFEST_PER_PAGE_MAX_BYTES
    )
_RAW_LABEL_TO_V1 = {
    # This is intentionally an explicit allow-map, rather than accepting the
    # portable vocabulary directly.  Adding a new Surya label is a reviewed
    # contract change.
    "Caption": "caption",
    "Footnote": "footnote",
    "Equation": "equation",
    "ListGroup": "list_group",
    "PageHeader": "page_header",
    "PageFooter": "page_footer",
    "Picture": "picture",
    "SectionHeader": "section_header",
    "Table": "table",
    "Text": "text",
    "Figure": "figure",
    "Code": "code",
    "Form": "form",
    "TableOfContents": "table_of_contents",
    "ChemicalBlock": "chemical_block",
    "Diagram": "diagram",
    "Bibliography": "bibliography",
    "BlankPage": "blank_page",
}


class _ContentFailure(ValueError):
    """A supplied request/input/inference result cannot produce a safe artifact."""


class _InfrastructureFailure(RuntimeError):
    """A transient port/runtime failure; no provider detail crosses the boundary."""


class ContentPortFailure(ValueError):
    """A deterministic adapter rejection of untrusted content or policy input.

    Storage adapters use this for redirect, MIME, byte-limit and integrity
    failures.  A create-only PUT collision remains a distinct subclass: it
    never permits overwrite, but can be reconciled as an ambiguous delivery.
    """


class InfrastructurePortFailure(RuntimeError):
    """A timeout, 5xx, transport fault, or unavailable provider dependency."""


# The short aliases make a port's failure contract legible without coupling a
# deployment adapter to this handler's private terminal-state exceptions.
PortContentFailure = ContentPortFailure
PortInfrastructureFailure = InfrastructurePortFailure


class StorageContentFailure(ContentPortFailure):
    """A GET/PUT storage result is deterministic invalid content/policy."""


class StorageGetContentFailure(StorageContentFailure):
    """GET redirect, MIME, bounded-read, or expected-hash rejection."""


class StoragePutConflictFailure(StorageContentFailure):
    """The immutable create-only result object already exists.

    This is deliberately a distinct adapter outcome.  The handler reports it
    as retryable rather than declaring the work invalid: a trusted EC2
    reconciler may be able to read and verify the existing immutable object
    after an acknowledgement-loss or a duplicate delivery.
    """


class StorageInfrastructureFailure(InfrastructurePortFailure):
    """Storage timeout, 5xx, or transport failure."""


class InferenceContentFailure(ContentPortFailure):
    """The pinned inference adapter rejected a deterministic page input/result."""


class InferenceInfrastructureFailure(InfrastructurePortFailure):
    """Inference timeout, GPU/provider 5xx, or unavailable runtime."""


@dataclass(frozen=True)
class SuryaModelConfidenceMarker:
    """Attestation emitted only by the pinned model adapter.

    An adapter may emit this marker only after it has selected the configured
    Surya model.  A confidence number without this identity binding is
    deliberately discarded: a fallback predictor is allowed to return layout
    geometry, but it must not be represented as a pinned-model confidence.
    """

    producer: SuryaProducerIdentity
    source: Literal["surya_layout_model"] = "surya_layout_model"


@dataclass(frozen=True)
class SuryaLayoutPageResult:
    """Typed, page-scoped subset of official Surya layout output.

    ``image_bbox`` is the official image canvas as ``(x0, y0, x1, y1)`` and
    must equal the decoded PNG canvas.  ``regions`` remain mappings because
    Surya's individual layout box is adapted at the port boundary, but their
    label, geometry, and required reading ``position`` are verified below.
    """

    error: object
    image_bbox: object
    regions: object
    confidence_marker: SuryaModelConfidenceMarker | None = None


@dataclass(frozen=True)
class RunPodSuryaExecutionPolicy:
    """Mandatory deployment-owned execution limits and pinned producer.

    ``dispatch_policy`` carries indivisible host/bucket/path/method scopes and
    the request caps (TTL, execution, pages, bytes, pixels, capability bytes).
    The remaining limits are deliberately worker-owned rather than accepted
    from the capability-bearing request.
    """

    dispatch_policy: AcceleratorDispatchPolicy
    producer: SuryaProducerIdentity
    max_output_bytes: int
    max_terminal_output_bytes: int
    max_regions_per_page: int

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_policy, AcceleratorDispatchPolicy):
            raise TypeError("dispatch_policy must be an AcceleratorDispatchPolicy")
        if not isinstance(self.producer, SuryaProducerIdentity):
            raise TypeError("producer must be a SuryaProducerIdentity")
        for name in (
            "max_output_bytes",
            "max_terminal_output_bytes",
            "max_regions_per_page",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        # A compact terminal envelope contains a bounded job id and a stable
        # reason.  Refuse a deployment setting that cannot carry one safely.
        if self.max_terminal_output_bytes < 192:
            raise ValueError("max_terminal_output_bytes must allow a safe envelope")
        if self.max_output_bytes > MAX_ARTIFACT_BYTES:
            raise ValueError("max_output_bytes exceeds the portable artifact ceiling")
        if self.max_page_count > MAX_REQUESTED_PAGES:
            raise ValueError("max_page_count exceeds the portable artifact ceiling")
        if self.max_regions_per_page > MAX_REGIONS_PER_PAGE:
            raise ValueError("max_regions_per_page exceeds the portable artifact ceiling")
        if self.max_page_count * self.max_regions_per_page > MAX_TOTAL_REGIONS:
            raise ValueError("page/region limits exceed the portable artifact ceiling")

    @property
    def max_ttl_seconds(self) -> int:
        return self.dispatch_policy.max_ttl_seconds

    @property
    def max_execution_timeout_seconds(self) -> int:
        return self.dispatch_policy.max_execution_timeout_seconds

    @property
    def max_page_count(self) -> int:
        return self.dispatch_policy.max_page_count

    @property
    def max_total_input_bytes(self) -> int:
        return self.dispatch_policy.max_total_input_bytes

    @property
    def max_total_rendered_pixels(self) -> int:
        return self.dispatch_policy.max_total_rendered_pixels

    @property
    def max_capability_bytes(self) -> int:
        return self.dispatch_policy.max_capability_bytes


class StoragePort(Protocol):
    """Bounded capability I/O with no ambient storage authority.

    Adapters must enforce no redirects, exact read caps, expected content MIME,
    expected hash, and create-only PUT semantics.  Deterministic GET failures
    (redirect/MIME/oversize/hash) raise :class:`StorageContentFailure`.
    Create-only PUT conflicts raise :class:`StoragePutConflictFailure` for
    reconciliation; timeout/5xx/transport faults raise
    :class:`StorageInfrastructureFailure`.  They receive a validated
    capability, not a bucket/path or an unscoped bearer credential.
    """

    def get(
        self,
        capability: SignedStorageCapability,
        *,
        max_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> bytes: ...

    def put_create_only(
        self,
        capability: SignedStorageCapability,
        *,
        content: bytes,
        content_type: str,
        deadline_monotonic: float | None = None,
    ) -> None: ...


class InferencePort(Protocol):
    """The textless subset of a pinned Surya layout predictor.

    The port returns one :class:`SuryaLayoutPageResult` for one decoded page,
    and raises the typed content/infrastructure failures above.  Any predictor
    text/OCR/HTML payload is deliberately ignored.  A finite confidence is
    retained only with a matching :class:`SuryaModelConfidenceMarker`; the
    handler never estimates one or turns a fallback score into a model score.
    """

    def layout(
        self,
        *,
        page_number: int,
        png_bytes: bytes,
        pixel_width: int,
        pixel_height: int,
        deadline_monotonic: float | None = None,
    ) -> SuryaLayoutPageResult: ...


class RunPodSuryaLayoutHandler:
    """One synchronous RunPod handler invocation over injected safe ports."""

    def __init__(
        self,
        *,
        storage: StoragePort,
        inference: InferencePort,
        policy: RunPodSuryaExecutionPolicy,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._storage = storage
        self._inference = inference
        self._policy = policy
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or _system_monotonic

    def handle(self, event: Mapping[str, object]) -> dict[str, object]:
        """Return a redacted, ``AcceleratorJobStatus``-compatible result dict.

        Invalid event envelopes cannot have a valid full job status because
        request digest/key bindings do not exist yet.  They return the small
        provider error envelope instead.  Once the request validates, every
        terminal result is constructed through ``AcceleratorJobStatus``.
        """

        event_id = _event_id_or_none(event)
        if event_id is None:
            return self._compact_failure("event_invalid")
        raw_input = event.get("input") if isinstance(event, Mapping) else None
        if not isinstance(raw_input, Mapping):
            return self._compact_failure("input_invalid")
        try:
            _raw_request_preflight(raw_input, self._policy)
            request = SuryaLayoutRequest.from_wire_payload(raw_input)
        except Exception:
            # ``from_wire_payload`` must never receive an unbounded page range
            # or page list.  Its diagnostics can contain signed input values,
            # so all pre-request failures collapse to this fixed envelope.
            return self._compact_failure("input_invalid")

        started_at = _safe_now(self._now)
        try:
            deadline = self._execution_deadline(
                request.resource_caps.execution_timeout_seconds
            )
        except _InfrastructureFailure as error:
            return self._terminal_failure(
                request,
                event_id,
                started_at,
                AcceleratorJobState.INFRA_RETRYABLE,
                str(error),
            )
        try:
            self._validate_execution_policy(request, now=started_at)
        except _ContentFailure as error:
            return self._terminal_failure(
                request, event_id, started_at, AcceleratorJobState.CONTENT_FAILED, str(error)
            )
        except Exception:
            # No raw validation detail, signed URL, or traceback crosses this
            # boundary even if a deployment accidentally supplied a bad policy.
            return self._terminal_failure(
                request,
                event_id,
                started_at,
                AcceleratorJobState.INFRA_RETRYABLE,
                "worker_policy_validation_failed",
            )

        try:
            self._assert_before_deadline(deadline)
            artifact = self._build_artifact(request, deadline=deadline)
            self._assert_before_deadline(deadline)
            raw_artifact = artifact.canonical_json()
            self._assert_artifact_size(len(raw_artifact), request=request)
            completed_at = _safe_now(self._now, not_before=started_at)
            success = self._success_status(
                request,
                event_id,
                started_at,
                completed_at,
                raw_artifact,
            )
            terminal_wire = self._bounded_terminal_wire(success, event_id)
            if terminal_wire is None:
                # Do not publish an artifact when the corresponding terminal
                # result cannot fit the worker's independently owned response
                # budget.  A compact content envelope is returned below.
                return self._compact_failure("terminal_output_exceeds_cap")
            try:
                self._assert_before_deadline(deadline)
                self._storage.put_create_only(
                    request.result_upload_capability,
                    content=raw_artifact,
                    content_type="application/json",
                    deadline_monotonic=deadline,
                )
            except StoragePutConflictFailure as error:
                # A duplicate delivery or lost PUT acknowledgement can leave a
                # valid immutable object behind.  EC2 owns the readback and
                # reconciliation decision, so do not permanently classify the
                # original work as invalid here.
                self._assert_before_deadline(deadline)
                raise _InfrastructureFailure("result_upload_conflict") from error
            except StorageContentFailure as error:
                self._assert_before_deadline(deadline)
                raise _ContentFailure("result_upload_rejected") from error
            except StorageInfrastructureFailure as error:
                self._assert_before_deadline(deadline)
                raise _InfrastructureFailure("result_upload_failed") from error
            except Exception as error:  # adapter details must never leave this process
                self._assert_before_deadline(deadline)
                raise _InfrastructureFailure("result_upload_failed") from error
            self._assert_before_deadline(deadline)
        except _ContentFailure as error:
            return self._terminal_failure(
                request, event_id, started_at, AcceleratorJobState.CONTENT_FAILED, str(error)
            )
        except _InfrastructureFailure as error:
            return self._terminal_failure(
                request, event_id, started_at, AcceleratorJobState.INFRA_RETRYABLE, str(error)
            )
        except Exception as error:
            # Predictor/port bugs and unexpected runtime faults are retryable;
            # never return their potentially secret-bearing messages.
            return self._terminal_failure(
                request,
                event_id,
                started_at,
                AcceleratorJobState.INFRA_RETRYABLE,
                "worker_runtime_failed",
            )

        return terminal_wire

    def _validate_execution_policy(
        self,
        request: SuryaLayoutRequest,
        *,
        now: datetime,
    ) -> None:
        """Perform deployment-owned admission checks before any port call.

        Dispatch validates that a capability survives the *whole* requested
        queue-plus-execution TTL.  Repeating that condition after an unknown
        RunPod queue delay would reset that TTL at worker start and reject a
        perfectly valid queued job.  This worker instead requires each live
        capability to cover the remaining execution window from ``now``.

        A capability whose *remaining* lifetime exceeds the deployment TTL
        ceiling is still safely rejectable: because it must have been issued
        no later than ``now``, its original lifetime necessarily exceeded that
        ceiling.  The converse cannot be proved without an authenticated
        issuance timestamp, so original dispatch-time TTL enforcement remains
        the trusted dispatcher's responsibility.
        """

        if request.identity.producer.model_dump(mode="json") != self._policy.producer.model_dump(
            mode="json"
        ):
            raise _ContentFailure("producer_identity_mismatch")
        if request.resource_caps.max_output_bytes > self._policy.max_output_bytes:
            raise _ContentFailure("request_output_cap_exceeds_policy")
        policy = self._policy.dispatch_policy
        try:
            request._assert_live_integrity()
        except AcceleratorContractError as error:
            raise _ContentFailure("execution_policy_rejected") from error
        if request.resource_caps.ttl_seconds > policy.max_ttl_seconds:
            raise _ContentFailure("execution_policy_rejected")
        if (
            request.resource_caps.execution_timeout_seconds
            > policy.max_execution_timeout_seconds
        ):
            raise _ContentFailure("execution_policy_rejected")
        actual_page_count = len(request.identity.page_images)
        if (
            actual_page_count > policy.max_page_count
            or request.resource_caps.max_page_count > policy.max_page_count
        ):
            raise _ContentFailure("execution_policy_rejected")
        actual_total_input_bytes = request.identity.render_manifest.size_bytes + sum(
            page.size_bytes for page in request.identity.page_images
        )
        if (
            actual_total_input_bytes > policy.max_total_input_bytes
            or request.resource_caps.max_total_input_bytes
            > policy.max_total_input_bytes
        ):
            raise _ContentFailure("execution_policy_rejected")
        aggregate_get_capability_bytes = (
            request.identity.render_manifest.capability.resource_caps.max_bytes
            + sum(
                page.capability.resource_caps.max_bytes
                for page in request.identity.page_images
            )
        )
        if aggregate_get_capability_bytes > policy.max_total_input_bytes:
            raise _ContentFailure("execution_policy_rejected")
        actual_total_rendered_pixels = sum(
            page.rendered_pixels for page in request.identity.page_images
        )
        if (
            actual_total_rendered_pixels > policy.max_total_rendered_pixels
            or request.resource_caps.max_total_rendered_pixels
            > policy.max_total_rendered_pixels
        ):
            raise _ContentFailure("execution_policy_rejected")

        minimum_expiry = now + timedelta(
            seconds=request.resource_caps.execution_timeout_seconds
        )
        maximum_remaining_expiry = now + timedelta(seconds=policy.max_ttl_seconds)
        capabilities = (
            request.identity.render_manifest.capability,
            *(page.capability for page in request.identity.page_images),
            request.result_upload_capability,
        )
        for capability in capabilities:
            if not any(scope.permits(capability) for scope in policy.allowed_scopes):
                raise _ContentFailure("execution_policy_rejected")
            if capability.expires_at < minimum_expiry:
                raise _ContentFailure("execution_policy_rejected")
            if capability.expires_at > maximum_remaining_expiry:
                raise _ContentFailure("execution_policy_rejected")
            if capability.resource_caps.max_bytes > policy.max_capability_bytes:
                raise _ContentFailure("execution_policy_rejected")
            if not set(capability.resource_caps.allowed_mime_types).issubset(
                policy.allowed_mime_types
            ):
                raise _ContentFailure("execution_policy_rejected")

    def _execution_deadline(self, execution_timeout_seconds: int) -> float:
        """Create one monotonic deadline for this synchronous invocation."""

        started = self._monotonic_seconds()
        return started + float(execution_timeout_seconds)

    def _assert_before_deadline(self, deadline: float) -> None:
        if self._monotonic_seconds() >= deadline:
            raise _InfrastructureFailure("execution_timeout")

    def _monotonic_seconds(self) -> float:
        try:
            value = self._monotonic()
        except Exception as error:
            raise _InfrastructureFailure("execution_timeout") from error
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _InfrastructureFailure("execution_timeout")
        converted = float(value)
        if not math.isfinite(converted):
            raise _InfrastructureFailure("execution_timeout")
        return converted

    def _success_status(
        self,
        request: SuryaLayoutRequest,
        event_id: str,
        started_at: datetime,
        completed_at: datetime,
        raw_artifact: bytes,
    ) -> AcceleratorJobStatus:
        descriptor = ArtifactDescriptor(
            object_key=request.result_upload_capability.object_key,
            sha256=sha256(raw_artifact).hexdigest(),
            size_bytes=len(raw_artifact),
            mime_type="application/json",
            schema_version="surya_layout_artifact/v1",
        )
        manifest = self._result_manifest(
            request,
            event_id,
            started_at,
            completed_at,
            state=AcceleratorJobState.SUCCEEDED,
            result_artifact=descriptor,
        )
        return AcceleratorJobStatus(
            external_job_id=event_id,
            state=AcceleratorJobState.SUCCEEDED,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
            result_manifest=manifest,
        )

    def _build_artifact(
        self,
        request: SuryaLayoutRequest,
        *,
        deadline: float,
    ) -> SuryaLayoutArtifact:
        render_manifest = self._load_render_manifest(request, deadline=deadline)
        pages: list[SuryaLayoutPage] = []
        requested_pages = request.identity.page_range.page_numbers
        pages_by_number = {page.page: page for page in render_manifest.pages}
        estimated_artifact_bytes = _artifact_empty_pages_size(
            request=request,
            requested_pages=requested_pages,
            producer=self._policy.producer,
        )
        self._assert_artifact_size(estimated_artifact_bytes, request=request)
        total_regions = 0

        for image in request.identity.page_images:
            self._assert_before_deadline(deadline)
            if image.size_bytes > MAX_PNG_FILE_BYTES:
                # PNG parsing has the same hard compressed-byte cap.  Refuse
                # the declared object before a signed URL can be dereferenced.
                raise _ContentFailure("page_png_exceeds_download_cap")
            rendered = pages_by_number.get(image.page_number)
            if rendered is None:
                raise _ContentFailure("render_manifest_page_missing")
            png_bytes = self._read_page(
                image.capability,
                image.size_bytes,
                deadline=deadline,
                max_download_bytes=MAX_PNG_FILE_BYTES,
            )
            try:
                digest, size_bytes, width, height = inspect_png_bytes(png_bytes)
            except (TypeError, ValueError, PdfRenderManifestError) as error:
                raise _ContentFailure("page_png_invalid") from error
            if (
                digest != image.page_image_sha256
                or size_bytes != image.size_bytes
                or digest != rendered.image_sha256
                or size_bytes != rendered.image_size_bytes
                or width != image.pixel_width
                or height != image.pixel_height
                or width != rendered.coordinate_manifest.rendered_width_px
                or height != rendered.coordinate_manifest.rendered_height_px
            ):
                raise _ContentFailure("page_png_binding_invalid")
            if dict(image.sidecar_binding.model_dump()) != build_sidecar_binding(
                rendered.coordinate_manifest
            ):
                raise _ContentFailure("page_sidecar_binding_invalid")
            try:
                self._assert_before_deadline(deadline)
                page_result = self._inference.layout(
                    page_number=image.page_number,
                    png_bytes=png_bytes,
                    pixel_width=width,
                    pixel_height=height,
                    deadline_monotonic=deadline,
                )
            except InferenceContentFailure as error:
                self._assert_before_deadline(deadline)
                raise _ContentFailure("layout_inference_rejected") from error
            except InferenceInfrastructureFailure as error:
                self._assert_before_deadline(deadline)
                raise _InfrastructureFailure("layout_inference_failed") from error
            except Exception as error:
                self._assert_before_deadline(deadline)
                raise _InfrastructureFailure("layout_inference_failed") from error
            self._assert_before_deadline(deadline)
            if not isinstance(page_result, SuryaLayoutPageResult):
                raise _ContentFailure("layout_result_invalid")
            if not isinstance(page_result.error, bool):
                raise _ContentFailure("layout_result_invalid")
            if page_result.error:
                # Official Surya marks an internal page/runtime failure with
                # ``error=True``.  It is not evidence that the caller's page
                # is malformed, so preserve retry/reconciliation semantics.
                raise _InfrastructureFailure("layout_page_error")
            try:
                image_bbox = _bbox(page_result.image_bbox)
            except _ContentFailure as error:
                raise _ContentFailure("layout_image_bbox_invalid") from error
            if image_bbox != (0.0, 0.0, float(width), float(height)):
                raise _ContentFailure("layout_canvas_mismatch")
            try:
                regions = _canonical_regions(
                    page_number=image.page_number,
                    raw_regions=page_result.regions,
                    pixel_width=width,
                    pixel_height=height,
                    max_regions=self._policy.max_regions_per_page,
                    preserve_confidence=_has_pinned_model_confidence(
                        page_result.confidence_marker,
                        self._policy.producer,
                    ),
                )
                total_regions += len(regions)
                if total_regions > MAX_TOTAL_REGIONS:
                    raise _ContentFailure("layout_total_region_count_exceeds_policy")
                page = SuryaLayoutPage(
                    page=image.page_number,
                    sidecar_binding=build_sidecar_binding(rendered.coordinate_manifest),
                    pixel_width=width,
                    pixel_height=height,
                    rendered_page_px=(0.0, 0.0, float(width), float(height)),
                    regions=regions,
                )
                page_bytes = _canonical_json_bytes(page.to_dict())
                # The empty artifact already includes ``"pages":[]``.  Each
                # page adds its canonical bytes and, after the first, one
                # comma.  This is the exact final wire-size growth, checked
                # while only one page's temporary mapping is resident.
                estimated_artifact_bytes += len(page_bytes) + (1 if pages else 0)
                self._assert_artifact_size(estimated_artifact_bytes, request=request)
                pages.append(page)
                self._assert_before_deadline(deadline)
            except SuryaLayoutArtifactError as error:
                raise _ContentFailure("layout_geometry_invalid") from error

        try:
            return SuryaLayoutArtifact(
                logical_compute_key=request.logical_compute_key,
                source_sha256=request.identity.source_sha256,
                page_count=request.identity.source_page_count,
                render_manifest_schema_version=request.identity.render_manifest.schema_version,
                render_manifest_sha256=request.identity.render_manifest.render_manifest_sha256,
                producer=_portable_producer(self._policy.producer),
                requested_pages=requested_pages,
                pages=tuple(pages),
            )
        except SuryaLayoutArtifactError as error:
            raise _ContentFailure("layout_artifact_invalid") from error

    def _assert_artifact_size(
        self,
        size_bytes: int,
        *,
        request: SuryaLayoutRequest,
    ) -> None:
        if size_bytes > MAX_ARTIFACT_BYTES:
            raise _ContentFailure("layout_artifact_exceeds_portable_cap")
        if (
            size_bytes > request.resource_caps.max_output_bytes
            or size_bytes > request.result_upload_capability.resource_caps.max_bytes
        ):
            raise _ContentFailure("result_artifact_exceeds_cap")
        if size_bytes > self._policy.max_output_bytes:
            raise _ContentFailure("result_artifact_exceeds_worker_cap")

    def _load_render_manifest(
        self,
        request: SuryaLayoutRequest,
        *,
        deadline: float,
    ) -> PdfRenderManifest:
        source = request.identity.render_manifest
        manifest_byte_cap = _render_manifest_max_bytes(request.identity.source_page_count)
        if source.size_bytes > manifest_byte_cap:
            raise _ContentFailure("render_manifest_exceeds_download_cap")
        raw = self._read_page(
            source.capability,
            source.size_bytes,
            deadline=deadline,
            max_download_bytes=manifest_byte_cap,
        )
        if sha256(raw).hexdigest() != source.render_manifest_sha256 or len(raw) != source.size_bytes:
            raise _ContentFailure("render_manifest_binding_invalid")
        try:
            decoded = json.loads(raw.decode("utf-8"))
            manifest = PdfRenderManifest.from_dict(decoded)
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError, PdfRenderManifestError) as error:
            raise _ContentFailure("render_manifest_invalid") from error
        if raw != manifest.canonical_json():
            raise _ContentFailure("render_manifest_not_canonical")
        identity = request.identity
        if (
            manifest.schema_version != source.schema_version
            or manifest.manifest_sha256() != source.render_manifest_sha256
            or manifest.source_pdf_sha256 != identity.source_sha256
            or manifest.page_count != identity.source_page_count
        ):
            raise _ContentFailure("render_manifest_binding_invalid")
        return manifest

    def _read_page(
        self,
        capability: SignedStorageCapability,
        expected_size: int,
        *,
        deadline: float,
        max_download_bytes: int | None = None,
    ) -> bytes:
        self._assert_before_deadline(deadline)
        if max_download_bytes is not None and expected_size > max_download_bytes:
            raise _ContentFailure("input_download_exceeds_cap")
        try:
            raw = self._storage.get(
                capability,
                max_bytes=(
                    min(expected_size, max_download_bytes)
                    if max_download_bytes is not None
                    else expected_size
                ),
                deadline_monotonic=deadline,
            )
        except StorageContentFailure as error:
            self._assert_before_deadline(deadline)
            raise _ContentFailure("input_download_rejected") from error
        except StorageInfrastructureFailure as error:
            self._assert_before_deadline(deadline)
            raise _InfrastructureFailure("input_download_failed") from error
        except Exception as error:
            self._assert_before_deadline(deadline)
            raise _InfrastructureFailure("input_download_failed") from error
        self._assert_before_deadline(deadline)
        if not isinstance(raw, bytes):
            raise _InfrastructureFailure("input_download_invalid")
        if len(raw) != expected_size:
            raise _ContentFailure("input_size_mismatch")
        if sha256(raw).hexdigest() != capability.expected_sha256:
            raise _ContentFailure("input_hash_mismatch")
        return raw

    def _terminal_failure(
        self,
        request: SuryaLayoutRequest,
        event_id: str,
        started_at: datetime,
        state: AcceleratorJobState,
        reason_code: str,
    ) -> dict[str, object]:
        completed_at = _safe_now(self._now, not_before=started_at)
        manifest = self._result_manifest(
            request,
            event_id,
            started_at,
            completed_at,
            state=state,
            reason_code=reason_code,
        )
        status = AcceleratorJobStatus(
            external_job_id=event_id,
            state=state,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
            result_manifest=manifest,
            reason_code=reason_code,
        )
        terminal_wire = self._bounded_terminal_wire(status, event_id)
        if terminal_wire is None:
            return self._compact_failure("terminal_output_exceeds_cap")
        return terminal_wire

    def _compact_failure(self, reason_code: str) -> dict[str, object]:
        """Return the bounded, redacted fallback without an external job id.

        An otherwise-valid RunPod id can be 128 characters, so including it in
        a fallback would make the fallback depend on untrusted request size.
        The policy minimum reserves enough room for this fixed two-field
        envelope; never truncate values or leak an input id to make it fit.
        """

        compact = _early_failure(reason_code)
        if _wire_json_size(compact) <= self._policy.max_terminal_output_bytes:
            return compact
        # ``RunPodSuryaExecutionPolicy`` currently rejects any cap below 192,
        # so this branch is defensive against post-construction tampering.
        minimal = {"state": AcceleratorJobState.CONTENT_FAILED.value}
        if _wire_json_size(minimal) <= self._policy.max_terminal_output_bytes:
            return minimal
        # A malformed in-memory policy cannot be made safe by echoing input.
        # This is the smallest JSON object the handler can return.
        return {}

    def _bounded_terminal_wire(
        self,
        status: AcceleratorJobStatus,
        event_id: str,
    ) -> dict[str, object] | None:
        """Serialize a terminal status only when it fits the worker cap.

        Terminal status JSON has a separate deployment limit from the uploaded
        artifact.  Returning ``None`` lets callers select the constant-size,
        signed-URL-free compact envelope without attempting an unsafe truncate.
        """

        wire = _status_wire(status)
        if _wire_json_size(wire) <= self._policy.max_terminal_output_bytes:
            return wire
        del event_id  # Documents that compact output never needs request data.
        return None

    def _result_manifest(
        self,
        request: SuryaLayoutRequest,
        event_id: str,
        started_at: datetime,
        completed_at: datetime,
        *,
        state: AcceleratorJobState,
        result_artifact: ArtifactDescriptor | None = None,
        reason_code: str | None = None,
    ) -> SuryaLayoutResultArtifactManifest:
        identity = request.identity
        return SuryaLayoutResultArtifactManifest(
            external_job_id=event_id,
            status=state.value,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
            result_artifact=result_artifact,
            source_sha256=identity.source_sha256,
            source_page_count=identity.source_page_count,
            page_range=identity.page_range,
            render_manifest_schema_version=identity.render_manifest.schema_version,
            render_manifest_sha256=identity.render_manifest.render_manifest_sha256,
            page_images=tuple(page.sidecar_binding for page in identity.page_images),
            producer=self._policy.producer,
            started_at=started_at,
            completed_at=completed_at,
            reason_code=reason_code,
        )


def handle_event(
    event: Mapping[str, object],
    *,
    storage: StoragePort,
    inference: InferencePort,
    policy: RunPodSuryaExecutionPolicy,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> dict[str, object]:
    """Functional entrypoint convenient for a future ``runpod.serverless`` adapter."""

    return RunPodSuryaLayoutHandler(
        storage=storage,
        inference=inference,
        policy=policy,
        now=now,
        monotonic=monotonic,
    ).handle(event)


def _event_id_or_none(event: Mapping[str, object]) -> str | None:
    if not isinstance(event, Mapping):
        return None
    event_id = event.get("id")
    if not isinstance(event_id, str) or _EXTERNAL_JOB_ID.fullmatch(event_id) is None:
        return None
    return event_id


def _early_failure(reason_code: str) -> dict[str, object]:
    # No request identity is available, so constructing AcceleratorJobStatus
    # would be a lie.  This small envelope is intentionally provider-shaped.
    return {
        "state": AcceleratorJobState.CONTENT_FAILED.value,
        "reason_code": reason_code,
    }


def _raw_request_preflight(
    payload: Mapping[str, object],
    policy: RunPodSuryaExecutionPolicy,
) -> None:
    """Reject oversized wire shapes before Pydantic expands or copies them.

    In particular, ``LayoutComputeIdentity`` turns a valid page range into a
    tuple to compare it with page images.  This intentionally tiny structural
    check makes the range/list/cap ceilings deployment-owned *before* that
    allocation occurs.  It reads only bounded mappings and a list no longer
    than the policy page limit; it never parses or reports a signed URL.
    """

    identity = _raw_mapping(payload.get("identity"))
    page_range = _raw_mapping(identity.get("page_range"))
    start = _raw_positive_int(page_range.get("start_page"))
    end = _raw_positive_int(page_range.get("end_page"))
    if end < start or end - start + 1 > policy.max_page_count:
        raise _ContentFailure("raw_page_range_exceeds_policy")

    pages = identity.get("page_images")
    if not isinstance(pages, (list, tuple)):
        raise _ContentFailure("raw_page_list_invalid")
    if len(pages) > policy.max_page_count or len(pages) != end - start + 1:
        raise _ContentFailure("raw_page_list_exceeds_policy")

    resource_caps = _raw_mapping(payload.get("resource_caps"))
    _raw_ceiling(
        resource_caps.get("max_page_count"), policy.max_page_count
    )
    _raw_ceiling(
        resource_caps.get("max_total_input_bytes"), policy.max_total_input_bytes
    )
    _raw_ceiling(
        resource_caps.get("max_total_rendered_pixels"),
        policy.max_total_rendered_pixels,
    )
    _raw_ceiling(resource_caps.get("max_output_bytes"), policy.max_output_bytes)
    _raw_ceiling(
        resource_caps.get("execution_timeout_seconds"),
        policy.max_execution_timeout_seconds,
    )
    _raw_ceiling(resource_caps.get("ttl_seconds"), policy.max_ttl_seconds)

    render_manifest = _raw_mapping(identity.get("render_manifest"))
    total_input_bytes = _raw_bounded_artifact(
        render_manifest,
        policy=policy,
        pixels=None,
    )
    total_pixels = 0
    for raw_page in pages:
        page = _raw_mapping(raw_page)
        width = _raw_positive_int(page.get("pixel_width"))
        height = _raw_positive_int(page.get("pixel_height"))
        pixels = width * height
        if pixels > policy.max_total_rendered_pixels:
            raise _ContentFailure("raw_page_pixels_exceed_policy")
        total_pixels += pixels
        total_input_bytes += _raw_bounded_artifact(
            page,
            policy=policy,
            pixels=pixels,
        )
        if (
            total_input_bytes > policy.max_total_input_bytes
            or total_pixels > policy.max_total_rendered_pixels
        ):
            raise _ContentFailure("raw_aggregate_exceeds_policy")

    result_capability = _raw_mapping(payload.get("result_upload_capability"))
    _raw_capability_bytes(result_capability, policy)


def _raw_bounded_artifact(
    value: Mapping[str, object],
    *,
    policy: RunPodSuryaExecutionPolicy,
    pixels: int | None,
) -> int:
    del pixels  # The caller validates page geometry; manifests have no pixels.
    size_bytes = _raw_positive_int(value.get("size_bytes"))
    if size_bytes > policy.max_capability_bytes:
        raise _ContentFailure("raw_input_bytes_exceed_policy")
    capability = _raw_mapping(value.get("capability"))
    _raw_capability_bytes(capability, policy)
    return size_bytes


def _raw_capability_bytes(
    capability: Mapping[str, object],
    policy: RunPodSuryaExecutionPolicy,
) -> None:
    resource_caps = _raw_mapping(capability.get("resource_caps"))
    _raw_ceiling(resource_caps.get("max_bytes"), policy.max_capability_bytes)


def _raw_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _ContentFailure("raw_mapping_invalid")
    return value


def _raw_positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _ContentFailure("raw_integer_invalid")
    return value


def _raw_ceiling(value: object, ceiling: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ContentFailure("raw_cap_invalid")
    if value > ceiling:
        raise _ContentFailure("raw_cap_exceeds_policy")


def _safe_now(clock: Callable[[], datetime], *, not_before: datetime | None = None) -> datetime:
    try:
        current = clock()
    except Exception:
        current = datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        current = datetime.now(UTC)
    if not_before is not None and current < not_before:
        return not_before
    return current


def _wire_json_size(value: Mapping[str, object]) -> int:
    """Measure the actual compact JSON wire representation without logging it."""

    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _portable_producer(producer: SuryaProducerIdentity) -> PortableSuryaProducerIdentity:
    return PortableSuryaProducerIdentity(
        engine_id=producer.engine_id,
        engine_version=producer.engine_version,
        model_id=producer.model_id,
        model_revision=producer.model_revision,
        model_weights_sha256=producer.model_weights_sha256,
        pipeline_revision=producer.pipeline_revision,
        config_sha256=producer.config_sha256,
        worker_image_digest=producer.worker_image_digest,
    )


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        raise _ContentFailure("layout_artifact_invalid") from None


def _artifact_empty_pages_size(
    *,
    request: SuryaLayoutRequest,
    requested_pages: Sequence[int],
    producer: SuryaProducerIdentity,
) -> int:
    """Return exact canonical artifact bytes before inserting page objects."""

    return len(
        _canonical_json_bytes(
            {
                "schema_version": SURYA_LAYOUT_ARTIFACT_SCHEMA_VERSION,
                "logical_compute_key": request.logical_compute_key,
                "source_sha256": request.identity.source_sha256,
                "page_count": request.identity.source_page_count,
                "render_manifest_schema_version": request.identity.render_manifest.schema_version,
                "render_manifest_sha256": request.identity.render_manifest.render_manifest_sha256,
                "producer": _portable_producer(producer).to_dict(),
                "requested_pages": list(requested_pages),
                "pages": [],
            }
        )
    )


def _canonical_regions(
    *,
    page_number: int,
    raw_regions: object,
    pixel_width: int,
    pixel_height: int,
    max_regions: int,
    preserve_confidence: bool,
) -> tuple[SuryaLayoutRegion, ...]:
    if isinstance(raw_regions, (str, bytes)) or not isinstance(raw_regions, Sequence):
        raise _ContentFailure("layout_result_invalid")
    if len(raw_regions) > max_regions:
        raise _ContentFailure("layout_region_count_exceeds_policy")
    candidates: list[
        tuple[
            str,
            tuple[float, float, float, float],
            tuple[tuple[float, float], ...],
            float | None,
        ]
    ] = []
    for raw_index, raw in enumerate(raw_regions):
        if not isinstance(raw, Mapping):
            raise _ContentFailure("layout_region_invalid")
        label = raw.get("label")
        if not isinstance(label, str) or label not in _RAW_LABEL_TO_V1:
            raise _ContentFailure("layout_label_unsupported")
        polygon_value = raw.get("polygon")
        bbox_value = raw.get("bbox")
        if polygon_value is not None:
            polygon = _four_point_polygon(polygon_value)
            bbox = _polygon_bbox(polygon)
            if bbox_value is not None and not _bbox_matches(bbox, _bbox(bbox_value)):
                raise _ContentFailure("layout_geometry_inconsistent")
        elif bbox_value is not None:
            bbox = _bbox(bbox_value)
            polygon = ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3]))
        else:
            raise _ContentFailure("layout_geometry_missing")
        position = raw.get("position")
        if isinstance(position, bool) or not isinstance(position, int) or position != raw_index:
            raise _ContentFailure("layout_position_invalid")
        confidence = (
            _confidence(raw.get("confidence"))
            if preserve_confidence and "confidence" in raw
            else None
        )
        if not _within_canvas(bbox, pixel_width, pixel_height) or not all(
            0.0 <= x <= float(pixel_width) and 0.0 <= y <= float(pixel_height)
            for x, y in polygon
        ):
            raise _ContentFailure("layout_geometry_out_of_bounds")
        candidates.append((_RAW_LABEL_TO_V1[label], bbox, polygon, confidence))

    regions: list[SuryaLayoutRegion] = []
    for order, (label, bbox, polygon, confidence) in enumerate(candidates):
        regions.append(
            SuryaLayoutRegion(
                region_id=f"p{page_number:04d}-{label}-{order + 1:04d}",
                label=label,
                bbox_px=bbox,
                polygon_px=polygon,  # portable class normalizes orientation deterministically
                confidence=confidence,
                reading_order=order,
            )
        )
    # ``layout.bboxes`` already expresses Surya reading order.  Only assign
    # contiguous deterministic IDs; do not replace that semantic order with
    # a geometric y/x heuristic.
    return tuple(regions)


def _has_pinned_model_confidence(
    marker: SuryaModelConfidenceMarker | None,
    producer: SuryaProducerIdentity,
) -> bool:
    """Accept confidence only with the explicit matching pinned-model marker."""

    return (
        isinstance(marker, SuryaModelConfidenceMarker)
        and marker.source == "surya_layout_model"
        and isinstance(marker.producer, SuryaProducerIdentity)
        and marker.producer.model_dump(mode="json") == producer.model_dump(mode="json")
    )


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _ContentFailure("layout_geometry_invalid")
    converted = float(value)
    if converted != converted or converted in {float("inf"), float("-inf")}:
        raise _ContentFailure("layout_geometry_invalid")
    return 0.0 if converted == 0.0 else converted


def _confidence(value: object) -> float:
    """Preserve only a predictor-supplied finite confidence; never estimate it."""

    confidence = _finite(value)
    if not 0.0 <= confidence <= 1.0:
        raise _ContentFailure("layout_confidence_invalid")
    return confidence


def _bbox(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise _ContentFailure("layout_geometry_invalid")
    x0, y0, x1, y1 = tuple(_finite(item) for item in value)
    if not x0 < x1 or not y0 < y1:
        raise _ContentFailure("layout_geometry_invalid")
    return x0, y0, x1, y1


def _four_point_polygon(value: object) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise _ContentFailure("layout_geometry_invalid")
    points: list[tuple[float, float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise _ContentFailure("layout_geometry_invalid")
        points.append((_finite(point[0]), _finite(point[1])))
    if len(set(points)) != 4:
        raise _ContentFailure("layout_geometry_invalid")
    return tuple(points)


def _polygon_bbox(points: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)


def _within_canvas(bbox: tuple[float, float, float, float], width: int, height: int) -> bool:
    return 0.0 <= bbox[0] < bbox[2] <= float(width) and 0.0 <= bbox[1] < bbox[3] <= float(height)


def _bbox_matches(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tolerance: float = 0.001,
) -> bool:
    """Accept only the small rounding difference allowed by v1 milli-pixels."""

    return all(abs(a - b) <= tolerance for a, b in zip(left, right))


def _status_wire(status: AcceleratorJobStatus) -> dict[str, object]:
    # model_dump cannot contain a raw signed URL because result models contain
    # no capability; keeping one explicit point documents that wire guarantee.
    return status.model_dump(mode="json")
