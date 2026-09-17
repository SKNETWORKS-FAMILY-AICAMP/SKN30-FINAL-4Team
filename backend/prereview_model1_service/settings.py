"""Fail-closed settings for the isolated resident Model 1 service."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from .contract import (
    MODEL1_DEFAULT_MAX_REQUEST_BYTES,
    MODEL1_MAX_REQUEST_BYTES,
    MODEL1_MIN_REQUEST_BYTES,
)

__all__ = [
    "Model1ServiceConfigurationError",
    "Model1ServiceSettings",
    "load_settings",
]


_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}")
_RUNPOD_WORKER_ROOT = Path("/workspace/project/prereview-model1/worker")
_RUNPOD_RUNTIME_DIR = Path("/workspace/project/prereview-model1/model1")
_RUNPOD_PREPROCESSOR = (
    _RUNPOD_WORKER_ROOT / "ml" / "pipelines" / "model1" / "dl07_m1_apply.py"
)
_BACKEND_CPU_RUNTIME_DIR = Path("/opt/prereview/model1")
_BACKEND_CPU_PREPROCESSOR = Path("/app/ml/pipelines/model1/dl07_m1_apply.py")
_DEPLOYMENT_PATHS = {
    "runpod": (_RUNPOD_RUNTIME_DIR, _RUNPOD_PREPROCESSOR),
    "backend-cpu": (_BACKEND_CPU_RUNTIME_DIR, _BACKEND_CPU_PREPROCESSOR),
}


class Model1ServiceConfigurationError(RuntimeError):
    """A non-diagnostic error safe to expose at process startup."""

    def __init__(self) -> None:
        super().__init__("model1_service_configuration_invalid")


@dataclass(frozen=True)
class Model1ServiceSettings:
    """Validated process settings; the bearer is never represented in repr()."""

    bearer_token: str = field(repr=False)
    runtime_directory: Path = _RUNPOD_RUNTIME_DIR
    preprocessor_path: Path = _RUNPOD_PREPROCESSOR
    max_request_bytes: int = MODEL1_DEFAULT_MAX_REQUEST_BYTES
    port: int = 8791
    require_cuda: bool = True
    device: str = "cuda"

    def __post_init__(self) -> None:
        if not isinstance(self.bearer_token, str) or _TOKEN.fullmatch(self.bearer_token) is None:
            raise ValueError("invalid bearer token")
        for name in ("runtime_directory", "preprocessor_path"):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        if isinstance(self.max_request_bytes, bool) or not isinstance(self.max_request_bytes, int):
            raise ValueError("max_request_bytes must be an integer")
        if not (
            MODEL1_MIN_REQUEST_BYTES
            <= self.max_request_bytes
            <= MODEL1_MAX_REQUEST_BYTES
        ):
            raise ValueError("max_request_bytes outside safe range")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or self.port != 8791:
            raise ValueError("Model 1 port is fixed to 8791")
        if not isinstance(self.require_cuda, bool):
            raise ValueError("require_cuda must be boolean")
        if self.device not in {"cpu", "cuda"}:
            raise ValueError("device must be cpu or cuda")
        if self.require_cuda != (self.device == "cuda"):
            raise ValueError("require_cuda must match device")


def load_settings(environ: Mapping[str, str] | None = None) -> Model1ServiceSettings:
    """Load one explicit deployment profile and its protected bearer secret."""

    env = os.environ if environ is None else environ
    try:
        profile = env.get("PREREVIEW_MODEL1_DEPLOYMENT_PROFILE", "runpod")
        if profile not in _DEPLOYMENT_PATHS:
            raise ValueError("invalid deployment profile")
        runtime_directory, preprocessor_path = _DEPLOYMENT_PATHS[profile]

        # Paths are selected by a closed profile rather than arbitrary operator
        # input.  Keep the old variables as assertions so a stale deployment
        # fails instead of silently loading a different artifact.
        if env.get("PREREVIEW_MODEL1_RUNTIME_DIR", str(runtime_directory)) != str(runtime_directory):
            raise ValueError("runtime path override")
        if env.get("PREREVIEW_MODEL1_PREPROCESSOR", str(preprocessor_path)) != str(preprocessor_path):
            raise ValueError("preprocessor path override")
        default_device = "cpu" if profile == "backend-cpu" else "cuda"
        device = env.get("PREREVIEW_MODEL1_DEVICE", default_device)
        if device not in {"cpu", "cuda"}:
            raise ValueError("invalid device")
        if profile == "backend-cpu" and device != "cpu":
            raise ValueError("backend CPU profile requires CPU")
        return Model1ServiceSettings(
            bearer_token=_load_bearer_token(env),
            runtime_directory=runtime_directory,
            preprocessor_path=preprocessor_path,
            max_request_bytes=_parse_int(
                env,
                "PREREVIEW_MODEL1_MAX_REQUEST_BYTES",
                MODEL1_DEFAULT_MAX_REQUEST_BYTES,
            ),
            port=_parse_int(env, "PREREVIEW_MODEL1_PORT", 8791),
            require_cuda=device == "cuda",
            device=device,
        )
    except Exception:
        raise Model1ServiceConfigurationError() from None


def _load_bearer_token(env: Mapping[str, str]) -> str:
    direct = env.get("PREREVIEW_MODEL1_API_BEARER_TOKEN", "")
    token_path_raw = env.get("PREREVIEW_MODEL1_API_BEARER_TOKEN_FILE", "")
    if bool(direct) == bool(token_path_raw):
        raise ValueError("exactly one bearer source is required")
    if direct:
        return direct

    token_path = Path(token_path_raw)
    if not token_path.is_absolute():
        raise ValueError("bearer token file must be absolute")
    expected_metadata = os.lstat(token_path)
    if not stat.S_ISREG(expected_metadata.st_mode):
        raise ValueError("unsafe bearer token file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(token_path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or (metadata.st_dev, metadata.st_ino)
            != (expected_metadata.st_dev, expected_metadata.st_ino)
        ):
            raise ValueError("unsafe bearer token file")
        raw = os.read(descriptor, 514)
        if os.read(descriptor, 1):
            raise ValueError("bearer token file is too large")
    finally:
        os.close(descriptor)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\n" in raw or b"\r" in raw:
        raise ValueError("unsafe bearer token file")
    return raw.decode("ascii")


def _parse_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name)
    return default if value is None else int(value)
