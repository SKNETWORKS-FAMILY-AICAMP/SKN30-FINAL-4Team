"""Focused tests for the capability-only RunPod HTTPS storage adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import logging

import httpx
import pytest

from prereview_runpod_worker.surya_layout_worker.handler import (
    StorageContentFailure,
    StorageGetContentFailure,
    StorageInfrastructureFailure,
    StoragePutConflictFailure,
)
from prereview_runpod_worker.surya_layout_worker.http_storage import (
    BoundedHttpsStorageAdapter,
    validate_loopback_proxy_url,
)
from worker.contracts.accelerator import (
    SignedStorageCapability,
    StorageResourceCaps,
)


NOW = datetime(2026, 9, 16, tzinfo=UTC)


def _capability(
    *,
    method: str,
    body: bytes = b"bounded-object",
    max_bytes: int = 1_024,
    mime_type: str = "application/json",
) -> SignedStorageCapability:
    object_key = "run/job-01/input.json" if method == "GET" else "run/job-01/result.json"
    operation = "sign" if method == "GET" else "upload/sign"
    path = f"/storage/v1/object/{operation}/request-temp/{object_key}"
    return SignedStorageCapability(
        method=method,  # type: ignore[arg-type]
        access_mode="read_only" if method == "GET" else "create_only",
        url=f"https://storage.example.test{path}?token=signed-value",
        storage_host="storage.example.test",
        bucket="request-temp",
        path=path,
        object_key=object_key,
        expected_sha256=sha256(body).hexdigest() if method == "GET" else None,
        expires_at=NOW + timedelta(minutes=5),
        resource_caps=StorageResourceCaps(
            max_bytes=max_bytes,
            allowed_mime_types=(mime_type,),
        ),
    )


def _adapter(
    handler: object,
    *,
    hard_max_bytes: int = 2_048,
    monotonic: object | None = None,
) -> BoundedHttpsStorageAdapter:
    return BoundedHttpsStorageAdapter(
        hard_max_bytes=hard_max_bytes,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        now=lambda: NOW,
        monotonic=monotonic,  # type: ignore[arg-type]
    )


class _Chunks(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self._chunks


class _MonotonicClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _SlowChunks(httpx.SyncByteStream):
    def __init__(self, clock: _MonotonicClock, *chunks: bytes, advance: float) -> None:
        self._clock = clock
        self._chunks = chunks
        self._advance = advance

    def __iter__(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            yield chunk
            self._clock.advance(self._advance)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:1055", "http://127.0.0.1:1055"),
        ("socks5://127.0.0.1:1055", "socks5://127.0.0.1:1055"),
        ("socks5h://[::1]:1055/", "socks5h://[::1]:1055"),
    ],
)
def test_storage_proxy_accepts_only_explicit_loopback_transport(
    raw: str,
    expected: str,
) -> None:
    assert validate_loopback_proxy_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://100.64.0.2:1055",
        "http://localhost:1055",
        "https://127.0.0.1:1055",
        "http://user:secret@127.0.0.1:1055",
        "http://127.0.0.1:1055/path",
        "http://127.0.0.1:1055?token=secret",
        "http://127.0.0.1",
    ],
)
def test_storage_proxy_rejects_non_loopback_credentials_and_extra_authority(
    raw: str,
) -> None:
    with pytest.raises(ValueError, match="proxy URL rejected"):
        validate_loopback_proxy_url(raw)


def test_adapter_passes_reviewed_proxy_explicitly_and_still_ignores_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    adapter = BoundedHttpsStorageAdapter(
        hard_max_bytes=1024,
        proxy_url="http://127.0.0.1:1055",
        now=lambda: NOW,
    )
    monkeypatch.setattr(httpx, "Client", Client)

    adapter._client(None)  # noqa: SLF001 - focused composition contract

    assert captured["proxy"] == "http://127.0.0.1:1055"
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


def test_get_streams_verified_content_without_ambient_credentials() -> None:
    body = b"bounded-object"
    capability = _capability(method="GET", body=body)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            stream=_Chunks(body),
        )

    result = _adapter(handler).get(capability, max_bytes=1_024)

    assert result == body
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].headers["accept-encoding"] == "identity"
    assert "authorization" not in seen[0].headers
    assert "cookie" not in seen[0].headers


def test_adapter_clamps_transport_loggers_that_would_expose_signed_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for logger_name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(logger_name), "level", logging.INFO)

    _adapter(lambda _request: httpx.Response(500))

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_adapter_reclamps_transport_loggers_immediately_before_signed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later root logging reconfiguration cannot reactivate URL logging."""

    body = b"bounded-object"
    capability = _capability(method="GET", body=body)
    adapter = _adapter(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_Chunks(body),
        )
    )
    for logger_name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(logger_name), "level", logging.INFO)

    assert adapter.get(capability, max_bytes=1_024) == body
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_get_rejects_redirect_without_following_or_leaking_location() -> None:
    capability = _capability(method="GET")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://evil.test/object?token=provider-secret"},
        )

    with pytest.raises(StorageGetContentFailure) as captured:
        _adapter(handler).get(capability, max_bytes=1_024)

    assert len(seen) == 1
    assert "provider-secret" not in str(captured.value)
    assert "evil.test" not in str(captured.value)


