"""Pinned Surya 0.22.1 layout inference adapter.

The adapter is deliberately narrower than Surya's public API:

* it accepts one already-validated PNG page, never a PDF or a path;
* it configures an already-running vLLM endpoint and never starts one;
* it keeps one manager/predictor pair for the process and serializes calls;
* it returns only label/geometry/reading-order and proven model confidence;
* provider diagnostics, OCR text, HTML, URLs, and raw responses never cross
  this boundary.

Pillow and Surya are imported lazily so importing the normal backend does not
require GPU-only dependencies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from hashlib import sha256
import http.client
from io import BytesIO
from importlib.metadata import version as distribution_version
import ipaddress
import json
import math
import os
import stat
from threading import RLock
import time
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
import warnings

from worker.contracts.accelerator import SuryaProducerIdentity

from .handler import (
    InferenceContentFailure,
    InferenceInfrastructureFailure,
    SuryaLayoutPageResult,
    SuryaModelConfidenceMarker,
)


__all__ = [
    "DEFAULT_SURYA_DEPLOYMENT_ATTESTATION_PATH",
    "SURYA_DEPLOYMENT_ATTESTATION_SCHEMA_VERSION",
    "SuryaDeploymentAttestation",
    "SuryaEndpointAllowlistPolicy",
    "SuryaLayoutInferenceAdapter",
    "SuryaVllmEndpointSettings",
    "load_deployment_attestation",
    "verify_deployment_attestation",
]


_SURYA_VERSION = "0.22.1"
_SURYA_LABELS = frozenset(
    {
        "Caption",
        "Footnote",
        "Equation",
        "ListGroup",
        "PageHeader",
        "PageFooter",
        "Picture",
        "SectionHeader",
        "Table",
        "Text",
        "Figure",
        "Code",
        "Form",
        "TableOfContents",
        "ChemicalBlock",
        "Diagram",
        "Bibliography",
        "BlankPage",
    }
)
_FALSE_ENV_VALUES = frozenset({"0", "false", "no"})
_MISSING = object()
_ENVIRONMENT_LOCK = RLock()
_SURYA_TIMEOUT_LOCK = RLock()
DEFAULT_SURYA_DEPLOYMENT_ATTESTATION_PATH = (
    "/run/prereview-surya/attestations/surya-vllm.json"
)
SURYA_DEPLOYMENT_ATTESTATION_SCHEMA_VERSION = "prereview.surya-vllm-attestation/v1"
_MAX_ATTESTATION_BYTES = 4 * 1024
_MAX_VLLM_MODELS_RESPONSE_BYTES = 64 * 1024
_HF_HUB_CACHE_ROOT = "/workspace/persistent/prereview/cache/huggingface/hub"
_MAX_MODEL_WEIGHTS_BYTES = 20 * 1024 * 1024 * 1024
_MAX_PROCESS_CMDLINE_BYTES = 16 * 1024


class _ImageLike(Protocol):
    format: str | None
    mode: str
    size: tuple[int, int]

    def load(self) -> object: ...

    def convert(self, mode: str) -> "_ImageLike": ...

    def close(self) -> None: ...


class _ImageModuleLike(Protocol):
    DecompressionBombWarning: type[Warning]
    DecompressionBombError: type[BaseException]

    def open(self, fp: BytesIO) -> _ImageLike: ...


VllmModelProbe = Callable[[str], frozenset[str]]
AttestationVerifier = Callable[["SuryaDeploymentAttestation"], None]
LiveProcessProbe = Callable[["SuryaDeploymentAttestation"], "_LiveVllmProcessIdentity"]
TimeoutOverrideFactory = Callable[[float], AbstractContextManager[None]]


@dataclass(frozen=True)
class _LiveVllmProcessIdentity:
    """Kernel- and argv-bound identity for one running vLLM server."""

    start_time_ticks: int
    actual_model_id: str
    served_model_ids: tuple[str, ...]
    revision: str


@dataclass(frozen=True)
class SuryaDeploymentAttestation:
    """Identity statement created locally by the reviewed RunPod launcher.

    The worker never accepts a path or an identity statement from a job or the
    deployment JSON.  The launcher writes this small, non-secret file only
    after it has pinned the Hugging Face revision and checked the model weight
    digest.  At cold start we bind it to the configured artifact producer and
    independently check the vLLM ``/v1/models`` identity.
    """

    endpoint_url: str
    producer_model_id: str
    producer_model_revision: str
    producer_model_weights_sha256: str
    vllm_served_model_id: str
    server_pid: int

    def __post_init__(self) -> None:
        normalized, _, _ = _normalize_endpoint(self.endpoint_url)
        object.__setattr__(self, "endpoint_url", normalized)
        for name in ("producer_model_id", "vllm_served_model_id"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 256
                or any(ord(character) < 0x20 or ord(character) == 127 for character in value)
            ):
                raise ValueError("invalid deployment attestation")
        if not _is_immutable_revision(self.producer_model_revision):
            raise ValueError("invalid deployment attestation")
        if not _is_sha256(self.producer_model_weights_sha256):
            raise ValueError("invalid deployment attestation")
        if _is_all_zero_identity(
            self.producer_model_revision,
            self.producer_model_weights_sha256,
        ):
            raise ValueError("invalid deployment attestation")
        if isinstance(self.server_pid, bool) or not isinstance(self.server_pid, int) or self.server_pid <= 1:
            raise ValueError("invalid deployment attestation")

    def assert_matches(
        self,
        *,
        producer: SuryaProducerIdentity,
        endpoint: "SuryaVllmEndpointSettings",
    ) -> None:
        normalized_endpoint, _, _ = _normalize_endpoint(endpoint.url)
        if (
            self.endpoint_url != normalized_endpoint
            or self.producer_model_id != producer.model_id
            or self.producer_model_revision != producer.model_revision
            or self.producer_model_weights_sha256 != producer.model_weights_sha256
        ):
            raise ValueError("deployment attestation does not match worker identity")


def verify_deployment_attestation(attestation: SuryaDeploymentAttestation) -> None:
    """Bind the attestation to the live pinned vLLM process and cached weights.

    A stale JSON file alone cannot satisfy cold start: its PID must still
    exist, its command line must name the attested model and immutable
    revision, and the fixed Hugging Face snapshot must resolve to a regular
    blob inside the reviewed persistent cache with the attested SHA-256.
    """

    if not isinstance(attestation, SuryaDeploymentAttestation):
        raise InferenceInfrastructureFailure("surya_attestation_invalid")
    try:
        _probe_live_vllm_process(attestation)
        weights_path = _resolve_attested_weights_path(attestation)
        if _sha256_file(weights_path) != attestation.producer_model_weights_sha256:
            raise ValueError("model weight identity mismatch")
    except (OSError, ValueError):
        raise InferenceInfrastructureFailure("surya_attestation_invalid") from None


@dataclass(frozen=True)
class SuryaVllmEndpointSettings:
    """Explicit settings for a caller-managed Surya vLLM endpoint."""

    url: str
    backend: str = "vllm"
    autostart: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("Surya endpoint URL is required")
        if self.backend != "vllm":
            raise ValueError("Surya inference backend must be vllm")
        if self.autostart is not False:
            raise ValueError("Surya inference autostart must be disabled")


def load_deployment_attestation(
    path: str | os.PathLike[str] | None = None,
) -> SuryaDeploymentAttestation:
    """Load the launcher-owned identity file from its one fixed deployment path.

    ``path`` is a test seam only; production callers use no argument and thus
    cannot redirect the attestation through deployment configuration or a job.
    The file must be a regular, current-UID-owned, non-group/world-writable
    file.  Its contents contain no credentials, but the identity is security
    relevant and therefore never appears in an exception message.
    """

    chosen_path = DEFAULT_SURYA_DEPLOYMENT_ATTESTATION_PATH if path is None else path
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(chosen_path, flags)
    except (OSError, TypeError, ValueError):
        raise ValueError("deployment attestation unavailable") from None
    try:
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError("deployment attestation unavailable")
            payload = os.read(descriptor, _MAX_ATTESTATION_BYTES + 1)
        except (OSError, ValueError):
            raise ValueError("deployment attestation unavailable") from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if not payload or len(payload) > _MAX_ATTESTATION_BYTES:
        raise ValueError("deployment attestation unavailable")
    try:
        decoded = payload.decode("utf-8")
        parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
        if not isinstance(parsed, dict) or set(parsed) != {
            "schema_version",
            "endpoint_url",
            "producer",
            "vllm_served_model_id",
            "server_pid",
        }:
            raise ValueError("invalid deployment attestation")
        producer = parsed["producer"]
        if not isinstance(producer, dict) or set(producer) != {
            "model_id",
            "model_revision",
            "model_weights_sha256",
        }:
            raise ValueError("invalid deployment attestation")
        if parsed["schema_version"] != SURYA_DEPLOYMENT_ATTESTATION_SCHEMA_VERSION:
            raise ValueError("invalid deployment attestation")
        return SuryaDeploymentAttestation(
            endpoint_url=parsed["endpoint_url"],
            producer_model_id=producer["model_id"],
            producer_model_revision=producer["model_revision"],
            producer_model_weights_sha256=producer["model_weights_sha256"],
            vllm_served_model_id=parsed["vllm_served_model_id"],
            server_pid=parsed["server_pid"],
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("deployment attestation unavailable") from None


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate deployment attestation key")
        result[key] = value
    return result


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_immutable_revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_all_zero_identity(*values: str) -> bool:
    return any(value and set(value) == {"0"} for value in values)


def _read_process_command_line(pid: int) -> tuple[str, ...]:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("invalid server pid")
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/cmdline", "rb", buffering=0) as stream:
            raw = stream.read(_MAX_PROCESS_CMDLINE_BYTES + 1)
    except OSError:
        raise ValueError("vLLM process unavailable") from None
    if not raw or len(raw) > _MAX_PROCESS_CMDLINE_BYTES or not raw.endswith(b"\0"):
        raise ValueError("vLLM process command line unavailable")
    try:
        values = tuple(part.decode("utf-8") for part in raw[:-1].split(b"\0"))
    except UnicodeDecodeError:
        raise ValueError("vLLM process command line unavailable") from None
    if not values or any(not value or "\0" in value for value in values):
        raise ValueError("vLLM process command line unavailable")
    return values


def _read_process_start_time(pid: int) -> int:
    """Read Linux ``/proc/<pid>/stat`` field 22 without trusting ``comm``."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("invalid server pid")
    try:
        with open(f"/proc/{pid}/stat", "rb", buffering=0) as stream:
            raw = stream.read(8 * 1024 + 1)
    except OSError:
        raise ValueError("vLLM process unavailable") from None
    if not raw or len(raw) > 8 * 1024 or b"\0" in raw:
        raise ValueError("vLLM process identity unavailable")
    # ``comm`` is parenthesized and may itself contain spaces or ``)``.  The
    # final ``) `` therefore provides the only safe split before field 3.
    close = raw.rfind(b") ")
    if close < 0:
        raise ValueError("vLLM process identity unavailable")
    fields_after_comm = raw[close + 2 :].split()
    # field 3 starts at index zero, so field 22 (starttime) is index 19.
    if len(fields_after_comm) <= 19:
        raise ValueError("vLLM process identity unavailable")
    try:
        start_time = int(fields_after_comm[19])
    except ValueError:
        raise ValueError("vLLM process identity unavailable") from None
    if start_time <= 0:
        raise ValueError("vLLM process identity unavailable")
    return start_time


