"""Focused wire-contract tests for the combined resident Model 2 + 3 API."""

from __future__ import annotations

import json

import httpx
import pytest

from prereview_model23_service.contract import MODEL23_DEFAULT_MAX_REQUEST_BYTES
from worker.adapters.model23_http import (
    MODEL2_BUNDLE_SHA256,
    MODEL2_REMOTE_RESULT_SCHEMA_VERSION,
    MODEL3_POOL_SHA256,
    MODEL3_REMOTE_RESULT_SCHEMA_VERSION,
    Model2HttpAdapter,
    Model3HttpAdapter,
    Model23HttpContractError,
    Model23HttpError,
    canonical_model23_input_digest,
    validate_model23_remote_base_url,
)
from worker.contracts.ml_result import INPUT_EVIDENCE_MISSING
from worker.ml_reference import MlUnavailable


_MODEL2_INPUT = {
    "title": "스마트 기술 사업화",
    "evidence_text": "기업당 최대 5천만원을 지원한다.",
    "quantities": {"support_scale": ["기업당 최대 5천만원"]},
    "support_type": "사업화",
    "support_type_status": "신뢰",
    "support_type_model_version": "model1-v1",
}
_MODEL3_INPUT = {
    "evidence_text": "기업당 최대 5천만원, 사업기간 12개월",
    "quantities": {
        "support_scale": ["기업당 최대 5천만원"],
        "support_period": ["12개월"],
    },
    "support_type": "사업화",
    "support_type_status": "신뢰",
    "support_type_model_version": "model1-v1",
}
_RUNTIME_MANIFEST = "b" * 64
_TOKEN = "model23_test_token_0123456789abcde"


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


def _producer(**overrides: str) -> dict[str, str]:
    value = {
        "service": "prereview-model23",
        "runtime_manifest_sha256": _RUNTIME_MANIFEST,
        "model2_bundle_sha256": MODEL2_BUNDLE_SHA256,
        "model3_pool_sha256": MODEL3_POOL_SHA256,
    }
    value.update(overrides)
    return value


def _result(
    model: int,
    inputs: dict[str, object],
    *,
    producer: dict[str, str] | None = None,
    prediction: dict[str, object] | None = None,
) -> dict[str, object]:
    if model == 2:
        schema = MODEL2_REMOTE_RESULT_SCHEMA_VERSION
        default_prediction: dict[str, object] = {
            "predictions": [{"pred_won": 50_000_000, "bucket": "5천만원 이하"}],
            "adapter": {"private_diagnostic": "discarded"},
        }
    else:
        schema = MODEL3_REMOTE_RESULT_SCHEMA_VERSION
        default_prediction = {
            "score": 0.96,
            "level": "L1 support_typexsupport_method",
            "top1_axis": "per_recipient",
            "cohort_n": 125,
        }
    return {
        "schema_version": schema,
        "input_digest": canonical_model23_input_digest(inputs),
        "producer": _producer() if producer is None else producer,
        "prediction": default_prediction if prediction is None else prediction,
    }


def _ready(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ready": True,
        "max_concurrency": 1,
        "max_request_bytes": MODEL23_DEFAULT_MAX_REQUEST_BYTES,
        "device": "cpu",
        "producer": _producer(),
    }
    value.update(overrides)
    return value


def _model2(handler: object, **kwargs: object) -> Model2HttpAdapter:
    return Model2HttpAdapter(
        base_url="https://model23.tailnet.test",
        bearer_token=_TOKEN,
        expected_runtime_manifest_sha256=_RUNTIME_MANIFEST,
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,
    )


def _model3(handler: object, **kwargs: object) -> Model3HttpAdapter:
    return Model3HttpAdapter(
        base_url="https://model23.tailnet.test",
        bearer_token=_TOKEN,
        expected_runtime_manifest_sha256=_RUNTIME_MANIFEST,
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,
    )


