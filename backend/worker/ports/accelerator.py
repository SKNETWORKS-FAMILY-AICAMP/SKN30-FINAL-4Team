"""Port and deterministic in-memory fake for remote document acceleration.

The fake exists solely for contract tests and offline orchestration tests.  It
does not contact a provider or grant any storage/DB authority.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Protocol

from worker.contracts.accelerator import (
    AcceleratorContractError,
    AcceleratorDispatchPolicy,
    AcceleratorJobState,
    AcceleratorJobStatus,
    SuryaLayoutRequest,
    validate_accelerator_dispatch,
)


__all__ = [
    "AcceleratorJobNotFoundError",
    "AcceleratorPort",
    "InMemoryAccelerator",
]


class AcceleratorJobNotFoundError(LookupError):
    """The remote provider has no retained job under this external ID."""


class AcceleratorPort(Protocol):
    """The small submit/poll/cancel surface a future provider adapter must implement."""

    def submit(self, request: SuryaLayoutRequest) -> AcceleratorJobStatus: ...

    def get_status(self, external_job_id: str) -> AcceleratorJobStatus: ...

    def cancel(self, external_job_id: str) -> AcceleratorJobStatus: ...


class InMemoryAccelerator:
    """A deterministic fake that models reattach and cancel semantics.

    The same logical key may be reattached only when its non-secret request
    digest is identical.  That mirrors the design rule that a changed compute
    request must not silently reuse an unrelated external job.
    """

    def __init__(
        self,
        *,
        dispatch_policy: AcceleratorDispatchPolicy,
        now: Callable[[], datetime],
    ) -> None:
        """Build a fake with the same injected dispatch gate as a real adapter."""

        self._dispatch_policy = dispatch_policy
        self._now = now
        self._jobs: dict[str, AcceleratorJobStatus] = {}
        self._job_id_by_logical_key: dict[str, str] = {}
        self._next_job_number = 1

    def submit(self, request: SuryaLayoutRequest) -> AcceleratorJobStatus:
        validate_accelerator_dispatch(
            request,
            self._dispatch_policy,
            now=self._now(),
        )
        existing_job_id = self._job_id_by_logical_key.get(request.logical_compute_key)
        if existing_job_id is not None:
            existing = self._jobs[existing_job_id]
            if existing.request_digest != request.request_digest:
                raise AcceleratorContractError(
                    "logical_compute_key is already bound to a different request digest"
                )
            return existing

        external_job_id = f"in-memory-surya-{self._next_job_number:06d}"
        self._next_job_number += 1
        status = AcceleratorJobStatus(
            external_job_id=external_job_id,
            state=AcceleratorJobState.QUEUED,
            logical_compute_key=request.logical_compute_key,
            request_digest=request.request_digest,
        )
        self._jobs[external_job_id] = status
        self._job_id_by_logical_key[request.logical_compute_key] = external_job_id
        return status

    def get_status(self, external_job_id: str) -> AcceleratorJobStatus:
        try:
            return self._jobs[external_job_id]
        except KeyError as error:
            raise AcceleratorJobNotFoundError(external_job_id) from error

    def cancel(self, external_job_id: str) -> AcceleratorJobStatus:
        current = self.get_status(external_job_id)
        if current.state.is_terminal:
            return current
        cancelled = AcceleratorJobStatus(
            external_job_id=current.external_job_id,
            state=AcceleratorJobState.CANCELLED,
            logical_compute_key=current.logical_compute_key,
            request_digest=current.request_digest,
        )
        self._jobs[external_job_id] = cancelled
        return cancelled
