"""One-step, Existing-PDF-only acceptance coordinator for Surya acceleration.

This module intentionally has no runtime wiring.  It turns one provider
submit/poll response into a small local outcome while keeping the trusted EC2
worker in charge of the lease fence and artifact acceptance.  In particular,
the coordinator never follows URLs, sleeps, loops, deletes provider objects,
or retries a provider operation on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import re
from typing import Protocol

from common_ir_pipeline.pdf_fusion.coordinate_manifest import build_sidecar_binding
from common_ir_pipeline.pdf_fusion.render_manifest import PdfRenderManifest
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    MAX_ARTIFACT_BYTES,
    SuryaLayoutArtifact,
    SuryaProducerIdentity as PortableSuryaProducerIdentity,
    parse_surya_layout_artifact_bytes,
)

from worker.contracts.accelerator import (
    AcceleratorContractError,
    AcceleratorJobState,
    AcceleratorJobStatus,
    SuryaLayoutReconciliationHandle,
    SuryaLayoutRequest,
    SuryaProducerIdentity,
    validate_accelerator_result_acceptance,
)
from worker.ports.accelerator import AcceleratorJobNotFoundError, AcceleratorPort


__all__ = [
    "AcceleratorArtifactReader",
    "ArtifactNotFound",
    "ArtifactRead",
    "ArtifactReadLimitExceeded",
    "CoordinatorDisposition",
    "ExistingPdfSuryaCoordinator",
    "ExistingPdfSuryaOutcome",
    "FenceProbe",
]


@dataclass(frozen=True, slots=True)
class ArtifactRead:
    """Bytes and exact response metadata from one bounded trusted-object read."""

    content: bytes
    content_type: str


class ArtifactReadLimitExceeded(Exception):
    """The trusted object exceeded the mandatory bounded-read ceiling."""


class ArtifactNotFound(Exception):
    """The exact trusted output key does not exist.

    This is intentionally distinct from a storage outage.  Callers may safely
    continue the provider lifecycle after this observation, whereas every
    other reader exception remains retryable infrastructure failure.
    """


class AcceleratorArtifactReader(Protocol):
    """Read one trusted output object without URLs and with a non-negotiable cap.

    Implementations must fail rather than truncate when the object exceeds
    ``max_bytes``.  The returned ``content_type`` is the storage response's
    exact media type, not an inferred filename or a caller supplied default.
    """

    def read(
        self,
        bucket: str,
        object_key: str,
        *,
        max_bytes: int,
    ) -> ArtifactRead: ...


class FenceProbe(Protocol):
    """The worker's current lease/fence authority, sampled around every I/O."""

    def is_current(self) -> bool: ...