def _single_option_value(arguments: Sequence[str], option: str) -> str | None:
    """Return one exact CLI option value, rejecting duplicates/empty values."""

    values: list[str] = []
    prefix = f"{option}="
    for index, argument in enumerate(arguments):
        if argument.startswith(prefix):
            values.append(argument[len(prefix) :])
        elif argument == option:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                raise ValueError("vLLM command identity mismatch")
            values.append(arguments[index + 1])
    if len(values) > 1 or any(not value for value in values):
        raise ValueError("vLLM command identity mismatch")
    return values[0] if values else None


def _served_model_values(arguments: Sequence[str]) -> tuple[str, ...] | None:
    """Parse vLLM's optional ``--served-model-name`` ``nargs='+'`` value."""

    occurrences = [
        index
        for index, argument in enumerate(arguments)
        if argument == "--served-model-name" or argument.startswith("--served-model-name=")
    ]
    if not occurrences:
        return None
    if len(occurrences) != 1:
        raise ValueError("vLLM command identity mismatch")
    index = occurrences[0]
    argument = arguments[index]
    if argument.startswith("--served-model-name="):
        value = argument.split("=", 1)[1]
        if not value:
            raise ValueError("vLLM command identity mismatch")
        return (value,)
    values: list[str] = []
    for value in arguments[index + 1 :]:
        if value.startswith("-"):
            break
        values.append(value)
    if not values or any(not value for value in values):
        raise ValueError("vLLM command identity mismatch")
    return tuple(values)


