"""Focused contract checks for the trusted-side persistent Surya HTTP adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import httpx
import pytest

from worker.adapters.persistent_surya_http import (
    PersistentSuryaHttpAdapter,
    PersistentSuryaHttpError,
    validate_persistent_surya_base_url,
)
from worker.contracts.accelerator import (
    AcceleratorContractError,
    AcceleratorDispatchPolicy,
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
from worker.ports.accelerator import AcceleratorJobNotFoundError


NOW = datetime(2026, 9, 16, tzinfo=UTC)


def _digest(character: str) -> str:
    return character * 64


def _capability(
    *,
    method: str,
    key: str,
    expected_sha256: str | None,
) -> SignedStorageCapability:
    operation = "sign" if method == "GET" else "upload/sign"
    path = f"/storage/v1/object/{operation}/request-temp/{key}"
    return SignedStorageCapability(
        method=method,  # type: ignore[arg-type]
        access_mode="read_only" if method == "GET" else "create_only",
        url=f"https://storage.example.test{path}?signature=provider-secret",
        storage_host="storage.example.test",
        bucket="request-temp",
        path=path,
        object_key=key,
        expected_sha256=expected_sha256,
        expires_at=NOW + timedelta(seconds=300),
        resource_caps=StorageResourceCaps(
            max_bytes=3_000,
            allowed_mime_types=("image/png",) if key.endswith(".png") else ("application/json",),
        ),
    )


def _request() -> SuryaLayoutRequest:
    identity = LayoutComputeIdentity.model_validate(
        {
            "source_sha256": _digest("a"),
            "source_page_count": 1,
            "page_range": PageRange(start_page=1, end_page=1),
            "render_manifest": RenderManifestInput(
                schema_version="pdf_render_manifest/v1",
                render_manifest_sha256=_digest("c"),
                size_bytes=300,
                mime_type="application/json",
                capability=_capability(
                    method="GET",
                    key="run-1/render.json",
                    expected_sha256=_digest("c"),
                ),
            ),
            "page_images": (
                PageImageInput(
                    page_number=1,
                    page_image_sha256=_digest("b"),
                    size_bytes=400,
                    mime_type="image/png",
                    pixel_width=800,
                    pixel_height=600,
                    sidecar_binding=PageCoordinateBinding(
                        coordinate_manifest_schema_version="pdf_coordinate_manifest/v1",
                        coordinate_manifest_sha256=_digest("d"),
                        source_sha256=_digest("a"),
                        page=1,
                        page_image_sha256=_digest("b"),
                    ),
                    capability=_capability(
                        method="GET",
                        key="run-1/page-001.png",
                        expected_sha256=_digest("b"),
                    ),
                ),
            ),
            "workload_scope": "existing_pdf_shadow",
            "mode": "layout",
            "producer": SuryaProducerIdentity(
                engine_id="surya",
                engine_version="0.22.1",
                model_id="surya-layout",
                model_revision="1" * 40,
                model_weights_sha256=_digest("e"),
                pipeline_revision="surya-layout-pipeline/2026-09-16",
                config_sha256=_digest("f"),
                worker_image_digest=f"sha256:{_digest('1')}",
            ),
        }
    )
    return SuryaLayoutRequest(
        identity=identity,
        result_upload_capability=_capability(
            method="PUT",
            key=build_surya_layout_result_object_key(identity.logical_compute_key),
            expected_sha256=None,
        ),
        resource_caps=AcceleratorResourceCaps(
            max_page_count=1,
            max_total_input_bytes=9_000,
            max_total_rendered_pixels=1_000_000,
            max_output_bytes=3_000,
            execution_timeout_seconds=60,
            ttl_seconds=120,
        ),
    )


def _policy() -> AcceleratorDispatchPolicy:
    return AcceleratorDispatchPolicy(
        allowed_scopes=tuple(
            AcceleratorStorageScope(
                method=method,  # type: ignore[arg-type]
                origin="https://storage.example.test",
                endpoint_path_template=(
                    "/storage/v1/object/sign/{bucket}/{object_key}"
                    if method == "GET"
                    else "/storage/v1/object/upload/sign/{bucket}/{object_key}"
                ),
                bucket="request-temp",
                object_key_prefix=(
                    "run-1/"
                    if method == "GET"
                    else "accelerator/surya-layout/"
                ),
            )
            for method in ("GET", "PUT")
        ),
        max_ttl_seconds=600,
        max_execution_timeout_seconds=300,
        max_page_count=100,
        max_total_input_bytes=20_000,
        max_total_rendered_pixels=20_000_000,
        max_capability_bytes=10_000,
        allowed_mime_types=("image/png", "application/json"),
    )


def _public_status(
    request: SuryaLayoutRequest,
    *,
    job_id: str = "pj_0000000000000001",
    state: str = "queued",
    output: object = None,
    logical_compute_key: str | None = None,
    request_digest: str | None = None,
    reason_code: str | None = None,
) -> dict[str, object]:
    return {
        "id": job_id,
        "state": state,
        "logical_compute_key": logical_compute_key or request.logical_compute_key,
        "request_digest": request_digest or request.request_digest,
        "created_at": NOW.isoformat(),
        "started_at": None,
        "completed_at": None,
        "reason_code": reason_code,
        "output": output,
    }


def _adapter(handler: object, *, response_limit: int = 1_048_576) -> PersistentSuryaHttpAdapter:
    return PersistentSuryaHttpAdapter(
        base_url="https://runpod.tailnet.test",
        bearer_token="worker-token-not-to-log",
        dispatch_policy=_policy(),
        timeout_seconds=10,
        max_response_bytes=response_limit,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        now=lambda: NOW,
    )


class _Chunks(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self._chunks


def _json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json"},
        stream=_Chunks(json.dumps(payload).encode("utf-8")),
    )


def test_submit_dispatches_only_after_policy_gate_with_idempotency_binding() -> None:
    request = _request()
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return _json_response(
            _public_status(request, job_id=request.logical_compute_key),
            202,
        )

    result = _adapter(handler).submit(request)

    assert result.external_job_id == request.logical_compute_key
    assert len(seen) == 1
    outbound = seen[0]
    assert outbound.method == "POST"
    assert outbound.url == httpx.URL("https://runpod.tailnet.test/jobs")
    assert outbound.headers["authorization"] == "Bearer worker-token-not-to-log"
    assert outbound.headers["idempotency-key"] == request.logical_compute_key
    assert outbound.headers["accept"] == "application/json"
    assert json.loads(outbound.content)["input"]["request_digest"] == request.request_digest


def test_submit_rejects_unbound_response_without_exposing_signed_url_or_token() -> None:
    request = _request()

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            _public_status(
                request,
                job_id=request.logical_compute_key,
                request_digest=_digest("0"),
            ),
            202,
        )

    with pytest.raises(AcceleratorContractError) as captured:
        _adapter(handler).submit(request)

    message = str(captured.value)
    assert "provider-secret" not in message
    assert "worker-token-not-to-log" not in message


def test_submit_rejects_response_job_id_not_bound_to_idempotency_key() -> None:
    request = _request()

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            _public_status(request, job_id="pj_0000000000000001"),
            202,
        )

    with pytest.raises(AcceleratorContractError, match="not bound to request"):
        _adapter(handler).submit(request)


def test_get_validates_job_id_before_constructing_path() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _json_response({})

    with pytest.raises(AcceleratorContractError, match="job ID is invalid"):
        _adapter(handler).get_status("../../provider-secret")

    assert called is False


def test_get_404_becomes_typed_not_found_and_hides_provider_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"token=provider-secret")

    with pytest.raises(AcceleratorJobNotFoundError) as captured:
        _adapter(handler).get_status("pj_0000000000000001")

    assert "provider-secret" not in str(captured.value)


def test_unmaterialized_infra_retry_is_redacted_retryable_error() -> None:
    request = _request()

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            _public_status(
                request,
                state="infra_retryable",
                reason_code="worker_runtime_failed",
            )
        )

    with pytest.raises(PersistentSuryaHttpError) as captured:
        _adapter(handler).get_status("pj_0000000000000001")

    message = str(captured.value)
    assert "worker_runtime_failed" not in message
    assert "worker-token-not-to-log" not in message
    assert "provider-secret" not in message


def test_get_rejects_redirect_without_following_location() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://evil.test/jobs/x?token=provider-secret"},
        )

    with pytest.raises(PersistentSuryaHttpError) as captured:
        _adapter(handler).get_status("pj_0000000000000001")

    assert len(seen) == 1
    assert "evil.test" not in str(captured.value)
    assert "provider-secret" not in str(captured.value)


@pytest.mark.parametrize("content_type", ["text/plain", "application/json; charset=utf-8", "Application/JSON"])
def test_requires_exact_json_response_content_type(content_type: str) -> None:
    request = _request()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": content_type}, json=_public_status(request))

    with pytest.raises(AcceleratorContractError, match="content type is invalid"):
        _adapter(handler).get_status("pj_0000000000000001")


def test_response_body_is_bounded_while_streaming() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"{" + b"x" * 100 + b"}",
        )

    with pytest.raises(AcceleratorContractError, match="response is too large"):
        _adapter(handler, response_limit=32).get_status("pj_0000000000000001")


def test_cancel_uses_post_cancel_path_and_returns_cancelled_status() -> None:
    request = _request()
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return _json_response(_public_status(request, state="cancelled"))

    result = _adapter(handler).cancel("pj_0000000000000001")

    assert result.state == "cancelled"
    assert seen[0].method == "POST"
    assert seen[0].url == httpx.URL("https://runpod.tailnet.test/jobs/pj_0000000000000001/cancel")


@pytest.mark.parametrize(
    ("value", "allowed", "expected"),
    [
        ("https://runpod.tailnet.test/", False, "https://runpod.tailnet.test"),
        ("http://100.64.0.2", False, None),
        ("http://127.0.0.1:8787", False, None),
        ("http://127.0.0.1:8787", True, "http://127.0.0.1:8787"),
        ("https://runpod.tailnet.test/jobs", False, None),
        ("https://token@runpod.tailnet.test", False, None),
    ],
)
def test_base_url_requires_exact_https_origin_except_explicit_loopback_test_mode(
    value: str,
    allowed: bool,
    expected: str | None,
) -> None:
    if expected is None:
        with pytest.raises(ValueError, match="HTTPS origin"):
            validate_persistent_surya_base_url(value, allow_loopback_http=allowed)
    else:
        assert validate_persistent_surya_base_url(value, allow_loopback_http=allowed) == expected
