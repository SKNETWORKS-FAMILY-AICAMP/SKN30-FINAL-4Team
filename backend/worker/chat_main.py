"""Standalone PostgreSQL-polling worker for result-grounded chat replies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import math
import os

from dotenv import load_dotenv

from worker.adapters.openai_llm_client import OpenAILLMClient
from worker.chat import ResultGroundedChatHandler
from worker.config import MissingConfigError, OpenAIConfig
from worker.main import make_worker_id
from worker.postgres_chat_repository import PostgresChatJobRepository
from worker.runtime import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_IDLE_POLL_SECONDS,
    DEFAULT_LEASE_SECONDS,
    JobHandler,
    JobRepository,
    WorkerRuntime,
)


LOGGER = logging.getLogger(__name__)


class ChatWorkerConfigurationError(RuntimeError):
    """A missing or invalid chat-worker deployment setting."""


@dataclass(frozen=True, slots=True)
class ChatWorkerSettings:
    database_url: str
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS
    database_connect_timeout_seconds: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ChatWorkerSettings":
        values = os.environ if env is None else env
        database_url = _first_required(values, "DATABASE_URL", "SUPABASE_DB_URL")
        lease_seconds = _positive_int(
            values, "PREREVIEW_CHAT_WORKER_LEASE_SECONDS", DEFAULT_LEASE_SECONDS
        )
        if not 30 <= lease_seconds <= 3600:
            raise ChatWorkerConfigurationError(
                "PREREVIEW_CHAT_WORKER_LEASE_SECONDS must be between 30 and 3600"
            )
        heartbeat_seconds = _positive_float(
            values,
            "PREREVIEW_CHAT_WORKER_HEARTBEAT_SECONDS",
            DEFAULT_HEARTBEAT_SECONDS,
        )
        if heartbeat_seconds >= lease_seconds:
            raise ChatWorkerConfigurationError(
                "PREREVIEW_CHAT_WORKER_HEARTBEAT_SECONDS must be shorter than "
                "PREREVIEW_CHAT_WORKER_LEASE_SECONDS"
            )
        return cls(
            database_url=database_url,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
            idle_poll_seconds=_positive_float(
                values,
                "PREREVIEW_CHAT_WORKER_IDLE_POLL_SECONDS",
                DEFAULT_IDLE_POLL_SECONDS,
            ),
            database_connect_timeout_seconds=_positive_int(
                values,
                "PREREVIEW_CHAT_DATABASE_CONNECT_TIMEOUT_SECONDS",
                _positive_int(
                    values,
                    "PREREVIEW_WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS",
                    10,
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class ChatWorkerComposition:
    repository: JobRepository
    handler: JobHandler
    settings: ChatWorkerSettings


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ChatWorkerConfigurationError(
            f"required environment variable is not set: {name}"
        )
    return value


def _first_required(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    raise ChatWorkerConfigurationError(
        "required environment variable is not set: " + " or ".join(names)
    )


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ChatWorkerConfigurationError(
            f"environment variable must be an integer: {name}"
        ) from None
    if value <= 0:
        raise ChatWorkerConfigurationError(
            f"environment variable must be positive: {name}"
        )
    return value


def _nonnegative_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ChatWorkerConfigurationError(
            f"environment variable must be an integer: {name}"
        ) from None
    if value < 0:
        raise ChatWorkerConfigurationError(
            f"environment variable must not be negative: {name}"
        )
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ChatWorkerConfigurationError(
            f"environment variable must be a number: {name}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ChatWorkerConfigurationError(
            f"environment variable must be finite and positive: {name}"
        )
    return value


def build_chat_worker(
    env: Mapping[str, str] | None = None,
) -> ChatWorkerComposition:
    """Build chat-only adapters without importing FastAPI or analysis storage."""

    settings = ChatWorkerSettings.from_env(env)
    openai = _openai_config(env)
    llm = OpenAILLMClient(
        api_key=openai.api_key,
        model_profiles=openai.llm_model_profiles("chat"),
        timeout_seconds=openai.timeout_seconds,
    )
    return ChatWorkerComposition(
        repository=PostgresChatJobRepository(
            settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        ),
        handler=ResultGroundedChatHandler(llm, model_profile="chat"),
        settings=settings,
    )


def _openai_config(env: Mapping[str, str] | None) -> OpenAIConfig:
    """Use the shared OpenAIConfig while keeping composition tests injectable."""

    if env is None:
        return OpenAIConfig.from_env()
    return OpenAIConfig(
        api_key=_required(env, "OPENAI_API_KEY"),
        llm_model=_required(env, "OPENAI_LLM_MODEL"),
        embedding_model=_required(env, "OPENAI_EMBEDDING_MODEL"),
        timeout_seconds=_positive_float(env, "OPENAI_TIMEOUT_SECONDS", 60.0),
        max_repairs=_nonnegative_int(env, "OPENAI_MAX_REPAIRS", 1),
    )


def run_chat_worker(
    repository: JobRepository,
    handler: JobHandler,
    *,
    worker_id: str | None = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS,
    logger: logging.Logger | None = None,
) -> None:
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


def main() -> int:
    load_dotenv(override=False)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    try:
        composition = build_chat_worker()
    except (ChatWorkerConfigurationError, MissingConfigError) as error:
        LOGGER.error("chat worker configuration is invalid: %s", error)
        return 2

    LOGGER.info(
        "starting chat worker heartbeat_seconds=%s lease_seconds=%s idle_poll_seconds=%s",
        composition.settings.heartbeat_seconds,
        composition.settings.lease_seconds,
        composition.settings.idle_poll_seconds,
    )
    run_chat_worker(
        composition.repository,
        composition.handler,
        heartbeat_seconds=composition.settings.heartbeat_seconds,
        lease_seconds=composition.settings.lease_seconds,
        idle_poll_seconds=composition.settings.idle_poll_seconds,
        logger=LOGGER,
    )
    return 0


__all__ = [
    "ChatWorkerComposition",
    "ChatWorkerConfigurationError",
    "ChatWorkerSettings",
    "build_chat_worker",
    "main",
    "run_chat_worker",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
