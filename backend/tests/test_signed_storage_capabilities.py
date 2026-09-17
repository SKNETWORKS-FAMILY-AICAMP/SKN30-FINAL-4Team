"""Focused checks for trusted Supabase signed Storage capability issuance."""

from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import UTC, datetime
import json

import httpx
import pytest

from worker import signed_storage_capabilities as capability_module
from worker.signed_storage_capabilities import (
    SignedStorageCapabilityIssuerError,
    SupabaseSignedStorageCapabilityIssuer,
    build_surya_layout_result_object_key,
)


NOW = datetime(2026, 9, 16, tzinfo=UTC)
SERVICE_KEY = "service-role-key-is-local-only"
SOURCE_KEY = "input/job-01/page-001.png"
SOURCE_HASH = "a" * 64


class _Bytes(httpx.SyncByteStream):
    def __init__(self, body: bytes = b"") -> None:
        self._body = body

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield self._body


def _token(exp: int) -> str:
    payload = urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def _upload_token(object_key: str, exp: int, *, upsert: bool = False) -> str:
    payload = {
        "url": f"request-temp/{object_key}",
        "upsert": upsert,
        "iat": int(NOW.timestamp()),
        "exp": exp,
    }
    encoded = urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


def _issuer(handler: object, *, max_ttl: int = 1_800) -> SupabaseSignedStorageCapabilityIssuer:
    return SupabaseSignedStorageCapabilityIssuer(
        supabase_url="http://127.0.0.1:8000",
        service_role_key=SERVICE_KEY,
        public_storage_origin="https://local-desktop.tailnet.ts.net",
        max_capability_ttl_seconds=max_ttl,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        now=lambda: NOW,
    )


def _json(payload: dict[str, object], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json; charset=utf-8"},
        stream=_Bytes(json.dumps(payload).encode()),
    )


def test_issues_exact_read_capability_and_rebases_only_authority() -> None:
    seen: list[httpx.Request] = []
    token = _token(int(NOW.timestamp()) + 120)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == f"/storage/v1/object/sign/request-temp/{SOURCE_KEY}"
        assert request.headers["apikey"] == SERVICE_KEY
        assert request.headers["authorization"] == f"Bearer {SERVICE_KEY}"
        assert json.loads(request.content) == {"expiresIn": 120}
        return _json({"signedURL": f"/object/sign/request-temp/{SOURCE_KEY}?token={token}"})

    capability = _issuer(handler).issue_read(
        bucket="request-temp",
        object_key=SOURCE_KEY,
        expected_sha256=SOURCE_HASH,
        size_bytes=4096,
        mime_type="image/png",
        ttl_seconds=120,
    )

    assert len(seen) == 1
    assert capability.method == "GET"
    assert capability.access_mode == "read_only"
    assert capability.storage_host == "local-desktop.tailnet.ts.net"
    assert capability.path == f"/storage/v1/object/sign/request-temp/{SOURCE_KEY}"
    assert capability.object_key == SOURCE_KEY
    assert capability.expected_sha256 == SOURCE_HASH
    assert capability.expires_at == datetime.fromtimestamp(int(NOW.timestamp()) + 120, UTC)
    assert capability.signed_url() == (
        "https://local-desktop.tailnet.ts.net"
        f"/storage/v1/object/sign/request-temp/{SOURCE_KEY}?token={token}"
    )
    assert SERVICE_KEY not in repr(capability)
    assert "token=" not in json.dumps(capability.model_dump(mode="json"))


def test_read_capability_retries_bounded_transient_gateway_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _token(int(NOW.timestamp()) + 120)
    statuses = [504, 200]
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 504:
            return httpx.Response(status)
        return _json(
            {
                "signedURL": (
                    f"/object/sign/request-temp/{SOURCE_KEY}?token={token}"
                )
            }
        )

    monkeypatch.setattr(capability_module.time, "sleep", delays.append)

    capability = _issuer(handler).issue_read(
        bucket="request-temp",
        object_key=SOURCE_KEY,
        expected_sha256=SOURCE_HASH,
        size_bytes=4096,
        mime_type="image/png",
        ttl_seconds=120,
    )

    assert capability.method == "GET"
    assert statuses == []
    assert delays == [capability_module._REQUEST_RETRY_DELAY_SECONDS]


