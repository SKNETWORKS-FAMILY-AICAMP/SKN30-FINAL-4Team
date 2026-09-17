"""Standalone long-running worker for HTML-to-PDF report generation."""

from __future__ import annotations

import logging
import math
import os
import time
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from worker.postgres_report_repository import PostgresReportJobRepository
from worker.reporting.handler import DEFAULT_REPORT_MAX_BYTES, ReportJobHandler
from worker.reporting.renderer import PersistentChromiumRenderer, ReportRenderError
from worker.runtime import DEFAULT_HEARTBEAT_SECONDS, DEFAULT_IDLE_POLL_SECONDS, DEFAULT_LEASE_SECONDS, RunOutcome, WorkerRuntime
from worker.supabase_storage import SupabaseWorkerStorage


LOGGER = logging.getLogger(__name__)
DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "reporting" / "templates"
REPORT_CLEANUP_INTERVAL_SECONDS = 60.0


class ReportWorkerConfigurationError(RuntimeError):
    pass


def _required(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    raise ReportWorkerConfigurationError("required environment variable is not set: " + " or ".join(names))


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ReportWorkerConfigurationError(f"{name} must be a number") from None
    if not math.isfinite(value) or value <= 0:
        raise ReportWorkerConfigurationError(f"{name} must be positive")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _positive_float(env, name, float(default))
    if not value.is_integer():
        raise ReportWorkerConfigurationError(f"{name} must be an integer")
    return int(value)


@dataclass(frozen=True, slots=True)
class ReportWorkerSettings:
    database_url: str
    supabase_url: str
    service_role_key: str
    chromium_executable: str | None
    template_dir: Path
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS
    render_timeout_seconds: float = 60.0
    storage_timeout_seconds: float = 30.0
    database_connect_timeout_seconds: int = 10
    max_pdf_bytes: int = DEFAULT_REPORT_MAX_BYTES

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ReportWorkerSettings":
        values = os.environ if env is None else env
        lease = _positive_int(values, "PREREVIEW_REPORT_WORKER_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
        heartbeat = _positive_float(values, "PREREVIEW_REPORT_WORKER_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS)
        if not 30 <= lease <= 3600:
            raise ReportWorkerConfigurationError("PREREVIEW_REPORT_WORKER_LEASE_SECONDS must be between 30 and 3600")
        if heartbeat >= lease:
            raise ReportWorkerConfigurationError("report worker heartbeat must be shorter than its lease")
        max_pdf_bytes = _positive_int(
            values, "PREREVIEW_REPORT_MAX_BYTES", DEFAULT_REPORT_MAX_BYTES
        )
        if max_pdf_bytes > DEFAULT_REPORT_MAX_BYTES:
            raise ReportWorkerConfigurationError(
                f"PREREVIEW_REPORT_MAX_BYTES must not exceed {DEFAULT_REPORT_MAX_BYTES}"
            )
        configured_template = values.get("PREREVIEW_REPORT_TEMPLATE_DIR", "").strip()
        return cls(
            database_url=_required(values, "DATABASE_URL", "SUPABASE_DB_URL"),
            supabase_url=_required(values, "SUPABASE_URL"),
            service_role_key=_required(values, "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"),
            chromium_executable=values.get("PREREVIEW_REPORT_CHROMIUM_EXECUTABLE", "").strip() or None,
            template_dir=Path(configured_template) if configured_template else DEFAULT_TEMPLATE_DIR,
            heartbeat_seconds=heartbeat,
            lease_seconds=lease,
            idle_poll_seconds=_positive_float(values, "PREREVIEW_REPORT_WORKER_IDLE_POLL_SECONDS", DEFAULT_IDLE_POLL_SECONDS),
            render_timeout_seconds=_positive_float(values, "PREREVIEW_REPORT_RENDER_TIMEOUT_SECONDS", 60.0),
            storage_timeout_seconds=_positive_float(values, "PREREVIEW_REPORT_STORAGE_TIMEOUT_SECONDS", 30.0),
            database_connect_timeout_seconds=_positive_int(values, "PREREVIEW_REPORT_DATABASE_CONNECT_TIMEOUT_SECONDS", 10),
            max_pdf_bytes=max_pdf_bytes,
        )


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


def _next_cleanup_deadline(*, now: float, cleaned: bool) -> float:
    """Drain cleanup work immediately; back off only after an empty/error sweep."""

    return now if cleaned else now + REPORT_CLEANUP_INTERVAL_SECONDS


def main() -> int:
    load_dotenv(override=False)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    try:
        settings = ReportWorkerSettings.from_env()
        renderer = PersistentChromiumRenderer(
            template_dir=settings.template_dir,
            executable_path=settings.chromium_executable,
            timeout_seconds=settings.render_timeout_seconds,
        )
        repository = PostgresReportJobRepository(
            settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        )
        storage = SupabaseWorkerStorage(
            supabase_url=settings.supabase_url, service_role_key=settings.service_role_key,
            timeout_seconds=settings.storage_timeout_seconds,
        )
        handler = ReportJobHandler(
            renderer=renderer, storage=storage, max_pdf_bytes=settings.max_pdf_bytes
        )
        renderer.start()
    except (ReportWorkerConfigurationError, ReportRenderError, ValueError) as exc:
        LOGGER.error("report worker configuration is invalid: %s", exc)
        return 2

    LOGGER.info(
        "starting PDF report worker heartbeat_seconds=%s lease_seconds=%s",
        settings.heartbeat_seconds, settings.lease_seconds,
    )
    worker_id = _worker_id()
    runtime = WorkerRuntime(
        repository, handler, worker_id=worker_id,
        heartbeat_seconds=settings.heartbeat_seconds,
        lease_seconds=settings.lease_seconds,
        idle_poll_seconds=settings.idle_poll_seconds, logger=LOGGER,
    )
    next_cleanup_at = 0.0
    try:
        with runtime.install_signal_handlers():
            while not runtime.stop_requested:
                cleaned = False
                now = time.monotonic()
                if now >= next_cleanup_at:
                    try:
                        cleaned = repository.cleanup_once(
                            storage=storage, worker_id=worker_id,
                            lease_seconds=settings.lease_seconds,
                        )
                    except Exception:
                        LOGGER.exception("PDF report storage cleanup failed")
                    next_cleanup_at = _next_cleanup_deadline(
                        now=now, cleaned=cleaned
                    )
                outcome = runtime.run_once()
                if not cleaned and outcome in (RunOutcome.IDLE, RunOutcome.UNAVAILABLE):
                    time.sleep(settings.idle_poll_seconds)
    finally:
        renderer.close()
    return 0



if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
