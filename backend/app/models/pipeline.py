"""Pipeline kind, job, stage, and status models.

Statuses align with ops.processing_run / workspace.analysis_run CHECK
constraints. Stage names follow the Request/Existing lifecycles in the
system baseline without inventing extra job microservices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from app.models.outcomes import OutcomeCode


class PipelineKind(StrEnum):
    REQUEST = "request"
    EXISTING = "existing"


class JobType(StrEnum):
    DOCUMENT_PROCESSING = "request_document_processing"
    STRUCTURING = "request_structuring"
    ANALYSIS = "analysis"
    REPORT_GENERATION = "report_generation"
    EXISTING_INGEST = "existing_ingest"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLEANUP_PENDING = "cleanup_pending"


class StageName(StrEnum):
    FORMAT_VALIDATE = "format_validate"
    SOURCE_HASH = "source_hash"
    PERSIST_SOURCE = "persist_source"
    COMMON_IR = "common_ir"
    CANDIDATE_PACK = "candidate_pack"
    STRUCTURED_PROFILE = "structured_profile"
    CONTRACT_VALIDATE = "contract_validate"
    CPL = "cpl"
    FIT = "fit"
    RETRIEVE = "retrieve"
    SIM = "sim"
    BEN = "ben"
    DIF = "dif"
    EVIDENCE_VALIDATE = "evidence_validate"
    MATERIALIZE = "materialize"


class StageStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    NEEDS_REVIEW = "needs_review"


REQUEST_STAGES: tuple[StageName, ...] = (
    StageName.FORMAT_VALIDATE,
    StageName.SOURCE_HASH,
    StageName.PERSIST_SOURCE,
    StageName.COMMON_IR,
    StageName.CANDIDATE_PACK,
    StageName.STRUCTURED_PROFILE,
    StageName.CONTRACT_VALIDATE,
    StageName.CPL,
    StageName.FIT,
    StageName.RETRIEVE,
    StageName.SIM,
    StageName.BEN,
    StageName.DIF,
    StageName.EVIDENCE_VALIDATE,
    StageName.MATERIALIZE,
)

EXISTING_STAGES: tuple[StageName, ...] = (
    StageName.FORMAT_VALIDATE,
    StageName.SOURCE_HASH,
    StageName.PERSIST_SOURCE,
    StageName.COMMON_IR,
    StageName.CANDIDATE_PACK,
    StageName.STRUCTURED_PROFILE,
    StageName.CONTRACT_VALIDATE,
)

STAGE_JOB_TYPE: dict[StageName, JobType] = {
    StageName.FORMAT_VALIDATE: JobType.DOCUMENT_PROCESSING,
    StageName.SOURCE_HASH: JobType.DOCUMENT_PROCESSING,
    StageName.PERSIST_SOURCE: JobType.DOCUMENT_PROCESSING,
    StageName.COMMON_IR: JobType.DOCUMENT_PROCESSING,
    StageName.CANDIDATE_PACK: JobType.STRUCTURING,
    StageName.STRUCTURED_PROFILE: JobType.STRUCTURING,
    StageName.CONTRACT_VALIDATE: JobType.STRUCTURING,
    StageName.CPL: JobType.ANALYSIS,
    StageName.FIT: JobType.ANALYSIS,
    StageName.RETRIEVE: JobType.ANALYSIS,
    StageName.SIM: JobType.ANALYSIS,
    StageName.BEN: JobType.ANALYSIS,
    StageName.DIF: JobType.ANALYSIS,
    StageName.EVIDENCE_VALIDATE: JobType.ANALYSIS,
    StageName.MATERIALIZE: JobType.ANALYSIS,
}


def stages_for(kind: PipelineKind) -> tuple[StageName, ...]:
    if kind is PipelineKind.REQUEST:
        return REQUEST_STAGES
    if kind is PipelineKind.EXISTING:
        return EXISTING_STAGES
    raise ValueError(f"unknown pipeline kind: {kind}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class StageRecord:
    stage: StageName
    status: StageStatus
    job_type: JobType
    outcome: OutcomeCode | None = None
    error_code: str | None = None
    message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PipelineRunState:
    kind: PipelineKind
    status: RunStatus
    stages: tuple[StageRecord, ...]
    current_stage: StageName | None
    error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
