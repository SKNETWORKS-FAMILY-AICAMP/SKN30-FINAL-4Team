"""Dependency-free polling runtime for one database-backed worker process.

The runtime deliberately knows nothing about PostgreSQL, Supabase, or an
analysis pipeline.  A repository adapter owns atomic claiming, retry limits,
lease recovery, and all state transitions.  A handler only computes one
claimed job's result.

Every mutation after ``claim`` is fenced by ``processing_run_pk``.  Repository
implementations must return ``False`` when that processing attempt no longer
owns the lease; a stale worker must never overwrite a newer attempt.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from types import FrameType
from typing import Protocol, TypeAlias, runtime_checkable
from uuid import UUID


DEFAULT_HEARTBEAT_SECONDS = 30.0
DEFAULT_LEASE_SECONDS = 120
DEFAULT_IDLE_POLL_SECONDS = 1.0

JobKey: TypeAlias = str | UUID
ProcessingRunPK: TypeAlias = str | UUID
JobResult: TypeAlias = object


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """One leased queue item and the immutable token for this attempt."""

    job_pk: JobKey
    processing_run_pk: ProcessingRunPK
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobFailure:
    """Small, storage-neutral failure value passed to the repository."""

    kind: str
    message: str

    @classmethod
    def from_exception(cls, error: BaseException) -> JobFailure:
        kind = type(error).__name__
        message = " ".join(str(error).splitlines()).strip()
        return cls(kind=kind, message=message or kind)


@runtime_checkable
class JobRepository(Protocol):
    """Queue persistence boundary implemented by a fake or PostgreSQL adapter.

    ``claim`` must atomically choose only work that is eligible under the
    database's retry policy and create a fresh ``processing_run_pk``.  The
    other methods must use that token in their update predicate and return
    ``False`` when the caller has been fenced out.
    """

    def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        """Claim one eligible job, or return ``None`` when the queue is idle."""

        ...

    def heartbeat(
        self,
        *,
        job_pk: JobKey,
        processing_run_pk: ProcessingRunPK,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        """Extend a live lease if this processing attempt still owns it."""

        ...

    def complete(
        self,
        *,
        job_pk: JobKey,
        processing_run_pk: ProcessingRunPK,
        worker_id: str,
        result: JobResult,
    ) -> bool:
        """Atomically persist success if this processing attempt is live."""

        ...

    def fail(
        self,
        *,
        job_pk: JobKey,
        processing_run_pk: ProcessingRunPK,
        worker_id: str,
        failure: JobFailure,
    ) -> bool:
        """Record failure if this processing attempt is live.

        The repository/database decides whether another attempt is eligible.
        """

        ...


@runtime_checkable
class JobHandler(Protocol):
    """Pipeline boundary.  CPL/FIT/SIM composition is intentionally external."""

    def handle(self, job: ClaimedJob) -> JobResult:
        """Compute the result for one claim, raising on processing failure."""

        ...


class RunOutcome(str, Enum):
    """Observable result of one call to :meth:`WorkerRuntime.run_once`."""

    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    FENCED = "fenced"
    UNAVAILABLE = "unavailable"


class _HeartbeatFailure(RuntimeError):
    pass


class _Heartbeat:
    """Maintain a lease while the synchronous handler occupies the main thread."""

    def __init__(
        self,
        *,
        repository: JobRepository,
        job: ClaimedJob,
        worker_id: str,
        heartbeat_seconds: float,
        lease_seconds: int,
        logger: logging.Logger,
    ) -> None:
        self._repository = repository
        self._job = job
        self._worker_id = worker_id
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_seconds = lease_seconds
        self._logger = logger
        self._done = threading.Event()
        self.error: Exception | None = None
        self.lease_lost = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"worker-heartbeat-{job.processing_run_pk}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._done.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._done.wait(self._heartbeat_seconds):
            try:
                live = self._repository.heartbeat(
                    job_pk=self._job.job_pk,
                    processing_run_pk=self._job.processing_run_pk,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception as error:  # repository errors are handled after the job
                self.error = error
                self._logger.exception(
                    "worker heartbeat failed processing_run_pk=%s",
                    self._job.processing_run_pk,
                )
                return
            if not live:
                self.lease_lost = True
                self._logger.warning(
                    "worker lease lost processing_run_pk=%s",
                    self._job.processing_run_pk,
                )
                return


class WorkerRuntime:
    """Poll and process one job at a time in the current process."""

    def __init__(
        self,
        repository: JobRepository,
        handler: JobHandler,
        *,
        worker_id: str,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be shorter than lease_seconds")
        if idle_poll_seconds <= 0:
            raise ValueError("idle_poll_seconds must be positive")

        self._repository = repository
        self._handler = handler
        self.worker_id = worker_id
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.idle_poll_seconds = idle_poll_seconds
        self._logger = logger or logging.getLogger(__name__)
        self._stop_requested = threading.Event()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def request_stop(self) -> None:
        """Stop before the next claim; an active claim is allowed to finish."""

        self._stop_requested.set()

    def run_once(self) -> RunOutcome:
        """Claim, handle, and finalize at most one job."""

        try:
            job = self._repository.claim(
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
        except Exception as error:
            self._logger.exception(
                "worker claim failed error_type=%s", type(error).__name__
            )
            return RunOutcome.UNAVAILABLE
        if job is None:
            return RunOutcome.IDLE

        heartbeat = _Heartbeat(
            repository=self._repository,
            job=job,
            worker_id=self.worker_id,
            heartbeat_seconds=self.heartbeat_seconds,
            lease_seconds=self.lease_seconds,
            logger=self._logger,
        )
        heartbeat.start()
        failure: JobFailure | None = None
        try:
            result = self._handler.handle(job)
        except Exception as error:
            failure = JobFailure.from_exception(error)
        finally:
            heartbeat.close()

        if failure is not None:
            return self._record_failure(job, failure)

        if heartbeat.error is not None:
            error = _HeartbeatFailure(
                f"heartbeat failed: {type(heartbeat.error).__name__}: "
                f"{heartbeat.error}"
            )
            return self._record_failure(job, JobFailure.from_exception(error))

        try:
            completed = self._repository.complete(
                job_pk=job.job_pk,
                processing_run_pk=job.processing_run_pk,
                worker_id=self.worker_id,
                result=result,
            )
        except Exception as error:
            self._logger.exception(
                "worker completion failed processing_run_pk=%s error_type=%s",
                job.processing_run_pk,
                type(error).__name__,
            )
            return self._record_failure(job, JobFailure.from_exception(error))
        return (
            RunOutcome.COMPLETED
            if completed and not heartbeat.lease_lost
            else RunOutcome.FENCED
        )

    def run_forever(self) -> None:
        """Poll until shutdown, sleeping interruptibly whenever no job exists."""

        while not self.stop_requested:
            outcome = self.run_once()
            if outcome in (RunOutcome.IDLE, RunOutcome.UNAVAILABLE):
                self._stop_requested.wait(self.idle_poll_seconds)

    @contextmanager
    def install_signal_handlers(self) -> Iterator[None]:
        """Translate SIGINT/SIGTERM into a graceful stop request.

        Python only permits signal registration on the main thread, so the
        composition root should enter this context there.
        """

        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("signal handlers must be installed on the main thread")

        previous: dict[signal.Signals, signal.Handlers] = {}

        def handle_stop(signum: int, _frame: FrameType | None) -> None:
            self._logger.info("worker shutdown requested signal=%s", signum)
            self.request_stop()

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, handle_stop)
            yield
        finally:
            for signum, old_handler in previous.items():
                signal.signal(signum, old_handler)

    def _record_failure(self, job: ClaimedJob, failure: JobFailure) -> RunOutcome:
        try:
            failed = self._repository.fail(
                job_pk=job.job_pk,
                processing_run_pk=job.processing_run_pk,
                worker_id=self.worker_id,
                failure=failure,
            )
        except Exception as error:
            self._logger.exception(
                "worker failure recording failed processing_run_pk=%s error_type=%s",
                job.processing_run_pk,
                type(error).__name__,
            )
            return RunOutcome.UNAVAILABLE
        return RunOutcome.FAILED if failed else RunOutcome.FENCED


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_IDLE_POLL_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "ClaimedJob",
    "JobFailure",
    "JobHandler",
    "JobKey",
    "JobRepository",
    "JobResult",
    "ProcessingRunPK",
    "RunOutcome",
    "WorkerRuntime",
]