@pytest.mark.parametrize(
    "actual_mime",
    ["text/plain", "application/json; charset=utf-8", "Application/JSON"],
)
def test_get_requires_exact_capability_mime(actual_mime: str) -> None:
    capability = _capability(method="GET")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": actual_mime},
            content=b"bounded-object",
        )

    with pytest.raises(StorageGetContentFailure, match="content type rejected"):
        _adapter(handler).get(capability, max_bytes=1_024)


@pytest.mark.parametrize(
    ("call_limit", "capability_limit", "hard_limit"),
    [(8, 100, 100), (100, 8, 100), (100, 100, 8)],
)
def test_get_rejects_content_length_over_smallest_limit(
    call_limit: int,
    capability_limit: int,
    hard_limit: int,
) -> None:
    capability = _capability(method="GET", max_bytes=capability_limit)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": "14",
            },
            content=b"",
        )

    with pytest.raises(StorageGetContentFailure, match="exceeds byte limit"):
        _adapter(handler, hard_max_bytes=hard_limit).get(
            capability,
            max_bytes=call_limit,
        )


def test_get_stops_a_chunked_stream_at_the_byte_limit() -> None:
    body = b"0123456789"
    capability = _capability(method="GET", body=body, max_bytes=8)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_Chunks(b"0123", b"4567", b"89"),
        )

    with pytest.raises(StorageGetContentFailure, match="exceeds byte limit"):
        _adapter(handler).get(capability, max_bytes=100)


def test_get_enforces_absolute_deadline_during_slow_progress_stream() -> None:
    """Repeated small chunks must not evade the idle HTTP read timeout."""

    body = b"abcdef"
    capability = _capability(method="GET", body=body)
    clock = _MonotonicClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_SlowChunks(clock, b"ab", b"cd", b"ef", advance=0.6),
        )

    with pytest.raises(StorageInfrastructureFailure, match="deadline exceeded"):
        _adapter(handler, monotonic=clock).get(
            capability,
            max_bytes=1_024,
            deadline_monotonic=1.0,
        )


def test_put_enforces_absolute_deadline_while_streaming_request_body() -> None:
    content = b"x" * (64 * 1024 * 3)
    capability = _capability(method="PUT", max_bytes=len(content) + 1)
    # The monotonically increasing values model progress that is continuous
    # enough to evade an idle write timeout but crosses the one invocation
    # budget while the third bounded upload chunk is about to be sent.
    clock_values = iter((0.0, 0.0, 0.0, 0.6, 1.2))

    def handler(request: httpx.Request) -> httpx.Response:
        # MockTransport calls request.read before it reaches this handler; a
        # real transport observes the same bounded source iterator at writes.
        request.read()
        return httpx.Response(201)

    with pytest.raises(StorageInfrastructureFailure, match="deadline exceeded"):
        _adapter(
            handler,
            hard_max_bytes=len(content) + 1,
            monotonic=lambda: next(clock_values),
        ).put_create_only(
            capability,
            content=content,
            content_type="application/json",
            deadline_monotonic=1.0,
        )


@pytest.mark.parametrize("status_code", [400, 403, 404, 422])
def test_get_classifies_4xx_as_deterministic_content_failure(status_code: int) -> None:
    capability = _capability(method="GET")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=b"must-not-enter-error")

    with pytest.raises(StorageGetContentFailure) as captured:
        _adapter(handler).get(capability, max_bytes=1_024)

    assert "must-not-enter-error" not in str(captured.value)


@pytest.mark.parametrize("status_code", [408, 425, 429])
def test_get_classifies_transient_4xx_as_infrastructure_failure(
    status_code: int,
) -> None:
    capability = _capability(method="GET")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=b"provider-secret")

    with pytest.raises(StorageInfrastructureFailure) as captured:
        _adapter(handler).get(capability, max_bytes=1_024)

    assert "provider-secret" not in str(captured.value)


@pytest.mark.parametrize("status_code", [500, 502, 503, 599])
def test_get_classifies_5xx_as_retryable_infrastructure_failure(
    status_code: int,
) -> None:
    capability = _capability(method="GET")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=b"provider-secret")

    with pytest.raises(StorageInfrastructureFailure) as captured:
        _adapter(handler).get(capability, max_bytes=1_024)

    assert "provider-secret" not in str(captured.value)


