"""Production composition and CLI entry point for the polling worker.

This module is deliberately the only place that turns deployment environment
variables into concrete worker adapters. The pipeline itself is kept free of
FastAPI, browser credentials, Redis/RQ, and Edge Function callback concerns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import math
import os
import socket
import sys
from uuid import uuid4

from dotenv import load_dotenv

from worker.adapters.openai_embedding_client import OpenAIEmbeddingClient
from worker.adapters.openai_llm_client import OpenAILLMClient
from worker.analysis_job import (
    AnalysisJobHandler,
    CoreAnalysisEngine,
    VendoredRequestProfileProducer,
)
from worker.config import MissingConfigError, OpenAIConfig
from worker.postgres_analysis_store import PostgresAnalysisStore
from worker.postgres_repository import PostgresJobRepository
from worker.runtime import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_IDLE_POLL_SECONDS,
    DEFAULT_LEASE_SECONDS,
    JobHandler,
    JobRepository,
    WorkerRuntime,
)
from worker.profiles import DEFAULT_PARSE_TIMEOUT_SECONDS
from worker.supabase_storage import SupabaseWorkerStorage


LOGGER = logging.getLogger(__name__)


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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WorkerSettings":
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
        if top_k > 100:
            raise WorkerConfigurationError("PREREVIEW_WORKER_TOP_K must be at most 100")
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
        )


@dataclass(frozen=True, slots=True)
class WorkerComposition:
    """The assembled trusted boundaries for one worker process."""

    repository: JobRepository
    handler: JobHandler
    settings: WorkerSettings


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise WorkerConfigurationError(f"required environment variable is not set: {name}")
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
        raise WorkerConfigurationError(f"environment variable must be an integer: {name}") from None
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
        raise WorkerConfigurationError(f"environment variable must be a number: {name}") from None
    if not math.isfinite(value) or value <= 0:
        raise WorkerConfigurationError(f"environment variable must be finite and positive: {name}")
    return value


def build_worker() -> WorkerComposition:
    """Build the PostgreSQL-polling HWP/HWPX analysis worker without I/O."""

    settings = WorkerSettings.from_env()
    openai = OpenAIConfig.from_env()
    llm = OpenAILLMClient(
        api_key=openai.api_key,
        model_profiles=openai.llm_model_profiles("request_profile", "fit", "sim"),
        timeout_seconds=openai.timeout_seconds,
    )
    embedding = OpenAIEmbeddingClient(
        api_key=openai.api_key,
        model_name=openai.embedding_model,
        timeout_seconds=openai.timeout_seconds,
    )
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
            llm,
            model_profile="request_profile",
            model_id=openai.llm_model,
            max_repairs=openai.max_repairs,
            parse_timeout_seconds=settings.parse_timeout_seconds,
        ),
        embedding_client=embedding,
        analysis_engine=CoreAnalysisEngine(
            llm,
            fit_model_profile="fit",
            sim_model_profile="sim",
            max_repairs=openai.max_repairs,
        ),
        top_k=settings.top_k,
    )
    return WorkerComposition(
        repository=PostgresJobRepository(
            settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        ),
        handler=handler,
        settings=settings,
    )


def make_worker_id() -> str:
    """Return a process-unique, operator-readable worker identity."""

    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


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


def main() -> int:
    """Run the worker without ever printing deployment secrets."""

    load_dotenv(override=False)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    try:
        composition = build_worker()
    except (WorkerConfigurationError, MissingConfigError) as error:
        LOGGER.error("worker configuration is invalid: %s", error)
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
    "main",
    "make_worker_id",
    "run_worker",
]


if __name__ == "__main__":  # pragma: no cover - exercised by container command
    sys.exit(main())
