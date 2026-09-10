"""Ports shared by the analysis-run application service and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class AnalysisRunError(RuntimeError):
    """Base class for safe, expected analysis-run failures."""


class ActiveAnalysisRunExists(AnalysisRunError):
    """The owner already has an upload or analysis in progress."""


class AnalysisRunPersistenceUnavailable(AnalysisRunError):
    """The internal database cannot currently serve the request."""


class ObjectStorageUnavailable(AnalysisRunError):
    """The private object store cannot currently serve the request."""


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


class AnalysisRunRepository(Protocol):
    async def create_queued(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
    ) -> AnalysisRunRecord: ...

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
