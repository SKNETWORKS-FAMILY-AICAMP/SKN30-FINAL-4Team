"""Pure, default-off contract checks for the remote Surya accelerator boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any

import pytest
from pydantic import ValidationError

from worker.contracts.accelerator import (
    AcceleratorCredentialError,
    AcceleratorContractError,
    AcceleratorDispatchPolicy,
    AcceleratorStorageScope,
    AcceleratorFailureKind,
    AcceleratorJobState,
    AcceleratorResourceCaps,
    AcceleratorJobStatus,
    ArtifactDescriptor,
    PageImageBinding,
    PageImageInput,
    PageRange,
    SignedStorageCapability,
    StorageResourceCaps,
    SuryaLayoutResultArtifactManifest,
    SuryaLayoutRequest,
    validate_accelerator_result_acceptance,
    validate_accelerator_dispatch,
)
from worker.ports.accelerator import InMemoryAccelerator


def _digest(character: str) -> str:
    return character * 64


FIXED_NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _capability(
    *,
    method: str,
    object_key: str,
    signature: str,
    expiry: datetime,
    max_bytes: int,
    mime_types: tuple[str, ...],
) -> SignedStorageCapability:
    path = f"/storage/v1/object/sign/request-temp/{object_key}"
    return SignedStorageCapability(
        method=method,  # type: ignore[arg-type]
        url=(f"https://storage.example.test{path}?signature={signature}"),
        storage_host="storage.example.test",
        bucket="request-temp",
        path=path,
        object_key=object_key,
        expires_at=expiry,
        resource_caps=StorageResourceCaps(
            max_bytes=max_bytes,
            allowed_mime_types=mime_types,
        ),
    )


def _request(
    *,
    input_signature: str = "input-secret",
    output_signature: str = "output-secret",
    expiry: datetime = datetime(2026, 9, 15, tzinfo=UTC),
    output_key: str = "run-1/surya-layout/result.json",
) -> SuryaLayoutRequest:
    page = PageImageInput(
        page_number=1,
        page_image_sha256=_digest("b"),
        size_bytes=400,
        mime_type="image/png",
        pixel_width=800,
        pixel_height=600,
        capability=_capability(
            method="GET",
            object_key="run-1/page-images/page-001.png",
            signature=input_signature,
            expiry=expiry,
            max_bytes=1_000,
            mime_types=("image/png",),
        ),
    )

    return SuryaLayoutRequest(
        identity={
            "source_sha256": _digest("a"),
            "source_page_count": 1,
            "page_range": PageRange(start_page=1, end_page=1),
            "page_images": (page,),
            "mode": "layout",
            "pipeline_revision": "surya-layout-pipeline/2026-09-15",
            "model_weights_sha256": _digest("c"),
            "config_digest": _digest("d"),
            "coordinate_manifest_sha256": _digest("e"),
            "worker_image_digest": f"sha256:{_digest('1')}",
        },
        result_upload_capability=_capability(
            method="PUT",
            object_key=output_key,
            signature=output_signature,
            expiry=expiry,
            max_bytes=3_000,
            mime_types=("application/json",),
        ),
        resource_caps=AcceleratorResourceCaps(
            max_page_count=1,
            max_total_input_bytes=1_000,
            max_total_rendered_pixels=10_000_000,
            max_output_bytes=3_000,
            execution_timeout_seconds=60,
            ttl_seconds=120,
        ),
    )


def _dispatch_policy(
    *,
    hosts: tuple[str, ...] = ("storage.example.test",),
    buckets: tuple[str, ...] = ("request-temp",),
    prefixes: tuple[str, ...] = ("run-1/",),
    endpoint_path_template: str = "/storage/v1/object/sign/{bucket}/{object_key}",
) -> AcceleratorDispatchPolicy:
    return AcceleratorDispatchPolicy(
        allowed_scopes=tuple(
            AcceleratorStorageScope(
                method=method,
                origin=f"https://{host}",
                endpoint_path_template=endpoint_path_template,
                bucket=bucket,
                object_key_prefix=prefix,
            )
            for method in ("GET", "PUT")
            for host, bucket, prefix in zip(hosts, buckets, prefixes, strict=True)
        ),
    )


def _manifest(
    request: SuryaLayoutRequest,
    *,
    external_job_id: str = "provider-job-1",
    status: str = "succeeded",
    result_artifact: ArtifactDescriptor | None | object = ...,  # sentinel permits explicit None
    reason_code: str | None = None,
    **overrides: Any,
) -> SuryaLayoutResultArtifactManifest:
    if result_artifact is ...:
        result_artifact = ArtifactDescriptor(
            object_key=request.result_upload_capability.object_key,
            sha256=_digest("f"),
            size_bytes=900,
            mime_type="application/json",
            schema_version="surya_layout_result/v1",
        )
    values: dict[str, Any] = {
        "external_job_id": external_job_id,
        "status": status,
        "logical_compute_key": request.logical_compute_key,
        "request_digest": request.request_digest,
        "result_artifact": result_artifact,
        "source_sha256": request.identity.source_sha256,
        "source_page_count": request.identity.source_page_count,
        "page_range": request.identity.page_range,
        "page_images": tuple(
            PageImageBinding(
                page_number=page.page_number,
                page_image_sha256=page.page_image_sha256,
            )
            for page in request.identity.page_images
        ),
        "coordinate_manifest_sha256": request.identity.coordinate_manifest_sha256,
        "pipeline_revision": request.identity.pipeline_revision,
        "model_weights_sha256": request.identity.model_weights_sha256,
        "config_digest": request.identity.config_digest,
        "worker_image_digest": request.identity.worker_image_digest,
        "started_at": FIXED_NOW,
        "completed_at": FIXED_NOW + timedelta(seconds=30),
        "reason_code": reason_code,
    }
    values.update(overrides)
    return SuryaLayoutResultArtifactManifest(**values)


def test_logical_key_and_request_digest_ignore_signed_url_token_and_expiry() -> None:
    first = _request()
    renewed = _request(
        input_signature="renewed-input-secret",
        output_signature="renewed-output-secret",
        expiry=datetime(2026, 9, 16, tzinfo=UTC),
    )

    assert first.logical_compute_key == renewed.logical_compute_key
    assert first.request_digest == renewed.request_digest
    assert first.model_dump(mode="json")["logical_compute_key"] == first.logical_compute_key
    assert first.model_dump(mode="json")["request_digest"] == first.request_digest


def test_signed_capability_path_must_bind_declared_bucket_and_object_key() -> None:
    object_key = "run-1/surya-layout/result.json"
    wrong_path = "/storage/v1/object/sign/request-temp/run-1/not-the-result.json"
    with pytest.raises(ValidationError, match="bucket and object_key"):
        SignedStorageCapability(
            method="PUT",
            url=f"https://storage.example.test{wrong_path}?signature=masked-in-error",
            storage_host="storage.example.test",
            bucket="request-temp",
            path=wrong_path,
            object_key=object_key,
            expires_at=datetime(2026, 9, 15, tzinfo=UTC),
            resource_caps=StorageResourceCaps(
                max_bytes=3_000,
                allowed_mime_types=("application/json",),
            ),
        )


def test_capability_repr_and_safe_log_record_redact_signed_url_and_object_path() -> None:
    request = _request()
    raw_input_url = request.identity.page_images[0].capability.signed_url()
    raw_output_url = request.result_upload_capability.signed_url()

    assert "input-secret" not in repr(request)
    assert "output-secret" not in repr(request)
    safe_log = request.safe_log_record()
    assert raw_input_url not in str(safe_log)
    assert raw_output_url not in str(safe_log)
    assert "page-001.png" not in str(safe_log)
    assert safe_log["input_capabilities"][0]["url"] == "<redacted>"


@pytest.mark.parametrize(
    "credential_field",
    [
        "DATABASE_URL",
        "supabase_service_role_key",
        "supabase_anon_key",
        "user_jwt",
        "database_password",
        "x-api-key",
        "cookie",
    ],
)
def test_credentials_are_rejected_without_echoing_the_secret(credential_field: str) -> None:
    secret = "postgresql://do-not-leak"
    request = _request()
    with pytest.raises(AcceleratorCredentialError) as error:
        SuryaLayoutRequest(
            identity=request.identity,
            result_upload_capability=request.result_upload_capability,
            resource_caps=request.resource_caps,
            **{credential_field: secret},
        )

    assert secret not in str(error.value)


def test_nested_credential_shaped_field_is_rejected_without_echoing_the_secret() -> None:
    secret = "do-not-print-this-cookie"
    request = _request()
    payload = request.model_dump()
    payload["identity"]["worker_cookie"] = secret

    with pytest.raises(AcceleratorCredentialError) as error:
        SuryaLayoutRequest.model_validate(payload)

    assert secret not in str(error.value)


def test_generic_extra_field_validation_error_hides_its_input_value() -> None:
    secret = "do-not-print-this-unrelated-value"
    payload = _request().model_dump()
    payload["not_a_contract_field"] = secret

    with pytest.raises(ValidationError) as error:
        SuryaLayoutRequest.model_validate(payload)

    assert secret not in str(error.value)


def test_wire_fields_round_trip_and_recompute_validation() -> None:
    request = _request()
    identity_round_trip = request.identity.model_validate(request.identity.model_dump())
    request_round_trip = SuryaLayoutRequest.model_validate(request.model_dump())

    assert identity_round_trip == request.identity
    assert request_round_trip == request

    # JSON-oriented Pydantic dumps must keep signed URLs masked.  The explicit
    # network method is the only allowed full-fidelity JSON round trip.
    masked_json = request.model_dump(mode="json")
    assert "input-secret" not in str(masked_json)
    assert "output-secret" not in str(masked_json)
    assert "input-secret" not in request.model_dump_json()
    assert "output-secret" not in request.model_dump_json()
    wire_json = json.dumps(request.to_wire_payload())
    wire_round_trip = SuryaLayoutRequest.from_wire_payload(json.loads(wire_json))
    assert wire_round_trip == request

    bad_identity = request.identity.model_dump()
    bad_identity["logical_compute_key"] = _digest("0")
    with pytest.raises(ValidationError, match="logical_compute_key does not match"):
        request.identity.model_validate(bad_identity)

    bad_request = request.model_dump()
    bad_request["request_digest"] = _digest("0")
    with pytest.raises(ValidationError, match="request_digest does not match"):
        SuryaLayoutRequest.model_validate(bad_request)


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (_dispatch_policy(hosts=("other-storage.example.test",)), "host"),
        (_dispatch_policy(buckets=("other-bucket",)), "bucket"),
        (_dispatch_policy(prefixes=("another-run/",)), "object_key"),
    ],
)
def test_dispatch_policy_rejects_host_bucket_and_prefix_outside_allowlist(
    policy: AcceleratorDispatchPolicy, message: str
) -> None:
    with pytest.raises(AcceleratorContractError, match="bound dispatch scope"):
        validate_accelerator_dispatch(_request(), policy, now=FIXED_NOW)


def test_dispatch_policy_rejects_suffix_bound_object_on_an_unapproved_endpoint() -> None:
    payload = _request().to_wire_payload()
    capability = payload["identity"]["page_images"][0]["capability"]
    capability["path"] = (
        f"/admin/delete/{capability['bucket']}/{capability['object_key']}"
    )
    capability["url"] = (
        f"https://storage.example.test{capability['path']}?signature=do-not-log"
    )
    # The URL/path change is part of request_digest, so reconstruct the locally
    # computed digest exactly as a real request builder would.
    payload["request_digest"] = ""
    request = SuryaLayoutRequest.from_wire_payload(payload)

    with pytest.raises(AcceleratorContractError, match="bound dispatch scope"):
        validate_accelerator_dispatch(request, _dispatch_policy(), now=FIXED_NOW)


def test_dispatch_scope_requires_a_canonical_https_origin() -> None:
    with pytest.raises(ValidationError, match="canonical HTTPS spelling"):
        AcceleratorStorageScope(
            method="GET",
            origin="https://STORAGE.example.test:443",
            endpoint_path_template="/storage/v1/object/sign/{bucket}/{object_key}",
            bucket="request-temp",
            object_key_prefix="run-1/",
        )


def test_dispatch_requires_every_capability_to_cover_full_ttl() -> None:
    expires_too_soon = FIXED_NOW + timedelta(seconds=119)
    with pytest.raises(AcceleratorContractError, match="expires before dispatch TTL"):
        validate_accelerator_dispatch(
            _request(expiry=expires_too_soon),
            _dispatch_policy(),
            now=FIXED_NOW,
        )

    with pytest.raises(AcceleratorContractError, match="timezone"):
        validate_accelerator_dispatch(_request(), _dispatch_policy(), now=datetime(2026, 9, 14))


def test_rendered_pixel_cap_is_enforced_at_request_validation() -> None:
    payload = _request().model_dump()
    payload["resource_caps"]["max_total_rendered_pixels"] = 479_999

    with pytest.raises(ValidationError, match="max_total_rendered_pixels"):
        SuryaLayoutRequest.model_validate(payload)


def test_terminal_failure_taxonomy_is_explicit() -> None:
    assert AcceleratorJobState.CONTENT_FAILED.value == "content_failed"
    assert AcceleratorJobState.INFRA_RETRYABLE.value == "infra_retryable"
    assert AcceleratorJobState.FENCE_LOST.value == "fence_lost"
    assert AcceleratorFailureKind.CONTENT_FAILED == AcceleratorJobState.CONTENT_FAILED
    assert AcceleratorJobState.FENCE_LOST.is_terminal is True
    assert AcceleratorJobState.RUNNING.is_terminal is False


def test_succeeded_status_requires_a_bound_result_artifact_manifest() -> None:
    request = _request()
    manifest = _manifest(request)

    status = AcceleratorJobStatus(
        external_job_id="provider-job-1",
        state=AcceleratorJobState.SUCCEEDED,
        logical_compute_key=request.logical_compute_key,
        request_digest=request.request_digest,
        result_manifest=manifest,
    )

    assert status.result_manifest is manifest


@pytest.mark.parametrize(
    ("manifest_override", "message"),
    [
        ({"external_job_id": "other-provider-job"}, "external_job_id"),
        ({"logical_compute_key": _digest("9")}, "logical_compute_key"),
        ({"request_digest": _digest("8")}, "request_digest"),
        ({"status": "content_failed", "result_artifact": None, "reason_code": "bad_pdf"}, "status"),
    ],
)
def test_terminal_status_binds_manifest_to_outer_job(
    manifest_override: dict[str, Any], message: str
) -> None:
    request = _request()
    manifest = _manifest(request, **manifest_override)

    with pytest.raises(ValidationError, match=message):
        AcceleratorJobStatus(
            external_job_id="provider-job-1",
            state=AcceleratorJobState.SUCCEEDED,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
            result_manifest=manifest,
        )


def test_terminal_failure_requires_a_matching_failure_manifest_and_reason() -> None:
    request = _request()
    manifest = _manifest(
        request,
        status="content_failed",
        result_artifact=None,
        reason_code="bad_pdf",
    )
    status = AcceleratorJobStatus(
        external_job_id="provider-job-1",
        state=AcceleratorJobState.CONTENT_FAILED,
        logical_compute_key=request.logical_compute_key,
        request_digest=request.request_digest,
        result_manifest=manifest,
        reason_code="bad_pdf",
    )
    assert status.result_manifest is manifest

    with pytest.raises(ValidationError, match="requires a result manifest"):
        AcceleratorJobStatus(
            external_job_id="provider-job-1",
            state=AcceleratorJobState.CONTENT_FAILED,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
            reason_code="bad_pdf",
        )

    with pytest.raises(ValidationError, match="reason_code must match"):
        AcceleratorJobStatus(
            external_job_id="provider-job-1",
            state=AcceleratorJobState.CONTENT_FAILED,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
            result_manifest=manifest,
            reason_code="wrong_reason",
        )


@pytest.mark.parametrize(
    "reason_code",
    [
        "postgresql://user:password@db.example/secret",
        "bad pdf",
        " bad_pdf",
        "BAD_PDF",
        "bad-pdf",
        "a" * 65,
    ],
)
def test_public_reason_code_rejects_prose_urls_whitespace_and_uppercase(
    reason_code: str,
) -> None:
    request = _request()
    with pytest.raises(ValidationError, match="lowercase public machine code") as error:
        _manifest(
            request,
            status="content_failed",
            result_artifact=None,
            reason_code=reason_code,
        )

    assert reason_code not in str(error.value)


def test_manifest_timing_is_timezone_aware_paired_and_ordered() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="timezone"):
        _manifest(
            request,
            started_at=datetime(2026, 9, 14, 1),
            completed_at=datetime(2026, 9, 14, 2, tzinfo=UTC),
        )
    missing_timing = _manifest(request).model_dump()
    missing_timing.pop("completed_at")
    with pytest.raises(ValidationError, match="completed_at"):
        SuryaLayoutResultArtifactManifest.model_validate(missing_timing)
    with pytest.raises(ValidationError, match="must not precede"):
        _manifest(
            request,
            started_at=FIXED_NOW + timedelta(seconds=1),
            completed_at=FIXED_NOW,
        )


def test_worker_image_digest_is_an_immutable_oci_sha256() -> None:
    payload = _request().model_dump()
    payload["identity"]["worker_image_digest"] = "sha256:mutable-tag"
    payload["identity"]["logical_compute_key"] = ""
    payload["logical_compute_key"] = ""
    payload["request_digest"] = ""

    with pytest.raises(ValidationError, match="worker_image_digest"):
        SuryaLayoutRequest.model_validate(payload)


def _succeeded_status(
    request: SuryaLayoutRequest,
    manifest: SuryaLayoutResultArtifactManifest | None = None,
) -> AcceleratorJobStatus:
    return AcceleratorJobStatus(
        external_job_id="provider-job-1",
        state=AcceleratorJobState.SUCCEEDED,
        logical_compute_key=request.logical_compute_key,
        request_digest=request.request_digest,
        result_manifest=manifest or _manifest(request),
    )


def test_local_acceptance_gate_accepts_only_the_bound_requested_result() -> None:
    request = _request(expiry=datetime(2026, 9, 16, tzinfo=UTC))
    status = _succeeded_status(request)

    accepted = validate_accelerator_result_acceptance(
        request,
        status,
    )

    assert accepted is status.result_manifest


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        (
            ArtifactDescriptor(
                object_key="run-1/surya-layout/other-result.json",
                sha256=_digest("f"),
                size_bytes=900,
                mime_type="application/json",
                schema_version="surya_layout_result/v1",
            ),
            "object_key",
        ),
        (
            ArtifactDescriptor(
                object_key="run-1/surya-layout/result.json",
                sha256=_digest("f"),
                size_bytes=3_001,
                mime_type="application/json",
                schema_version="surya_layout_result/v1",
            ),
            "output cap",
        ),
        (
            ArtifactDescriptor(
                object_key="run-1/surya-layout/result.json",
                sha256=_digest("f"),
                size_bytes=900,
                mime_type="text/plain",
                schema_version="surya_layout_result/v1",
            ),
            "MIME",
        ),
        (
            ArtifactDescriptor(
                object_key="run-1/surya-layout/result.json",
                sha256=_digest("f"),
                size_bytes=900,
                mime_type="application/json",
                schema_version="surya_layout_result/v2",
            ),
            "schema_version",
        ),
    ],
)
def test_local_acceptance_gate_rejects_unrequested_artifact_properties(
    artifact: ArtifactDescriptor, message: str
) -> None:
    request = _request(expiry=datetime(2026, 9, 16, tzinfo=UTC))
    status = _succeeded_status(request, _manifest(request, result_artifact=artifact))

    with pytest.raises(AcceleratorContractError, match=message):
        validate_accelerator_result_acceptance(
            request,
            status,
        )


def test_local_acceptance_gate_rejects_lineage_mismatch_and_expired_capability() -> None:
    request = _request(expiry=datetime(2026, 9, 16, tzinfo=UTC))
    mismatched_manifest = _manifest(request, source_sha256=_digest("0"))
    with pytest.raises(AcceleratorContractError, match="lineage"):
        validate_accelerator_result_acceptance(
            request,
            _succeeded_status(request, mismatched_manifest),
        )

    # Result acceptance deliberately does not re-check expired GET URLs.  A
    # provider may have fetched the input while valid and report completion
    # after that short-lived capability has expired.
    assert validate_accelerator_result_acceptance(
        request,
        _succeeded_status(request),
    ).status == "succeeded"


def test_in_memory_accelerator_submit_status_cancel_and_reattach_contract() -> None:
    accelerator = InMemoryAccelerator(dispatch_policy=_dispatch_policy(), now=lambda: FIXED_NOW)
    request = _request()

    submitted = accelerator.submit(request)
    assert submitted.state == AcceleratorJobState.QUEUED
    assert accelerator.get_status(submitted.external_job_id) == submitted
    assert accelerator.submit(_request(input_signature="renewed")) == submitted

    cancelled = accelerator.cancel(submitted.external_job_id)
    assert cancelled.state == AcceleratorJobState.CANCELLED
    assert accelerator.get_status(submitted.external_job_id) == cancelled
    assert accelerator.cancel(submitted.external_job_id) == cancelled


def test_in_memory_accelerator_rejects_same_key_with_different_request_digest() -> None:
    accelerator = InMemoryAccelerator(dispatch_policy=_dispatch_policy(), now=lambda: FIXED_NOW)
    accelerator.submit(_request())

    with pytest.raises(AcceleratorContractError, match="different request digest"):
        accelerator.submit(_request(output_key="run-1/other-result.json"))
