"""Fail-closed settings for the combined resident Model 2/3 service."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from .contract import (
    MODEL23_DEFAULT_MAX_REQUEST_BYTES,
    MODEL23_MAX_REQUEST_BYTES,
    MODEL23_MIN_REQUEST_BYTES,
)

__all__ = [
    "Model23ServiceConfigurationError",
    "Model23ServiceSettings",
    "load_settings",
]


_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}")
_CPU_ML_ROOT = Path("/app/ml")


class Model23ServiceConfigurationError(RuntimeError):
    """A startup error that contains no secret or host path."""

    def __init__(self) -> None:
        super().__init__("model23_service_configuration_invalid")


@dataclass(frozen=True)
class Model23ServiceSettings:
    bearer_token: str = field(repr=False)
    ml_root: Path = _CPU_ML_ROOT
    max_request_bytes: int = MODEL23_DEFAULT_MAX_REQUEST_BYTES
    port: int = 8792

    def __post_init__(self) -> None:
        if not isinstance(self.bearer_token, str) or _TOKEN.fullmatch(self.bearer_token) is None:
            raise ValueError("invalid bearer token")
        if not isinstance(self.ml_root, Path) or not self.ml_root.is_absolute():
            raise ValueError("ml_root must be absolute")
        if isinstance(self.max_request_bytes, bool) or not isinstance(self.max_request_bytes, int):
            raise ValueError("max_request_bytes must be an integer")
        if not MODEL23_MIN_REQUEST_BYTES <= self.max_request_bytes <= MODEL23_MAX_REQUEST_BYTES:
            raise ValueError("max_request_bytes outside safe range")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or self.port != 8792:
            raise ValueError("Model 2/3 port is fixed to 8792")


def load_settings(environ: Mapping[str, str] | None = None) -> Model23ServiceSettings:
    """Load the fixed backend CPU profile and one protected bearer secret."""

    env = os.environ if environ is None else environ
    try:
        # Keep the path variable as an assertion for existing deployments.  An
        # arbitrary host path must not select unregistered joblib bytes.
        if env.get("PREREVIEW_ML_ROOT", str(_CPU_ML_ROOT)) != str(_CPU_ML_ROOT):
            raise ValueError("ml root override")
        return Model23ServiceSettings(
            bearer_token=_load_bearer_token(env),
            ml_root=_CPU_ML_ROOT,
            max_request_bytes=_parse_int(
                env,
                "PREREVIEW_MODEL23_MAX_REQUEST_BYTES",
                MODEL23_DEFAULT_MAX_REQUEST_BYTES,
            ),
            port=_parse_int(env, "PREREVIEW_MODEL23_PORT", 8792),
        )
    except Exception:
        raise Model23ServiceConfigurationError() from None


def _load_bearer_token(env: Mapping[str, str]) -> str:
    direct = env.get("PREREVIEW_MODEL23_API_BEARER_TOKEN", "")
    token_path_raw = env.get("PREREVIEW_MODEL23_API_BEARER_TOKEN_FILE", "")
    if bool(direct) == bool(token_path_raw):
        raise ValueError("exactly one bearer source is required")
    if direct:
        return direct

    token_path = Path(token_path_raw)
    if not token_path.is_absolute():
        raise ValueError("bearer token file must be absolute")
    expected = os.lstat(token_path)
    if not stat.S_ISREG(expected.st_mode):
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
            or (metadata.st_dev, metadata.st_ino) != (expected.st_dev, expected.st_ino)
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
