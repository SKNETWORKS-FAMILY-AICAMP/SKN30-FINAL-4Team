"""Production composition and CLI entry point for the polling worker.

This module is deliberately the only place that turns deployment environment
variables into concrete worker adapters. The pipeline itself is kept free of
FastAPI, browser credentials, Redis/RQ, and Edge Function callback concerns.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import socket
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from prereview_model1_service.contract import (
    MODEL1_DEFAULT_MAX_REQUEST_BYTES,
    MODEL1_MAX_REQUEST_BYTES,
    MODEL1_MIN_REQUEST_BYTES,
)
from prereview_model23_service.contract import (
    MODEL23_DEFAULT_MAX_REQUEST_BYTES,
    MODEL23_MAX_REQUEST_BYTES,
    MODEL23_MIN_REQUEST_BYTES,
)

from worker.adapters.ml_subprocess import (
    Model1SubprocessMlModel,
    Model2SubprocessMlModel,
    Model3SubprocessMlModel,
    model1_command,
    model2_command,
    model3_command,
)
from worker.adapters.model1_http import (
    Model1HttpAdapter,
    Model1HttpContractError,
    Model1HttpError,
    validate_model1_remote_base_url,
)
from worker.adapters.model23_http import (
    Model2HttpAdapter,
    Model3HttpAdapter,
    Model23HttpContractError,
    Model23HttpError,
    validate_model23_remote_base_url,
)
from worker.analysis_job import (
    AnalysisJobHandler,
    CoreAnalysisEngine,
    VendoredRequestProfileProducer,
)
from worker.config import MissingConfigError
from worker.contracts.ml_result import MlModelId
from worker.cpl_prompt import check_prompts_ready
from worker.ml_reference import (
    MlModel,
    missing_artifact_model,
    missing_runtime_model,
)
from worker.ml_retry import RetryingMlModel
from worker.ml_runtime_preflight import MlRuntimePreflightError, verify_ml_runtime
from worker.postgres_analysis_store import PostgresAnalysisStore
from worker.postgres_repository import PostgresJobRepository
from worker.profiles import (
    DEFAULT_PARSE_TIMEOUT_SECONDS,
    REQUEST_NATIVE_EXACT_CANDIDATE_MODE_ENV,
    normalize_request_native_exact_candidate_mode,
)
from worker.providers import build_embedding_client, build_llm_provider
from worker.runtime import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_IDLE_POLL_SECONDS,
    DEFAULT_LEASE_SECONDS,
    JobHandler,
    JobRepository,
    WorkerRuntime,
)
from worker.supabase_storage import SupabaseWorkerStorage

LOGGER = logging.getLogger(__name__)
DEFAULT_ML_ROOT = Path(__file__).resolve().parents[2] / "ml"
DEFAULT_ML_TIMEOUT_SECONDS = 180.0
STRICT_ML_RUNTIME_PREFLIGHT_ENV = "PREREVIEW_STRICT_ML_RUNTIME_PREFLIGHT"
MODEL1_REMOTE_BASE_URL_ENV = "PREREVIEW_MODEL1_REMOTE_BASE_URL"
MODEL1_REMOTE_BEARER_TOKEN_FILE_ENV = "PREREVIEW_MODEL1_REMOTE_BEARER_TOKEN_FILE"
MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256_ENV = (
    "PREREVIEW_MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256"
)
MODEL1_INTERNAL_HTTP_HOSTNAME_ENV = "PREREVIEW_MODEL1_INTERNAL_HTTP_HOSTNAME"
MODEL1_MAX_REQUEST_BYTES_ENV = "PREREVIEW_MODEL1_MAX_REQUEST_BYTES"
_MODEL1_REMOTE_ENV_GROUP = (
    MODEL1_REMOTE_BASE_URL_ENV,
    MODEL1_REMOTE_BEARER_TOKEN_FILE_ENV,
    MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256_ENV,
)
_MODEL1_REMOTE_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}\Z")
MODEL23_REMOTE_BASE_URL_ENV = "PREREVIEW_MODEL23_REMOTE_BASE_URL"
MODEL23_REMOTE_BEARER_TOKEN_FILE_ENV = "PREREVIEW_MODEL23_REMOTE_BEARER_TOKEN_FILE"
MODEL23_REMOTE_RUNTIME_MANIFEST_SHA256_ENV = (
    "PREREVIEW_MODEL23_REMOTE_RUNTIME_MANIFEST_SHA256"
)
MODEL23_INTERNAL_HTTP_HOSTNAME_ENV = "PREREVIEW_MODEL23_INTERNAL_HTTP_HOSTNAME"
MODEL23_MAX_REQUEST_BYTES_ENV = "PREREVIEW_MODEL23_MAX_REQUEST_BYTES"
_MODEL23_REMOTE_ENV_GROUP = (
    MODEL23_REMOTE_BASE_URL_ENV,
    MODEL23_REMOTE_BEARER_TOKEN_FILE_ENV,
    MODEL23_REMOTE_RUNTIME_MANIFEST_SHA256_ENV,
)
_MODEL23_REMOTE_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}\Z")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})


# These SDKs can include request details in DEBUG records.  The worker sends
# uploaded notice text to OpenAI, so a process-wide DEBUG setting must not turn
# an operator log into another copy of the source document.
_SENSITIVE_TRANSPORT_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
)


class WorkerConfigurationError(RuntimeError):
    """A missing or invalid deployment setting, never containing its value."""


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Non-secret worker process settings safe to log during operations."""

    database_url: str
    supabase_url: str
    service_role_key: str
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS
    top_k: int = 5
    storage_timeout_seconds: float = 30.0
    database_connect_timeout_seconds: int = 10
    parse_timeout_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS
    request_native_exact_candidate_mode: str = "off"
    ml_root: Path = DEFAULT_ML_ROOT
    model1_serving_dir: Path | None = None
    ml_python_executable: str | None = None
    ml_timeout_seconds: float = DEFAULT_ML_TIMEOUT_SECONDS
    model1_remote_base_url: str | None = None
    model1_remote_bearer_token_file: Path | None = None
    model1_remote_runtime_manifest_sha256: str | None = None
    model1_internal_http_hostname: str | None = None
    model1_remote_max_request_bytes: int = MODEL1_DEFAULT_MAX_REQUEST_BYTES
    model23_remote_base_url: str | None = None
    model23_remote_bearer_token_file: Path | None = None
    model23_remote_runtime_manifest_sha256: str | None = None
    model23_internal_http_hostname: str | None = None
    model23_remote_max_request_bytes: int = MODEL23_DEFAULT_MAX_REQUEST_BYTES
    existing_kb_required: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerSettings:
        values = os.environ if env is None else env
        database_url = _first_required(values, "DATABASE_URL", "SUPABASE_DB_URL")
        service_role_key = _first_required(
            values, "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"
        )
        heartbeat_seconds = _positive_float(
            values,
            "PREREVIEW_WORKER_HEARTBEAT_SECONDS",
            DEFAULT_HEARTBEAT_SECONDS,
        )
        lease_seconds = _positive_int(
            values, "PREREVIEW_WORKER_LEASE_SECONDS", DEFAULT_LEASE_SECONDS
        )
        if not 30 <= lease_seconds <= 3600:
            raise WorkerConfigurationError(
                "PREREVIEW_WORKER_LEASE_SECONDS must be between 30 and 3600"
            )
        if heartbeat_seconds >= lease_seconds:
            raise WorkerConfigurationError(
                "PREREVIEW_WORKER_HEARTBEAT_SECONDS must be shorter than "
                "PREREVIEW_WORKER_LEASE_SECONDS"
            )
        top_k = _positive_int(values, "PREREVIEW_WORKER_TOP_K", 5)
        if top_k > 5:
            raise WorkerConfigurationError("PREREVIEW_WORKER_TOP_K must be at most 5")
        ml_root = _optional_path(values, "PREREVIEW_ML_ROOT") or DEFAULT_ML_ROOT
        model1_serving_dir = _optional_path(values, "PREREVIEW_MODEL1_SERVING_DIR")
        ml_python_executable = _optional_string(
            values, "PREREVIEW_ML_PYTHON_EXECUTABLE"
        )
        model1_remote = _model1_remote_configuration(values)
        model1_remote_max_request_bytes = _positive_int(
            values,
            MODEL1_MAX_REQUEST_BYTES_ENV,
            MODEL1_DEFAULT_MAX_REQUEST_BYTES,
        )
        if not (
            MODEL1_MIN_REQUEST_BYTES
            <= model1_remote_max_request_bytes
            <= MODEL1_MAX_REQUEST_BYTES
        ):
            raise WorkerConfigurationError(
                f"{MODEL1_MAX_REQUEST_BYTES_ENV} must be between "
                f"{MODEL1_MIN_REQUEST_BYTES} and {MODEL1_MAX_REQUEST_BYTES}"
            )
        if model1_remote is not None and model1_serving_dir is not None:
            raise WorkerConfigurationError(
                "local and remote Model 1 configurations cannot be enabled together"
            )
        model23_remote = _model23_remote_configuration(values)
        model23_remote_max_request_bytes = _positive_int(
            values,
            MODEL23_MAX_REQUEST_BYTES_ENV,
            MODEL23_DEFAULT_MAX_REQUEST_BYTES,
        )
        if not (
            MODEL23_MIN_REQUEST_BYTES
            <= model23_remote_max_request_bytes
            <= MODEL23_MAX_REQUEST_BYTES
        ):
            raise WorkerConfigurationError(
                f"{MODEL23_MAX_REQUEST_BYTES_ENV} must be between "
                f"{MODEL23_MIN_REQUEST_BYTES} and {MODEL23_MAX_REQUEST_BYTES}"
            )
        try:
            request_native_exact_candidate_mode = (
                normalize_request_native_exact_candidate_mode(
                    values.get(REQUEST_NATIVE_EXACT_CANDIDATE_MODE_ENV)
                )
            )
        except ValueError:
            raise WorkerConfigurationError(
                f"{REQUEST_NATIVE_EXACT_CANDIDATE_MODE_ENV} must be one of "
                "off, lines, lines+continuations"
            ) from None
        return cls(
            database_url=database_url,
            supabase_url=_required(values, "SUPABASE_URL").rstrip("/"),
            service_role_key=service_role_key,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
            idle_poll_seconds=_positive_float(
                values,
                "PREREVIEW_WORKER_IDLE_POLL_SECONDS",
                DEFAULT_IDLE_POLL_SECONDS,
            ),
            top_k=top_k,
            storage_timeout_seconds=_positive_float(
                values, "PREREVIEW_WORKER_STORAGE_TIMEOUT_SECONDS", 30.0
            ),
            database_connect_timeout_seconds=_positive_int(
                values, "PREREVIEW_WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS", 10
            ),
            parse_timeout_seconds=_positive_float(
                values,
                "PREREVIEW_WORKER_PARSE_TIMEOUT_SECONDS",
                DEFAULT_PARSE_TIMEOUT_SECONDS,
            ),
            request_native_exact_candidate_mode=request_native_exact_candidate_mode,
            ml_root=ml_root,
            model1_serving_dir=model1_serving_dir,
            ml_python_executable=ml_python_executable,
            ml_timeout_seconds=_positive_float(
                values,
                "PREREVIEW_ML_TIMEOUT_SECONDS",
                DEFAULT_ML_TIMEOUT_SECONDS,
            ),
            model1_remote_base_url=(
                None if model1_remote is None else model1_remote[0]
            ),
            model1_remote_bearer_token_file=(
                None if model1_remote is None else model1_remote[1]
            ),
            model1_remote_runtime_manifest_sha256=(
                None if model1_remote is None else model1_remote[2]
            ),
            model1_internal_http_hostname=(
                None if model1_remote is None else model1_remote[3]
            ),
            model1_remote_max_request_bytes=model1_remote_max_request_bytes,
            model23_remote_base_url=(
                None if model23_remote is None else model23_remote[0]
            ),
            model23_remote_bearer_token_file=(
                None if model23_remote is None else model23_remote[1]
            ),
            model23_remote_runtime_manifest_sha256=(
                None if model23_remote is None else model23_remote[2]
            ),
            model23_internal_http_hostname=(
                None if model23_remote is None else model23_remote[3]
            ),
            model23_remote_max_request_bytes=model23_remote_max_request_bytes,
            existing_kb_required=_boolean(
                values, "PREREVIEW_EXISTING_KB_REQUIRED", default=True
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkerComposition:
    """The assembled trusted boundaries for one worker process."""

    repository: JobRepository
    handler: JobHandler
    settings: WorkerSettings
    model1_remote_adapter: Model1HttpAdapter | None = None
    model23_remote_adapter: Model2HttpAdapter | None = None


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise WorkerConfigurationError(
            f"required environment variable is not set: {name}"
        )
    return value


def _first_required(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    raise WorkerConfigurationError(
        "required environment variable is not set: " + " or ".join(names)
    )


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise WorkerConfigurationError(
            f"environment variable must be an integer: {name}"
        ) from None
    if value <= 0:
        raise WorkerConfigurationError(f"environment variable must be positive: {name}")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise WorkerConfigurationError(
            f"environment variable must be a number: {name}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise WorkerConfigurationError(
            f"environment variable must be finite and positive: {name}"
        )
    return value


def _optional_string(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    value = _optional_string(env, name)
    return None if value is None else Path(value).expanduser()


def _model1_remote_configuration(
    env: Mapping[str, str],
) -> tuple[str, Path, str, str | None] | None:
    """Validate the all-or-nothing remote Model 1 configuration group."""

    raw_values = {name: _optional_string(env, name) for name in _MODEL1_REMOTE_ENV_GROUP}
    configured = tuple(name for name, value in raw_values.items() if value is not None)
    internal_http_hostname = _optional_string(
        env, MODEL1_INTERNAL_HTTP_HOSTNAME_ENV
    )
    if not configured:
        if internal_http_hostname is not None:
            raise WorkerConfigurationError(
                f"{MODEL1_INTERNAL_HTTP_HOSTNAME_ENV} requires the remote Model 1 configuration"
            )
        return None
    if len(configured) != len(_MODEL1_REMOTE_ENV_GROUP):
        raise WorkerConfigurationError(
            "remote Model 1 configuration requires "
            + ", ".join(_MODEL1_REMOTE_ENV_GROUP)
        )

    assert raw_values[MODEL1_REMOTE_BASE_URL_ENV] is not None
    assert raw_values[MODEL1_REMOTE_BEARER_TOKEN_FILE_ENV] is not None
    assert raw_values[MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256_ENV] is not None
    try:
        base_url = validate_model1_remote_base_url(
            raw_values[MODEL1_REMOTE_BASE_URL_ENV],
            internal_http_hostname=internal_http_hostname,
        )
    except ValueError:
        raise WorkerConfigurationError("remote Model 1 base URL is invalid") from None
    manifest_sha256 = raw_values[MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256_ENV]
    if _SHA256_DIGEST.fullmatch(manifest_sha256) is None:
        raise WorkerConfigurationError(
            "remote Model 1 runtime manifest SHA-256 is invalid"
        )
    if internal_http_hostname is not None and not base_url.startswith("http://"):
        raise WorkerConfigurationError(
            f"{MODEL1_INTERNAL_HTTP_HOSTNAME_ENV} is valid only for an HTTP base URL"
        )
    return (
        base_url,
        Path(raw_values[MODEL1_REMOTE_BEARER_TOKEN_FILE_ENV]).expanduser(),
        manifest_sha256,
        internal_http_hostname.lower() if internal_http_hostname is not None else None,
    )


def _model23_remote_configuration(
    env: Mapping[str, str],
) -> tuple[str, Path, str, str | None] | None:
    """Validate the all-or-nothing resident Model 2/3 configuration."""

    raw_values = {
        name: _optional_string(env, name) for name in _MODEL23_REMOTE_ENV_GROUP
    }
    configured = tuple(name for name, value in raw_values.items() if value is not None)
    internal_http_hostname = _optional_string(
        env, MODEL23_INTERNAL_HTTP_HOSTNAME_ENV
    )
    if not configured:
        if internal_http_hostname is not None:
            raise WorkerConfigurationError(
                f"{MODEL23_INTERNAL_HTTP_HOSTNAME_ENV} requires the remote Model 2/3 configuration"
            )
        return None
    if len(configured) != len(_MODEL23_REMOTE_ENV_GROUP):
        raise WorkerConfigurationError(
            "remote Model 2/3 configuration requires "
            + ", ".join(_MODEL23_REMOTE_ENV_GROUP)
        )

    assert raw_values[MODEL23_REMOTE_BASE_URL_ENV] is not None
    assert raw_values[MODEL23_REMOTE_BEARER_TOKEN_FILE_ENV] is not None
    assert raw_values[MODEL23_REMOTE_RUNTIME_MANIFEST_SHA256_ENV] is not None
    try:
        base_url = validate_model23_remote_base_url(
            raw_values[MODEL23_REMOTE_BASE_URL_ENV],
            internal_http_hostname=internal_http_hostname,
        )
    except ValueError:
        raise WorkerConfigurationError("remote Model 2/3 base URL is invalid") from None
    manifest_sha256 = raw_values[MODEL23_REMOTE_RUNTIME_MANIFEST_SHA256_ENV]
    if _SHA256_DIGEST.fullmatch(manifest_sha256) is None:
        raise WorkerConfigurationError(
            "remote Model 2/3 runtime manifest SHA-256 is invalid"
        )
    if internal_http_hostname is not None and not base_url.startswith("http://"):
        raise WorkerConfigurationError(
            f"{MODEL23_INTERNAL_HTTP_HOSTNAME_ENV} is valid only for an HTTP base URL"
        )
    return (
        base_url,
        Path(raw_values[MODEL23_REMOTE_BEARER_TOKEN_FILE_ENV]).expanduser(),
        manifest_sha256,
        internal_http_hostname.lower() if internal_http_hostname is not None else None,
    )


def _read_model1_remote_bearer_token(path: Path) -> str:
    """Read a single-owner bearer file without ever exposing its value.

    A symlink, shared hard link, or group/world-readable file is a deployment
    error.  The API token is deliberately kept out of environment variables,
    reprs, and errors so it cannot leak through normal worker diagnostics.
    """

    return _read_remote_bearer_token(
        path,
        token_pattern=_MODEL1_REMOTE_TOKEN,
        error_message="remote Model 1 bearer token file is invalid",
    )


def _read_remote_bearer_token(
    path: Path,
    *,
    token_pattern: re.Pattern[str],
    error_message: str,
) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        expected_metadata = os.lstat(path)
        if (
            not stat.S_ISREG(expected_metadata.st_mode)
            or expected_metadata.st_nlink != 1
            or expected_metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise WorkerConfigurationError(error_message)
        descriptor = os.open(path, flags)
    except OSError:
        raise WorkerConfigurationError(error_message) from None

    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or (metadata.st_dev, metadata.st_ino)
            != (expected_metadata.st_dev, expected_metadata.st_ino)
        ):
            raise WorkerConfigurationError(error_message)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            raw = stream.read(514)
    except OSError:
        raise WorkerConfigurationError(error_message) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\n" in raw or b"\r" in raw:
        raise WorkerConfigurationError(error_message)
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError:
        raise WorkerConfigurationError(error_message) from None
    if token_pattern.fullmatch(token) is None:
        raise WorkerConfigurationError(error_message)
    return token


def _read_model23_remote_bearer_token(path: Path) -> str:
    """Read the separate Model 2/3 service token using the same owner gate."""

    return _read_remote_bearer_token(
        path,
        token_pattern=_MODEL23_REMOTE_TOKEN,
        error_message="remote Model 2/3 bearer token file is invalid",
    )


def _boolean(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_ENV_VALUES:
        return True
    if raw in _FALSE_ENV_VALUES:
        return False
    raise WorkerConfigurationError(
        f"{name} must be one of 1,true,yes,on,0,false,no,off"
    )


def _first_missing(paths: tuple[Path, ...]) -> Path | None:
    for path in paths:
        if not path.is_file():
            return path
    return None


def _model1_root(configured: Path | None) -> Path | None:
    """Accept either a mounted ``model1`` directory or its serving parent."""

    if configured is None:
        return None
    if (configured / "inference.py").is_file():
        return configured
    nested = configured / "model1"
    if (nested / "inference.py").is_file():
        return nested
    return configured


def _ml_executable(settings: WorkerSettings) -> tuple[str | None, str | None]:
    """Return the child interpreter, or a safe unavailable detail."""

    executable = settings.ml_python_executable or sys.executable
    if settings.ml_python_executable is not None:
        candidate = Path(executable)
        if not candidate.is_file() and shutil.which(executable) is None:
            return None, executable
    return executable, None


def _build_ml_models(settings: WorkerSettings) -> dict[MlModelId, MlModel]:
    """Compose all three isolated ML ports without making ML mandatory.

    The worker remains bootable when a mounted source tree, artifact, or ML
    interpreter is absent.  In those cases an ``UnavailableModel`` preserves
    the reason for the per-model result instead of failing worker startup.
    """

    model_ids = (
        MlModelId.MODEL_1_SUPPORT_TYPE,
        MlModelId.MODEL_2_AMOUNT,
        MlModelId.MODEL_3_ANOMALY,
    )
    executable, runtime_detail = _ml_executable(settings)
    models: dict[MlModelId, MlModel] = {}
    model1_id = MlModelId.MODEL_1_SUPPORT_TYPE
    if settings.model1_remote_base_url is not None:
        assert settings.model1_remote_bearer_token_file is not None
        assert settings.model1_remote_runtime_manifest_sha256 is not None
        models[model1_id] = Model1HttpAdapter(
            base_url=settings.model1_remote_base_url,
            bearer_token=_read_model1_remote_bearer_token(
                settings.model1_remote_bearer_token_file
            ),
            expected_runtime_manifest_sha256=(
                settings.model1_remote_runtime_manifest_sha256
            ),
            timeout_seconds=settings.ml_timeout_seconds,
            max_request_bytes=settings.model1_remote_max_request_bytes,
            artifact_version="model1-external-serving-v1",
            internal_http_hostname=settings.model1_internal_http_hostname,
        )
    elif runtime_detail is not None:
        models[model1_id] = missing_runtime_model(model1_id, runtime_detail)
    else:
        assert executable is not None
        model1_root = _model1_root(settings.model1_serving_dir)
        if model1_root is None:
            models[model1_id] = missing_artifact_model(
                model1_id, "PREREVIEW_MODEL1_SERVING_DIR"
            )
        else:
            missing = _first_missing(
                (
                    model1_root / "inference.py",
                    model1_root / "model" / "model.safetensors",
                    model1_root / "label_mapping.json",
                    settings.ml_root / "pipelines" / "model1" / "dl07_m1_apply.py",
                )
            )
            if missing is None and not (model1_root / "tokenizer").is_dir():
                missing = model1_root / "tokenizer"
            if missing is not None:
                models[model1_id] = missing_artifact_model(model1_id, missing)
            else:
                environment = {"PREREVIEW_ML_ROOT": str(settings.ml_root)}
                if settings.model1_serving_dir is not None:
                    environment["PREREVIEW_MODEL1_SERVING_DIR"] = str(
                        settings.model1_serving_dir
                    )
                models[model1_id] = Model1SubprocessMlModel(
                    model1_command(python_executable=executable),
                    timeout_seconds=settings.ml_timeout_seconds,
                    environment=environment,
                    artifact_version="model1-external-serving-v1",
                )

    model2_id = MlModelId.MODEL_2_AMOUNT
    model3_id = MlModelId.MODEL_3_ANOMALY
    if settings.model23_remote_base_url is not None:
        assert settings.model23_remote_bearer_token_file is not None
        assert settings.model23_remote_runtime_manifest_sha256 is not None
        token = _read_model23_remote_bearer_token(
            settings.model23_remote_bearer_token_file
        )
        common_remote = {
            "base_url": settings.model23_remote_base_url,
            "bearer_token": token,
            "expected_runtime_manifest_sha256": (
                settings.model23_remote_runtime_manifest_sha256
            ),
            "timeout_seconds": settings.ml_timeout_seconds,
            "max_request_bytes": settings.model23_remote_max_request_bytes,
            "internal_http_hostname": settings.model23_internal_http_hostname,
        }
        models[model2_id] = Model2HttpAdapter(
            **common_remote,
            artifact_version="model2-p3-v1",
        )
        models[model3_id] = Model3HttpAdapter(
            **common_remote,
            artifact_version="model3-design-v3",
        )
        return models

    if runtime_detail is not None:
        models.update(
            {
                model_id: missing_runtime_model(model_id, runtime_detail)
                for model_id in model_ids
                if model_id is not model1_id
            }
        )
        return models

    assert executable is not None
    environment = {"PREREVIEW_ML_ROOT": str(settings.ml_root)}
    model2_root = settings.ml_root / "serving" / "model2"
    model2_missing = _first_missing(
        (
            model2_root / "predict.py",
            model2_root / "feature_builder.py",
            model2_root / "preprocessing.py",
            model2_root / "proximity.py",
            model2_root / "router.py",
            model2_root / "masking.py",
            model2_root / "cohort_reference.parquet",
            settings.ml_root / "serving" / "shared" / "preconsultation_adapter.py",
            settings.ml_root
            / "models"
            / "model2_canonical"
            / "model2_p3_bundle.joblib",
        )
    )
    if model2_missing is not None:
        models[model2_id] = missing_artifact_model(model2_id, model2_missing)
    else:
        models[model2_id] = RetryingMlModel(
            Model2SubprocessMlModel(
                model2_command(python_executable=executable),
                timeout_seconds=settings.ml_timeout_seconds,
                environment=environment,
                artifact_version="model2-p3-v1",
            )
        )

    model3_root = settings.ml_root / "serving" / "model3"
    model3_missing = _first_missing(
        (
            model3_root / "score.py",
            model3_root / "inference.py",
            model3_root / "design_features_v3.parquet",
            settings.ml_root / "serving" / "shared" / "preconsultation_adapter.py",
        )
    )
    if model3_missing is not None:
        models[model3_id] = missing_artifact_model(model3_id, model3_missing)
    else:
        models[model3_id] = RetryingMlModel(
            Model3SubprocessMlModel(
                model3_command(python_executable=executable),
                timeout_seconds=settings.ml_timeout_seconds,
                environment=environment,
                artifact_version="model3-design-v3",
            )
        )

    return models


def build_worker(env: Mapping[str, str] | None = None) -> WorkerComposition:
    """Build the PostgreSQL-polling HWP/HWPX analysis worker without I/O."""

    settings = WorkerSettings.from_env(env)
    # 설정이 잘못됐으면 작업을 받기 전에 멈춘다. 문서마다 축을 조용히 비우는
    # 것보다 기동에 실패하는 편이 낫다.
    check_prompts_ready()
    llm_provider = build_llm_provider(
        profiles=("request_profile", "cpl", "fit", "sim"), env=env
    )
    embedding = build_embedding_client(env)
    ml_models = _build_ml_models(settings)
    handler = AnalysisJobHandler(
        storage=SupabaseWorkerStorage(
            supabase_url=settings.supabase_url,
            service_role_key=settings.service_role_key,
            timeout_seconds=settings.storage_timeout_seconds,
        ),
        store=PostgresAnalysisStore(
            settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        ),
        producer=VendoredRequestProfileProducer(
            llm_provider.client,
            model_profile="request_profile",
            model_id=llm_provider.model_id_for("request_profile"),
            max_repairs=llm_provider.max_repairs,
            parse_timeout_seconds=settings.parse_timeout_seconds,
            native_exact_candidate_mode=settings.request_native_exact_candidate_mode,
        ),
        embedding_client=embedding,
        analysis_engine=CoreAnalysisEngine(
            llm_provider.client,
            cpl_model_profile="cpl",
            fit_model_profile="fit",
            sim_model_profile="sim",
            max_repairs=llm_provider.max_repairs,
            ml_models=ml_models,
        ),
        top_k=settings.top_k,
        existing_kb_required=settings.existing_kb_required,
    )
    model1_model = ml_models[MlModelId.MODEL_1_SUPPORT_TYPE]
    model2_model = ml_models[MlModelId.MODEL_2_AMOUNT]
    return WorkerComposition(
        repository=PostgresJobRepository(
            settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        ),
        handler=handler,
        settings=settings,
        model1_remote_adapter=(
            model1_model if isinstance(model1_model, Model1HttpAdapter) else None
        ),
        model23_remote_adapter=(
            model2_model if isinstance(model2_model, Model2HttpAdapter) else None
        ),
    )


def make_worker_id() -> str:
    """Return a process-unique, operator-readable worker identity."""

    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


def configure_runtime_logging(*, level: str | int) -> None:
    """Configure operator logging without enabling provider request tracing."""

    logging.basicConfig(level=level)
    for logger_name in _SENSITIVE_TRANSPORT_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def run_worker(
    repository: JobRepository,
    handler: JobHandler,
    *,
    worker_id: str | None = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS,
    logger: logging.Logger | None = None,
) -> None:
    """Run one synchronous worker process until SIGINT or SIGTERM."""

    runtime = WorkerRuntime(
        repository,
        handler,
        worker_id=worker_id or make_worker_id(),
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
        idle_poll_seconds=idle_poll_seconds,
        logger=logger,
    )
    with runtime.install_signal_handlers():
        runtime.run_forever()


def _strict_ml_runtime_preflight_enabled(raw_value: str | None) -> bool:
    """Parse the integrity gate setting without silently accepting typos."""

    normalized = (raw_value or "").strip().lower()
    if normalized in _TRUE_ENV_VALUES:
        return True
    if normalized in _FALSE_ENV_VALUES:
        return False
    raise WorkerConfigurationError(
        f"{STRICT_ML_RUNTIME_PREFLIGHT_ENV} must be one of 1,true,yes,on,0,false,no,off"
    )


def _validate_strict_ml_process_identity(*, enabled: bool) -> None:
    """Keep a manually written Compose environment from restoring root."""

    if not enabled:
        return
    get_euid = getattr(os, "geteuid", None)
    get_egid = getattr(os, "getegid", None)
    if not callable(get_euid) or not callable(get_egid):
        raise WorkerConfigurationError(
            "strict ML worker requires POSIX effective UID/GID inspection"
        )
    if get_euid() == 0 or get_egid() == 0:
        raise WorkerConfigurationError(
            "strict ML worker must run with non-zero effective UID and GID"
        )


def main() -> int:
    """Run the worker without ever printing deployment secrets."""

    load_dotenv(override=False)
    configure_runtime_logging(level=os.getenv("LOG_LEVEL", "INFO").upper())
    try:
        strict_ml_preflight = _strict_ml_runtime_preflight_enabled(
            os.getenv(STRICT_ML_RUNTIME_PREFLIGHT_ENV)
        )
        _validate_strict_ml_process_identity(enabled=strict_ml_preflight)
        composition = build_worker()
    except (WorkerConfigurationError, MissingConfigError) as error:
        LOGGER.error("worker configuration is invalid: %s", error)
        return 2

    # Docker Compose opts into this strict gate because it supplies a required,
    # read-only Model 1 mount and a curated image-local Model 2/3 closure. A
    # direct host worker intentionally preserves the documented best-effort
    # unavailable-model mode unless its operator explicitly enables the gate.
    if strict_ml_preflight:
        try:
            verify_ml_runtime(
                ml_root=composition.settings.ml_root,
                model1_serving_dir=composition.settings.model1_serving_dir,
                backend_root=Path(__file__).resolve().parents[1],
                verify_model1=(
                    getattr(composition.settings, "model1_remote_base_url", None) is None
                ),
                verify_model23=(
                    getattr(composition.settings, "model23_remote_base_url", None) is None
                ),
            )
            remote_model1 = getattr(composition, "model1_remote_adapter", None)
            if remote_model1 is not None:
                remote_model1.check_ready()
            remote_model23 = getattr(composition, "model23_remote_adapter", None)
            if remote_model23 is not None:
                remote_model23.check_ready()
        except MlRuntimePreflightError as error:
            LOGGER.error("worker ML runtime preflight failed: %s", error)
            return 2
        except (Model1HttpError, Model1HttpContractError):
            LOGGER.error(
                "worker ML runtime preflight failed: remote Model 1 readiness check failed"
            )
            return 2
        except (Model23HttpError, Model23HttpContractError):
            LOGGER.error(
                "worker ML runtime preflight failed: remote Model 2/3 readiness check failed"
            )
            return 2
        except OSError:
            LOGGER.error(
                "worker ML runtime preflight failed: a runtime input is inaccessible"
            )
            return 2

    LOGGER.info(
        "starting PostgreSQL polling worker heartbeat_seconds=%s lease_seconds=%s "
        "idle_poll_seconds=%s top_k=%s",
        composition.settings.heartbeat_seconds,
        composition.settings.lease_seconds,
        composition.settings.idle_poll_seconds,
        composition.settings.top_k,
    )
    run_worker(
        composition.repository,
        composition.handler,
        heartbeat_seconds=composition.settings.heartbeat_seconds,
        lease_seconds=composition.settings.lease_seconds,
        idle_poll_seconds=composition.settings.idle_poll_seconds,
        logger=LOGGER,
    )
    return 0


__all__ = [
    "WorkerComposition",
    "WorkerConfigurationError",
    "WorkerSettings",
    "build_worker",
    "configure_runtime_logging",
    "main",
    "make_worker_id",
    "run_worker",
]


if __name__ == "__main__":  # pragma: no cover - exercised by container command
    sys.exit(main())
