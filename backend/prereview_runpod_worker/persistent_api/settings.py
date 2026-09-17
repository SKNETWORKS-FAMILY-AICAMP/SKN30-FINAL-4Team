"""Fail-closed settings for the persistent RunPod HTTP service."""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import os
from pathlib import Path
import re
from typing import Mapping

__all__ = [
    "PersistentApiConfigurationError",
    "PersistentApiSettings",
    "load_persistent_api_settings",
]


_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}")
_DEFAULT_STATE_DIRECTORY = Path("/workspace/persistent/prereview/surya-jobs")
_JOURNAL_KEY_DOMAIN = b"prereview.persistent-surya/journal-key/v1"


class PersistentApiConfigurationError(RuntimeError):
    """A deliberately non-diagnostic deployment configuration failure."""

    def __init__(self) -> None:
        super().__init__("persistent_api_configuration_invalid")


@dataclass(frozen=True)
class PersistentApiSettings:
    """Validated limits and the bearer secret used only in process memory."""

    bearer_token: str = field(repr=False)
    state_directory: Path = _DEFAULT_STATE_DIRECTORY
    queue_capacity: int = 4
    max_request_bytes: int = 2 * 1024 * 1024
    max_job_records: int = 10_000
    shutdown_grace_seconds: float = 210.0
    port: int = 8787
    _journal_mac_key: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.bearer_token, str) or _TOKEN.fullmatch(
            self.bearer_token
        ) is None:
            raise ValueError("bearer_token must be 32-512 URL-safe characters")
        if not isinstance(self.state_directory, Path) or not self.state_directory.is_absolute():
            raise ValueError("state_directory must be an absolute Path")
        for name, upper_bound in (
            ("queue_capacity", 64),
            ("max_request_bytes", 8 * 1024 * 1024),
            ("max_job_records", 1_000_000),
            ("port", 65_535),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper_bound
            ):
                raise ValueError(f"{name} is outside its safe range")
        if self.port < 1024:
            raise ValueError("port must be unprivileged")
        if (
            isinstance(self.shutdown_grace_seconds, bool)
            or not isinstance(self.shutdown_grace_seconds, (int, float))
            or not 0.0 <= float(self.shutdown_grace_seconds) <= 300.0
        ):
            raise ValueError("shutdown_grace_seconds is outside its safe range")
        # The Network Volume does not make POSIX mode bits authoritative.
        # Derive a separate, process-memory-only key so persisted state is
        # authenticated without writing another secret alongside it.
        object.__setattr__(
            self,
            "_journal_mac_key",
            hmac.digest(
                self.bearer_token.encode("utf-8"), _JOURNAL_KEY_DOMAIN, "sha256"
            ),
        )

    @property
    def journal_mac_key(self) -> bytes:
        """Return the in-memory key for authenticated journal envelopes."""

        return self._journal_mac_key


def load_persistent_api_settings(
    environ: Mapping[str, str] | None = None,
) -> PersistentApiSettings:
    """Load settings without including a token or invalid value in errors."""

    environment = os.environ if environ is None else environ
    try:
        token = environment.get("PREREVIEW_SURYA_API_BEARER_TOKEN", "")
        raw_state_directory = environment.get(
            "PREREVIEW_SURYA_API_STATE_DIRECTORY",
            str(_DEFAULT_STATE_DIRECTORY),
        )
        # The persistent-Pod recovery journal must survive Pod replacement.
        # Do not allow an otherwise-valid absolute override such as /run or
        # /tmp to silently turn it into ephemeral state.  Tests may still
        # construct ``PersistentApiSettings`` directly with a temporary path;
        # deployed environment loading is deliberately fixed to the mounted
        # Network Volume location.
        if raw_state_directory != str(_DEFAULT_STATE_DIRECTORY):
            raise ValueError("persistent journal location is fixed")
        state_directory = _DEFAULT_STATE_DIRECTORY
        return PersistentApiSettings(
            bearer_token=token,
            state_directory=state_directory,
            queue_capacity=_integer(
                environment,
                "PREREVIEW_SURYA_API_QUEUE_CAPACITY",
                default=4,
            ),
            max_request_bytes=_integer(
                environment,
                "PREREVIEW_SURYA_API_MAX_REQUEST_BYTES",
                default=2 * 1024 * 1024,
            ),
            max_job_records=_integer(
                environment,
                "PREREVIEW_SURYA_API_MAX_JOB_RECORDS",
                default=10_000,
            ),
            shutdown_grace_seconds=_float(
                environment,
                "PREREVIEW_SURYA_API_SHUTDOWN_GRACE_SECONDS",
                default=210.0,
            ),
            port=_integer(
                environment,
                "PREREVIEW_SURYA_API_PORT",
                default=8787,
            ),
        )
    except Exception:
        raise PersistentApiConfigurationError() from None


def _integer(environment: Mapping[str, str], name: str, *, default: int) -> int:
    raw = environment.get(name)
    return default if raw is None else int(raw)


def _float(environment: Mapping[str, str], name: str, *, default: float) -> float:
    raw = environment.get(name)
    return default if raw is None else float(raw)
