"""One-process bounded FIFO around the synchronous Surya handler."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
import threading
from typing import Any, Protocol

from worker.contracts.accelerator import (
    AcceleratorJobState,
    AcceleratorJobStatus,
    SuryaLayoutRequest,
)

from .settings import PersistentApiSettings
from .store import DurableJobStore, JobRecord

__all__ = [
    "JobCapacityExceeded",
    "JobConflict",
    "JobNotCancellable",
    "JobSubmissionInvalid",
    "PersistentJobService",
]


class _Handler(Protocol):
    def handle(self, event: Mapping[str, object]) -> Mapping[str, object]: ...


class JobSubmissionInvalid(ValueError):
    pass


class JobCapacityExceeded(RuntimeError):
    pass


class JobConflict(RuntimeError):
    pass


class JobNotCancellable(RuntimeError):
    pass


@dataclass(frozen=True)
class _WorkItem:
    job_id: str
    # Capability-bearing input is memory-only and is never part of JobRecord.
    raw_input: Mapping[str, object]


class PersistentJobService:
    """Bounded job admission and exactly-one in-process GPU consumer."""

    def __init__(
        self,
        *,
        handler: _Handler,
        store: DurableJobStore,
        settings: PersistentApiSettings,
    ) -> None:
        self._handler = handler
        self._store = store
        self._settings = settings
        self._pending: OrderedDict[str, Mapping[str, object]] = OrderedDict()
        self._work_available = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._accepting = False
        self._running_job_id: str | None = None

    @property
    def accepting(self) -> bool:
        return (
            self._accepting
            and self._worker_task is not None
            and not self._worker_task.done()
            and self._store.available
        )

    @property
    def queue_depth(self) -> int:
        return len(self._pending)

    @property
    def queue_capacity(self) -> int:
        return self._settings.queue_capacity

    @property
    def running_job_id(self) -> str | None:
        return self._running_job_id

    def start(self) -> None:
        if self._worker_task is not None:
            raise RuntimeError("persistent job service already started")
        self._store.recover_incomplete(now=_now())
        self._accepting = True
        self._worker_task = asyncio.create_task(
            self._consume(), name="persistent-surya-gpu-consumer"
        )

    async def shutdown(self) -> bool:
        """Stop admission, fence queued work, and let the active call finish.

        A synchronous GPU/HTTP call cannot safely be interrupted from another
        Python thread.  If the configured grace expires, its durable record is
        deliberately left ``running``; the next process start fences it as
        ``infra_retryable/worker_restarted``.  The external supervisor owns
        the eventual process kill boundary.
        """

        self._accepting = False
        self._store.fail_queued_for_shutdown(now=_now())
        # Dropping the only capability-bearing references immediately releases
        # cancelled/shutdown queue payloads instead of retaining them behind a
        # long-running GPU call.  The journal contains no such material.
        self._pending.clear()
        self._work_available.set()

        task = self._worker_task
        if task is None:
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=float(self._settings.shutdown_grace_seconds),
            )
            return True
        except TimeoutError:
            # Do not cancel asyncio.to_thread: cancellation cannot stop the
            # underlying synchronous handler and could close the journal while
            # it is still completing a safe terminal write.
            return False

    def submit(
        self,
        raw_input: Mapping[str, object],
        *,
        requested_job_id: str | None = None,
    ) -> tuple[JobRecord, bool]:
        try:
            request = SuryaLayoutRequest.from_wire_payload(raw_input)
        except Exception:
            raise JobSubmissionInvalid() from None
        if requested_job_id is None or requested_job_id != request.logical_compute_key:
            raise JobSubmissionInvalid()
        if not self.accepting:
            raise JobCapacityExceeded()

        job_id = requested_job_id
        existing = self._store.get(job_id)
        if existing is not None:
            if existing.request_digest != request.request_digest:
                raise JobConflict()
            if existing.state != AcceleratorJobState.INFRA_RETRYABLE:
                return existing, False
            # The trusted EC2 coordinator explicitly retries by submitting
            # again after an infra-retryable terminal state.  Capabilities
            # may have been renewed while the stable digest/job key remains
            # unchanged, so replace the one remote-attempt record and enqueue
            # only the new in-memory body.  EC2/ops owns cross-attempt history.
            if len(self._pending) >= self._settings.queue_capacity:
                raise JobCapacityExceeded()
            retry = JobRecord(
                job_id=job_id,
                state=AcceleratorJobState.QUEUED,
                logical_compute_key=request.logical_compute_key,
                request_digest=request.request_digest,
                created_at=_now(),
            )
            self._store.replace(retry)
            self._pending[job_id] = raw_input
            self._work_available.set()
            return retry, True

        if len(self._pending) >= self._settings.queue_capacity:
            raise JobCapacityExceeded()
        record = JobRecord(
            job_id=job_id,
            state=AcceleratorJobState.QUEUED,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
            created_at=_now(),
        )
        self._store.create(record)
        self._pending[job_id] = raw_input
        self._work_available.set()
        return record, True

    def get(self, job_id: str) -> JobRecord | None:
        return self._store.get(job_id)

    def cancel(self, job_id: str) -> JobRecord | None:
        record = self._store.get(job_id)
        if record is None:
            return None
        if record.state == AcceleratorJobState.CANCELLED:
            return record
        if record.state != AcceleratorJobState.QUEUED:
            raise JobNotCancellable()
        cancelled = JobRecord.model_validate(
            record.model_copy(
                update={
                    "state": AcceleratorJobState.CANCELLED,
                    "completed_at": _now(),
                }
            ).model_dump(mode="python")
        )
        self._store.replace(cancelled)
        # Release signed URLs immediately and make queue capacity available.
        self._pending.pop(job_id, None)
        return cancelled

    async def _consume(self) -> None:
        while True:
            await self._work_available.wait()
            if not self._pending:
                self._work_available.clear()
                if not self._accepting:
                    return
                continue
            job_id, raw_input = self._pending.popitem(last=False)
            if not self._pending:
                self._work_available.clear()
            queued = _WorkItem(job_id=job_id, raw_input=raw_input)
            # Do not leave a second capability-bearing reference in this
            # long-lived consumer frame.
            del raw_input
            try:
                await self._consume_one(queued)
            finally:
                # The consumer waits indefinitely for the next job. Clear its
                # last work item even when a durable write raises so signed
                # URLs are not retained by the task frame or traceback.
                del queued

    async def _consume_one(self, queued: _WorkItem) -> None:
        """Process one item in a disposable capability-bearing frame."""

        try:
            record = self._store.get(queued.job_id)
            if record is None or record.state != AcceleratorJobState.QUEUED:
                return
            started = JobRecord.model_validate(
                record.model_copy(
                    update={
                        "state": AcceleratorJobState.RUNNING,
                        "started_at": _now(),
                    }
                ).model_dump(mode="python")
            )
            self._store.replace(started)
            self._running_job_id = queued.job_id
            try:
                terminal = await self._execute(queued, started)
                self._store.replace(terminal)
            finally:
                self._running_job_id = None
        finally:
            # Clear every local reference held by an exceptional coroutine
            # traceback. JobRecord never contains capabilities.
            del queued

    async def _execute(self, item: _WorkItem, started: JobRecord) -> JobRecord:
        try:
            raw_output = await _run_handler_thread(
                self._handler,
                {"id": item.job_id, "input": item.raw_input},
            )
            output = AcceleratorJobStatus.model_validate(raw_output)
            if (
                not output.state.is_terminal
                or output.state == AcceleratorJobState.FENCE_LOST
                or output.external_job_id != item.job_id
                or output.logical_compute_key != started.logical_compute_key
                or output.request_digest != started.request_digest
            ):
                raise ValueError("unbound worker terminal output")
            completed_at = _now()
            return JobRecord(
                job_id=item.job_id,
                state=output.state,
                logical_compute_key=started.logical_compute_key,
                request_digest=started.request_digest,
                created_at=started.created_at,
                started_at=started.started_at,
                completed_at=completed_at,
                reason_code=output.reason_code,
                terminal_output=output,
            )
        except Exception:
            # Handler/provider diagnostics can contain endpoint or content
            # details.  Persist and return only the fixed public reason.
            return JobRecord(
                job_id=item.job_id,
                state=AcceleratorJobState.INFRA_RETRYABLE,
                logical_compute_key=started.logical_compute_key,
                request_digest=started.request_digest,
                created_at=started.created_at,
                started_at=started.started_at,
                completed_at=_now(),
                reason_code="worker_runtime_failed",
            )


def _now() -> datetime:
    return datetime.now(UTC)


async def _run_handler_thread(
    handler: _Handler,
    event: Mapping[str, object],
) -> Mapping[str, object]:
    """Run the synchronous GPU adapter on one disposable daemon thread.

    ``asyncio.to_thread`` uses the process-wide default executor, whose
    non-daemon threads can keep a Pod alive after Uvicorn has exhausted its
    shutdown grace.  This private bridge retains concurrency=1 in the sole
    consumer while letting the external supervisor terminate a wedged call.
    """

    loop = asyncio.get_running_loop()
    completed: asyncio.Future[Mapping[str, object]] = loop.create_future()

    def settle(result: Mapping[str, object] | None, error: BaseException | None) -> None:
        if completed.done():
            return
        if error is not None:
            completed.set_exception(error)
        elif result is None:
            completed.set_exception(RuntimeError("worker returned no result"))
        else:
            completed.set_result(result)

    def invoke() -> None:
        try:
            result = handler.handle(event)
            error: BaseException | None = None
        except BaseException as caught:  # normalized at the async trust boundary
            result = None
            error = caught
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:
            # Event loop already exited after supervisor shutdown.  The
            # daemon thread must not retain or log the capability-bearing event.
            pass

    threading.Thread(
        target=invoke,
        name="persistent-surya-handler",
        daemon=True,
    ).start()
    return await completed
