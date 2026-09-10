"""Sequential pipeline stage machine.

A missing parser/runtime marks COMMON_IR as UNAVAILABLE. Downstream
structuring/analysis stages are skipped rather than invented.
"""

from __future__ import annotations

from app.models.outcomes import ErrorCode, OutcomeCode
from app.models.pipeline import (
    STAGE_JOB_TYPE,
    PipelineKind,
    PipelineRunState,
    RunStatus,
    StageName,
    StageRecord,
    StageStatus,
    stages_for,
    utc_now,
)


def new_run(kind: PipelineKind) -> PipelineRunState:
    records = tuple(
        StageRecord(
            stage=stage,
            status=StageStatus.QUEUED,
            job_type=STAGE_JOB_TYPE[stage],
        )
        for stage in stages_for(kind)
    )
    return PipelineRunState(
        kind=kind,
        status=RunStatus.QUEUED,
        stages=records,
        current_stage=records[0].stage if records else None,
    )


def mark_stage(
    state: PipelineRunState,
    stage: StageName,
    status: StageStatus,
    *,
    outcome: OutcomeCode | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> PipelineRunState:
    names = [record.stage for record in state.stages]
    if stage not in names:
        raise ValueError(f"{stage} is not part of {state.kind} pipeline")
    index = names.index(stage)
    previous = state.stages[index]
    if previous.status not in {StageStatus.QUEUED, StageStatus.RUNNING} and previous.status != status:
        raise ValueError(f"{stage} is already {previous.status}")
    if index:
        prior = state.stages[index - 1]
        if prior.status is not StageStatus.SUCCEEDED:
            raise ValueError(
                f"{stage} cannot start because {prior.stage} is {prior.status}"
            )
    now = utc_now()
    started = previous.started_at or now
    finished = now if status is not StageStatus.RUNNING else None
    updated = StageRecord(
        stage=stage,
        status=status,
        job_type=previous.job_type,
        outcome=outcome,
        error_code=error_code,
        message=message,
        started_at=started,
        finished_at=finished,
    )
    stages = list(state.stages)
    stages[index] = updated

    if status in {StageStatus.FAILED, StageStatus.UNAVAILABLE}:
        for later in range(index + 1, len(stages)):
            queued = stages[later]
            if queued.status is StageStatus.QUEUED:
                stages[later] = StageRecord(
                    stage=queued.stage,
                    status=StageStatus.SKIPPED,
                    job_type=queued.job_type,
                    outcome=OutcomeCode.SKIPPED,
                    error_code=error_code or ErrorCode.PARSER_UNAVAILABLE,
                    message=f"skipped because {stage.value} is {status.value}",
                    started_at=now,
                    finished_at=now,
                )
        run_status = RunStatus.FAILED
        current = stage
    elif status is StageStatus.NEEDS_REVIEW:
        for later in range(index + 1, len(stages)):
            queued = stages[later]
            if queued.status is StageStatus.QUEUED:
                stages[later] = StageRecord(
                    stage=queued.stage,
                    status=StageStatus.SKIPPED,
                    job_type=queued.job_type,
                    outcome=OutcomeCode.SKIPPED,
                    error_code=error_code or ErrorCode.EVIDENCE_MISSING,
                    message=f"held for review after {stage.value}",
                    started_at=now,
                    finished_at=now,
                )
        run_status = RunStatus.SUCCEEDED
        current = stage
    elif status is StageStatus.SUCCEEDED:
        nxt = stages[index + 1].stage if index + 1 < len(stages) else None
        run_status = RunStatus.SUCCEEDED if nxt is None else RunStatus.RUNNING
        current = nxt or stage
    else:
        run_status = RunStatus.RUNNING
        current = stage

    return PipelineRunState(
        kind=state.kind,
        status=run_status,
        stages=tuple(stages),
        current_stage=current,
        error_code=error_code if run_status is RunStatus.FAILED else None,
        created_at=state.created_at,
    )


def stage_map(state: PipelineRunState) -> dict[StageName, StageRecord]:
    return {record.stage: record for record in state.stages}