def _positional_vllm_model(arguments: Sequence[str]) -> str | None:
    """Parse only the reviewed ``vllm serve MODEL`` positional form."""

    positional_index: int | None = None
    if len(arguments) >= 3 and os.path.basename(arguments[0]) == "vllm" and arguments[1] == "serve":
        positional_index = 2
    elif (
        len(arguments) >= 4
        and os.path.basename(arguments[1]) == "vllm"
        and arguments[2] == "serve"
    ):
        # Console scripts use a Python shebang.  Linux exposes both the
        # interpreter and the vllm script in /proc/<pid>/cmdline, followed by
        # the same reviewed ``serve MODEL`` arguments.
        positional_index = 3
    elif (
        len(arguments) >= 5
        and arguments[1:4] == ("-m", "vllm.entrypoints.cli.main", "serve")
    ):
        positional_index = 4
    if positional_index is None:
        return None
    value = arguments[positional_index]
    if value.startswith("-"):
        raise ValueError("vLLM command identity mismatch")
    return value


def _parse_vllm_process_identity(arguments: Sequence[str]) -> tuple[str, tuple[str, ...], str]:
    option_model = _single_option_value(arguments, "--model")
    positional_model = _positional_vllm_model(arguments)
    api_server_module = (
        len(arguments) >= 3
        and tuple(arguments[1:3]) == ("-m", "vllm.entrypoints.openai.api_server")
    )
    if option_model is not None and not api_server_module:
        raise ValueError("vLLM command identity mismatch")
    if option_model is not None and positional_model is not None:
        raise ValueError("vLLM command identity mismatch")
    actual_model = option_model or positional_model
    revision = _single_option_value(arguments, "--revision")
    served_models = _served_model_values(arguments)
    if actual_model is None or revision is None:
        raise ValueError("vLLM command identity mismatch")
    return actual_model, served_models or (actual_model,), revision


def _probe_live_vllm_process(
    attestation: SuryaDeploymentAttestation,
) -> _LiveVllmProcessIdentity:
    arguments = _read_process_command_line(attestation.server_pid)
    actual_model, served_models, revision = _parse_vllm_process_identity(arguments)
    if (
        actual_model != attestation.producer_model_id
        or attestation.vllm_served_model_id not in served_models
        or revision != attestation.producer_model_revision
    ):
        raise ValueError("vLLM command identity mismatch")
    return _LiveVllmProcessIdentity(
        start_time_ticks=_read_process_start_time(attestation.server_pid),
        actual_model_id=actual_model,
        served_model_ids=served_models,
        revision=revision,
    )


