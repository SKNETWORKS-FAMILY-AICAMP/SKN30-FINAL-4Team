"""Trusted-side HTTP adapter for the resident remote Model 1 service.

Model 1 is a small, immutable KLUE-BERT classifier which may live on the GPU
node instead of being started once per analysis-job.  The GPU node is *not* a
database worker: this adapter sends only the four classifier input fields and
accepts only one tightly-bound prediction response.  In particular, it never
forwards database, Supabase, OpenAI, or browser credentials.

The service protocol is intentionally small and deterministic:

* ``POST /v1/model1/predict``;
* the canonical four-field input digest is also the ``Idempotency-Key``;
* the returned digest, Model 1 weight digest, and runtime manifest digest
  must exactly match the values pinned by the trusted worker.

Provider diagnostics and document text are deliberately not included in
exceptions.  The caller's existing ML boundary records the model-local
failure without disclosing either of them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from hashlib import sha256
import ipaddress
import json
import math
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from prereview_model1_service.contract import (
    MODEL1_DEFAULT_MAX_REQUEST_BYTES,
    MODEL1_MAX_REQUEST_BYTES,
    MODEL1_MIN_REQUEST_BYTES,
)

from ..contracts.ml_result import MlModelId
from ..ml_reference import MODEL_1_FIELDS
from .ml_subprocess import SubprocessModelError, normalize_model1_output

__all__ = [
    "MODEL1_REMOTE_SERVICE",
    "MODEL1_REMOTE_RESULT_SCHEMA_VERSION",
    "MODEL1_WEIGHT_SHA256",
    "Model1HttpAdapter",
    "Model1HttpContractError",
    "Model1HttpError",
    "canonical_model1_input_digest",
    "validate_model1_remote_base_url",
]


MODEL1_REMOTE_SERVICE = "prereview-model1"
MODEL1_REMOTE_RESULT_SCHEMA_VERSION = "prereview.model1-predict-result/v1"
# These are the registered artifact values emitted by prepare_model1_runtime.
# Keeping the remote service pinned to the same release prevents a GPU-node
# deployment typo from silently changing classification semantics.
MODEL1_WEIGHT_SHA256 = "8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779"

_JSON_CONTENT_TYPE = "application/json"
_READY_PATH = "/v1/model1/ready"
_PREDICT_PATH = "/v1/model1/predict"
_MAX_BODY_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
_DEFAULT_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_AFTER_SECONDS = 5.0
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BEARER_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}")
_INTERNAL_HTTP_HOSTNAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 502, 503, 504})


class Model1HttpError(RuntimeError):
    """A redacted remote Model 1 transport or provider failure."""

    def __init__(self) -> None:
        super().__init__("remote Model 1 service unavailable")


class Model1HttpContractError(RuntimeError):
    """The remote response did not prove it belongs to this Model 1 call."""

    def __init__(self) -> None:
        super().__init__("remote Model 1 response violated its contract")


def _is_literal_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_model1_remote_base_url(
    value: str,
    *,
    allow_loopback_http: bool = False,
    internal_http_hostname: str | None = None,
) -> str:
    """Validate one service origin; bearer-bearing remote traffic is HTTPS by default.

    Plain HTTP is limited to an explicit literal loopback test opt-in or one
    exact, single-label Docker-internal hostname supplied by the trusted
    worker configuration.  A suffix or subdomain match is deliberately not
    accepted.
    """

    if not isinstance(value, str):
        raise ValueError("remote Model 1 base URL must be an HTTPS origin")
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise ValueError("remote Model 1 base URL must be an HTTPS origin") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 0 < port < 65_536)
    ):
        raise ValueError("remote Model 1 base URL must be an HTTPS origin")
    trusted_internal_hostname = _validated_internal_http_hostname(
        internal_http_hostname
    )
    http_is_allowed = allow_loopback_http and _is_literal_loopback(parsed.hostname)
    if (
        parsed.scheme == "http"
        and trusted_internal_hostname is not None
        and parsed.hostname.lower() == trusted_internal_hostname
    ):
        if port != 8791:
            raise ValueError(
                "remote Model 1 internal HTTP origin must use the fixed service port"
            )
        http_is_allowed = True
    if parsed.scheme == "http" and not http_is_allowed:
        raise ValueError("remote Model 1 base URL must be an HTTPS origin")

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _validated_internal_http_hostname(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("remote Model 1 internal HTTP hostname is invalid")
    normalized = value.lower()
    if (
        _INTERNAL_HTTP_HOSTNAME.fullmatch(normalized) is None
        or normalized == "localhost"
    ):
        raise ValueError("remote Model 1 internal HTTP hostname is invalid")
    return normalized


def _validated_sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validated_bearer_token(value: str) -> str:
    if not isinstance(value, str) or _BEARER_TOKEN.fullmatch(value) is None:
        raise ValueError("remote Model 1 bearer token is invalid")
    return value


def _validated_body_cap(value: int, *, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_BODY_BYTES
    ):
        raise ValueError(f"remote Model 1 {label} limit is invalid")
    return value


def _validated_request_cap(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MODEL1_MIN_REQUEST_BYTES <= value <= MODEL1_MAX_REQUEST_BYTES
    ):
        raise ValueError("remote Model 1 request limit is invalid")
    return value


def _canonical_input(inputs: Mapping[str, Any]) -> dict[str, str]:
    """Return the only data permitted to cross the Model 1 GPU boundary."""

    if not isinstance(inputs, Mapping) or set(inputs) != set(MODEL_1_FIELDS):
        raise ValueError("remote Model 1 input must contain exactly four configured fields")
    canonical: dict[str, str] = {}
    for field in MODEL_1_FIELDS:
        value = inputs[field]
        if not isinstance(value, str):
            raise ValueError("remote Model 1 input fields must be strings")
        canonical[field] = value
    if not any(value.strip() for value in canonical.values()):
        raise ValueError("remote Model 1 input must contain text")
    return canonical


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("remote Model 1 input cannot be serialized") from error


def canonical_model1_input_digest(inputs: Mapping[str, Any]) -> str:
    """Return the stable digest used for request binding and idempotency."""

    return sha256(_canonical_json(_canonical_input(inputs))).hexdigest()


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


class Model1HttpAdapter:
    """Synchronous ``MlModel`` implementation backed by a resident Model 1 API."""

    model_id = MlModelId.MODEL_1_SUPPORT_TYPE

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        expected_runtime_manifest_sha256: str,
        timeout_seconds: float = 30.0,
        max_request_bytes: int = MODEL1_DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        expected_weight_sha256: str = MODEL1_WEIGHT_SHA256,
        artifact_version: str | None = None,
        transport: httpx.BaseTransport | None = None,
        allow_loopback_http: bool = False,
        internal_http_hostname: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._base_url = validate_model1_remote_base_url(
            base_url,
            allow_loopback_http=allow_loopback_http,
            internal_http_hostname=internal_http_hostname,
        )
        self._bearer_token = _validated_bearer_token(bearer_token)
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("remote Model 1 timeout must be a finite positive number")
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._max_request_bytes = _validated_request_cap(max_request_bytes)
        self._max_response_bytes = _validated_body_cap(
            max_response_bytes, label="response"
        )
        self._expected_weight_sha256 = _validated_sha256(
            expected_weight_sha256, label="remote Model 1 weight SHA-256"
        )
        self._expected_runtime_manifest_sha256 = _validated_sha256(
            expected_runtime_manifest_sha256,
            label="remote Model 1 runtime manifest SHA-256",
        )
        if artifact_version is not None and (
            not isinstance(artifact_version, str) or not artifact_version.strip()
        ):
            raise ValueError("remote Model 1 artifact version is invalid")
        self.artifact_version = artifact_version
        self._transport = transport
        if not callable(sleeper) or not callable(wall_clock):
            raise ValueError("remote Model 1 retry timing hooks are invalid")
        self._sleeper = sleeper
        self._wall_clock = wall_clock

    def check_ready(self) -> None:
        """Authenticate readiness and verify the pinned resident runtime."""

        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": _JSON_CONTENT_TYPE,
        }
        payload = self._request_json(
            "GET", f"{self._base_url}{_READY_PATH}", headers=headers
        )
        if set(payload) != {
            "ready",
            "max_concurrency",
            "max_request_bytes",
            "device",
            "producer",
        }:
            raise Model1HttpContractError()
        if (
            payload.get("ready") is not True
            or type(payload.get("max_concurrency")) is not int
            or payload.get("max_concurrency") != 1
            or type(payload.get("max_request_bytes")) is not int
            or payload.get("max_request_bytes") != self._max_request_bytes
            or payload.get("device") not in {"cpu", "cuda"}
        ):
            raise Model1HttpContractError()
        self._validate_producer(payload.get("producer"))

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        canonical_input = _canonical_input(inputs)
        input_digest = sha256(_canonical_json(canonical_input)).hexdigest()
        request_body = _canonical_json({"input": canonical_input})
        if len(request_body) > self._max_request_bytes:
            raise Model1HttpContractError()

        payload = self._post(request_body, input_digest)
        prediction = self._validate_response(payload, input_digest)
        try:
            return normalize_model1_output(prediction)
        except SubprocessModelError as error:
            # ``normalize_model1_output`` is the existing Model 1 L2 boundary.
            # Its messages describe only shape/type constraints, never remote
            # response bytes or the source document.
            raise Model1HttpContractError() from error

    def _post(self, request_body: bytes, input_digest: str) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": _JSON_CONTENT_TYPE,
            "Content-Type": _JSON_CONTENT_TYPE,
            "Idempotency-Key": input_digest,
        }
        return self._request_json(
            "POST",
            f"{self._base_url}{_PREDICT_PATH}",
            headers=headers,
            content=request_body,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None = None,
    ) -> Mapping[str, Any]:
        for attempt in range(2):
            retry_delay: float | None = None
            try:
                with httpx.Client(
                    transport=self._transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    with client.stream(
                        method, url, headers=headers, content=content
                    ) as response:
                        if response.status_code in _RETRYABLE_STATUS_CODES and attempt == 0:
                            retry_delay = self._retry_delay(response)
                        else:
                            if response.status_code != 200:
                                raise Model1HttpError()
                            if response.headers.get("content-type") != _JSON_CONTENT_TYPE:
                                raise Model1HttpContractError()
                            body = self._read_bounded_body(response)
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == 0:
                    continue
                raise Model1HttpError() from None
            except (Model1HttpContractError, Model1HttpError):
                raise
            except httpx.HTTPError:
                raise Model1HttpError() from None

            if retry_delay is not None:
                if retry_delay > 0:
                    self._sleeper(retry_delay)
                continue

            try:
                decoded = json.loads(body, parse_constant=_reject_nonfinite_json)
            except (TypeError, ValueError, RecursionError):
                raise Model1HttpContractError() from None
            if not isinstance(decoded, dict):
                raise Model1HttpContractError()
            return decoded
        raise AssertionError("remote Model 1 retry loop did not return")  # pragma: no cover

    def _retry_delay(self, response: httpx.Response) -> float:
        """Return a bounded retry delay; only 429 is deliberately back-pressured."""

        if response.status_code != 429:
            return 0.0
        raw = response.headers.get("retry-after")
        delay = _DEFAULT_RETRY_DELAY_SECONDS
        if raw is not None:
            try:
                parsed_seconds = int(raw, 10)
            except ValueError:
                try:
                    parsed_date = parsedate_to_datetime(raw)
                    if parsed_date.tzinfo is None:
                        raise ValueError
                    delay = parsed_date.timestamp() - self._wall_clock()
                except (TypeError, ValueError, OverflowError, OSError):
                    delay = _DEFAULT_RETRY_DELAY_SECONDS
            else:
                delay = float(parsed_seconds)
        if not math.isfinite(delay) or delay <= 0:
            delay = _DEFAULT_RETRY_DELAY_SECONDS
        return min(delay, _MAX_RETRY_AFTER_SECONDS)

    def _read_bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                raise Model1HttpContractError() from None
            if declared < 0 or declared > self._max_response_bytes:
                raise Model1HttpContractError()
        body = bytearray()
        for chunk in response.iter_raw():
            if len(body) + len(chunk) > self._max_response_bytes:
                raise Model1HttpContractError()
            body.extend(chunk)
        return bytes(body)

    def _validate_response(
        self, value: Mapping[str, Any], input_digest: str
    ) -> dict[str, Any]:
        if set(value) != {"schema_version", "input_digest", "producer", "prediction"}:
            raise Model1HttpContractError()
        if value.get("schema_version") != MODEL1_REMOTE_RESULT_SCHEMA_VERSION:
            raise Model1HttpContractError()
        returned_digest = value.get("input_digest")
        if (
            not isinstance(returned_digest, str)
            or _SHA256.fullmatch(returned_digest) is None
            or returned_digest != input_digest
        ):
            raise Model1HttpContractError()
        self._validate_producer(value.get("producer"))
        prediction = value.get("prediction")
        if not isinstance(prediction, dict) or set(prediction) != {
            "support_type_pred",
            "confidence",
            "status",
        }:
            raise Model1HttpContractError()
        return dict(prediction)

    def _validate_producer(self, value: object) -> None:
        if not isinstance(value, dict) or set(value) != {
            "service",
            "weight_sha256",
            "runtime_manifest_sha256",
        }:
            raise Model1HttpContractError()
        if (
            value.get("service") != MODEL1_REMOTE_SERVICE
            or value.get("weight_sha256") != self._expected_weight_sha256
            or value.get("runtime_manifest_sha256")
            != self._expected_runtime_manifest_sha256
        ):
            raise Model1HttpContractError()
