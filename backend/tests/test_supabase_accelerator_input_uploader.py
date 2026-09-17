"""Security and idempotency tests for accelerator-only input uploads."""

from __future__ import annotations

from hashlib import sha256

import httpx
import pytest

from worker.adapters import supabase_accelerator_input as adapter_module
from worker.adapters.supabase_accelerator_input import (
    SupabaseAcceleratorInputUploadError,
    SupabaseAcceleratorInputUploader,
)


SERVICE_KEY = "service-role-key-local-only"
BUCKET = "request-temp"
CONTENT = b'{"schema_version":"fixture/v1"}'
CONTENT_SHA256 = sha256(CONTENT).hexdigest()
KEY = f"accelerator/input/{CONTENT_SHA256}/render-manifest.json"


class _Chunks(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self._chunks


def _uploader(handler: object, **overrides: object) -> SupabaseAcceleratorInputUploader:
    kwargs: dict[str, object] = {
        "supabase_url": "http://127.0.0.1:8000",
        "service_role_key": SERVICE_KEY,
        "allowed_bucket": BUCKET,
        "object_key_prefix": "accelerator/input/",
        "max_object_bytes": 1_024,
        "transport": httpx.MockTransport(handler),  # type: ignore[arg-type]
    }
    kwargs.update(overrides)
    return SupabaseAcceleratorInputUploader(**kwargs)  # type: ignore[arg-type]


def test_create_only_upload_uses_exact_scope_and_ignores_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    client_options: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, content=b'{"Key":"provider-detail"}')

    real_client = httpx.Client

    def recording_client(*args: object, **kwargs: object) -> httpx.Client:
        client_options.append(dict(kwargs))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(adapter_module.httpx, "Client", recording_client)

    created = _uploader(handler).put_if_absent(
        bucket=BUCKET,
        object_key=KEY,
        content=CONTENT,
        content_type="application/json",
    )

    assert created is True
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == f"http://127.0.0.1:8000/storage/v1/object/{BUCKET}/{KEY}"
    assert request.headers["apikey"] == SERVICE_KEY
    assert request.headers["authorization"] == f"Bearer {SERVICE_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-upsert"] == "false"
    assert request.content == CONTENT
    assert client_options[0]["follow_redirects"] is False
    assert client_options[0]["trust_env"] is False


@pytest.mark.parametrize("conflict_status", [400, 409])
def test_existing_identical_bytes_and_media_type_are_reused(
    conflict_status: int,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                conflict_status,
                content=b"provider conflict diagnostic must remain private",
            )
        assert request.headers["apikey"] == SERVICE_KEY
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            stream=_Chunks(CONTENT[:7], CONTENT[7:]),
        )

    created = _uploader(handler).put_if_absent(
        bucket=BUCKET,
        object_key=KEY,
        content=CONTENT,
        content_type="application/json",
    )

    assert created is False
    assert methods == ["POST", "GET"]


def test_ambiguous_create_gateway_failure_reconciles_exact_existing_object() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(504)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_Chunks(CONTENT),
        )

    assert _uploader(handler).put_if_absent(
        bucket=BUCKET,
        object_key=KEY,
        content=CONTENT,
        content_type="application/json",
    ) is False
    assert methods == ["POST", "GET"]


def test_ambiguous_create_transport_failure_reconciles_without_replaying_post() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("response was lost", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_Chunks(CONTENT),
        )

    assert _uploader(handler).put_if_absent(
        bucket=BUCKET,
        object_key=KEY,
        content=CONTENT,
        content_type="application/json",
    ) is False
    assert methods == ["POST", "GET"]


def test_existing_object_get_retries_bounded_gateway_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods: list[str] = []
    get_attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_attempts
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(409)
        get_attempts += 1
        if get_attempts == 1:
            return httpx.Response(504)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_Chunks(CONTENT),
        )

    monkeypatch.setattr(adapter_module.time, "sleep", delays.append)

    assert _uploader(handler).put_if_absent(
        bucket=BUCKET,
        object_key=KEY,
        content=CONTENT,
        content_type="application/json",
    ) is False
    assert methods == ["POST", "GET", "GET"]
    assert delays == [adapter_module._VERIFY_RETRY_DELAY_SECONDS]


@pytest.mark.parametrize(
    ("existing", "response_content_type"),
    [
        (CONTENT + b"x", "application/json"),
        (b"different", "application/json"),
        (CONTENT, "image/png"),
        (CONTENT, None),
    ],
)
def test_conflict_is_not_reused_without_exact_bytes_and_media_type(
    existing: bytes,
    response_content_type: str | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409)
        headers = {} if response_content_type is None else {"content-type": response_content_type}
        return httpx.Response(200, headers=headers, stream=_Chunks(existing))

    with pytest.raises(SupabaseAcceleratorInputUploadError) as raised:
        _uploader(handler).put_if_absent(
            bucket=BUCKET,
            object_key=KEY,
            content=CONTENT,
            content_type="application/json",
        )

    assert str(raised.value) == "accelerator input upload failed"