@pytest.mark.parametrize(
    ("model", "inputs", "expected_path"),
    (
        (2, _MODEL2_INPUT, "/v1/model2/predict"),
        (3, _MODEL3_INPUT, "/v1/model3/predict"),
    ),
)
def test_predict_sends_only_canonical_current_payload(
    model: int, inputs: dict[str, object], expected_path: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(_result(model, inputs))

    adapter = _model2(handler) if model == 2 else _model3(handler)
    result = adapter.predict(dict(inputs))

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == expected_path
    assert request.headers["authorization"] == f"Bearer {_TOKEN}"
    assert request.headers["idempotency-key"] == canonical_model23_input_digest(inputs)
    assert json.loads(request.content) == {"input": inputs}
    if model == 2:
        assert result == {"pred_won": 50_000_000, "bucket": "5천만원 이하"}
    else:
        assert result["level"] == "동일 유형 대비 비전형적"
        assert result["cause_axes"] == ["기업(과제)당 지원한도"]


def test_canonical_digest_is_independent_of_mapping_order() -> None:
    reversed_input = dict(reversed(list(_MODEL2_INPUT.items())))
    assert canonical_model23_input_digest(reversed_input) == canonical_model23_input_digest(
        _MODEL2_INPUT
    )


@pytest.mark.parametrize(
    ("inputs", "message"),
    (
        ({**_MODEL3_INPUT, "support_type": ""}, "지원유형 근거가 없다"),
        ({**_MODEL3_INPUT, "support_type_status": "판단보류"}, "판단보류"),
    ),
)
def test_model3_preserves_support_type_and_withheld_guards(
    inputs: dict[str, object], message: str
) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _response(_result(3, inputs))

    with pytest.raises(MlUnavailable, match=message) as captured:
        _model3(handler).predict(inputs)

    assert captured.value.reason_code == INPUT_EVIDENCE_MISSING
    assert not called


def test_ready_is_authenticated_and_pins_all_identity_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(_ready())

    assert _model2(handler).check_ready() is None
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/model23/ready"
    assert seen[0].headers["authorization"] == f"Bearer {_TOKEN}"
    assert seen[0].content == b""


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update({"extra": True}),
        lambda value: value.__setitem__("ready", False),
        lambda value: value.__setitem__("max_concurrency", True),
        lambda value: value.__setitem__("max_concurrency", 2),
        lambda value: value.__setitem__("max_request_bytes", 65_536),
        lambda value: value.__setitem__("device", "cuda"),
        lambda value: value["producer"].__setitem__(  # type: ignore[index]
            "runtime_manifest_sha256", "0" * 64
        ),
        lambda value: value["producer"].__setitem__(  # type: ignore[index]
            "model2_bundle_sha256", "0" * 64
        ),
        lambda value: value["producer"].__setitem__(  # type: ignore[index]
            "model3_pool_sha256", "0" * 64
        ),
    ),
)
def test_ready_requires_exact_capability_and_identity(mutate: object) -> None:
    body = _ready()
    mutate(body)  # type: ignore[operator]
    with pytest.raises(Model23HttpContractError):
        _model2(lambda _request: _response(body)).check_ready()


@pytest.mark.parametrize(
    "mutation",
    (
        "extra",
        "digest",
        "service",
        "runtime",
        "bundle",
        "pool",
        "schema",
        "prediction_type",
    ),
)
def test_prediction_response_requires_exact_bound_envelope(mutation: str) -> None:
    body = _result(2, _MODEL2_INPUT)
    if mutation == "extra":
        body["extra"] = True
    elif mutation == "digest":
        body["input_digest"] = "0" * 64
    elif mutation == "schema":
        body["schema_version"] = MODEL3_REMOTE_RESULT_SCHEMA_VERSION
    elif mutation == "prediction_type":
        body["prediction"] = []
    else:
        producer = body["producer"]
        assert isinstance(producer, dict)
        key = {
            "service": "service",
            "runtime": "runtime_manifest_sha256",
            "bundle": "model2_bundle_sha256",
            "pool": "model3_pool_sha256",
        }[mutation]
        producer[key] = "wrong" if key == "service" else "0" * 64

    with pytest.raises(Model23HttpContractError):
        _model2(lambda _request: _response(body)).predict(dict(_MODEL2_INPUT))


