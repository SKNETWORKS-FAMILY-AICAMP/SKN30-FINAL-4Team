"""Pydantic contracts for the HTTP API.

These contracts intentionally avoid exposing Supabase rows.  The persistence
adapter can evolve independently as long as this boundary remains stable.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ProcessingStatus(str, Enum):
    queued = "QUEUED"
    parsing = "PARSING"
    structuring = "STRUCTURING"
    comparing = "COMPARING"
    completed = "COMPLETED"
    completed_with_warnings = "COMPLETED_WITH_WARNINGS"
    needs_review = "NEEDS_REVIEW"
    failed = "FAILED"


class RequestCaseSummary(BaseModel):
    id: str
    filename: str
    status: ProcessingStatus
    created_at: datetime
    updated_at: datetime


class RequestCaseCreated(RequestCaseSummary):
    content_type: str | None = None


class ProcessingState(BaseModel):
    id: str
    status: ProcessingStatus
    progress: int = Field(ge=0, le=100)
    stage: str
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    updated_at: datetime


class AnalysisResult(BaseModel):
    case_id: str
    status: ProcessingStatus
    request_profile: dict[str, Any] | None = None
    comparison: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime | None = None


class EvidenceItem(BaseModel):
    evidence_id: str
    source_kind: str
    source_document_id: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    excerpt: str | None = None


class EvidenceList(BaseModel):
    case_id: str
    items: list[EvidenceItem] = Field(default_factory=list)


class ExistingIngestionCreated(BaseModel):
    ingestion_id: str
    filename: str
    notice_id: str
    source_profile_id: str
    status: ProcessingStatus
    created_at: datetime


class ExistingIngestionSummary(ExistingIngestionCreated):
    updated_at: datetime
