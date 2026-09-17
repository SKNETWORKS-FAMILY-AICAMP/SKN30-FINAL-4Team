"""Authenticated HTTP adapters for the resident Model 2 + Model 3 service.

Only the already-built worker payload crosses this boundary.  Database,
Storage, OpenAI, and browser credentials are never forwarded.  Every request
and response is bound to the canonical input digest and to the immutable
resident runtime identity selected by the trusted worker.
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
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from prereview_model23_service.contract import (
    MODEL23_DEFAULT_MAX_REQUEST_BYTES,
    MODEL23_MAX_CONCURRENCY,
    MODEL23_MAX_REQUEST_BYTES,
    MODEL23_MIN_REQUEST_BYTES,
    MODEL2_RESULT_SCHEMA_VERSION,
    MODEL3_RESULT_SCHEMA_VERSION,
)

from ..contracts.ml_result import INPUT_EVIDENCE_MISSING, MlModelId
from ..ml_reference import MlUnavailable
from .ml_subprocess import (
    SubprocessModelError,
    normalize_model2_output,
    normalize_model3_output,
)

__all__ = [
    "MODEL2_BUNDLE_SHA256",
    "MODEL2_REMOTE_RESULT_SCHEMA_VERSION",
    "MODEL3_POOL_SHA256",
    "MODEL3_REMOTE_RESULT_SCHEMA_VERSION",
    "MODEL23_REMOTE_SERVICE",
    "Model2HttpAdapter",
    "Model3HttpAdapter",
    "Model23HttpContractError",
    "Model23HttpError",
    "canonical_model23_input_digest",
    "validate_model23_remote_base_url",
]


MODEL23_REMOTE_SERVICE = "prereview-model23"
MODEL2_REMOTE_RESULT_SCHEMA_VERSION = MODEL2_RESULT_SCHEMA_VERSION
MODEL3_REMOTE_RESULT_SCHEMA_VERSION = MODEL3_RESULT_SCHEMA_VERSION
MODEL2_BUNDLE_SHA256 = (
    "0c5b93e2a04c778ace8e07d7551b1fc7c3cc2b91cde80d94b2b1b1cf38cbabff"
)
MODEL3_POOL_SHA256 = (
    "79649c095b1775832b73c18c629eca7695688a3eaa3554f3af3b96565a23a0bf"
)

_JSON_CONTENT_TYPE = "application/json"
_READY_PATH = "/v1/model23/ready"
_MODEL2_PREDICT_PATH = "/v1/model2/predict"
_MODEL3_PREDICT_PATH = "/v1/model3/predict"
_INTERNAL_PORT = 8792
_MAX_BODY_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_DEFAULT_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_AFTER_SECONDS = 5.0
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BEARER_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}")
_INTERNAL_HTTP_HOSTNAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 502, 503, 504})


class Model23HttpError(RuntimeError):
    """A redacted remote Model 2 + Model 3 transport/provider failure."""

    def __init__(self) -> None:
        super().__init__("remote Model 2 + Model 3 service unavailable")


class Model23HttpContractError(RuntimeError):
    """The remote response failed its input or immutable identity binding."""

    def __init__(self) -> None:
        super().__init__("remote Model 2 + Model 3 response violated its contract")


def _is_literal_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validated_internal_http_hostname(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("remote Model 2 + Model 3 internal HTTP hostname is invalid")
    normalized = value.lower()
    if (
        _INTERNAL_HTTP_HOSTNAME.fullmatch(normalized) is None
        or normalized == "localhost"
    ):
        raise ValueError("remote Model 2 + Model 3 internal HTTP hostname is invalid")
    return normalized


def validate_model23_remote_base_url(
    value: str,
    *,
    allow_loopback_http: bool = False,
    internal_http_hostname: str | None = None,
) -> str:
    """Validate one origin; bearer-bearing traffic is HTTPS by default."""

    if not isinstance(value, str):
        raise ValueError("remote Model 2 + Model 3 base URL must be an HTTPS origin")
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise ValueError(
            "remote Model 2 + Model 3 base URL must be an HTTPS origin"
        ) from None
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
        raise ValueError("remote Model 2 + Model 3 base URL must be an HTTPS origin")

    trusted_internal_hostname = _validated_internal_http_hostname(
        internal_http_hostname
    )
    http_is_allowed = allow_loopback_http and _is_literal_loopback(parsed.hostname)
    if (
        parsed.scheme == "http"
        and trusted_internal_hostname is not None
        and parsed.hostname.lower() == trusted_internal_hostname
    ):
        if port != _INTERNAL_PORT:
            raise ValueError(
                "remote Model 2 + Model 3 internal HTTP origin must use the fixed service port"
            )
        http_is_allowed = True
    if parsed.scheme == "http" and not http_is_allowed:
        raise ValueError("remote Model 2 + Model 3 base URL must be an HTTPS origin")

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _validated_sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validated_bearer_token(value: str) -> str:
    if not isinstance(value, str) or _BEARER_TOKEN.fullmatch(value) is None:
        raise ValueError("remote Model 2 + Model 3 bearer token is invalid")
    return value


def _validated_response_cap(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_BODY_BYTES
    ):
        raise ValueError("remote Model 2 + Model 3 response limit is invalid")
    return value


def _validated_request_cap(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MODEL23_MIN_REQUEST_BYTES <= value <= MODEL23_MAX_REQUEST_BYTES
    ):
        raise ValueError("remote Model 2 + Model 3 request limit is invalid")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("remote Model 2 + Model 3 input cannot be serialized") from error


def _canonical_input(inputs: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(inputs, Mapping):
        raise ValueError("remote Model 2 + Model 3 input must be an object")
    canonical = dict(inputs)
    if not canonical or any(not isinstance(key, str) for key in canonical):
        raise ValueError("remote Model 2 + Model 3 input must be a non-empty object")
    # Serialize once here so unsupported values and non-finite numbers fail
    # before a network request.  The same canonicalizer binds the digest below.
    _canonical_json(canonical)
    return canonical


def canonical_model23_input_digest(inputs: Mapping[str, Any]) -> str:
    """Return the stable digest of the exact current worker payload."""

    return sha256(_canonical_json(_canonical_input(inputs))).hexdigest()


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


class _Model23HttpAdapter:
    """Shared transport; concrete subclasses retain their existing L2 contract."""

    model_id: ClassVar[MlModelId]
    _predict_path: ClassVar[str]
    _result_schema_version: ClassVar[str]

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        expected_runtime_manifest_sha256: str,
        timeout_seconds: float = 60.0,
        max_request_bytes: int = MODEL23_DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        expected_model2_bundle_sha256: str = MODEL2_BUNDLE_SHA256,
        expected_model3_pool_sha256: str = MODEL3_POOL_SHA256,
        artifact_version: str | None = None,
        transport: httpx.BaseTransport | None = None,
        allow_loopback_http: bool = False,
        internal_http_hostname: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._base_url = validate_model23_remote_base_url(
            base_url,
            allow_loopback_http=allow_loopback_http,
            internal_http_hostname=internal_http_hostname,
        )
        self._bearer_token = _validated_bearer_token(bearer_token)
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError(
                "remote Model 2 + Model 3 timeout must be a finite positive number"
            )
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._max_request_bytes = _validated_request_cap(max_request_bytes)
        self._max_response_bytes = _validated_response_cap(max_response_bytes)
        self._expected_runtime_manifest_sha256 = _validated_sha256(
            expected_runtime_manifest_sha256,
            label="remote Model 2 + Model 3 runtime manifest SHA-256",
        )
        self._expected_model2_bundle_sha256 = _validated_sha256(
            expected_model2_bundle_sha256,
            label="remote Model 2 bundle SHA-256",
        )
        self._expected_model3_pool_sha256 = _validated_sha256(
            expected_model3_pool_sha256,
            label="remote Model 3 pool SHA-256",
        )
        if artifact_version is not None and (
            not isinstance(artifact_version, str) or not artifact_version.strip()
        ):
            raise ValueError("remote Model 2 + Model 3 artifact version is invalid")
        self.artifact_version = artifact_version
        self._transport = transport
        if not callable(sleeper) or not callable(wall_clock):
            raise ValueError("remote Model 2 + Model 3 retry timing hooks are invalid")
        self._sleeper = sleeper
        self._wall_clock = wall_clock

    def check_ready(self) -> None:
        """Authenticate readiness and verify every pinned resident artifact."""

        payload = self._request_json(
            "GET",
            f"{self._base_url}{_READY_PATH}",
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "Accept": _JSON_CONTENT_TYPE,
            },
        )
        if set(payload) != {
            "ready",
            "max_concurrency",
            "max_request_bytes",
            "device",
            "producer",
        }:
            raise Model23HttpContractError()
        if (
            payload.get("ready") is not True
            or type(payload.get("max_concurrency")) is not int
            or payload.get("max_concurrency") != MODEL23_MAX_CONCURRENCY
            or type(payload.get("max_request_bytes")) is not int
            or payload.get("max_request_bytes") != self._max_request_bytes
            or payload.get("device") != "cpu"
        ):
            raise Model23HttpContractError()
        self._validate_producer(payload.get("producer"))

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        canonical_input = _canonical_input(inputs)
        input_digest = sha256(_canonical_json(canonical_input)).hexdigest()
        request_body = _canonical_json({"input": canonical_input})
        if len(request_body) > self._max_request_bytes:
            raise Model23HttpContractError()

        payload = self._request_json(
            "POST",
            f"{self._base_url}{self._predict_path}",
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "Accept": _JSON_CONTENT_TYPE,
                "Content-Type": _JSON_CONTENT_TYPE,
                "Idempotency-Key": input_digest,
            },
            content=request_body,
        )
        prediction = self._validate_response(payload, input_digest)
        try:
            return self._normalize(prediction)
        except SubprocessModelError:
            # Model 3's normalizer can mention an invalid remote axis value.
            # Suppress that cause so document/provider-controlled material is
            # not disclosed by an exception-chain logger.
            raise Model23HttpContractError() from None

    def _normalize(self, prediction: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

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
                        if (
                            response.status_code in _RETRYABLE_STATUS_CODES
                            and attempt == 0
                        ):
                            retry_delay = self._retry_delay(response)
                        else:
                            if response.status_code != 200:
                                raise Model23HttpError()
                            if response.headers.get("content-type") != _JSON_CONTENT_TYPE:
                                raise Model23HttpContractError()
                            body = self._read_bounded_body(response)
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == 0:
                    continue
                raise Model23HttpError() from None
            except (Model23HttpContractError, Model23HttpError):
                raise
            except httpx.HTTPError:
                raise Model23HttpError() from None

            if retry_delay is not None:
                if retry_delay > 0:
                    self._sleeper(retry_delay)
                continue

            try:
                decoded = json.loads(body, parse_constant=_reject_nonfinite_json)
            except (TypeError, ValueError, RecursionError):
                raise Model23HttpContractError() from None
            if not isinstance(decoded, dict):
                raise Model23HttpContractError()
            return decoded
        raise AssertionError("remote Model 2 + Model 3 retry loop did not return")

    def _retry_delay(self, response: httpx.Response) -> float:
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
                raise Model23HttpContractError() from None
            if declared < 0 or declared > self._max_response_bytes:
                raise Model23HttpContractError()
        body = bytearray()
        for chunk in response.iter_raw():
            if len(body) + len(chunk) > self._max_response_bytes:
                raise Model23HttpContractError()
            body.extend(chunk)
        return bytes(body)

    def _validate_response(
        self, value: Mapping[str, Any], input_digest: str
    ) -> Mapping[str, Any]:
        if set(value) != {"schema_version", "input_digest", "producer", "prediction"}:
            raise Model23HttpContractError()
        if value.get("schema_version") != self._result_schema_version:
            raise Model23HttpContractError()
        returned_digest = value.get("input_digest")
        if (
            not isinstance(returned_digest, str)
            or _SHA256.fullmatch(returned_digest) is None
            or returned_digest != input_digest
        ):
            raise Model23HttpContractError()
        self._validate_producer(value.get("producer"))
        prediction = value.get("prediction")
        if not isinstance(prediction, dict):
            raise Model23HttpContractError()
        return prediction

    def _validate_producer(self, value: object) -> None:
        if not isinstance(value, dict) or set(value) != {
            "service",
            "runtime_manifest_sha256",
            "model2_bundle_sha256",
            "model3_pool_sha256",
        }:
            raise Model23HttpContractError()
        if (
            value.get("service") != MODEL23_REMOTE_SERVICE
            or value.get("runtime_manifest_sha256")
            != self._expected_runtime_manifest_sha256
            or value.get("model2_bundle_sha256")
            != self._expected_model2_bundle_sha256
            or value.get("model3_pool_sha256") != self._expected_model3_pool_sha256
        ):
            raise Model23HttpContractError()


class Model2HttpAdapter(_Model23HttpAdapter):
    """Model 2 worker port backed by the combined resident service."""

    model_id = MlModelId.MODEL_2_AMOUNT
    _predict_path = _MODEL2_PREDICT_PATH
    _result_schema_version = MODEL2_REMOTE_RESULT_SCHEMA_VERSION

    def _normalize(self, prediction: Mapping[str, Any]) -> dict[str, Any]:
        return normalize_model2_output(dict(prediction))


class Model3HttpAdapter(_Model23HttpAdapter):
    """Model 3 worker port backed by the combined resident service."""

    model_id = MlModelId.MODEL_3_ANOMALY
    _predict_path = _MODEL3_PREDICT_PATH
    _result_schema_version = MODEL3_REMOTE_RESULT_SCHEMA_VERSION

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Preserve the local adapter's support-type and withheld gates."""

        support_type = inputs.get("support_type")
        if not isinstance(support_type, str) or not support_type.strip():
            raise MlUnavailable(
                INPUT_EVIDENCE_MISSING,
                "모델 3 비교군을 정할 지원유형 근거가 없다.",
            )
        if inputs.get("support_type_status") == "판단보류":
            raise MlUnavailable(
                INPUT_EVIDENCE_MISSING,
                "모델 1 지원유형이 판단보류라 모델 3 비교군을 정할 수 없다.",
            )
        return super().predict(inputs)

    def _normalize(self, prediction: Mapping[str, Any]) -> dict[str, Any]:
        return normalize_model3_output(dict(prediction))
