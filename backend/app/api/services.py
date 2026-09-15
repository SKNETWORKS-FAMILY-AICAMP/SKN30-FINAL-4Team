"""Ports and safe offline implementation used by the API scaffold.

Supabase and the real pipeline should implement ``CaseRepository`` and be
injected at application startup.  Keeping the in-memory version here makes
local imports, OpenAPI generation, and frontend integration possible without
credentials or external services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from .models import (
    AnalysisResult,
    EvidenceList,
    ExistingIngestionCreated,
    ProcessingState,
    ProcessingStatus,
    RequestCaseCreated,
    RequestCaseSummary,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CaseRepository(Protocol):
    async def create_request(self, *, owner_id: str, filename: str, content_type: str | None, content: bytes) -> RequestCaseCreated: ...
    async def list_requests(self, *, owner_id: str) -> list[RequestCaseSummary]: ...
    async def get_request(self, *, owner_id: str, case_id: str) -> RequestCaseSummary | None: ...
    async def get_status(self, *, owner_id: str, case_id: str) -> ProcessingState | None: ...
    async def get_result(self, *, owner_id: str, case_id: str) -> AnalysisResult | None: ...
    async def get_evidence(self, *, owner_id: str, case_id: str) -> EvidenceList | None: ...
    async def create_existing_ingestion(self, *, owner_id: str, filename: str, content_type: str | None, content: bytes, notice_id: str, source_profile_id: str) -> ExistingIngestionCreated: ...
    async def list_existing_ingestions(self) -> list[ExistingIngestionCreated]: ...
    async def get_existing_status(self, ingestion_id: str) -> ProcessingState | None: ...


class AnalysisDispatcher(Protocol):
    async def enqueue_request(self, case_id: str) -> None: ...
    async def enqueue_existing(self, ingestion_id: str) -> None: ...


class NoopDispatcher:
    """Offline dispatcher. It intentionally never calls an LLM or OCR engine."""

    async def enqueue_request(self, case_id: str) -> None:
        return None

    async def enqueue_existing(self, ingestion_id: str) -> None:
        return None


@dataclass
class _StoredCase:
    summary: RequestCaseCreated
    owner_id: str
    payload: bytes = field(repr=False)


@dataclass
class _StoredExisting:
    summary: ExistingIngestionCreated
    owner_id: str
    payload: bytes = field(repr=False)


class InMemoryCaseRepository:
    """Development-only repository; replace with a Supabase adapter in runtime."""

    def __init__(self) -> None:
        self._requests: dict[str, _StoredCase] = {}
        self._existing: dict[str, _StoredExisting] = {}

    async def create_request(self, *, owner_id: str, filename: str, content_type: str | None, content: bytes) -> RequestCaseCreated:
        now = utcnow()
        record = RequestCaseCreated(id=str(uuid4()), filename=filename, content_type=content_type, status=ProcessingStatus.queued, created_at=now, updated_at=now)
        self._requests[record.id] = _StoredCase(summary=record, owner_id=owner_id, payload=content)
        return record

    async def list_requests(self, *, owner_id: str) -> list[RequestCaseSummary]:
        return sorted((item.summary for item in self._requests.values() if item.owner_id == owner_id), key=lambda item: item.created_at, reverse=True)

    async def get_request(self, *, owner_id: str, case_id: str) -> RequestCaseSummary | None:
        item = self._requests.get(case_id)
        return item.summary if item and item.owner_id == owner_id else None

    async def get_status(self, *, owner_id: str, case_id: str) -> ProcessingState | None:
        item = self._requests.get(case_id)
        if not item or item.owner_id != owner_id:
            return None
        return ProcessingState(id=case_id, status=item.summary.status, progress=0, stage="queued", updated_at=item.summary.updated_at)

    async def get_result(self, *, owner_id: str, case_id: str) -> AnalysisResult | None:
        item = self._requests.get(case_id)
        if not item or item.owner_id != owner_id:
            return None
        return AnalysisResult(case_id=case_id, status=item.summary.status)

    async def get_evidence(self, *, owner_id: str, case_id: str) -> EvidenceList | None:
        item = self._requests.get(case_id)
        return EvidenceList(case_id=case_id) if item and item.owner_id == owner_id else None

    async def create_existing_ingestion(self, *, owner_id: str, filename: str, content_type: str | None, content: bytes, notice_id: str, source_profile_id: str) -> ExistingIngestionCreated:
        now = utcnow()
        item = ExistingIngestionCreated(ingestion_id=str(uuid4()), filename=filename, notice_id=notice_id, source_profile_id=source_profile_id, status=ProcessingStatus.queued, created_at=now)
        self._existing[item.ingestion_id] = _StoredExisting(summary=item, owner_id=owner_id, payload=content)
        return item

    async def list_existing_ingestions(self) -> list[ExistingIngestionCreated]:
        return sorted((item.summary for item in self._existing.values()), key=lambda item: item.created_at, reverse=True)

    async def get_existing_status(self, ingestion_id: str) -> ProcessingState | None:
        item = self._existing.get(ingestion_id)
        if not item:
            return None
        return ProcessingState(id=ingestion_id, status=item.summary.status, progress=0, stage="queued", updated_at=item.summary.created_at)