def _resolve_attested_weights_path(attestation: SuryaDeploymentAttestation) -> str:
    """Resolve the one canonical HF snapshot symlink into its blob directory."""

    components = attestation.producer_model_id.split("/")
    if (
        len(components) != 2
        or any(
            not component
            or len(component) > 128
            or component in {".", ".."}
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for character in component)
            for component in components
        )
    ):
        raise ValueError("attested model id cannot identify an HF snapshot")
    namespace, repository = components
    cache_root = os.path.realpath(_HF_HUB_CACHE_ROOT)
    expected_repository = os.path.join(cache_root, f"models--{namespace}--{repository}")
    blobs_directory = os.path.realpath(os.path.join(expected_repository, "blobs"))
    snapshot_file = os.path.join(
        expected_repository,
        "snapshots",
        attestation.producer_model_revision,
        "model.safetensors",
    )
    resolved_blob = os.path.realpath(snapshot_file)
    try:
        if os.path.commonpath((resolved_blob, blobs_directory)) != blobs_directory:
            raise ValueError("attested weights escape the model cache")
        metadata = os.stat(resolved_blob, follow_symlinks=False)
    except OSError:
        raise ValueError("attested weights unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_MODEL_WEIGHTS_BYTES
    ):
        raise ValueError("attested weights unavailable")
    # RunPod Network Volumes expose cached files through FUSE with synthetic
    # 0666 modes even after chmod.  Cache permissions are therefore not an
    # identity signal.  The fixed cache root + immutable revision + regular
    # file/size checks above and the full SHA-256 rehash at attestation time
    # are the authority; the attestation itself remains on mode-600 /run.
    return resolved_blob


def _sha256_file(path: str) -> str:
    digest = sha256()
    try:
        with open(path, "rb", buffering=0) as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError:
        raise ValueError("attested weights unavailable") from None
    return digest.hexdigest()


@dataclass(frozen=True)
class SuryaEndpointAllowlistPolicy:
    """Explicit opt-in for non-loopback endpoint origins.

    Origins are exact, normalized ``scheme://host[:port]`` values.  Paths,
    credentials, query strings, fragments, and wildcard hosts are rejected.
    Merely constructing this policy does not grant an origin; it must appear
    in ``allowed_origins``.
    """

    allowed_origins: tuple[str, ...]
    _normalized_origins: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_origins, tuple) or not self.allowed_origins:
            raise ValueError("endpoint allowlist must contain at least one origin")
        normalized: set[str] = set()
        for value in self.allowed_origins:
            normalized.add(_normalize_origin(value))
        object.__setattr__(self, "_normalized_origins", frozenset(normalized))

    def allows(self, origin: str) -> bool:
        return origin in self._normalized_origins


RuntimeLoader = Callable[[], tuple[_ImageModuleLike, Callable[[], object], Callable[[object], object]]]


