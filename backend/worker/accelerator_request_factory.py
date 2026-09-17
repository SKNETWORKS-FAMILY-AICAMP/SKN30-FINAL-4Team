"""Trusted construction of one persistent-Surya request from local renders.

This module owns the narrow handoff between a locally validated PDF render
artifact and the capability-bearing accelerator request.  It deliberately
does not own provider dispatch, polling, a database lease, or reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from typing import Protocol

from common_ir_pipeline.pdf_fusion.coordinate_manifest import build_sidecar_binding
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    PdfRenderManifestError,
    RenderedPage,
    validate_render_manifest_files,
)

from worker.contracts.accelerator import (
    AcceleratorDispatchPolicy,
    AcceleratorResourceCaps,
    LayoutComputeIdentity,
    PageCoordinateBinding,
    PageImageInput,
    PageRange,
    RenderManifestInput,
    SignedStorageCapability,
    SuryaLayoutReconciliationHandle,
    SuryaLayoutRequest,
    SuryaProducerIdentity,
)

__all__ = [
    "AcceleratorInputUpload",
    "ImmutableStorageUploader",
    "PreparedSuryaLayoutRequest",
    "SuryaCapabilityIssuer",
    "preflight_surya_layout_request",
    "prepare_surya_layout_request",
]


_SAFE_BUCKET = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})?")
_SURYA_STORAGE_ENDPOINT_TEMPLATES = {
    "GET": "/storage/v1/object/sign/{bucket}/{object_key}",
    "PUT": "/storage/v1/object/upload/sign/{bucket}/{object_key}",
}


class ImmutableStorageUploader(Protocol):
    """Trusted uploader for immutable input objects."""

    def put_if_absent(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> bool: ...


class SuryaCapabilityIssuer(Protocol):
    """Trusted issuer limited to the capabilities this factory requires."""

    def issue_read(
        self,
        *,
        bucket: str,
        object_key: str,
        expected_sha256: str,
        size_bytes: int,
        mime_type: str,
        ttl_seconds: int,
    ) -> SignedStorageCapability: ...

    def issue_surya_layout_result_upload(
        self,
        *,
        bucket: str,
        logical_compute_key: str,
        max_bytes: int,
        mime_type: str = "application/json",
    ) -> SignedStorageCapability: ...


@dataclass(frozen=True, slots=True)
class AcceleratorInputUpload:
    """Non-secret metadata for one locally uploaded immutable input."""

    bucket: str
    object_key: str
    sha256: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class PreparedSuryaLayoutRequest:
    """The request plus the trusted local evidence needed for reconciliation."""

    request: SuryaLayoutRequest
    reconciliation_handle: SuryaLayoutReconciliationHandle
    trusted_render_manifest: PdfRenderManifest
    input_uploads: tuple[AcceleratorInputUpload, ...]


def prepare_surya_layout_request(
    *,
    render_manifest: PdfRenderManifest,
    artifact_root: str | Path,
    input_bucket: str,
    uploader: ImmutableStorageUploader,
    issuer: SuryaCapabilityIssuer,
    producer: SuryaProducerIdentity,
    resource_caps: AcceleratorResourceCaps,
    read_capability_ttl_seconds: int,
    dispatch_policy: AcceleratorDispatchPolicy,
) -> PreparedSuryaLayoutRequest:
    """Upload verified render inputs, then produce one bound Surya request.

    The result capability is deliberately issued only after the immutable
    identity exists, because its object key is derived from that identity.
    """

    if not isinstance(render_manifest, PdfRenderManifest):
        raise ValueError("render_manifest must be a PdfRenderManifest")
    if not isinstance(producer, SuryaProducerIdentity):
        raise ValueError("producer must be a SuryaProducerIdentity")
    if not isinstance(resource_caps, AcceleratorResourceCaps):
        raise ValueError("resource_caps must be an AcceleratorResourceCaps")
    if not isinstance(dispatch_policy, AcceleratorDispatchPolicy):
        raise ValueError("dispatch_policy must be an AcceleratorDispatchPolicy")
    if not isinstance(input_bucket, str) or _SAFE_BUCKET.fullmatch(input_bucket) is None:
        raise ValueError("input_bucket is invalid")
    if (
        isinstance(read_capability_ttl_seconds, bool)
        or not isinstance(read_capability_ttl_seconds, int)
        or read_capability_ttl_seconds < 1
    ):
        raise ValueError("read_capability_ttl_seconds must be a positive integer")
    validated_manifest = validate_render_manifest_files(
        render_manifest,
        artifact_root=artifact_root,
    )
    preflight_surya_layout_request(
        render_manifest=validated_manifest,
        input_bucket=input_bucket,
        resource_caps=resource_caps,
        read_capability_ttl_seconds=read_capability_ttl_seconds,
        dispatch_policy=dispatch_policy,
    )
    root = Path(artifact_root).resolve(strict=True)
    manifest_bytes = validated_manifest.canonical_json()
    manifest_sha256 = sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != validated_manifest.manifest_sha256():  # pragma: no cover
        raise PdfRenderManifestError("render manifest hash changed during preparation")
    namespace = f"accelerator/input/{manifest_sha256}"
    manifest_upload = AcceleratorInputUpload(
        bucket=input_bucket,
        object_key=f"{namespace}/render-manifest.json",
        sha256=manifest_sha256,
        size_bytes=len(manifest_bytes),
        mime_type="application/json",
    )

    # Every page was validated above and their declared bytes, together with
    # the canonical manifest, fit ``max_total_input_bytes``.  Retaining this
    # tuple therefore cannot exceed the caller-approved aggregate input cap.
    page_contents = tuple(
        (page, _read_verified_page(root, page)) for page in validated_manifest.pages
    )
    page_uploads = tuple(
        AcceleratorInputUpload(
            bucket=input_bucket,
            object_key=(
                f"{namespace}/page-{page.page:04d}-{page.image_sha256}.png"
            ),
            sha256=page.image_sha256,
            size_bytes=page.image_size_bytes,
            mime_type=page.image_mime_type,
        )
        for page, _ in page_contents
    )
    uploads = (manifest_upload, *page_uploads)

    uploader.put_if_absent(
        bucket=manifest_upload.bucket,
        object_key=manifest_upload.object_key,
        content=manifest_bytes,
        content_type=manifest_upload.mime_type,
    )
    for upload, (_, content) in zip(page_uploads, page_contents, strict=True):
        uploader.put_if_absent(
            bucket=upload.bucket,
            object_key=upload.object_key,
            content=content,
            content_type=upload.mime_type,
        )

    manifest_capability = issuer.issue_read(
        bucket=manifest_upload.bucket,
        object_key=manifest_upload.object_key,
        expected_sha256=manifest_upload.sha256,
        size_bytes=manifest_upload.size_bytes,
        mime_type=manifest_upload.mime_type,
        ttl_seconds=read_capability_ttl_seconds,
    )
    page_inputs = tuple(
        PageImageInput(
            page_number=page.page,
            page_image_sha256=upload.sha256,
            size_bytes=upload.size_bytes,
            mime_type="image/png",
            pixel_width=page.coordinate_manifest.rendered_width_px,
            pixel_height=page.coordinate_manifest.rendered_height_px,
            sidecar_binding=PageCoordinateBinding(
                **build_sidecar_binding(page.coordinate_manifest)
            ),
            capability=issuer.issue_read(
                bucket=upload.bucket,
                object_key=upload.object_key,
                expected_sha256=upload.sha256,
                size_bytes=upload.size_bytes,
                mime_type=upload.mime_type,
                ttl_seconds=read_capability_ttl_seconds,
            ),
        )
        for page, upload in zip(validated_manifest.pages, page_uploads, strict=True)
    )
    identity = LayoutComputeIdentity(
        source_sha256=validated_manifest.source_pdf_sha256,
        source_page_count=validated_manifest.page_count,
        page_range=PageRange(
            start_page=1,
            end_page=validated_manifest.page_count,
        ),
        render_manifest=RenderManifestInput(
            schema_version=validated_manifest.schema_version,
            render_manifest_sha256=manifest_upload.sha256,
            size_bytes=manifest_upload.size_bytes,
            mime_type="application/json",
            capability=manifest_capability,
        ),
        page_images=page_inputs,
        workload_scope="existing_pdf_shadow",
        mode="layout",
        producer=producer,
    )
    result_capability = issuer.issue_surya_layout_result_upload(
        bucket=input_bucket,
        logical_compute_key=identity.logical_compute_key,
        max_bytes=resource_caps.max_output_bytes,
    )
    request = SuryaLayoutRequest(
        identity=identity,
        result_upload_capability=result_capability,
        resource_caps=resource_caps,
    )
    return PreparedSuryaLayoutRequest(
        request=request,
        reconciliation_handle=SuryaLayoutReconciliationHandle.from_request(
            request,
            validated_manifest,
        ),
        trusted_render_manifest=validated_manifest,
        input_uploads=uploads,
    )


def preflight_surya_layout_request(
    *,
    render_manifest: PdfRenderManifest,
    input_bucket: str,
    resource_caps: AcceleratorResourceCaps,
    read_capability_ttl_seconds: int,
    dispatch_policy: AcceleratorDispatchPolicy,
) -> None:
    """Reject a locally knowable policy violation before Storage side effects.

    This deliberately complements, rather than replaces,
    :func:`validate_accelerator_dispatch`: the latter still validates the
    issued capabilities' URLs and expiry at the outbound HTTP boundary.
    """

    if not isinstance(render_manifest, PdfRenderManifest):
        raise ValueError("render_manifest must be a PdfRenderManifest")
    if not isinstance(resource_caps, AcceleratorResourceCaps):
        raise ValueError("resource_caps must be an AcceleratorResourceCaps")
    if not isinstance(dispatch_policy, AcceleratorDispatchPolicy):
        raise ValueError("dispatch_policy must be an AcceleratorDispatchPolicy")
    if not isinstance(input_bucket, str) or _SAFE_BUCKET.fullmatch(input_bucket) is None:
        raise ValueError("input_bucket is invalid")
    if (
        isinstance(read_capability_ttl_seconds, bool)
        or not isinstance(read_capability_ttl_seconds, int)
        or read_capability_ttl_seconds < 1
    ):
        raise ValueError("read_capability_ttl_seconds must be a positive integer")
    if read_capability_ttl_seconds < resource_caps.ttl_seconds:
        raise ValueError("read capability TTL must cover the request TTL")

    manifest_size = len(render_manifest.canonical_json())
    page_count = len(render_manifest.pages)
    total_input_bytes = manifest_size + sum(
        page.image_size_bytes for page in render_manifest.pages
    )
    total_rendered_pixels = sum(
        page.coordinate_manifest.rendered_width_px
        * page.coordinate_manifest.rendered_height_px
        for page in render_manifest.pages
    )
    policy = dispatch_policy
    if resource_caps.ttl_seconds > policy.max_ttl_seconds:
        raise ValueError("request TTL exceeds deployment policy ceiling")
    if read_capability_ttl_seconds > policy.max_ttl_seconds:
        raise ValueError("read capability TTL exceeds deployment policy ceiling")
    if resource_caps.execution_timeout_seconds > policy.max_execution_timeout_seconds:
        raise ValueError("request execution timeout exceeds deployment policy ceiling")
    if resource_caps.max_page_count > policy.max_page_count:
        raise ValueError("request page cap exceeds deployment policy ceiling")
    if resource_caps.max_total_input_bytes > policy.max_total_input_bytes:
        raise ValueError("request input byte cap exceeds deployment policy ceiling")
    if resource_caps.max_total_rendered_pixels > policy.max_total_rendered_pixels:
        raise ValueError("request pixel cap exceeds deployment policy ceiling")
    if resource_caps.max_output_bytes > policy.max_capability_bytes:
        raise ValueError("request output cap exceeds deployment capability-byte ceiling")
    if page_count > resource_caps.max_page_count:
        raise ValueError("render manifest page count exceeds resource caps")
    if page_count > policy.max_page_count:
        raise ValueError("render manifest page count exceeds deployment policy ceiling")
    if total_input_bytes > resource_caps.max_total_input_bytes:
        raise ValueError("render inputs exceed resource byte cap")
    if total_input_bytes > policy.max_total_input_bytes:
        raise ValueError("render inputs exceed deployment input byte ceiling")
    if total_rendered_pixels > resource_caps.max_total_rendered_pixels:
        raise ValueError("rendered page pixels exceed resource cap")
    if total_rendered_pixels > policy.max_total_rendered_pixels:
        raise ValueError("rendered page pixels exceed deployment policy ceiling")
    if manifest_size > policy.max_capability_bytes or any(
        page.image_size_bytes > policy.max_capability_bytes
        for page in render_manifest.pages
    ):
        raise ValueError("input capability bytes exceed deployment policy ceiling")
    if not {"application/json", "image/png"}.issubset(policy.allowed_mime_types):
        raise ValueError("deployment policy must allow Surya input MIME types")

    namespace = f"accelerator/input/{render_manifest.manifest_sha256()}"
    input_keys = (
        f"{namespace}/render-manifest.json",
        *(
            f"{namespace}/page-{page.page:04d}-{page.image_sha256}.png"
            for page in render_manifest.pages
        ),
    )
    if any(
        not _policy_has_storage_scope(
            policy,
            method="GET",
            bucket=input_bucket,
            object_key=object_key,
        )
        for object_key in input_keys
    ):
        raise ValueError("deployment policy does not permit all Surya input objects")
    # The output compute key is capability-independent, but cannot be
    # materialized until the read capabilities bind the request identity.  Its
    # fixed namespace is the complete non-secret binding available here.
    if not _policy_has_storage_scope(
        policy,
        method="PUT",
        bucket=input_bucket,
        object_key="accelerator/surya-layout/",
    ):
        raise ValueError("deployment policy does not permit the Surya output namespace")


def _policy_has_storage_scope(
    policy: AcceleratorDispatchPolicy,
    *,
    method: str,
    bucket: str,
    object_key: str,
) -> bool:
    """Check exact public scope bindings without minting a capability or URL."""

    endpoint_path_template = _SURYA_STORAGE_ENDPOINT_TEMPLATES.get(method)
    if endpoint_path_template is None:
        return False

    return any(
        scope.method == method
        and scope.endpoint_path_template == endpoint_path_template
        and scope.bucket == bucket
        and object_key.startswith(scope.object_key_prefix)
        # ``AcceleratorStorageScope`` has already canonicalized this origin.
        # The comparison with the issuer's real capability belongs at dispatch.
        and bool(scope.origin)
        for scope in policy.allowed_scopes
    )


def _read_verified_page(root: Path, page: RenderedPage) -> bytes:
    """Re-read one validated PNG and bind the exact bytes sent to Storage."""

    candidate = root
    for component in Path(page.image_relative_path).parts:
        candidate = candidate / component
        if candidate.is_symlink():
            raise PdfRenderManifestError("page image must not traverse a symlink")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PdfRenderManifestError("page image must be a regular file")
            content = handle.read(page.image_size_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise PdfRenderManifestError("failed to read page image") from error
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise PdfRenderManifestError("page image changed while it was being read")
    if len(content) != page.image_size_bytes or sha256(content).hexdigest() != page.image_sha256:
        raise PdfRenderManifestError("page image bytes do not match render manifest")
    return content
