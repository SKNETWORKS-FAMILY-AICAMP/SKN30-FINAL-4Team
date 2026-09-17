"""Focused wire-contract tests for the resident remote Model 1 adapter."""

from __future__ import annotations

import json

import httpx
import pytest

from prereview_model1_service.contract import MODEL1_DEFAULT_MAX_REQUEST_BYTES
from worker.adapters.model1_http import (
    MODEL1_REMOTE_RESULT_SCHEMA_VERSION,
    MODEL1_WEIGHT_SHA256,
    Model1HttpAdapter,
    Model1HttpContractError,
    Model1HttpError,
    canonical_model1_input_digest,
    validate_model1_remote_base_url,
)


_INPUT = {
    "title": "테스트 사업",
    "purpose": "사업화 지원",
    "content": "시제품 제작과 판로 지원",
    "target_text": "중소기업",
}
_REMOTE_RUNTIME_MANIFEST = "b" * 64
_TOKEN = "model1_test_token_0123456789abcde"


class _Chunks(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self._chunks


def _response(payload: object, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Type": "application/json", **headers},
        stream=_Chunks(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
    )


def _result(
    inputs: dict[str, object] = _INPUT,
    *,
    weight_sha256: str = MODEL1_WEIGHT_SHA256,
    runtime_manifest_sha256: str = _REMOTE_RUNTIME_MANIFEST,
) -> dict[str, object]:
    return {
        "schema_version": MODEL1_REMOTE_RESULT_SCHEMA_VERSION,
        "input_digest": canonical_model1_input_digest(inputs),
        "producer": {
            "service": "prereview-model1",
            "weight_sha256": weight_sha256,
            "runtime_manifest_sha256": runtime_manifest_sha256,
        },
        "prediction": {
            "support_type_pred": "사업화",
            "confidence": 0.91,
            "status": "신뢰",
        },
    }


def _ready(
    *,
    device: str = "cuda",
    max_request_bytes: int = MODEL1_DEFAULT_MAX_REQUEST_BYTES,
    weight_sha256: str = MODEL1_WEIGHT_SHA256,
    runtime_manifest_sha256: str = _REMOTE_RUNTIME_MANIFEST,
) -> dict[str, object]:
    return {
        "ready": True,
        "max_concurrency": 1,
        "max_request_bytes": max_request_bytes,
        "device": device,
        "producer": {
            "service": "prereview-model1",
            "weight_sha256": weight_sha256,
            "runtime_manifest_sha256": runtime_manifest_sha256,
        },
    }


def _adapter(handler: object, **kwargs: object) -> Model1HttpAdapter:
    return Model1HttpAdapter(
        base_url="https://model1.tailnet.test",
        bearer_token=_TOKEN,
        expected_runtime_manifest_sha256=_REMOTE_RUNTIME_MANIFEST,
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,
    )


def test_predict_sends_only_canonical_input_and_binds_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(_result())

    result = _adapter(handler).predict(dict(_INPUT))

    assert result == {
        "support_type_pred": "사업화",
        "confidence": 0.91,
        "status": "신뢰",
    }
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url == httpx.URL("https://model1.tailnet.test/v1/model1/predict")
    assert request.headers["authorization"] == f"Bearer {_TOKEN}"
    assert request.headers["idempotency-key"] == canonical_model1_input_digest(_INPUT)
    assert json.loads(request.content) == {"input": _INPUT}


def test_retryable_status_retries_once_with_same_key_and_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return _response({"detail": "not exposed"}, 503)
        return _response(_result())

    assert _adapter(handler).predict(dict(_INPUT))["support_type_pred"] == "사업화"
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]


@pytest.mark.parametrize(
    ("retry_after", "wall_clock", "expected_delay"),
    (
        ("2", 0.0, 2.0),
        ("999", 0.0, 5.0),
        ("invalid", 0.0, 1.0),
        (None, 0.0, 1.0),
        ("Thu, 01 Jan 1970 00:16:44 GMT", 1_000.0, 4.0),
    ),
)
def test_429_waits_for_bounded_retry_after_before_retrying(
    retry_after: str | None,
    wall_clock: float,
    expected_delay: float,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = {} if retry_after is None else {"Retry-After": retry_after}
            return _response({"detail": "busy"}, 429, **headers)
        return _response(_result())

    result = _adapter(
        handler,
        sleeper=sleeps.append,
        wall_clock=lambda: wall_clock,
    ).predict(dict(_INPUT))

    assert result["status"] == "신뢰"
    assert calls == 2
    assert sleeps == [expected_delay]


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("slow")])
def test_transient_connection_or_timeout_retries_once(error: Exception) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return _response(_result())

    assert _adapter(handler).predict(dict(_INPUT))["status"] == "신뢰"
    assert calls == 2


def test_non_retryable_error_and_diagnostics_are_redacted() -> None:
    private_text = "private-document-marker"
    private_token = _TOKEN

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"provider says {private_text} {private_token}")

    with pytest.raises(Model1HttpError) as captured:
        _adapter(handler).predict({**_INPUT, "content": private_text})

    message = str(captured.value)
    assert private_text not in message
    assert private_token not in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"extra": True}),
        lambda value: value.__setitem__("input_digest", "0" * 64),
        lambda value: value["producer"].__setitem__(  # type: ignore[index]
            "weight_sha256", "0" * 64
        ),
        lambda value: value["prediction"].__setitem__("status", "unknown"),  # type: ignore[index]
    ],
)
def test_response_requires_exact_bound_envelope(mutate: object) -> None:
    body = _result()
    mutate(body)  # type: ignore[operator]

    with pytest.raises(Model1HttpContractError):
        _adapter(lambda _request: _response(body)).predict(dict(_INPUT))