class SuryaLayoutInferenceAdapter:
    """Thread-safe, lazy ``InferencePort`` adapter for pinned Surya layout."""

    def __init__(
        self,
        *,
        producer: SuryaProducerIdentity,
        endpoint: SuryaVllmEndpointSettings,
        endpoint_policy: SuryaEndpointAllowlistPolicy | None = None,
        attestation: SuryaDeploymentAttestation,
        max_png_bytes: int = 64 * 1024 * 1024,
        max_image_pixels: int = 100_000_000,
        max_regions_per_page: int = 200,
        runtime_loader: RuntimeLoader | None = None,
        model_probe: VllmModelProbe | None = None,
        attestation_verifier: AttestationVerifier | None = None,
        live_process_probe: LiveProcessProbe | None = None,
        monotonic: Callable[[], float] | None = None,
        timeout_override_factory: TimeoutOverrideFactory | None = None,
    ) -> None:
        if not isinstance(producer, SuryaProducerIdentity):
            raise TypeError("producer must be a SuryaProducerIdentity")
        if producer.engine_id != "surya" or producer.engine_version != _SURYA_VERSION:
            raise ValueError("producer is not the supported pinned Surya runtime")
        if not isinstance(endpoint, SuryaVllmEndpointSettings):
            raise TypeError("endpoint must be SuryaVllmEndpointSettings")
        if not isinstance(attestation, SuryaDeploymentAttestation):
            raise TypeError("attestation must be a SuryaDeploymentAttestation")
        for name, value in (
            ("max_png_bytes", max_png_bytes),
            ("max_image_pixels", max_image_pixels),
            ("max_regions_per_page", max_regions_per_page),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        normalized_url, origin, is_loopback = _normalize_endpoint(endpoint.url)
        if not is_loopback and (
            endpoint_policy is None or not endpoint_policy.allows(origin)
        ):
            raise ValueError("non-loopback Surya endpoint is not allowlisted")
        attestation.assert_matches(producer=producer, endpoint=endpoint)

        self._producer = producer
        self._endpoint_url = normalized_url
        self._attestation = attestation
        self._max_png_bytes = max_png_bytes
        self._max_image_pixels = max_image_pixels
        self._max_regions_per_page = max_regions_per_page
        self._runtime_loader = runtime_loader or _load_runtime
        self._model_probe = model_probe or _probe_vllm_model_ids
        self._attestation_verifier = (
            attestation_verifier or verify_deployment_attestation
        )
        self._live_process_probe = live_process_probe or _probe_live_vllm_process
        self._monotonic = monotonic or time.monotonic
        self._timeout_override_factory = (
            timeout_override_factory or _surya_inference_timeout_override
        )
        self._manager: object | None = None
        self._predictor: object | None = None
        self._ready = False
        self._live_process_identity: _LiveVllmProcessIdentity | None = None
        # One lock covers lazy initialization and predictor invocation.  Surya
        # does not document LayoutPredictor as thread-safe, so parallel calls
        # must not share its mutable client/manager state concurrently.
        self._predictor_lock = RLock()

        _configure_environment(normalized_url, endpoint)

    def layout(
        self,
        *,
        page_number: int,
        png_bytes: bytes,
        pixel_width: int,
        pixel_height: int,
        deadline_monotonic: float | None = None,
    ) -> SuryaLayoutPageResult:
        """Infer one page and return the strict textless handler DTO."""

        self.initialize()
        self._assert_before_deadline(deadline_monotonic)
        _validate_input_shape(
            page_number=page_number,
            png_bytes=png_bytes,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            max_png_bytes=self._max_png_bytes,
            max_image_pixels=self._max_image_pixels,
        )

        image_module = self._image_module()
        image = _decode_png(
            image_module,
            png_bytes,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            max_image_pixels=self._max_image_pixels,
        )
        try:
            raw_result = self._predict(image, deadline_monotonic=deadline_monotonic)
        finally:
            _close_image(image)

        try:
            image_bbox, regions = _adapt_layout_result(
                raw_result,
                pixel_width=pixel_width,
                pixel_height=pixel_height,
                max_regions=self._max_regions_per_page,
            )
        except (InferenceContentFailure, InferenceInfrastructureFailure):
            raise
        except Exception:
            # Accessors on an unexpected provider object can execute arbitrary
            # properties.  Collapse all such diagnostics to a fixed message.
            raise InferenceContentFailure("surya_result_invalid") from None

        return SuryaLayoutPageResult(
            error=False,
            image_bbox=image_bbox,
            regions=regions,
            confidence_marker=SuryaModelConfidenceMarker(producer=self._producer),
        )

    def initialize(self) -> None:
        """Complete cold-start checks before this adapter accepts a job.

        This deliberately builds the real Surya manager and predictor first,
        then queries the already-running vLLM endpoint.  The latter compares
        its exact served id with the launcher-owned attestation; the request
        configuration alone can therefore not assert an arbitrary model.
        """

        with self._predictor_lock:
            if self._ready:
                return
            try:
                self._attestation_verifier(self._attestation)
            except Exception:
                raise InferenceInfrastructureFailure("surya_attestation_invalid") from None
            try:
                live_identity = self._live_process_probe(self._attestation)
            except Exception:
                raise InferenceInfrastructureFailure("surya_attestation_invalid") from None
            self._ensure_runtime()
            try:
                served_model_ids = self._model_probe(self._endpoint_url)
            except Exception:
                raise InferenceInfrastructureFailure("surya_vllm_unavailable") from None
            if self._attestation.vllm_served_model_id not in served_model_ids:
                raise InferenceInfrastructureFailure("surya_vllm_identity_mismatch")
            self._live_process_identity = live_identity
            self._ready = True

    def _image_module(self) -> _ImageModuleLike:
        with self._predictor_lock:
            self._ensure_runtime()
            image_module = getattr(self, "_loaded_image_module", None)
            if image_module is None:
                raise InferenceInfrastructureFailure("surya_runtime_unavailable")
            return image_module

    def _predict(
        self,
        image: _ImageLike,
        *,
        deadline_monotonic: float | None,
    ) -> object:
        with self._predictor_lock:
            self._ensure_runtime()
            predictor = self._predictor
            if predictor is None or not callable(predictor):
                raise InferenceInfrastructureFailure("surya_runtime_unavailable")
            try:
                self._assert_live_inference_identity()
                remaining = self._remaining_seconds(deadline_monotonic)
                timeout_scope: AbstractContextManager[None]
                if remaining is None:
                    timeout_scope = nullcontext()
                else:
                    timeout_scope = self._timeout_override_factory(remaining)
                with timeout_scope:
                    result = predictor([image])
                self._assert_before_deadline(deadline_monotonic)
            except InferenceInfrastructureFailure:
                raise
            except Exception:
                raise InferenceInfrastructureFailure("surya_inference_unavailable") from None
            if isinstance(result, (str, bytes)) or not isinstance(result, Sequence) or len(result) != 1:
                raise InferenceContentFailure("surya_result_invalid")
            return result[0]

    def _assert_live_inference_identity(self) -> None:
        """Rebind every inference to the same live process and served model."""

        expected = self._live_process_identity
        if expected is None:
            raise InferenceInfrastructureFailure("surya_attestation_invalid")
        try:
            current = self._live_process_probe(self._attestation)
        except Exception:
            raise InferenceInfrastructureFailure("surya_attestation_invalid") from None
        if current != expected:
            raise InferenceInfrastructureFailure("surya_attestation_invalid")
        try:
            served_model_ids = self._model_probe(self._endpoint_url)
        except Exception:
            raise InferenceInfrastructureFailure("surya_vllm_unavailable") from None
        if self._attestation.vllm_served_model_id not in served_model_ids:
            raise InferenceInfrastructureFailure("surya_vllm_identity_mismatch")

    def _ensure_runtime(self) -> None:
        if self._predictor is not None:
            return
        try:
            _assert_environment(self._endpoint_url)
            image_module, manager_factory, predictor_factory = self._runtime_loader()
            manager = manager_factory()
            predictor = predictor_factory(manager)
        except Exception:
            raise InferenceInfrastructureFailure("surya_runtime_unavailable") from None
        if not callable(predictor) or not callable(getattr(image_module, "open", None)):
            raise InferenceInfrastructureFailure("surya_runtime_unavailable")
        self._loaded_image_module = image_module
        self._manager = manager
        self._predictor = predictor

    def _remaining_seconds(self, deadline_monotonic: float | None) -> float | None:
        if deadline_monotonic is None:
            return None
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise InferenceInfrastructureFailure("surya_inference_deadline_invalid")
        try:
            now = self._monotonic()
        except Exception:
            raise InferenceInfrastructureFailure("surya_inference_deadline_invalid") from None
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
            raise InferenceInfrastructureFailure("surya_inference_deadline_invalid")
        remaining = float(deadline_monotonic) - float(now)
        if remaining <= 0.0:
            raise InferenceInfrastructureFailure("surya_inference_timeout")
        return remaining

    def _assert_before_deadline(self, deadline_monotonic: float | None) -> None:
        self._remaining_seconds(deadline_monotonic)


def _load_runtime() -> tuple[_ImageModuleLike, Callable[[], object], Callable[[object], object]]:
    """Import optional GPU dependencies only inside the RunPod process."""

    try:
        if distribution_version("surya-ocr") != _SURYA_VERSION:
            raise RuntimeError("unsupported Surya version")
        from PIL import Image
        from surya.inference import SuryaInferenceManager
        from surya.layout import LayoutPredictor
    except Exception:
        raise InferenceInfrastructureFailure("surya_runtime_unavailable") from None
    return Image, SuryaInferenceManager, LayoutPredictor


@contextmanager
def _surya_inference_timeout_override(remaining_seconds: float):
    """Apply an invocation's remaining budget to Surya's OpenAI client call.

    Surya 0.22.1 reads ``settings.SURYA_INFERENCE_TIMEOUT_SECONDS`` for every
    vLLM generation and passes it as the OpenAI request timeout.  The adapter
    serializes predictor use; this additional lock also protects the
    process-global setting while the temporary cap is installed and restored.
    """

    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, (int, float))
        or not math.isfinite(float(remaining_seconds))
        or remaining_seconds <= 0.0
    ):
        raise InferenceInfrastructureFailure("surya_inference_timeout")
    try:
        from surya.settings import settings
    except Exception:
        raise InferenceInfrastructureFailure("surya_runtime_unavailable") from None
    with _SURYA_TIMEOUT_LOCK:
        try:
            previous = settings.SURYA_INFERENCE_TIMEOUT_SECONDS
            if (
                isinstance(previous, bool)
                or not isinstance(previous, (int, float))
                or not math.isfinite(float(previous))
                or previous <= 0.0
            ):
                raise ValueError("invalid configured Surya timeout")
            settings.SURYA_INFERENCE_TIMEOUT_SECONDS = min(
                float(previous), float(remaining_seconds)
            )
        except InferenceInfrastructureFailure:
            raise
        except Exception:
            raise InferenceInfrastructureFailure("surya_inference_timeout") from None
        try:
            yield
        finally:
            try:
                settings.SURYA_INFERENCE_TIMEOUT_SECONDS = previous
            except Exception:
                # A failed restore is a process corruption condition.  Do not
                # let a later call inherit a possibly widened timeout.
                raise InferenceInfrastructureFailure("surya_inference_timeout") from None