def test_get_classifies_network_failure_as_retryable_infrastructure_failure() -> None:
    capability = _capability(method="GET")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("token=provider-secret", request=request)

    with pytest.raises(StorageInfrastructureFailure) as captured:
        _adapter(handler).get(capability, max_bytes=1_024)

    assert "provider-secret" not in str(captured.value)


def test_get_revalidates_capability_integrity_at_use() -> None:
    capability = _capability(method="GET")
    object.__setattr__(capability, "storage_host", "mutated.example.test")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with pytest.raises(StorageGetContentFailure, match="capability rejected"):
        _adapter(handler).get(capability, max_bytes=1_024)

    assert calls == 0


def test_adapter_enforces_capability_method_and_access_before_network() -> None:
    get_capability = _capability(method="GET")
    put_capability = _capability(method="PUT")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    adapter = _adapter(handler)
    with pytest.raises(StorageGetContentFailure, match="method rejected"):
        adapter.get(put_capability, max_bytes=1_024)
    with pytest.raises(StorageContentFailure, match="method rejected"):
        adapter.put_create_only(
            get_capability,
            content=b"result",
            content_type="application/json",
        )

    assert calls == 0


def test_put_uses_conditional_create_only_request() -> None:
    content = b'{"schema":"surya_layout_artifact/v1"}'
    capability = _capability(method="PUT", max_bytes=1_024)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.read() == content
        return httpx.Response(201)

    _adapter(handler).put_create_only(
        capability,
        content=content,
        content_type="application/json",
    )

    assert len(seen) == 1
    assert seen[0].method == "PUT"
    assert seen[0].headers["content-type"] == "application/json"
    assert seen[0].headers["if-none-match"] == "*"
    assert seen[0].headers["x-upsert"] == "false"
    assert "authorization" not in seen[0].headers
    assert "cookie" not in seen[0].headers


@pytest.mark.parametrize("status_code", [409, 412])
def test_put_classifies_create_only_conflict(status_code: int) -> None:
    capability = _capability(method="PUT")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, content=b"provider-secret")

    with pytest.raises(StoragePutConflictFailure) as captured:
        _adapter(handler).put_create_only(
            capability,
            content=b"result",
            content_type="application/json",
        )

    assert calls == 1
    assert "provider-secret" not in str(captured.value)


def test_put_does_not_retry_5xx_and_classifies_it_as_infrastructure() -> None:
    capability = _capability(method="PUT")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(StorageInfrastructureFailure):
        _adapter(handler).put_create_only(
            capability,
            content=b"result",
            content_type="application/json",
        )

    assert calls == 1


@pytest.mark.parametrize("status_code", [408, 425, 429])
def test_put_classifies_transient_4xx_as_infrastructure_failure(
    status_code: int,
) -> None:
    capability = _capability(method="PUT")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=b"provider-secret")

    with pytest.raises(StorageInfrastructureFailure) as captured:
        _adapter(handler).put_create_only(
            capability,
            content=b"result",
            content_type="application/json",
        )

    assert "provider-secret" not in str(captured.value)


def test_put_ack_loss_then_retry_surfaces_conflict_without_overwrite() -> None:
    """A caller retry cannot turn an ambiguous create into an overwrite."""

    capability = _capability(method="PUT")
    content = b'{"schema":"surya_layout_artifact/v1"}'
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.read() == content
        if len(seen) == 1:
            raise httpx.ReadTimeout("ack lost", request=request)
        # The object may have been created by the first request before its
        # acknowledgement was lost.  The second create-only attempt must
        # expose that ambiguity, never report success or overwrite it.
        return httpx.Response(409, content=b"provider-secret")

    adapter = _adapter(handler)
    with pytest.raises(StorageInfrastructureFailure):
        adapter.put_create_only(
            capability,
            content=content,
            content_type="application/json",
        )
    with pytest.raises(StoragePutConflictFailure) as captured:
        adapter.put_create_only(
            capability,
            content=content,
            content_type="application/json",
        )

    assert len(seen) == 2
    for request in seen:
        assert request.method == "PUT"
        assert request.headers["if-none-match"] == "*"
        assert request.headers["x-upsert"] == "false"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
    assert "provider-secret" not in str(captured.value)


def test_put_rejects_wrong_mime_and_over_hard_limit_before_network() -> None:
    capability = _capability(method="PUT", max_bytes=1_024)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(201)

    adapter = _adapter(handler, hard_max_bytes=4)
    with pytest.raises(StorageContentFailure, match="MIME rejected"):
        adapter.put_create_only(
            capability,
            content=b"ok",
            content_type="text/plain",
        )
    with pytest.raises(StorageContentFailure, match="exceeds byte limit"):
        adapter.put_create_only(
            capability,
            content=b"12345",
            content_type="application/json",
        )

    assert calls == 0