def test_invalid_model_output_is_redacted_as_contract_failure() -> None:
    private_text = "private-document-marker"
    body = _result(
        3,
        _MODEL3_INPUT,
        prediction={"score": 0.5, "level": "L0", "top1_axis": private_text},
    )
    with pytest.raises(Model23HttpContractError) as captured:
        _model3(lambda _request: _response(body)).predict(dict(_MODEL3_INPUT))
    assert private_text not in str(captured.value)


def test_retryable_status_retries_once_with_same_key_and_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return _response({"detail": "private"}, 503)
        return _response(_result(2, _MODEL2_INPUT))

    assert _model2(handler).predict(dict(_MODEL2_INPUT))["pred_won"] == 50_000_000
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers["idempotency-key"] == requests[1].headers[
        "idempotency-key"
    ]


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
def test_429_honors_bounded_retry_after(
    retry_after: str | None, wall_clock: float, expected_delay: float
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = {} if retry_after is None else {"Retry-After": retry_after}
            return _response({"detail": "busy"}, 429, **headers)
        return _response(_result(2, _MODEL2_INPUT))

    _model2(
        handler, sleeper=sleeps.append, wall_clock=lambda: wall_clock
    ).predict(dict(_MODEL2_INPUT))
    assert calls == 2
    assert sleeps == [expected_delay]


def test_errors_and_provider_body_are_redacted() -> None:
    private_text = "private-document-marker"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"provider says {private_text} {_TOKEN}")

    with pytest.raises(Model23HttpError) as captured:
        _model2(handler).predict({**_MODEL2_INPUT, "evidence_text": private_text})
    assert private_text not in str(captured.value)
    assert _TOKEN not in str(captured.value)


@pytest.mark.parametrize(
    "base_url",
    (
        "http://model23.internal",
        "http://10.0.0.8",
        "ftp://model23.internal",
        "https://user@model23.internal",
        "https://model23.internal/path",
        "https://model23.internal?q=1",
    ),
)
def test_base_url_fails_closed(base_url: str) -> None:
    with pytest.raises(ValueError):
        validate_model23_remote_base_url(base_url)


def test_loopback_and_internal_http_require_explicit_exact_opt_in() -> None:
    with pytest.raises(ValueError):
        validate_model23_remote_base_url("http://127.0.0.1:8792")
    assert validate_model23_remote_base_url(
        "http://127.0.0.1:8792", allow_loopback_http=True
    ) == "http://127.0.0.1:8792"

    base_url = "http://model23-cpu:8792"
    with pytest.raises(ValueError):
        validate_model23_remote_base_url(base_url)
    assert validate_model23_remote_base_url(
        base_url, internal_http_hostname="model23-cpu"
    ) == base_url
    for unsafe in (
        "http://model23-cpu.attacker:8792",
        "http://other:8792",
        "http://model23-cpu:8080",
    ):
        with pytest.raises(ValueError):
            validate_model23_remote_base_url(
                unsafe, internal_http_hostname="model23-cpu"
            )


@pytest.mark.parametrize(
    "token",
    ("too-short", "a" * 31, "a" * 513, "a" * 31 + ".", "a" * 31 + " "),
)
def test_bearer_matches_service_url_safe_length_contract(token: str) -> None:
    with pytest.raises(ValueError, match="bearer token is invalid"):
        Model2HttpAdapter(
            base_url="https://model23.tailnet.test",
            bearer_token=token,
            expected_runtime_manifest_sha256=_RUNTIME_MANIFEST,
        )


def test_body_limits_apply_before_send_and_while_reading() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _response(_result(2, _MODEL2_INPUT))

    with pytest.raises(Model23HttpContractError):
        _model2(handler, max_request_bytes=65_536).predict(
            {**_MODEL2_INPUT, "evidence_text": "x" * 65_536}
        )
    assert not called

    raw = json.dumps(_result(2, _MODEL2_INPUT)).encode("utf-8")
    with pytest.raises(Model23HttpContractError):
        _model2(
            lambda _request: httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw)),
                },
                stream=_Chunks(raw),
            ),
            max_response_bytes=20,
        ).predict(dict(_MODEL2_INPUT))


def test_non_json_serializable_input_fails_before_send() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _response(_result(2, _MODEL2_INPUT))

    with pytest.raises(ValueError, match="cannot be serialized"):
        _model2(handler).predict({"unsupported": object()})
    assert not called