def test_read_capability_transient_retry_is_strictly_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(504)

    monkeypatch.setattr(capability_module.time, "sleep", delays.append)

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler).issue_read(
            bucket="request-temp",
            object_key=SOURCE_KEY,
            expected_sha256=SOURCE_HASH,
            size_bytes=4096,
            mime_type="image/png",
            ttl_seconds=120,
        )

    assert calls == capability_module._REQUEST_ATTEMPTS
    assert len(delays) == capability_module._REQUEST_ATTEMPTS - 1


def test_issues_deterministic_create_only_upload_after_absence_check() -> None:
    logical_key = "b" * 64
    output_key = build_surya_layout_result_object_key(logical_key)
    token = _upload_token(output_key, int(NOW.timestamp()) + 1_800)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path.endswith(f"request-temp/{output_key}")
        if request.method == "GET":
            assert request.url.path == f"/storage/v1/object/info/request-temp/{output_key}"
            return _json(
                {"statusCode": "404", "error": "not_found", "message": "Object not found"},
                status=400,
            )
        assert request.method == "POST"
        assert request.url.path == f"/storage/v1/object/upload/sign/request-temp/{output_key}"
        assert json.loads(request.content) == {"upsert": False}
        return _json(
            {
                "url": f"/object/upload/sign/request-temp/{output_key}?token={token}",
                "token": token,
            }
        )

    capability = _issuer(handler).issue_surya_layout_result_upload(
        bucket="request-temp",
        logical_compute_key=logical_key,
        max_bytes=8192,
    )

    assert [request.method for request in seen] == ["GET", "POST"]
    assert capability.method == "PUT"
    assert capability.access_mode == "create_only"
    assert capability.expected_sha256 is None
    assert capability.object_key == output_key
    assert capability.path == f"/storage/v1/object/upload/sign/request-temp/{output_key}"
    assert capability.resource_caps.allowed_mime_types == ("application/json",)
    assert capability.expires_at == datetime.fromtimestamp(int(NOW.timestamp()) + 1_800, UTC)


def test_upload_signing_retries_bounded_transient_gateway_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_key = "d" * 64
    output_key = build_surya_layout_result_object_key(logical_key)
    token = _upload_token(output_key, int(NOW.timestamp()) + 1_800)
    methods: list[str] = []
    post_attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_attempts
        methods.append(request.method)
        if request.method == "GET":
            return _json(
                {"statusCode": "404", "error": "not_found", "message": "Object not found"},
                status=400,
            )
        post_attempts += 1
        if post_attempts == 1:
            return httpx.Response(504)
        return _json(
            {
                "url": f"/object/upload/sign/request-temp/{output_key}?token={token}",
                "token": token,
            }
        )

    monkeypatch.setattr(capability_module.time, "sleep", delays.append)

    capability = _issuer(handler).issue_surya_layout_result_upload(
        bucket="request-temp",
        logical_compute_key=logical_key,
        max_bytes=8192,
    )

    assert methods == ["GET", "POST", "POST"]
    assert delays == [capability_module._REQUEST_RETRY_DELAY_SECONDS]
    assert capability.object_key == output_key


def test_upload_token_lifetime_must_fit_dispatch_policy_ceiling() -> None:
    output_key = build_surya_layout_result_object_key("c" * 64)
    token = _upload_token(output_key, int(NOW.timestamp()) + 1_800)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _json(
                {"statusCode": "404", "error": "not_found", "message": "Object not found"},
                status=400,
            )
        return _json(
            {
                "url": f"/object/upload/sign/request-temp/{output_key}?token={token}",
                "token": token,
            }
        )

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler, max_ttl=120).issue_create_only_upload(
            bucket="request-temp",
            object_key=output_key,
            max_bytes=100,
            mime_type="application/json",
        )