@pytest.mark.parametrize(
    ("bucket", "object_key"),
    [
        ("other-bucket", KEY),
        (BUCKET, "other-prefix/input.json"),
        (BUCKET, f"accelerator/input/{CONTENT_SHA256}/other.json"),
        ("request-temp/other", KEY),
        (BUCKET, "/accelerator/input/file.json"),
        (BUCKET, "accelerator/input/../file.json"),
        (BUCKET, "accelerator/input/file%2f.json"),
        (BUCKET, "accelerator/input\\file.json"),
        (BUCKET, "accelerator/input/file name.json"),
    ],
)
def test_rejects_noncanonical_or_out_of_scope_locations_before_network(
    bucket: str,
    object_key: str,
) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201)

    with pytest.raises(ValueError):
        _uploader(handler).put_if_absent(
            bucket=bucket,
            object_key=object_key,
            content=CONTENT,
            content_type="application/json",
        )
    assert called is False


@pytest.mark.parametrize(
    "origin",
    [
        "http://storage.example.test",
        "http://localhost:8000",
        "http://192.168.1.10:8000",
        "HTTP://127.0.0.1:8000",
        "http://127.0.0.1:8000/",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000/storage",
        "http://127.0.0.1:8000?redirect=elsewhere",
        "http://127.0.0.1:8000#fragment",
        "http://[::1%25lo]:8000",
        "https://STORAGE.example.test",
    ],
)
def test_rejects_noncanonical_or_cleartext_non_loopback_origins(origin: str) -> None:
    with pytest.raises(ValueError, match="Supabase Storage API origin is invalid"):
        _uploader(lambda _: httpx.Response(201), supabase_url=origin)


@pytest.mark.parametrize(
    "origin",
    [
        "https://storage.example.test",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_accepts_https_or_literal_loopback_origins(origin: str) -> None:
    uploader = _uploader(lambda _: httpx.Response(201), supabase_url=origin)

    assert origin in repr(uploader)


def test_rejects_oversize_or_unapproved_content_before_network() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201)

    uploader = _uploader(handler, max_object_bytes=len(CONTENT))
    with pytest.raises(ValueError, match="content is invalid"):
        uploader.put_if_absent(
            bucket=BUCKET,
            object_key=KEY,
            content=CONTENT + b"x",
            content_type="application/json",
        )
    with pytest.raises(ValueError, match="content type is invalid"):
        uploader.put_if_absent(
            bucket=BUCKET,
            object_key=KEY,
            content=CONTENT,
            content_type="text/plain",
        )
    with pytest.raises(ValueError, match="content-address binding is invalid"):
        uploader.put_if_absent(
            bucket=BUCKET,
            object_key=KEY,
            content=CONTENT[:-1] + b"x",
            content_type="application/json",
        )
    assert called is False


def test_page_key_is_bound_to_canonical_page_number_and_png_digest() -> None:
    content = b"validated-png-fixture"
    digest = sha256(content).hexdigest()
    canonical_key = f"accelerator/input/{CONTENT_SHA256}/page-0001-{digest}.png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)

    assert _uploader(handler).put_if_absent(
        bucket=BUCKET,
        object_key=canonical_key,
        content=content,
        content_type="image/png",
    )
    with pytest.raises(ValueError, match="content-address binding is invalid"):
        _uploader(handler).put_if_absent(
            bucket=BUCKET,
            object_key=f"accelerator/input/{CONTENT_SHA256}/page-00001-{digest}.png",
            content=content,
            content_type="image/png",
        )
    assert len(requests) == 1


@pytest.mark.parametrize("status", [302, 307, 401, 500])
def test_provider_failures_and_redirects_are_fixed_redacted_errors(status: int) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"location": "http://attacker.invalid/steal"},
            content=b"provider diagnostic that must not escape",
        )

    with pytest.raises(SupabaseAcceleratorInputUploadError) as raised:
        _uploader(handler).put_if_absent(
            bucket=BUCKET,
            object_key=KEY,
            content=CONTENT,
            content_type="application/json",
        )

    assert calls == 1
    assert str(raised.value) == "accelerator input upload failed"
    assert SERVICE_KEY not in str(raised.value)
    assert KEY not in str(raised.value)
    assert "diagnostic" not in str(raised.value)


def test_repr_does_not_include_service_role_key_or_object_prefix() -> None:
    uploader = _uploader(lambda _: httpx.Response(500))

    assert SERVICE_KEY not in repr(uploader)
    assert "accelerator/input" not in repr(uploader)
