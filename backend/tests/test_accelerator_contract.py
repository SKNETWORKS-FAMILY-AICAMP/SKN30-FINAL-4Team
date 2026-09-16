"""Pure, default-off contract checks for the remote Surya accelerator boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from typing import Any

import pytest
from pydantic import ValidationError

from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaProducerIdentity as PortableSuryaProducerIdentity,
)

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
    PageCoordinateBinding,
    PageImageInput,
    PageRange,
    RenderManifestInput,
    SignedStorageCapability,
    StorageResourceCaps,
    SuryaLayoutResultArtifactManifest,
    SuryaLayoutRequest,
    SuryaProducerIdentity,
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
    expected_sha256: str | None,
) -> SignedStorageCapability:
    path = f"/storage/v1/object/sign/request-temp/{object_key}"
    return SignedStorageCapability(
        method=method,  # type: ignore[arg-type]
        access_mode="read_only" if method == "GET" else "create_only",
        url=(f"https://storage.example.test{path}?signature={signature}"),
        storage_host="storage.example.test",
        bucket="request-temp",
        path=path,
        object_key=object_key,
        expected_sha256=expected_sha256,
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
    expiry: datetime = FIXED_NOW + timedelta(seconds=300),
    output_key: str = "run-1/surya-layout/result.json",
) -> SuryaLayoutRequest:
    sidecar_binding = PageCoordinateBinding(
        coordinate_manifest_schema_version="pdf_coordinate_manifest/v1",
        coordinate_manifest_sha256=_digest("e"),
        source_sha256=_digest("a"),
        page=1,
        page_image_sha256=_digest("b"),
    )
    page = PageImageInput(
        page_number=1,
        page_image_sha256=_digest("b"),
        size_bytes=400,
        mime_type="image/png",
        pixel_width=800,
        pixel_height=600,
        sidecar_binding=sidecar_binding,
        capability=_capability(
            method="GET",
            object_key="run-1/page-images/page-001.png",
            signature=input_signature,
            expiry=expiry,
            max_bytes=1_000,
            mime_types=("image/png",),
            expected_sha256=_digest("b"),
        ),
    )

    return SuryaLayoutRequest(
        identity={
            "source_sha256": _digest("a"),
            "source_page_count": 1,
            "page_range": PageRange(start_page=1, end_page=1),
            "render_manifest": RenderManifestInput(
                schema_version="pdf_render_manifest/v1",
                render_manifest_sha256=_digest("4"),
                size_bytes=300,
                mime_type="application/json",
                capability=_capability(
                    method="GET",
                    object_key="run-1/page-images/render-manifest.json",
                    signature=f"render-{input_signature}",
                    expiry=expiry,
                    max_bytes=1_000,
                    mime_types=("application/json",),
                    expected_sha256=_digest("4"),
                ),
            ),
            "page_images": (page,),
            "workload_scope": "existing_pdf_shadow",
            "mode": "layout",
            "producer": SuryaProducerIdentity(
                engine_id="surya",
                engine_version="0.15.0",
                model_id="surya-layout",
                model_revision="1" * 40,
                model_weights_sha256=_digest("c"),
                pipeline_revision="surya-layout-pipeline/2026-09-15",
                config_sha256=_digest("d"),
                worker_image_digest=f"sha256:{_digest('1')}",
            ),
        },
        result_upload_capability=_capability(
            method="PUT",
            object_key=output_key,
            signature=output_signature,
            expiry=expiry,
            max_bytes=3_000,
            mime_types=("application/json",),
            expected_sha256=None,
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
    max_ttl_seconds: int = 600,
    max_execution_timeout_seconds: int = 300,
    max_page_count: int = 100,
    max_total_input_bytes: int = 20_000,
    max_total_rendered_pixels: int = 20_000_000,
    max_capability_bytes: int = 10_000,
    allowed_mime_types: tuple[str, ...] = ("image/png", "application/json"),
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
        max_ttl_seconds=max_ttl_seconds,
        max_execution_timeout_seconds=max_execution_timeout_seconds,
        max_page_count=max_page_count,
        max_total_input_bytes=max_total_input_bytes,
        max_total_rendered_pixels=max_total_rendered_pixels,
        max_capability_bytes=max_capability_bytes,
        allowed_mime_types=allowed_mime_types,
    )


def _rebuild_wire_request(payload: dict[str, Any]) -> SuryaLayoutRequest:
    """Recompute both derived digests after an intentional wire mutation."""

    payload["identity"]["logical_compute_key"] = ""
    payload["logical_compute_key"] = ""
    payload["request_digest"] = ""
    return SuryaLayoutRequest.from_wire_payload(payload)


def _two_page_request() -> SuryaLayoutRequest:
    payload = _request().to_wire_payload()
    second = json.loads(json.dumps(payload["identity"]["page_images"][0]))
    second["page_number"] = 2
    second["page_image_sha256"] = _digest("2")
    second["sidecar_binding"]["page"] = 2
    second["sidecar_binding"]["page_image_sha256"] = _digest("2")
    second["sidecar_binding"]["coordinate_manifest_sha256"] = _digest("3")
    second_key = "run-1/page-images/page-002.png"
    second["capability"]["object_key"] = second_key
    second["capability"]["expected_sha256"] = _digest("2")
    second["capability"]["path"] = (
        f"/storage/v1/object/sign/request-temp/{second_key}"
    )
    second["capability"]["url"] = (
        f"https://storage.example.test{second['capability']['path']}?signature=second"
    )
    payload["identity"]["source_page_count"] = 2
    payload["identity"]["page_range"]["end_page"] = 2
    payload["identity"]["page_images"].append(second)
    payload["resource_caps"]["max_page_count"] = 2
    payload["resource_caps"]["max_total_input_bytes"] = 2_000
    return _rebuild_wire_request(payload)


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
            schema_version="surya_layout_artifact/v1",
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
        "render_manifest_schema_version": request.identity.render_manifest.schema_version,
        "render_manifest_sha256": (
            request.identity.render_manifest.render_manifest_sha256
        ),
        "page_images": tuple(
            page.sidecar_binding
            for page in request.identity.page_images
        ),
        "producer": request.identity.producer,
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


def test_logical_key_binds_render_sidecars_and_full_producer_identity() -> None:
    original = _request()
    mutations = (
        (("identity", "render_manifest", "render_manifest_sha256"), _digest("5")),
        (
            (
                "identity",
                "page_images",
                0,
                "sidecar_binding",
                "coordinate_manifest_sha256",
            ),
            _digest("6"),
        ),
        (("identity", "producer", "engine_version"), "0.16.0"),
        (("identity", "producer", "model_revision"), "2" * 40),
        (("identity", "producer", "worker_image_digest"), f"sha256:{_digest('2')}"),
    )

    for path, replacement in mutations:
        payload = original.to_wire_payload()
        target: Any = payload
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = replacement
        if path[-1] == "render_manifest_sha256":
            payload["identity"]["render_manifest"]["capability"][
                "expected_sha256"
            ] = replacement
        changed = _rebuild_wire_request(payload)
        assert changed.logical_compute_key != original.logical_compute_key


def test_request_has_explicit_existing_pdf_scope_and_no_database_attempt_field() -> None:
    payload = _request().to_wire_payload()
    assert payload["identity"]["workload_scope"] == "existing_pdf_shadow"
    assert payload["output_schema_version"] == "surya_layout_artifact/v1"

    request_pdf = json.loads(json.dumps(payload))
    request_pdf["identity"]["workload_scope"] = "request_pdf"
    with pytest.raises(ValidationError, match="existing_pdf_shadow"):
        _rebuild_wire_request(request_pdf)

    with_attempt = json.loads(json.dumps(payload))
    with_attempt["identity"]["processing_run_pk"] = "local-fence-only"
    with pytest.raises(ValidationError, match="processing_run_pk"):
        _rebuild_wire_request(with_attempt)


def test_request_binds_render_manifest_and_exact_five_field_page_sidecar() -> None:
    request = _request()
    wire = request.to_wire_payload()
    manifest = wire["identity"]["render_manifest"]
    binding = wire["identity"]["page_images"][0]["sidecar_binding"]

    assert manifest["schema_version"] == "pdf_render_manifest/v1"
    assert manifest["capability"]["method"] == "GET"
    assert manifest["capability"]["access_mode"] == "read_only"
    assert set(binding) == {
        "coordinate_manifest_schema_version",
        "coordinate_manifest_sha256",
        "source_sha256",
        "page",
        "page_image_sha256",
    }

    wrong_source = json.loads(json.dumps(wire))
    wrong_source["identity"]["page_images"][0]["sidecar_binding"][
        "source_sha256"
    ] = _digest("7")
    with pytest.raises(ValidationError, match="sidecar source hash"):
        _rebuild_wire_request(wrong_source)

    wrong_page_hash = json.loads(json.dumps(wire))
    wrong_page_hash["identity"]["page_images"][0]["sidecar_binding"][
        "page_image_sha256"
    ] = _digest("8")
    with pytest.raises(ValidationError, match="page image hash"):
        _rebuild_wire_request(wrong_page_hash)


def test_get_capabilities_bind_expected_bytes_and_put_has_no_expected_hash() -> None:
    request = _request()
    wire = request.to_wire_payload()
    assert (
        wire["identity"]["render_manifest"]["capability"]["expected_sha256"]
        == request.identity.render_manifest.render_manifest_sha256
    )
    assert (
        wire["identity"]["page_images"][0]["capability"]["expected_sha256"]
        == request.identity.page_images[0].page_image_sha256
    )
    assert wire["result_upload_capability"]["expected_sha256"] is None
    assert (
        request.identity.page_images[0].capability.binding_for_digest()[
            "expected_sha256"
        ]
        == request.identity.page_images[0].page_image_sha256
    )

    wrong_page = json.loads(json.dumps(wire))
    wrong_page["identity"]["page_images"][0]["capability"][
        "expected_sha256"
    ] = _digest("9")
    with pytest.raises(ValidationError, match="page image hash"):
        _rebuild_wire_request(wrong_page)

    wrong_manifest = json.loads(json.dumps(wire))
    wrong_manifest["identity"]["render_manifest"]["capability"][
        "expected_sha256"
    ] = _digest("9")
    with pytest.raises(ValidationError, match="render manifest hash"):
        _rebuild_wire_request(wrong_manifest)

    predeclared_output = json.loads(json.dumps(wire))
    predeclared_output["result_upload_capability"]["expected_sha256"] = _digest("9")
    with pytest.raises(ValidationError, match="must not declare expected_sha256"):
        _rebuild_wire_request(predeclared_output)


def test_accelerator_producer_identity_matches_portable_artifact_shape() -> None:
    accelerator_producer = _request().identity.producer
    payload = accelerator_producer.model_dump(mode="json")
    portable_producer = PortableSuryaProducerIdentity.from_dict(payload)

    assert portable_producer.to_dict() == payload
    assert tuple(payload) == (
        "engine_id",
        "engine_version",
        "model_id",
        "model_revision",
        "model_weights_sha256",
        "pipeline_revision",
        "config_sha256",
        "worker_image_digest",
    )


@pytest.mark.parametrize(
    "model_revision",
    ["main", "master", "latest", "head", "stable", "r1", "A" * 40, "1" * 39],
)
def test_producer_rejects_mutable_or_nonimmutable_model_revisions(
    model_revision: str,
) -> None:
    payload = _request().identity.producer.model_dump(mode="json")
    payload["model_revision"] = model_revision
    with pytest.raises(ValidationError, match="immutable"):
        SuryaProducerIdentity.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("engine_version", " 0.15.0"),
        ("model_id", "surya-layout "),
        ("pipeline_revision", "x" * 257),
    ],
)
def test_producer_rejects_whitespace_and_oversized_identity_strings(
    field: str,
    value: str,
) -> None:
    payload = _request().identity.producer.model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        SuryaProducerIdentity.model_validate(payload)


def test_request_requires_ascending_pages_and_read_only_inputs() -> None:
    payload = _request().to_wire_payload()
    second = json.loads(json.dumps(payload["identity"]["page_images"][0]))
    second["page_number"] = 2
    second["page_image_sha256"] = _digest("2")
    second["sidecar_binding"]["page"] = 2
    second["sidecar_binding"]["page_image_sha256"] = _digest("2")
    second["sidecar_binding"]["coordinate_manifest_sha256"] = _digest("3")
    second_key = "run-1/page-images/page-002.png"
    second["capability"]["object_key"] = second_key
    second["capability"]["expected_sha256"] = _digest("2")
    second["capability"]["path"] = f"/storage/v1/object/sign/request-temp/{second_key}"
    second["capability"]["url"] = (
        f"https://storage.example.test{second['capability']['path']}?signature=second"
    )
    payload["identity"]["source_page_count"] = 2
    payload["identity"]["page_range"]["end_page"] = 2
    payload["identity"]["page_images"].append(second)
    payload["resource_caps"]["max_page_count"] = 2
    payload["resource_caps"]["max_total_input_bytes"] = 2_000
    ascending = _rebuild_wire_request(payload)
    assert tuple(page.page_number for page in ascending.identity.page_images) == (1, 2)

    reversed_payload = ascending.to_wire_payload()
    reversed_payload["identity"]["page_images"].reverse()
    with pytest.raises(ValidationError, match="ascending order"):
        _rebuild_wire_request(reversed_payload)

    wrong_manifest_method = ascending.to_wire_payload()
    capability = wrong_manifest_method["identity"]["render_manifest"]["capability"]
    capability["method"] = "PUT"
    capability["access_mode"] = "create_only"
    capability["expected_sha256"] = None
    with pytest.raises(ValidationError, match="read-only GET"):
        _rebuild_wire_request(wrong_manifest_method)


def test_result_upload_is_explicitly_create_only() -> None:
    request = _request()
    wire = request.to_wire_payload()
    assert wire["result_upload_capability"]["method"] == "PUT"
    assert wire["result_upload_capability"]["access_mode"] == "create_only"

    readable_output = json.loads(json.dumps(wire))
    readable_output["result_upload_capability"]["method"] = "GET"
    readable_output["result_upload_capability"]["access_mode"] = "read_only"
    readable_output["result_upload_capability"]["expected_sha256"] = _digest("f")
    with pytest.raises(ValidationError, match="create-only PUT"):
        _rebuild_wire_request(readable_output)


def test_signed_capability_path_must_bind_declared_bucket_and_object_key() -> None:
    object_key = "run-1/surya-layout/result.json"
    wrong_path = "/storage/v1/object/sign/request-temp/run-1/not-the-result.json"
    with pytest.raises(ValidationError, match="bucket and object_key"):
        SignedStorageCapability(
            method="PUT",
            access_mode="create_only",
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
    raw_manifest_url = request.identity.render_manifest.capability.signed_url()
    raw_input_url = request.identity.page_images[0].capability.signed_url()
    raw_output_url = request.result_upload_capability.signed_url()

    assert "input-secret" not in repr(request)
    assert "output-secret" not in repr(request)
    safe_log = request.safe_log_record()
    assert raw_manifest_url not in str(safe_log)
    assert raw_input_url not in str(safe_log)
    assert raw_output_url not in str(safe_log)
    assert "page-001.png" not in str(safe_log)
    assert safe_log["input_capabilities"][0]["url"] == "<redacted>"
    capability_dump = request.identity.page_images[0].capability.model_dump(mode="json")
    assert "url" not in capability_dump
    capability = request.identity.page_images[0].capability
    assert capability.url == "<redacted>"
    assert "_signed_url_value" not in capability.__dict__
    assert capability.__pydantic_private__ is not None
    assert capability.__pydantic_private__["_signed_url_value"] == raw_input_url
    assert "input-secret" not in request.model_dump_json()


def test_signed_capability_live_integrity_rejects_post_validation_mutation() -> None:
    request = _request()
    capability = request.identity.page_images[0].capability
    evil_url = capability.signed_url().replace("input-secret", "evil-signature")
    assert capability.__pydantic_private__ is not None
    capability.__pydantic_private__["_signed_url_value"] = evil_url

    with pytest.raises(AcceleratorContractError, match="changed after validation"):
        request.to_wire_payload()
    with pytest.raises(
        (AcceleratorContractError, ValidationError),
        match="changed after validation",
    ):
        PageImageInput(
            page_number=1,
            page_image_sha256=_digest("b"),
            size_bytes=400,
            mime_type="image/png",
            pixel_width=800,
            pixel_height=600,
            sidecar_binding=request.identity.page_images[0].sidecar_binding,
            capability=capability,
        )

    request = _request()
    capability = request.identity.page_images[0].capability
    object.__setattr__(capability.resource_caps, "max_bytes", 9_999)
    with pytest.raises(AcceleratorContractError, match="changed after validation"):
        request.to_wire_payload()


def test_request_model_validate_rechecks_nested_private_url_integrity() -> None:
    request = _request()
    capability = request.identity.render_manifest.capability
    evil_url = capability.signed_url().replace(
        "render-input-secret",
        "attacker-controlled-signature",
    )
    assert capability.__pydantic_private__ is not None
    capability.__pydantic_private__["_signed_url_value"] = evil_url

    with pytest.raises(
        (AcceleratorContractError, ValidationError),
        match="changed after validation",
    ):
        SuryaLayoutRequest.model_validate(request)
    with pytest.raises(AcceleratorContractError, match="changed after validation"):
        validate_accelerator_dispatch(request, _dispatch_policy(), now=FIXED_NOW)


def test_request_live_integrity_rejects_mutation_even_if_digest_is_recomputed() -> None:
    request = _request()
    object.__setattr__(request, "output_schema_version", "attacker_schema/v9")
    request_digest_payload = request.request_digest_payload()
    forged_digest = sha256(
        json.dumps(
            request_digest_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    object.__setattr__(request, "request_digest", forged_digest)

    with pytest.raises(
        (AcceleratorContractError, ValidationError),
        match="changed after validation",
    ):
        SuryaLayoutRequest.model_validate(request)
    with pytest.raises(AcceleratorContractError, match="changed after validation"):
        validate_accelerator_dispatch(request, _dispatch_policy(), now=FIXED_NOW)
    with pytest.raises(AcceleratorContractError, match="changed after validation"):
        request.to_wire_payload()


@pytest.mark.parametrize(
    "url_mutator",
    [
        lambda url: "\t" + url,
        lambda url: url.replace("?", "\r?", 1),
        lambda url: url + "\n",
        lambda url: url.replace("?", "%2F..?", 1),
        lambda url: url.replace("storage.example.test", "STORAGE.example.test"),
        lambda url: url.replace("storage.example.test", "storage.example.test:443"),
        lambda url: url.split("?", 1)[0] + "?" + "x=1&" * 33,
        lambda url: url.split("?", 1)[0] + "?signature=a+b",
        lambda url: url.split("?", 1)[0] + "?flag",
        lambda url: url.split("?", 1)[0] + "?_signature=value",
        lambda url: url.split("?", 1)[0] + "?%2Fsignature=value",
        lambda url: url.split("?", 1)[0] + "?signature=%ZZ",
        lambda url: url.split("?", 1)[0] + "?signature=" + "a" * 4_097,
        lambda url: url.split("?", 1)[0] + "?signature=" + "a" * 8_200,
    ],
)
def test_signed_capability_rejects_noncanonical_or_unbounded_urls(
    url_mutator: Any,
) -> None:
    payload = _request().to_wire_payload()
    capability = payload["identity"]["page_images"][0]["capability"]
    capability["url"] = url_mutator(capability["url"])

    with pytest.raises(
        ValidationError,
        match="signed storage capability|signed URL path",
    ):
        _rebuild_wire_request(payload)


@pytest.mark.parametrize(
    "object_key",
    [
        "%2e%2e/result.json",
        "%2F../result.json",
        "run-1//result.json",
        "run-1/./result.json",
        "run-1\\result.json",
        "run-1/result .json",
        "run-1/.hidden.json",
        "run-1/_private.json",
    ],
)
def test_storage_object_keys_reject_encoded_and_noncanonical_segments(
    object_key: str,
) -> None:
    with pytest.raises(ValidationError, match="object_key"):
        _capability(
            method="GET",
            object_key=object_key,
            signature="attack",
            expiry=datetime(2026, 9, 15, tzinfo=UTC),
            max_bytes=1_000,
            mime_types=("image/png",),
            expected_sha256=_digest("b"),
        )


@pytest.mark.parametrize(
    "prefix",
    [
        "%2e%2e/",
        "%2F../",
        "run-1//",
        "run-1/./",
        "run-1\\bad/",
    ],
)
def test_dispatch_scope_prefix_rejects_noncanonical_segments(prefix: str) -> None:
    with pytest.raises(ValidationError, match="prefix"):
        _dispatch_policy(prefixes=(prefix,))


@pytest.mark.parametrize(
    "template",
    [
        "/storage/%2e%2e/{bucket}/{object_key}",
        "/storage//{bucket}/{object_key}",
        "/storage/./{bucket}/{object_key}",
        "/storage\\admin/{bucket}/{object_key}",
        "/storage/{bucket}-suffix/{object_key}",
    ],
)
def test_dispatch_endpoint_template_rejects_noncanonical_static_paths(
    template: str,
) -> None:
    with pytest.raises(ValidationError, match="endpoint"):
        _dispatch_policy(endpoint_path_template=template)


def test_validation_bypass_apis_fail_closed() -> None:
    capability = _request().identity.page_images[0].capability
    with pytest.raises(AcceleratorContractError, match="model_construct"):
        SignedStorageCapability.model_construct(method="GET")
    with pytest.raises(AcceleratorContractError, match="model_copy"):
        capability.model_copy(update={"url": "https://evil.example/x?q=1"})
    with pytest.raises(AcceleratorContractError, match="copy"):
        capability.copy(update={"url": "https://evil.example/x?q=1"})


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
    payload = request.to_wire_payload()
    payload["identity"]["worker_cookie"] = secret

    with pytest.raises(AcceleratorCredentialError) as error:
        SuryaLayoutRequest.model_validate(payload)

    assert secret not in str(error.value)


def test_generic_extra_field_validation_error_hides_its_input_value() -> None:
    secret = "do-not-print-this-unrelated-value"
    payload = _request().to_wire_payload()
    payload["not_a_contract_field"] = secret

    with pytest.raises(ValidationError) as error:
        SuryaLayoutRequest.model_validate(payload)

    assert secret not in str(error.value)


def test_wire_fields_round_trip_and_recompute_validation() -> None:
    request = _request()

    # Generic dumps omit signed URLs entirely.  The explicit
    # network method is the only allowed full-fidelity JSON round trip.
    masked_json = request.model_dump(mode="json")
    assert "input-secret" not in str(masked_json)
    assert "output-secret" not in str(masked_json)
    assert "input-secret" not in request.model_dump_json()
    assert "output-secret" not in request.model_dump_json()
    wire_json = json.dumps(request.to_wire_payload())
    wire_round_trip = SuryaLayoutRequest.from_wire_payload(json.loads(wire_json))
    assert wire_round_trip == request

    bad_identity = request.to_wire_payload()["identity"]
    bad_identity["logical_compute_key"] = _digest("0")
    with pytest.raises(ValidationError, match="logical_compute_key does not match"):
        request.identity.model_validate(bad_identity)

    bad_request = request.to_wire_payload()
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


def test_dispatch_rejects_capability_expiry_beyond_deployment_maximum_ttl() -> None:
    request = _request(expiry=FIXED_NOW + timedelta(seconds=601))
    with pytest.raises(AcceleratorContractError, match="maximum TTL"):
        validate_accelerator_dispatch(
            request,
            _dispatch_policy(max_ttl_seconds=600),
            now=FIXED_NOW,
        )


def test_mime_allow_lists_are_sorted_unique_and_do_not_change_request_digest() -> None:
    caps = StorageResourceCaps(
        max_bytes=100,
        allowed_mime_types=("text/plain", "image/png", "text/plain"),
    )
    assert caps.allowed_mime_types == ("image/png", "text/plain")

    policy = _dispatch_policy(
        allowed_mime_types=("image/png", "application/json", "image/png")
    )
    assert policy.allowed_mime_types == ("application/json", "image/png")

    original = _request()
    payload = original.to_wire_payload()
    payload["identity"]["render_manifest"]["capability"]["resource_caps"][
        "allowed_mime_types"
    ] = ["application/json", "application/json"]
    payload["identity"]["page_images"][0]["capability"]["resource_caps"][
        "allowed_mime_types"
    ] = ["image/png", "image/png"]
    payload["result_upload_capability"]["resource_caps"][
        "allowed_mime_types"
    ] = ["application/json", "application/json"]
    duplicate_input = _rebuild_wire_request(payload)
    assert duplicate_input.request_digest == original.request_digest


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (_dispatch_policy(max_ttl_seconds=119), "TTL"),
        (_dispatch_policy(max_execution_timeout_seconds=59), "execution timeout"),
        (_dispatch_policy(max_capability_bytes=2_999), "request output cap"),
        (_dispatch_policy(allowed_mime_types=("application/json",)), "MIME"),
    ],
)
def test_dispatch_policy_enforces_deployment_owned_hard_ceilings(
    policy: AcceleratorDispatchPolicy,
    message: str,
) -> None:
    with pytest.raises(AcceleratorContractError, match=message):
        validate_accelerator_dispatch(_request(), policy, now=FIXED_NOW)


def test_dispatch_policy_rejects_individual_capability_byte_ceiling() -> None:
    payload = _request().to_wire_payload()
    payload["identity"]["page_images"][0]["capability"]["resource_caps"][
        "max_bytes"
    ] = 3_001
    request = _rebuild_wire_request(payload)

    with pytest.raises(AcceleratorContractError, match="capability byte cap"):
        validate_accelerator_dispatch(
            request,
            _dispatch_policy(max_capability_bytes=3_000),
            now=FIXED_NOW,
        )


def test_dispatch_policy_rejects_actual_page_count_ceiling() -> None:
    with pytest.raises(AcceleratorContractError, match="page count"):
        validate_accelerator_dispatch(
            _two_page_request(),
            _dispatch_policy(max_page_count=1),
            now=FIXED_NOW,
        )


def test_dispatch_policy_rejects_aggregate_input_byte_ceiling() -> None:
    payload = _request().to_wire_payload()
    # Declared aggregate is 300-byte manifest + 400-byte page.
    payload["resource_caps"]["max_total_input_bytes"] = 700
    request = _rebuild_wire_request(payload)
    with pytest.raises(AcceleratorContractError, match="aggregate input bytes"):
        validate_accelerator_dispatch(
            request,
            _dispatch_policy(max_total_input_bytes=699),
            now=FIXED_NOW,
        )


def test_dispatch_policy_rejects_aggregate_get_capability_budget() -> None:
    # Actual bytes are 700 and request max is 1,000, but the two GET grants
    # together permit 2,000 bytes.  Deployment policy owns that outer budget.
    with pytest.raises(AcceleratorContractError, match="aggregate GET capability"):
        validate_accelerator_dispatch(
            _request(),
            _dispatch_policy(max_total_input_bytes=1_500),
            now=FIXED_NOW,
        )


def test_dispatch_policy_rejects_actual_rendered_pixel_ceiling() -> None:
    payload = _request().to_wire_payload()
    payload["resource_caps"]["max_total_rendered_pixels"] = 480_000
    request = _rebuild_wire_request(payload)
    with pytest.raises(AcceleratorContractError, match="rendered pixels"):
        validate_accelerator_dispatch(
            request,
            _dispatch_policy(max_total_rendered_pixels=479_999),
            now=FIXED_NOW,
        )


def test_result_upload_capability_mime_is_exactly_json() -> None:
    payload = _request().to_wire_payload()
    payload["result_upload_capability"]["resource_caps"]["allowed_mime_types"] = [
        "application/json",
        "text/plain",
    ]
    with pytest.raises(ValidationError, match="exactly application/json"):
        _rebuild_wire_request(payload)


def test_rendered_pixel_cap_is_enforced_at_request_validation() -> None:
    payload = _request().to_wire_payload()
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


def test_remote_status_cannot_claim_a_local_fence_loss() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="fence_lost"):
        AcceleratorJobStatus(
            external_job_id="provider-job-1",
            state=AcceleratorJobState.FENCE_LOST,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
        )

    manifest_payload = _manifest(request).model_dump(mode="json")
    manifest_payload["status"] = "fence_lost"
    manifest_payload["result_artifact"] = None
    manifest_payload["reason_code"] = "stale_fence"
    with pytest.raises(ValidationError, match="status"):
        SuryaLayoutResultArtifactManifest.model_validate(manifest_payload)


@pytest.mark.parametrize(
    "external_job_id",
    [" provider-job", "provider job", "provider/job", "job\nheader", "x" * 129],
)
def test_provider_external_job_id_is_bounded_and_canonical(
    external_job_id: str,
) -> None:
    request = _request()
    with pytest.raises(ValidationError, match="external_job_id"):
        AcceleratorJobStatus(
            external_job_id=external_job_id,
            state=AcceleratorJobState.QUEUED,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
        )
    with pytest.raises(ValidationError, match="external_job_id"):
        _manifest(request, external_job_id=external_job_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mime_type", "application/json\nX-Evil: yes"),
        ("mime_type", "Application/JSON"),
        ("schema_version", " surya_layout_artifact/v1"),
        ("schema_version", "surya_layout_artifact/latest"),
        ("schema_version", "x" * 257),
    ],
)
def test_provider_artifact_identifiers_are_bounded_and_canonical(
    field: str,
    value: str,
) -> None:
    payload: dict[str, Any] = {
        "object_key": "run-1/surya-layout/result.json",
        "sha256": _digest("f"),
        "size_bytes": 900,
        "mime_type": "application/json",
        "schema_version": "surya_layout_artifact/v1",
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        ArtifactDescriptor.model_validate(payload)


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


def test_cancelled_manifest_and_outer_status_forbid_reason_codes() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="cancelled manifest"):
        _manifest(
            request,
            status="cancelled",
            result_artifact=None,
            reason_code="provider_cancelled",
        )

    cancelled_manifest = _manifest(
        request,
        status="cancelled",
        result_artifact=None,
        reason_code=None,
    )
    status = AcceleratorJobStatus(
        external_job_id="provider-job-1",
        state=AcceleratorJobState.CANCELLED,
        logical_compute_key=request.logical_compute_key,
        request_digest=request.request_digest,
        result_manifest=cancelled_manifest,
    )
    assert status.reason_code is None


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
    payload = _request().to_wire_payload()
    payload["identity"]["producer"]["worker_image_digest"] = "sha256:mutable-tag"
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


def test_local_acceptance_rejects_post_validation_output_schema_mutation() -> None:
    request = _request(expiry=datetime(2026, 9, 16, tzinfo=UTC))
    object.__setattr__(request, "output_schema_version", "future_layout_artifact/v2")
    artifact = ArtifactDescriptor(
        object_key=request.result_upload_capability.object_key,
        sha256=_digest("f"),
        size_bytes=900,
        mime_type="application/json",
        schema_version="future_layout_artifact/v2",
    )
    status = _succeeded_status(request, _manifest(request, result_artifact=artifact))
    with pytest.raises(AcceleratorContractError, match="request_digest"):
        validate_accelerator_result_acceptance(request, status)


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        (
            ArtifactDescriptor(
                object_key="run-1/surya-layout/other-result.json",
                sha256=_digest("f"),
                size_bytes=900,
                mime_type="application/json",
                schema_version="surya_layout_artifact/v1",
            ),
            "object_key",
        ),
        (
            ArtifactDescriptor(
                object_key="run-1/surya-layout/result.json",
                sha256=_digest("f"),
                size_bytes=3_001,
                mime_type="application/json",
                schema_version="surya_layout_artifact/v1",
            ),
            "output cap",
        ),
        (
            ArtifactDescriptor(
                object_key="run-1/surya-layout/result.json",
                sha256=_digest("f"),
                size_bytes=900,
                mime_type="text/plain",
                schema_version="surya_layout_artifact/v1",
            ),
            "MIME",
        ),
        (
            ArtifactDescriptor(
                object_key="run-1/surya-layout/result.json",
                sha256=_digest("f"),
                size_bytes=900,
                mime_type="application/json",
                schema_version="surya_layout_artifact/v2",
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
    mismatched_manifest = _manifest(request, render_manifest_sha256=_digest("0"))
    with pytest.raises(AcceleratorContractError, match="lineage"):
        validate_accelerator_result_acceptance(
            request,
            _succeeded_status(request, mismatched_manifest),
        )

    binding_payload = request.identity.page_images[0].sidecar_binding.model_dump()
    binding_payload["coordinate_manifest_sha256"] = _digest("9")
    mismatched_binding = _manifest(
        request,
        page_images=(PageCoordinateBinding.model_validate(binding_payload),),
    )
    with pytest.raises(AcceleratorContractError, match="lineage"):
        validate_accelerator_result_acceptance(
            request,
            _succeeded_status(request, mismatched_binding),
        )

    producer_payload = request.identity.producer.model_dump()
    producer_payload["model_revision"] = "3" * 40
    mismatched_producer = _manifest(
        request,
        producer=SuryaProducerIdentity.model_validate(producer_payload),
    )
    with pytest.raises(AcceleratorContractError, match="lineage"):
        validate_accelerator_result_acceptance(
            request,
            _succeeded_status(request, mismatched_producer),
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


def test_in_memory_accelerator_models_running_success_and_terminal_immutability() -> None:
    accelerator = InMemoryAccelerator(
        dispatch_policy=_dispatch_policy(), now=lambda: FIXED_NOW
    )
    request = _request()
    submitted = accelerator.submit(request)

    running = accelerator.mark_running(submitted.external_job_id)
    assert running.state == AcceleratorJobState.RUNNING
    assert accelerator.mark_running(submitted.external_job_id) == running

    terminal_manifest = _manifest(
        request,
        external_job_id=submitted.external_job_id,
    )
    terminal = accelerator.publish_terminal(
        submitted.external_job_id,
        state=AcceleratorJobState.SUCCEEDED,
        result_manifest=terminal_manifest,
    )
    assert terminal.state == AcceleratorJobState.SUCCEEDED
    assert accelerator.submit(_request(input_signature="renewed")) == terminal
    assert accelerator.cancel(submitted.external_job_id) == terminal

    with pytest.raises(AcceleratorContractError, match="immutable"):
        accelerator.mark_running(submitted.external_job_id)
    with pytest.raises(AcceleratorContractError, match="immutable"):
        accelerator.publish_terminal(
            submitted.external_job_id,
            state=AcceleratorJobState.CONTENT_FAILED,
            result_manifest=_manifest(
                request,
                external_job_id=submitted.external_job_id,
                status="content_failed",
                result_artifact=None,
                reason_code="bad_pdf",
            ),
            reason_code="bad_pdf",
        )


def test_in_memory_accelerator_rejects_local_only_fence_terminal() -> None:
    accelerator = InMemoryAccelerator(
        dispatch_policy=_dispatch_policy(), now=lambda: FIXED_NOW
    )
    request = _request()
    submitted = accelerator.submit(request)

    with pytest.raises(AcceleratorContractError, match="remote terminal outcome"):
        accelerator.publish_terminal(
            submitted.external_job_id,
            state=AcceleratorJobState.FENCE_LOST,
            result_manifest=_manifest(request, external_job_id=submitted.external_job_id),
            reason_code="stale_fence",
        )


def test_in_memory_accelerator_rejects_same_key_with_different_request_digest() -> None:
    accelerator = InMemoryAccelerator(dispatch_policy=_dispatch_policy(), now=lambda: FIXED_NOW)
    accelerator.submit(_request())

    with pytest.raises(AcceleratorContractError, match="different request digest"):
        accelerator.submit(_request(output_key="run-1/other-result.json"))


def test_in_memory_accelerator_creates_new_job_after_infra_retryable() -> None:
    accelerator = InMemoryAccelerator(
        dispatch_policy=_dispatch_policy(), now=lambda: FIXED_NOW
    )
    request = _request()
    first = accelerator.submit(request)
    retryable_manifest = _manifest(
        request,
        external_job_id=first.external_job_id,
        status="infra_retryable",
        result_artifact=None,
        reason_code="provider_unavailable",
    )
    terminal = accelerator.publish_terminal(
        first.external_job_id,
        state=AcceleratorJobState.INFRA_RETRYABLE,
        result_manifest=retryable_manifest,
        reason_code="provider_unavailable",
    )

    replacement = accelerator.submit(_request(input_signature="renewed"))
    assert replacement.external_job_id != terminal.external_job_id
    assert replacement.state == AcceleratorJobState.QUEUED
    assert accelerator.get_status(terminal.external_job_id) == terminal
    assert accelerator.submit(_request(input_signature="renewed-again")) == replacement