@pytest.mark.parametrize(
    "inputs",
    [
        {"title": "x", "purpose": "", "content": "", "target_text": "", "extra": "no"},
        {"title": "x", "purpose": "", "content": ""},
        {"title": 1, "purpose": "", "content": "", "target_text": ""},
        {"title": "", "purpose": "", "content": "", "target_text": ""},
    ],
)
def test_input_is_a_strict_four_field_allowlist(inputs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _adapter(lambda _request: _response(_result())).predict(inputs)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://gpu.internal",
        "http://10.0.0.8",
        "ftp://gpu.internal",
        "https://user@gpu.internal",
        "https://gpu.internal/path",
        "https://gpu.internal?q=1",
    ],
)
def test_base_url_fails_closed(base_url: str) -> None:
    with pytest.raises(ValueError):
        validate_model1_remote_base_url(base_url)


def test_loopback_http_requires_explicit_test_opt_in() -> None:
    with pytest.raises(ValueError):
        validate_model1_remote_base_url("http://127.0.0.1:8788")
    assert (
        validate_model1_remote_base_url(
            "http://127.0.0.1:8788", allow_loopback_http=True
        )
        == "http://127.0.0.1:8788"
    )


def test_internal_http_requires_one_explicit_exact_hostname() -> None:
    base_url = "http://model1-cpu:8791"
    with pytest.raises(ValueError):
        validate_model1_remote_base_url(base_url)
    assert (
        validate_model1_remote_base_url(
            base_url, internal_http_hostname="model1-cpu"
        )
        == base_url
    )
    for unsafe in (
        "http://model1-cpu.attacker:8791",
        "http://other:8791",
        "http://model1-cpu:8080",
    ):
        with pytest.raises(ValueError):
            validate_model1_remote_base_url(
                unsafe, internal_http_hostname="model1-cpu"
            )


def test_ready_is_authenticated_and_verifies_pinned_identity() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(_ready())

    assert _adapter(handler).check_ready() is None
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url == httpx.URL(
        "https://model1.tailnet.test/v1/model1/ready"
    )
    assert seen[0].headers["authorization"] == f"Bearer {_TOKEN}"
    assert seen[0].content == b""


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"extra": True}),
        lambda value: value.__setitem__("ready", False),
        lambda value: value.__setitem__("max_concurrency", True),
        lambda value: value.__setitem__("max_concurrency", 2),
        lambda value: value.__setitem__("max_request_bytes", 1_048_576),
        lambda value: value.__setitem__("device", "auto"),
        lambda value: value["producer"].__setitem__(  # type: ignore[index]
            "weight_sha256", "0" * 64
        ),
        lambda value: value["producer"].__setitem__(  # type: ignore[index]
            "runtime_manifest_sha256", "0" * 64
        ),
    ],
)
def test_ready_requires_exact_bound_envelope(mutate: object) -> None:
    body = _ready()
    mutate(body)  # type: ignore[operator]

    with pytest.raises(Model1HttpContractError):
        _adapter(lambda _request: _response(body)).check_ready()


@pytest.mark.parametrize(
    "token",
    (
        "too-short",
        "a" * 31,
        "a" * 513,
        "a" * 31 + ".",
        "a" * 31 + " ",
    ),
)
def test_bearer_matches_service_url_safe_length_contract(token: str) -> None:
    with pytest.raises(ValueError, match="bearer token is invalid"):
        Model1HttpAdapter(
            base_url="https://model1.tailnet.test",
            bearer_token=token,
            expected_runtime_manifest_sha256=_REMOTE_RUNTIME_MANIFEST,
        )


def test_body_limits_apply_before_send_and_while_reading() -> None:
    called = False

    def too_large_request(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _response(_result())

    with pytest.raises(Model1HttpContractError):
        _adapter(too_large_request, max_request_bytes=512).predict(
            {**_INPUT, "content": "x" * 600}
        )
    assert not called

    response = _result()
    raw = json.dumps(response).encode("utf-8")
    with pytest.raises(Model1HttpContractError):
        _adapter(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
                stream=_Chunks(raw),
            ),
            max_response_bytes=20,
        ).predict(dict(_INPUT))


def test_default_request_cap_accepts_exact_boundary_and_rejects_one_more_byte() -> None:
    def input_for_size(size: int) -> dict[str, str]:
        fields = {"title": "", "purpose": "", "content": "", "target_text": ""}
        empty_body = json.dumps(
            {"input": fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fields["content"] = "x" * (size - len(empty_body))
        body = json.dumps(
            {"input": fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert len(body) == size
        return fields

    boundary = input_for_size(MODEL1_DEFAULT_MAX_REQUEST_BYTES)
    seen_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_sizes.append(len(request.content))
        return _response(_result(boundary))

    assert _adapter(handler).predict(boundary)["status"] == "신뢰"
    assert seen_sizes == [MODEL1_DEFAULT_MAX_REQUEST_BYTES]

    with pytest.raises(Model1HttpContractError):
        _adapter(handler).predict(
            input_for_size(MODEL1_DEFAULT_MAX_REQUEST_BYTES + 1)
        )
    assert seen_sizes == [MODEL1_DEFAULT_MAX_REQUEST_BYTES]
