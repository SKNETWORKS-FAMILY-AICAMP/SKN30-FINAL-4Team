"""Ports shared by the analysis-run application service and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class AnalysisRunError(RuntimeError):
    """Base class for safe, expected analysis-run failures."""


class ActiveAnalysisRunExists(AnalysisRunError):
    """The owner already has an upload or analysis in progress."""


class IdempotencyKeyConflict(AnalysisRunError):
    """One owner reused the same Idempotency-Key for a different source.

    Distinct from :class:`ActiveAnalysisRunExists`: an exact replay (same
    owner, key, and source) is not a conflict at all — it returns the
    existing run. Keys are owner-scoped so an unrelated user's UUID collision
    remains isolated. This is only the *different input, same owner/key* case
    (v0.2 spec section 5.2, step 3).
    """


class ActiveResultSessionExists(AnalysisRunError):
    """The owner has an unclosed active result session (spec section 5.1/5.2).

    Distinct from :class:`ActiveAnalysisRunExists` (an in-flight processing
    run): this fires when the *previous* analysis already finished and its
    result session is still open. The caller must ``POST
    /analysis-sessions/{id}/close`` it before a new upload is accepted.

    Raised by the v0.2 reservation RPC before a new upload is created; the
    PostgreSQL adapter maps that domain conflict without exposing DB details.
    """


class AnalysisRunPersistenceUnavailable(AnalysisRunError):
    """The internal database cannot currently serve the request."""


class AnalysisQueueCapacityExceeded(AnalysisRunPersistenceUnavailable):
    """The shared analysis/chat worker backlog has reached its admission cap.

    This is intentionally a safe 503-class failure rather than a 409: callers
    should retry with the same idempotency key, and an exact replay remains
    accepted even while the queue is full.
    """


class AnalysisRunFinalizationExpired(AnalysisQueueCapacityExceeded):
    """An expired upload could not be re-admitted and must be cleaned up."""

    def __init__(self, message: str, cleanup_object: "UploadCleanupObject") -> None:
        super().__init__(message)
        self.cleanup_object = cleanup_object


class AnalysisRunFinalizationRejected(AnalysisRunPersistenceUnavailable):
    """The upload finalization transaction is known not to have committed."""


class AnalysisRunFinalizationUncertain(AnalysisRunPersistenceUnavailable):
    """The database could not prove whether upload finalization committed."""


class ObjectStorageUnavailable(AnalysisRunError):
    """The private object store cannot currently serve the request."""


class ObjectStorageWriteUncertain(ObjectStorageUnavailable):
    """The object store could not prove whether an upload was persisted."""


@dataclass(frozen=True, slots=True)
class AnalysisRunRecord:
    analysis_run_id: str
    status: str
    analysis_case_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SourceObject:
    bucket: str
    object_key: str
    content_sha256: str
    filename: str
    mime_type: str
    declared_mime_type: str | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadCleanupObject:
    analysis_run_id: str
    bucket: str
    object_key: str


@dataclass(frozen=True, slots=True)
class UploadReservation:
    record: AnalysisRunRecord
    cleanup_objects: tuple[UploadCleanupObject, ...] = ()
    replayed: bool = False


class AnalysisRunRepository(Protocol):
    async def reserve_uploading(
        self,
        *,
        idempotency_key: str,
        owner_id: str,
        source: SourceObject,
    ) -> UploadReservation: ...

    async def finalize_queued(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
    ) -> AnalysisRunRecord: ...

    async def mark_upload_cleanup_pending(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
        error_code: str,
        error_message: str,
    ) -> UploadCleanupObject | None: ...

    async def complete_upload_cleanup(
        self,
        *,
        analysis_run_id: str,
    ) -> bool: ...

    async def get_for_owner(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
    ) -> AnalysisRunRecord | None: ...


class PrivateObjectStorage(Protocol):
    async def put(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> None: ...

    async def delete(self, *, bucket: str, object_key: str) -> None: ...