class CoordinatorDisposition(StrEnum):
    """Local handling result, deliberately separate from provider wire states."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    CONTENT_FAILED = "content_failed"
    INFRA_RETRYABLE = "infra_retryable"
    CANCELLED = "cancelled"
    FENCE_LOST = "fence_lost"


@dataclass(frozen=True, slots=True)
class ExistingPdfSuryaOutcome:
    """Safe result of exactly one coordinator advance.

    It intentionally omits signed URLs, storage paths, buckets, and provider
    result manifests.  A successfully parsed portable layout artifact is the
    only artifact that can cross this boundary.
    """

    disposition: CoordinatorDisposition
    external_job_id: str | None = None
    artifact: SuryaLayoutArtifact | None = None
    reason_code: str | None = None
    provider_state: AcceleratorJobState | None = None

    def __post_init__(self) -> None:
        if self.external_job_id is not None and not _is_safe_external_job_id(
            self.external_job_id
        ):
            raise ValueError("coordinator outcome external_job_id is not safe")
        if self.reason_code is not None and _safe_provider_reason(self.reason_code) is None:
            raise ValueError("coordinator outcome reason_code is not safe")
        if self.disposition is CoordinatorDisposition.SUCCEEDED:
            if self.artifact is None or self.external_job_id is None:
                raise ValueError("a succeeded coordinator outcome requires artifact and job ID")
        elif self.artifact is not None:
            raise ValueError("only a succeeded coordinator outcome may expose an artifact")
        if self.disposition is CoordinatorDisposition.FENCE_LOST and self.provider_state is not None:
            raise ValueError("a local fence outcome cannot retain a provider state")


class ExistingPdfSuryaCoordinator:
    """Advance one Existing-PDF Surya job by one primary submit-or-poll operation.

    ``replacement_authorized`` is a caller-owned, persisted policy decision.
    A true value means the caller already persisted a prior missing or
    ``infra_retryable`` observation and authorizes this *later invocation* to
    submit a replacement directly.  The coordinator never polls and submits in
    the same invocation, and never retries a provider operation on its own.
    The coordinator never cancels a provider job.  A local fence probe cannot
    make a later remote cancel atomic with lease ownership, so cleanup is left
    to a holder with a provider-enforced fencing mechanism.
    """

    def __init__(
        self,
        *,
        accelerator: AcceleratorPort,
        artifact_reader: AcceleratorArtifactReader,
        fence: FenceProbe,
    ) -> None:
        self._accelerator = accelerator
        self._artifact_reader = artifact_reader
        self._fence = fence

    def advance(
        self,
        request: SuryaLayoutRequest,
        trusted_render_manifest: PdfRenderManifest,
        prior_external_job_id: str | None = None,
        *,
        replacement_authorized: bool = False,
    ) -> ExistingPdfSuryaOutcome:
        """Perform one submit or poll, then accept a terminal success if present."""

        if type(replacement_authorized) is not bool:
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                reason="replacement_authorization_invalid",
            )

        try:
            _bind_request_to_trusted_render_manifest(request, trusted_render_manifest)
            portable_producer = _portable_producer(request)
        except (TypeError, ValueError):
            return _outcome(CoordinatorDisposition.CONTENT_FAILED, reason="request_render_binding_invalid")

        if prior_external_job_id is not None and not _is_safe_external_job_id(prior_external_job_id):
            return _outcome(CoordinatorDisposition.CONTENT_FAILED, reason="prior_job_id_invalid")
        if replacement_authorized is True and prior_external_job_id is None:
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                reason="replacement_authorization_invalid",
            )

        if not self._fence_is_current():
            if prior_external_job_id is not None:
                return self._fence_lost_after_stale(prior_external_job_id)
            return _outcome(CoordinatorDisposition.FENCE_LOST, reason="fence_lost")

        if prior_external_job_id is None:
            status = self._provider_submit(request)
            if isinstance(status, ExistingPdfSuryaOutcome):
                return status
            return self._from_status(request, trusted_render_manifest, portable_producer, status)

        if replacement_authorized is True:
            replacement = self._provider_submit(
                request,
                known_cancel_job_id=prior_external_job_id,
            )
            if isinstance(replacement, ExistingPdfSuryaOutcome):
                return replacement
            return self._from_status(request, trusted_render_manifest, portable_producer, replacement)

        status = self._provider_poll(request, prior_external_job_id)
        if isinstance(status, ExistingPdfSuryaOutcome):
            return status
        return self._from_status(request, trusted_render_manifest, portable_producer, status)

    def reconcile_result_artifact(
        self,
        handle: SuryaLayoutReconciliationHandle,
        *,
        external_job_id: str,
    ) -> ExistingPdfSuryaOutcome:
        """Accept an already-written deterministic result without provider status.

        A Pod may complete its create-only PUT and die before it writes a
        durable terminal journal/status.  This one-step repair path reads only
        the trusted, deterministic output key from a credential-free handle
        persisted before dispatch.  It never needs the original request,
        polls the provider, follows a signed URL, consumes an untrusted result
        descriptor, or mutates storage.  ``external_job_id`` must be the
        caller's previously persisted provider ID and must exactly equal the
        handle's logical compute key, as required by the persistent adapter's
        idempotent job contract.  It is not learned from a remote response
        here.

        ``PENDING/artifact_not_found`` means no immutable result exists yet
        and lets the caller continue its normal lifecycle.  Any existing but
        malformed or mismatched object is terminal ``CONTENT_FAILED`` so it is
        never silently overwritten.
        """

        try:
            if not isinstance(handle, SuryaLayoutReconciliationHandle):
                raise TypeError(
                    "handle must be a SuryaLayoutReconciliationHandle"
                )
            handle._assert_live_integrity()
            trusted_render_manifest = handle.trusted_render_manifest
            portable_producer = _portable_producer_identity(handle.producer)
        except (TypeError, ValueError):
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                reason="reconciliation_handle_invalid",
            )
        try:
            external_job_id = handle.validate_external_job_id(external_job_id)
        except (TypeError, ValueError):
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                reason="reconciliation_job_id_mismatch",
            )

        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)

        hard_cap = min(handle.max_output_bytes, MAX_ARTIFACT_BYTES)
        try:
            read = self._artifact_reader.read(
                handle.result_bucket,
                handle.result_object_key,
                max_bytes=hard_cap,
            )
        except ArtifactNotFound:
            if not self._fence_is_current():
                return self._fence_lost_after_stale(external_job_id)
            return _outcome(
                CoordinatorDisposition.PENDING,
                external_job_id=external_job_id,
                reason="artifact_not_found",
            )
        except ArtifactReadLimitExceeded:
            if not self._fence_is_current():
                return self._fence_lost_after_stale(external_job_id)
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                external_job_id=external_job_id,
                reason="artifact_size_cap_exceeded",
            )
        except Exception:
            if not self._fence_is_current():
                return self._fence_lost_after_stale(external_job_id)
            return _outcome(
                CoordinatorDisposition.INFRA_RETRYABLE,
                external_job_id=external_job_id,
                reason="artifact_read_failed",
            )

        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)
        if not isinstance(read, ArtifactRead) or not isinstance(read.content, bytes):
            return self._content_failure_after_reconciliation_read(
                external_job_id, "artifact_read_invalid"
            )
        if len(read.content) > hard_cap:
            return self._content_failure_after_reconciliation_read(
                external_job_id, "artifact_size_cap_exceeded"
            )
        if _normalized_media_type(read.content_type) != handle.artifact_content_type:
            return self._content_failure_after_reconciliation_read(
                external_job_id, "artifact_content_type_mismatch"
            )

        try:
            artifact = parse_surya_layout_artifact_bytes(
                read.content,
                render_manifest=trusted_render_manifest,
                expected_logical_compute_key=handle.logical_compute_key,
                expected_producer=portable_producer,
                expected_requested_pages=tuple(
                    page.page for page in trusted_render_manifest.pages
                ),
            )
        except Exception:
            return self._content_failure_after_reconciliation_read(
                external_job_id, "artifact_parse_failed"
            )
        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)
        return ExistingPdfSuryaOutcome(
            disposition=CoordinatorDisposition.SUCCEEDED,
            external_job_id=external_job_id,
            artifact=artifact,
        )

    def _provider_submit(
        self,
        request: SuryaLayoutRequest,
        *,
        known_cancel_job_id: str | None = None,
    ) -> AcceleratorJobStatus | ExistingPdfSuryaOutcome:
        if not self._fence_is_current():
            if known_cancel_job_id is not None:
                return self._fence_lost_after_stale(known_cancel_job_id)
            return _outcome(CoordinatorDisposition.FENCE_LOST, reason="fence_lost")
        try:
            status = self._accelerator.submit(request)
        except AcceleratorContractError:
            if not self._fence_is_current():
                if known_cancel_job_id is not None:
                    return self._fence_lost_after_stale(known_cancel_job_id)
                return _outcome(CoordinatorDisposition.FENCE_LOST, reason="fence_lost")
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                reason="provider_submit_contract_invalid",
            )
        except Exception:
            if not self._fence_is_current():
                if known_cancel_job_id is not None:
                    return self._fence_lost_after_stale(known_cancel_job_id)
                return _outcome(CoordinatorDisposition.FENCE_LOST, reason="fence_lost")
            return _outcome(CoordinatorDisposition.INFRA_RETRYABLE, reason="provider_submit_failed")
        return self._validate_provider_response(
            request,
            status,
            known_cancel_job_id=known_cancel_job_id,
        )

    def _provider_poll(
        self,
        request: SuryaLayoutRequest,
        external_job_id: str,
    ) -> AcceleratorJobStatus | ExistingPdfSuryaOutcome:
        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)
        try:
            status = self._accelerator.get_status(external_job_id)
        except AcceleratorJobNotFoundError:
            if not self._fence_is_current():
                return self._fence_lost_after_stale(external_job_id)
            return _outcome(
                CoordinatorDisposition.INFRA_RETRYABLE,
                external_job_id=external_job_id,
                reason="provider_job_not_found",
            )
        except Exception:
            if not self._fence_is_current():
                return self._fence_lost_after_stale(external_job_id)
            return _outcome(
                CoordinatorDisposition.INFRA_RETRYABLE,
                external_job_id=external_job_id,
                reason="provider_poll_failed",
            )
        return self._validate_provider_response(
            request,
            status,
            expected_external_job_id=external_job_id,
            known_cancel_job_id=external_job_id,
        )

    def _validate_provider_response(
        self,
        request: SuryaLayoutRequest,
        status: object,
        *,
        expected_external_job_id: str | None = None,
        known_cancel_job_id: str | None = None,
    ) -> AcceleratorJobStatus | ExistingPdfSuryaOutcome:
        """Validate every status before it can select a local disposition."""

        failure: ExistingPdfSuryaOutcome | None = None
        cancel_job_id: str | None = None
        if not isinstance(status, AcceleratorJobStatus):
            failure = _outcome(CoordinatorDisposition.CONTENT_FAILED, reason="provider_status_malformed")
        else:
            try:
                # A future adapter may retain or mutate a model instance after
                # its original construction.  Rebuild it from plain data so
                # every field and nested manifest crosses validation again.
                status = AcceleratorJobStatus.model_validate(
                    status.model_dump(mode="python", warnings="none")
                )
            except Exception:
                failure = _outcome(
                    CoordinatorDisposition.CONTENT_FAILED,
                    external_job_id=(
                        expected_external_job_id
                        if _is_safe_external_job_id(expected_external_job_id)
                        else None
                    ),
                    reason="provider_status_malformed",
                )
            else:
                if not _is_safe_external_job_id(status.external_job_id):
                    failure = _outcome(
                        CoordinatorDisposition.CONTENT_FAILED,
                        external_job_id=(
                            expected_external_job_id
                            if _is_safe_external_job_id(expected_external_job_id)
                            else None
                        ),
                        reason="provider_status_malformed",
                    )
                elif (
                    status.logical_compute_key != request.logical_compute_key
                    or status.request_digest != request.request_digest
                    or (
                        expected_external_job_id is not None
                        and status.external_job_id != expected_external_job_id
                    )
                ):
                    failure = _outcome(
                        CoordinatorDisposition.CONTENT_FAILED,
                        external_job_id=(
                            expected_external_job_id
                            if _is_safe_external_job_id(expected_external_job_id)
                            else None
                        ),
                        reason="provider_status_mismatch",
                    )
                else:
                    cancel_job_id = status.external_job_id

        # The post-call sample is mandatory even when a provider response is
        # malformed.  Never dereference an untrusted response while stale.
        if not self._fence_is_current():
            return self._fence_lost_after_stale(
                cancel_job_id or known_cancel_job_id
            )
        if failure is not None:
            return failure
        assert isinstance(status, AcceleratorJobStatus)
        return status

    def _from_status(
        self,
        request: SuryaLayoutRequest,
        trusted_render_manifest: PdfRenderManifest,
        portable_producer: PortableSuryaProducerIdentity,
        status: AcceleratorJobStatus,
    ) -> ExistingPdfSuryaOutcome:
        state = status.state
        external_job_id = status.external_job_id

        if state in {AcceleratorJobState.QUEUED, AcceleratorJobState.RUNNING}:
            return _outcome(
                CoordinatorDisposition.PENDING,
                external_job_id=external_job_id,
                provider_state=state,
            )
        if state is AcceleratorJobState.CONTENT_FAILED:
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                external_job_id=external_job_id,
                reason=_safe_provider_reason(status.reason_code),
                provider_state=state,
            )
        if state is AcceleratorJobState.CANCELLED:
            return _outcome(
                CoordinatorDisposition.CANCELLED,
                external_job_id=external_job_id,
                provider_state=state,
            )
        if state is AcceleratorJobState.INFRA_RETRYABLE:
            return _outcome(
                CoordinatorDisposition.INFRA_RETRYABLE,
                external_job_id=external_job_id,
                reason=_safe_provider_reason(status.reason_code),
                provider_state=state,
            )
        if state is not AcceleratorJobState.SUCCEEDED:
            return _outcome(CoordinatorDisposition.CONTENT_FAILED, reason="provider_status_malformed")

        try:
            result_manifest = validate_accelerator_result_acceptance(request, status)
            artifact_descriptor = result_manifest.result_artifact
            if artifact_descriptor is None:  # Defensive against a bypassed wire model.
                raise ValueError("succeeded provider status has no artifact")
        except Exception:
            stale = self._fence_lost_if_stale(external_job_id)
            if stale is not None:
                return stale
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                external_job_id=external_job_id,
                reason="provider_result_mismatch",
                provider_state=state,
            )

        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)

        hard_cap = min(
            artifact_descriptor.size_bytes,
            request.resource_caps.max_output_bytes,
            request.result_upload_capability.resource_caps.max_bytes,
            MAX_ARTIFACT_BYTES,
        )
        if artifact_descriptor.size_bytes != hard_cap:
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                external_job_id=external_job_id,
                reason="artifact_size_cap_exceeded",
                provider_state=state,
            )
        try:
            read = self._artifact_reader.read(
                request.result_upload_capability.bucket,
                artifact_descriptor.object_key,
                max_bytes=hard_cap,
            )
        except ArtifactReadLimitExceeded:
            if not self._fence_is_current():
                return self._fence_lost_after_stale(external_job_id)
            return _outcome(
                CoordinatorDisposition.CONTENT_FAILED,
                external_job_id=external_job_id,
                reason="artifact_size_cap_exceeded",
                provider_state=state,
            )
        except Exception:
            if not self._fence_is_current():
                return self._fence_lost_after_stale(external_job_id)
            return _outcome(
                CoordinatorDisposition.INFRA_RETRYABLE,
                external_job_id=external_job_id,
                reason="artifact_read_failed",
                provider_state=state,
            )

        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)

        if not isinstance(read, ArtifactRead) or not isinstance(read.content, bytes):
            return self._content_failure_after_read(external_job_id, "artifact_read_invalid", state)
        if len(read.content) != artifact_descriptor.size_bytes:
            return self._content_failure_after_read(external_job_id, "artifact_size_mismatch", state)
        if sha256(read.content).hexdigest() != artifact_descriptor.sha256:
            return self._content_failure_after_read(external_job_id, "artifact_digest_mismatch", state)
        if _normalized_media_type(read.content_type) != "application/json":
            return self._content_failure_after_read(
                external_job_id,
                "artifact_content_type_mismatch",
                state,
            )

        try:
            artifact = parse_surya_layout_artifact_bytes(
                read.content,
                render_manifest=trusted_render_manifest,
                expected_logical_compute_key=request.logical_compute_key,
                expected_producer=portable_producer,
                expected_requested_pages=request.identity.page_range.page_numbers,
            )
        except Exception:
            return self._content_failure_after_read(external_job_id, "artifact_parse_failed", state)

        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)
        return ExistingPdfSuryaOutcome(
            disposition=CoordinatorDisposition.SUCCEEDED,
            external_job_id=external_job_id,
            artifact=artifact,
            provider_state=state,
        )

    def _content_failure_after_read(
        self,
        external_job_id: str,
        reason: str,
        provider_state: AcceleratorJobState,
    ) -> ExistingPdfSuryaOutcome:
        """Give a concurrent fence loss priority over any artifact diagnosis."""

        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)
        return _outcome(
            CoordinatorDisposition.CONTENT_FAILED,
            external_job_id=external_job_id,
            reason=reason,
            provider_state=provider_state,
        )

    def _content_failure_after_reconciliation_read(
        self,
        external_job_id: str,
        reason: str,
    ) -> ExistingPdfSuryaOutcome:
        """Fence-prioritized content failure for a status-free repair read."""

        if not self._fence_is_current():
            return self._fence_lost_after_stale(external_job_id)
        return _outcome(
            CoordinatorDisposition.CONTENT_FAILED,
            external_job_id=external_job_id,
            reason=reason,
        )

    def _fence_lost_if_stale(self, external_job_id: str) -> ExistingPdfSuryaOutcome | None:
        """Return the common stale result after a local non-I/O validation step."""

        if self._fence_is_current():
            return None
        return self._fence_lost_after_stale(external_job_id)

    def _fence_lost_after_stale(
        self,
        external_job_id: str | None,
    ) -> ExistingPdfSuryaOutcome:
        """Return a fence-loss outcome without mutating the provider.

        Every caller has already observed a stale fence.  In particular, do
        not turn the known job ID into a cleanup cancel: ownership may have
        moved to another actor.
        """
        return _outcome(
            CoordinatorDisposition.FENCE_LOST,
            external_job_id=(
                external_job_id if _is_safe_external_job_id(external_job_id) else None
            ),
            reason="fence_lost",
        )

    def _fence_is_current(self) -> bool:
        try:
            return self._fence.is_current() is True
        except Exception:
            return False


def _bind_request_to_trusted_render_manifest(
    request: SuryaLayoutRequest,
    trusted_render_manifest: PdfRenderManifest,
) -> None:
    """Reject a request that does not exactly describe the trusted local render."""

    if not isinstance(request, SuryaLayoutRequest):
        raise TypeError("request must be a SuryaLayoutRequest")
    if not isinstance(trusted_render_manifest, PdfRenderManifest):
        raise TypeError("trusted_render_manifest must be a PdfRenderManifest")
    request._assert_live_integrity()
    identity = request.identity
    trusted_page_numbers = tuple(page.page for page in trusted_render_manifest.pages)
    if (
        identity.source_sha256 != trusted_render_manifest.source_pdf_sha256
        or identity.source_page_count != trusted_render_manifest.page_count
        or identity.render_manifest.schema_version != trusted_render_manifest.schema_version
        or identity.render_manifest.render_manifest_sha256
        != trusted_render_manifest.manifest_sha256()
        or identity.render_manifest.size_bytes
        != len(trusted_render_manifest.canonical_json())
        or identity.page_range.page_numbers != trusted_page_numbers
        or tuple(page.page_number for page in identity.page_images) != trusted_page_numbers
    ):
        raise ValueError("request is not bound to the trusted render manifest")
    rendered_by_page = {page.page: page for page in trusted_render_manifest.pages}
    for requested in identity.page_images:
        rendered = rendered_by_page.get(requested.page_number)
        if rendered is None or (
            requested.page_image_sha256 != rendered.image_sha256
            or requested.size_bytes != rendered.image_size_bytes
            or requested.mime_type != rendered.image_mime_type
            or requested.pixel_width != rendered.coordinate_manifest.rendered_width_px
            or requested.pixel_height != rendered.coordinate_manifest.rendered_height_px
            or requested.sidecar_binding.model_dump(mode="json")
            != build_sidecar_binding(rendered.coordinate_manifest)
        ):
            raise ValueError("request page image is not bound to the trusted render manifest")


def _portable_producer(request: SuryaLayoutRequest) -> PortableSuryaProducerIdentity:
    """Explicitly cross the accelerator-wire to portable-artifact identity boundary."""

    return _portable_producer_identity(request.identity.producer)


def _portable_producer_identity(
    producer: SuryaProducerIdentity,
) -> PortableSuryaProducerIdentity:
    """Convert a validated producer without requiring a capability-bearing request."""

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


def _outcome(
    disposition: CoordinatorDisposition,
    *,
    external_job_id: str | None = None,
    reason: str | None = None,
    provider_state: AcceleratorJobState | None = None,
) -> ExistingPdfSuryaOutcome:
    return ExistingPdfSuryaOutcome(
        disposition=disposition,
        external_job_id=external_job_id,
        reason_code=reason,
        provider_state=provider_state,
    )


_EXTERNAL_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_REASON = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*\Z")
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z")
_MAX_CONTENT_TYPE_CHARS = 255


def _is_safe_external_job_id(value: object) -> bool:
    return isinstance(value, str) and _EXTERNAL_JOB_ID.fullmatch(value) is not None


def _safe_provider_reason(value: object) -> str | None:
    if (
        isinstance(value, str)
        and len(value) <= 128
        and _SAFE_REASON.fullmatch(value) is not None
    ):
        return value
    return None


def _normalized_media_type(value: object) -> str | None:
    """Extract a safe lowercase media type while rejecting control characters."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_CONTENT_TYPE_CHARS
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type if _MEDIA_TYPE.fullmatch(media_type) is not None else None