def test_upload_rejects_response_token_that_does_not_bind_url() -> None:
    output_key = build_surya_layout_result_object_key("e" * 64)
    url_token = _upload_token(output_key, int(NOW.timestamp()) + 1_800)
    other_token = _upload_token(output_key, int(NOW.timestamp()) + 1_799)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _json(
                {"statusCode": "404", "error": "not_found", "message": "Object not found"},
                status=400,
            )
        return _json(
            {
                "url": f"/object/upload/sign/request-temp/{output_key}?token={url_token}",
                "token": other_token,
            }
        )

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler).issue_create_only_upload(
            bucket="request-temp",
            object_key=output_key,
            max_bytes=100,
            mime_type="application/json",
        )


def test_absence_check_rejects_other_storage_400() -> None:
    output_key = build_surya_layout_result_object_key("f" * 64)

    def handler(_: httpx.Request) -> httpx.Response:
        return _json(
            {"statusCode": "400", "error": "invalid_request", "message": "Invalid key"},
            status=400,
        )

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler).issue_create_only_upload(
            bucket="request-temp",
            object_key=output_key,
            max_bytes=100,
            mime_type="application/json",
        )


def test_upload_rejects_token_without_create_only_claim() -> None:
    output_key = build_surya_layout_result_object_key("9" * 64)
    token = _upload_token(
        output_key,
        int(NOW.timestamp()) + 1_800,
        upsert=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _json(
                {"statusCode": "404", "error": "not_found", "message": "Object not found"},
                status=400,
            )
        return _json(
            {
                "url": f"/object/upload/sign/request-temp/{output_key}?token={token}",
                "token": token,
            }
        )

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler).issue_create_only_upload(
            bucket="request-temp",
            object_key=output_key,
            max_bytes=100,
            mime_type="application/json",
        )


def test_service_role_origin_rejects_cleartext_non_loopback() -> None:
    with pytest.raises(ValueError, match="origin is invalid"):
        SupabaseSignedStorageCapabilityIssuer(
            supabase_url="http://supabase.internal:8000",
            service_role_key=SERVICE_KEY,
            public_storage_origin="https://local-desktop.tailnet.ts.net",
            max_capability_ttl_seconds=1_800,
        )


def test_existing_result_key_fails_closed_without_signing_upload() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert "/storage/v1/object/info/" in request.url.path
        return _json({}, status=200)

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler).issue_create_only_upload(
            bucket="request-temp",
            object_key="accelerator/surya-layout/" + "d" * 64 + "/result.json",
            max_bytes=100,
            mime_type="application/json",
        )
    assert calls == 1


@pytest.mark.parametrize(
    "returned_url",
    [
        "https://evil.example.test/storage/v1/object/sign/request-temp/input/job-01/page-001.png?token=x",
        "/object/sign/request-temp/other/job.json?token=x",
        "/object/sign/request-temp/input/job-01/page-001.png?token=x#fragment",
    ],
)
def test_read_rejects_unbound_provider_signed_url(returned_url: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _json({"signedURL": returned_url})

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler).issue_read(
            bucket="request-temp",
            object_key=SOURCE_KEY,
            expected_sha256=SOURCE_HASH,
            size_bytes=100,
            mime_type="image/png",
            ttl_seconds=60,
        )


def test_rejects_oversized_signing_response_before_json_parse() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": "65537"},
            stream=_Bytes(b"{}"),
        )

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler).issue_read(
            bucket="request-temp",
            object_key=SOURCE_KEY,
            expected_sha256=SOURCE_HASH,
            size_bytes=100,
            mime_type="image/png",
            ttl_seconds=60,
        )


def test_rejects_duplicate_keys_in_signing_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_Bytes(
                b'{"signedURL":"/object/sign/request-temp/input/job-01/'
                b'page-001.png?token=first","signedURL":"/object/sign/'
                b'request-temp/input/job-01/page-001.png?token=second"}'
            ),
        )

    with pytest.raises(SignedStorageCapabilityIssuerError, match="issuance failed"):
        _issuer(handler).issue_read(
            bucket="request-temp",
            object_key=SOURCE_KEY,
            expected_sha256=SOURCE_HASH,
            size_bytes=100,
            mime_type="image/png",
            ttl_seconds=60,
        )


@pytest.mark.parametrize("value", ["A" * 64, "x" * 63, "x/" + "a" * 62])
def test_deterministic_result_key_requires_logical_sha256(value: str) -> None:
    with pytest.raises(ValueError, match="logical compute key"):
        build_surya_layout_result_object_key(value)