def _configure_environment(
    normalized_url: str,
    endpoint: SuryaVllmEndpointSettings,
) -> None:
    with _ENVIRONMENT_LOCK:
        # Surya's OpenAI/httpx client must never inherit an ambient proxy for
        # page-image inference. Storage uses its own explicit loopback proxy;
        # vLLM is a separately attested endpoint and is direct-only.
        for proxy_name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            os.environ.pop(proxy_name, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        existing_backend = os.environ.get("SURYA_INFERENCE_BACKEND")
        if existing_backend is not None and existing_backend.strip().lower() != "vllm":
            raise ValueError("existing Surya backend configuration is incompatible")
        existing_autostart = os.environ.get("SURYA_INFERENCE_AUTOSTART")
        if (
            existing_autostart is not None
            and existing_autostart.strip().lower() not in _FALSE_ENV_VALUES
        ):
            raise ValueError("existing Surya autostart configuration is unsafe")
        existing_url = os.environ.get("SURYA_INFERENCE_URL")
        if existing_url is not None:
            try:
                configured_url, _, _ = _normalize_endpoint(existing_url)
            except ValueError:
                raise ValueError("existing Surya endpoint configuration is invalid") from None
            if configured_url != normalized_url:
                raise ValueError("existing Surya endpoint configuration conflicts")

        os.environ["SURYA_INFERENCE_URL"] = normalized_url
        os.environ["SURYA_INFERENCE_BACKEND"] = endpoint.backend
        os.environ["SURYA_INFERENCE_AUTOSTART"] = "false"


def _assert_environment(normalized_url: str) -> None:
    """Fail closed if process-global Surya settings changed before import."""

    with _ENVIRONMENT_LOCK:
        try:
            configured_url, _, _ = _normalize_endpoint(
                os.environ.get("SURYA_INFERENCE_URL", "")
            )
        except ValueError:
            raise InferenceInfrastructureFailure("surya_runtime_configuration_changed") from None
        if (
            configured_url != normalized_url
            or os.environ.get("SURYA_INFERENCE_BACKEND", "").strip().lower() != "vllm"
            or os.environ.get("SURYA_INFERENCE_AUTOSTART", "").strip().lower()
            not in _FALSE_ENV_VALUES
        ):
            raise InferenceInfrastructureFailure("surya_runtime_configuration_changed")


def _normalize_endpoint(value: str) -> tuple[str, str, bool]:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Surya endpoint URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("Surya endpoint URL is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise ValueError("Surya endpoint URL is invalid")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        is_loopback = host == "localhost"
    else:
        is_loopback = address.is_loopback

    # Plain HTTP is acceptable only for the vLLM sidecar in the same Pod.
    # An allowlist grants a remote origin identity; it must not also waive
    # transport confidentiality for page pixels or model responses.
    if not is_loopback and parsed.scheme != "https":
        raise ValueError("non-loopback Surya endpoint must use HTTPS")

    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    origin = urlunsplit((parsed.scheme, netloc, "", "", ""))
    normalized = urlunsplit((parsed.scheme, netloc, "/v1", "", ""))
    return normalized, origin, is_loopback


def _normalize_origin(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("endpoint allowlist origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("endpoint allowlist origin is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint allowlist origin is invalid")
    host = parsed.hostname.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _probe_vllm_model_ids(endpoint_url: str) -> frozenset[str]:
    """Fetch only the bounded OpenAI-compatible model-id response.

    ``http.client`` avoids ambient proxy configuration and does not follow
    redirects.  A remote endpoint has already been constrained to HTTPS by
    :func:`_normalize_endpoint`; loopback HTTP is permitted for the sidecar.
    """

    normalized, _, _ = _normalize_endpoint(endpoint_url)
    parsed = urlsplit(normalized)
    connection_class: type[http.client.HTTPConnection]
    if parsed.scheme == "https":
        connection_class = http.client.HTTPSConnection
    else:
        connection_class = http.client.HTTPConnection
    connection: http.client.HTTPConnection | None = None
    try:
        connection = connection_class(parsed.hostname, parsed.port, timeout=15.0)
        connection.request(
            "GET",
            "/v1/models",
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("vLLM models endpoint unavailable")
        raw = response.read(_MAX_VLLM_MODELS_RESPONSE_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException):
        raise InferenceInfrastructureFailure("surya_vllm_unavailable") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
    if not raw or len(raw) > _MAX_VLLM_MODELS_RESPONSE_BYTES:
        raise InferenceInfrastructureFailure("surya_vllm_unavailable")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
        data = payload["data"] if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data or len(data) > 32:
            raise ValueError("vLLM model list invalid")
        identifiers = frozenset(
            item["id"]
            for item in data
            if isinstance(item, dict)
            and set(item).issuperset({"id"})
            and isinstance(item["id"], str)
            and item["id"]
            and len(item["id"]) <= 256
            and item["id"] == item["id"].strip()
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise InferenceInfrastructureFailure("surya_vllm_unavailable") from None
    if not identifiers:
        raise InferenceInfrastructureFailure("surya_vllm_unavailable")
    return identifiers


def _validate_input_shape(
    *,
    page_number: int,
    png_bytes: bytes,
    pixel_width: int,
    pixel_height: int,
    max_png_bytes: int,
    max_image_pixels: int,
) -> None:
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number <= 0:
        raise InferenceContentFailure("surya_input_invalid")
    if not isinstance(png_bytes, bytes) or not png_bytes or len(png_bytes) > max_png_bytes:
        raise InferenceContentFailure("surya_input_invalid")
    if (
        isinstance(pixel_width, bool)
        or not isinstance(pixel_width, int)
        or pixel_width <= 0
        or isinstance(pixel_height, bool)
        or not isinstance(pixel_height, int)
        or pixel_height <= 0
        or pixel_width * pixel_height > max_image_pixels
    ):
        raise InferenceContentFailure("surya_input_invalid")


def _decode_png(
    image_module: _ImageModuleLike,
    png_bytes: bytes,
    *,
    pixel_width: int,
    pixel_height: int,
    max_image_pixels: int,
) -> _ImageLike:
    source: _ImageLike | None = None
    converted: _ImageLike | None = None
    try:
        bomb_warning = getattr(image_module, "DecompressionBombWarning")
        # Require the real Pillow bomb-error surface even though the generic
        # deterministic-content branch below deliberately redacts its text.
        getattr(image_module, "DecompressionBombError")
        with warnings.catch_warnings():
            warnings.simplefilter("error", bomb_warning)
            source = image_module.open(BytesIO(png_bytes))
            if getattr(source, "format", None) != "PNG":
                raise ValueError("not PNG")
            decoded_size = getattr(source, "size", None)
            if (
                not isinstance(decoded_size, tuple)
                or decoded_size != (pixel_width, pixel_height)
                or decoded_size[0] * decoded_size[1] > max_image_pixels
                or getattr(source, "is_animated", False)
                or getattr(source, "n_frames", 1) != 1
            ):
                raise ValueError("decoded image binding mismatch")
            # Inspect the header-derived geometry before decoding pixels.  The
            # independent cap still holds if another library user changed
            # Pillow's process-global MAX_IMAGE_PIXELS setting.
            source.load()
            converted = source.convert("RGB")
            converted.load()
            if converted.size != (pixel_width, pixel_height) or converted.mode != "RGB":
                raise ValueError("RGB conversion binding mismatch")
    except MemoryError:
        _close_image(converted)
        _close_image(source)
        raise InferenceInfrastructureFailure("surya_image_runtime_unavailable") from None
    except Exception:
        _close_image(converted)
        _close_image(source)
        raise InferenceContentFailure("surya_png_decode_rejected") from None

    if converted is None:
        _close_image(source)
        raise InferenceContentFailure("surya_png_decode_rejected")
    if converted is not source:
        _close_image(source)
    return converted


def _adapt_layout_result(
    result: object,
    *,
    pixel_width: int,
    pixel_height: int,
    max_regions: int,
) -> tuple[tuple[float, float, float, float], tuple[dict[str, object], ...]]:
    error = _required_field(result, "error")
    if not isinstance(error, bool):
        raise InferenceContentFailure("surya_result_invalid")
    if error:
        # Surya uses this in-band flag for model-output and parsing failures.
        # It does not prove that the already validated PNG is bad content, so
        # let the trusted coordinator apply its bounded infrastructure retry
        # policy instead of permanently rejecting the document.
        raise InferenceInfrastructureFailure("surya_inference_rejected")

    image_bbox = _bbox(_required_field(result, "image_bbox"))
    expected_bbox = (0.0, 0.0, float(pixel_width), float(pixel_height))
    if not _geometry_equal(image_bbox, expected_bbox):
        raise InferenceContentFailure("surya_result_canvas_mismatch")

    raw_regions = _required_field(result, "bboxes")
    if (
        isinstance(raw_regions, (str, bytes))
        or not isinstance(raw_regions, Sequence)
        or len(raw_regions) > max_regions
    ):
        raise InferenceContentFailure("surya_result_invalid")

    regions_by_position: list[tuple[int, dict[str, object]]] = []
    for raw_region in raw_regions:
        position = _required_field(raw_region, "position")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise InferenceContentFailure("surya_result_invalid")
        label = _required_field(raw_region, "label")
        if not isinstance(label, str) or label not in _SURYA_LABELS:
            raise InferenceContentFailure("surya_result_invalid")

        bbox = _bbox(_required_field(raw_region, "bbox"))
        polygon = _polygon(_required_field(raw_region, "polygon"))
        if not _geometry_equal(_polygon_bbox(polygon), bbox):
            raise InferenceContentFailure("surya_result_invalid")
        if not _within_canvas(bbox, polygon, pixel_width, pixel_height):
            raise InferenceContentFailure("surya_result_invalid")

        adapted: dict[str, object] = {
            "label": label,
            "bbox": bbox,
            "polygon": polygon,
            "position": position,
        }
        if _field_was_supplied(raw_region, "confidence"):
            confidence = _finite(_required_field(raw_region, "confidence"))
            if not 0.0 <= confidence <= 1.0:
                raise InferenceContentFailure("surya_result_invalid")
            # Surya 0.22.1's official LayoutPredictor substitutes exactly 1.0
            # when the vLLM output has no mean_token_prob, then constructs the
            # LayoutBox as if that value had been supplied.  LayoutResult does
            # not retain enough provenance to distinguish that fallback from
            # a genuine exact 1.0.  Conservatively omit ambiguous 1.0 values;
            # every retained value is therefore demonstrably model-derived.
            if confidence != 1.0:
                adapted["confidence"] = confidence
        regions_by_position.append((position, adapted))

    regions_by_position.sort(key=lambda item: item[0])
    if [position for position, _ in regions_by_position] != list(range(len(regions_by_position))):
        raise InferenceContentFailure("surya_result_invalid")
    return image_bbox, tuple(region for _, region in regions_by_position)


def _required_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        result = value.get(name, _MISSING)
    else:
        try:
            result = getattr(value, name)
        except Exception:
            result = _MISSING
    if result is _MISSING:
        raise InferenceContentFailure("surya_result_invalid")
    return result


def _field_was_supplied(value: object, name: str) -> bool:
    """Prove that a confidence came from the result rather than a default.

    Pydantic v2 exposes ``model_fields_set``; v1 used ``__fields_set__``.
    Plain objects cannot prove whether an attribute was synthesized, so their
    confidence is discarded.  Mappings prove supply by key presence.
    """

    if isinstance(value, Mapping):
        return name in value
    for marker_name in ("model_fields_set", "__pydantic_fields_set__", "__fields_set__"):
        try:
            fields = getattr(value, marker_name)
        except Exception:
            continue
        if isinstance(fields, (set, frozenset)):
            return name in fields
    return False


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InferenceContentFailure("surya_result_invalid")
    converted = float(value)
    if not math.isfinite(converted):
        raise InferenceContentFailure("surya_result_invalid")
    return 0.0 if converted == 0.0 else converted


def _bbox(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise InferenceContentFailure("surya_result_invalid")
    x0, y0, x1, y1 = (_finite(item) for item in value)
    if not x0 < x1 or not y0 < y1:
        raise InferenceContentFailure("surya_result_invalid")
    return x0, y0, x1, y1


def _polygon(value: object) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise InferenceContentFailure("surya_result_invalid")
    result: list[tuple[float, float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise InferenceContentFailure("surya_result_invalid")
        result.append((_finite(point[0]), _finite(point[1])))
    if len(set(result)) != 4:
        raise InferenceContentFailure("surya_result_invalid")
    return tuple(result)


def _polygon_bbox(
    polygon: Sequence[tuple[float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in polygon),
        min(point[1] for point in polygon),
        max(point[0] for point in polygon),
        max(point[1] for point in polygon),
    )


def _geometry_equal(
    left: Sequence[float],
    right: Sequence[float],
    *,
    tolerance: float = 0.001,
) -> bool:
    return all(abs(first - second) <= tolerance for first, second in zip(left, right))


def _within_canvas(
    bbox: tuple[float, float, float, float],
    polygon: Sequence[tuple[float, float]],
    width: int,
    height: int,
) -> bool:
    return (
        0.0 <= bbox[0] < bbox[2] <= float(width)
        and 0.0 <= bbox[1] < bbox[3] <= float(height)
        and all(
            0.0 <= x <= float(width) and 0.0 <= y <= float(height)
            for x, y in polygon
        )
    )


def _close_image(image: object | None) -> None:
    if image is None:
        return
    try:
        close = getattr(image, "close")
        close()
    except Exception:
        # Closing an in-memory image must not replace the classified inference
        # result with provider-controlled diagnostics.
        return
