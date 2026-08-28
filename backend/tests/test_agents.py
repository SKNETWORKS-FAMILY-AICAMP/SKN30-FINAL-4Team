import asyncio
from types import SimpleNamespace

from app.core.config import Settings
from app.schemas.cpl import (
    CPL_FIELDS,
    CplAxisCode,
    CplFieldCode,
    CplItem,
    CplOccurrence,
    CplResult,
    CplSourceRole,
    CplStatus,
)
from app.schemas.fit import FitInputFeedbackReason
from app.services.agents.orchestrator import reconcile_cpl_for_fit
from app.services.fit.fit_engine import inspect_fit_inputs
from app.services import analysis_pipeline


def _occurrence(
    field_code: CplFieldCode,
    axis_code: CplAxisCode,
    *,
    role: CplSourceRole | None = None,
    suffix: str = "0",
) -> CplOccurrence:
    return CplOccurrence(
        raw_text=f"{field_code.value}:{axis_code.value}",
        normalized_value={"text": axis_code.value},
        axis_code=axis_code,
        source_role=role,
        block_id=f"block:{field_code.value}:{suffix}",
        source_locator={"index": suffix},
        extraction_method="LLM",
    )


def _cpl(*occurrences: CplOccurrence) -> CplResult:
    by_field: dict[CplFieldCode, list[CplOccurrence]] = {}
    for occurrence in occurrences:
        by_field.setdefault(
            next(
                field_code
                for field_code in CPL_FIELDS
                if occurrence.block_id.startswith(f"block:{field_code.value}:")
            ),
            [],
        ).append(occurrence)
    return CplResult(
        ruleset_version="cpl-alpha-v0.2",
        items=[
            CplItem(
                field_code=field_code,
                status=(
                    CplStatus.NEEDS_CONFIRMATION
                    if by_field.get(field_code)
                    else CplStatus.MISSING
                ),
                occurrences=by_field.get(field_code, []),
            )
            for field_code in CPL_FIELDS
        ],
    )


def _mostly_complete_cpl() -> CplResult:
    return _cpl(
        _occurrence(
            CplFieldCode.PURPOSE_GOAL,
            CplAxisCode.PURPOSE_TARGET_CONDITION,
            suffix="purpose-target",
        ),
        _occurrence(
            CplFieldCode.TARGET_AND_CONDITIONS,
            CplAxisCode.TARGET_GROUP,
            role=CplSourceRole.TARGET,
        ),
        _occurrence(
            CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
            CplAxisCode.SUPPORT_ACTIVITY,
            role=CplSourceRole.SUPPORT_CONTENT,
        ),
        _occurrence(
            CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
            CplAxisCode.EFFECT_DIRECTION,
            role=CplSourceRole.EXPECTED_EFFECT,
        ),
        _occurrence(
            CplFieldCode.DELIVERY_SYSTEM,
            CplAxisCode.DELIVERY_ORG_NAME,
            role=CplSourceRole.DELIVERY_ORG,
        ),
        _occurrence(
            CplFieldCode.DELIVERY_SYSTEM,
            CplAxisCode.DELIVERY_PROCEDURE_STEP,
            role=CplSourceRole.DELIVERY_PROCEDURE,
            suffix="procedure",
        ),
    )


def test_fit_input_feedback_is_typed_and_excludes_fixed_or_valid_gaps() -> None:
    feedback = inspect_fit_inputs(_mostly_complete_cpl())

    assert feedback
    assert all(item.model_config.get("extra") == "forbid" for item in feedback)
    assert {
        (item.relation_id.value, item.side, item.field_code.value, item.reason_code)
        for item in feedback
    } >= {
        (
            "FIT-2",
            "left",
            "PURPOSE_GOAL",
            FitInputFeedbackReason.REQUIRED_AXIS_MISSING,
        ),
        (
            "FIT-3",
            "left",
            "PURPOSE_GOAL",
            FitInputFeedbackReason.REQUIRED_AXIS_MISSING,
        ),
    }
    assert not any(item.relation_id.value in {"FIT-4", "FIT-5", "FIT-7"} for item in feedback)


def test_fit_input_feedback_distinguishes_axis_from_source_role() -> None:
    cpl = _cpl(
        _occurrence(
            CplFieldCode.TARGET_AND_CONDITIONS,
            CplAxisCode.TARGET_GROUP,
            role=None,
        )
    )
    feedback = inspect_fit_inputs(cpl)
    target_feedback = next(
        item
        for item in feedback
        if item.relation_id.value == "FIT-1" and item.side == "right"
    )
    assert target_feedback.reason_code == FitInputFeedbackReason.SOURCE_ROLE_MISSING
    assert target_feedback.required_axis_codes == [CplAxisCode.TARGET_GROUP]
    assert target_feedback.required_source_roles == [CplSourceRole.TARGET]


def test_reconciliation_calls_one_targeted_recheck_and_records_resolution() -> None:
    original = _mostly_complete_cpl()
    calls: list[tuple[CplResult, set[CplFieldCode]]] = []

    async def recheck(
        current: CplResult,
        fields: set[CplFieldCode],
    ) -> CplResult:
        calls.append((current, fields))
        updated = current.model_copy(deep=True)
        purpose = next(
            item
            for item in updated.items
            if item.field_code == CplFieldCode.PURPOSE_GOAL
        )
        purpose.occurrences.append(
            _occurrence(
                CplFieldCode.PURPOSE_GOAL,
                CplAxisCode.PURPOSE_DIRECTION,
                suffix="direction",
            )
        )
        return updated

    reconciled = asyncio.run(
        reconcile_cpl_for_fit(original, recheck=recheck, max_rechecks=99)
    )

    assert len(calls) == 1
    assert calls[0][0] is original
    assert calls[0][1] == {CplFieldCode.PURPOSE_GOAL}
    assert reconciled.recheck_count == 1
    assert not reconciled.remaining_feedback
    assert "CPL/FIT reconciliation recheck 1 requested" in reconciled.result.warnings[-1]


def test_reconciliation_keeps_original_result_when_recheck_fails() -> None:
    original = _mostly_complete_cpl()

    async def recheck(*_args) -> CplResult:
        raise RuntimeError("temporary failure")

    reconciled = asyncio.run(
        reconcile_cpl_for_fit(original, recheck=recheck)
    )

    assert reconciled.recheck_count == 1
    assert reconciled.result.items == original.items
    assert reconciled.remaining_feedback == reconciled.initial_feedback
    assert reconciled.result.warnings[-1].startswith(
        "CPL/FIT reconciliation recheck unavailable"
    )


def test_analysis_pipeline_runs_targeted_cpl_recheck_before_fit(monkeypatch) -> None:
    original = _mostly_complete_cpl()
    complete_calls: list[set[CplFieldCode]] = []
    fit_inputs: list[CplResult] = []
    purpose_direction = _occurrence(
        CplFieldCode.PURPOSE_GOAL,
        CplAxisCode.PURPOSE_DIRECTION,
        suffix="direction",
    )

    async def parse(*_args) -> None:
        return None

    async def complete(_document, current, fields, *_args) -> CplResult:
        complete_calls.append(set(fields))
        if len(complete_calls) == 1:
            return current
        updated = current.model_copy(deep=True)
        purpose = next(
            item
            for item in updated.items
            if item.field_code == CplFieldCode.PURPOSE_GOAL
        )
        purpose.occurrences.append(purpose_direction)
        return updated

    async def fit(current, *_args) -> object:
        fit_inputs.append(current)
        return "fit-result"

    monkeypatch.setattr(analysis_pipeline, "run_case_parsing", parse)
    monkeypatch.setattr(analysis_pipeline, "_case_status", lambda *_args: "CHECKING")
    monkeypatch.setattr(analysis_pipeline, "_load_parsed_document", lambda *_args: object())
    monkeypatch.setattr(analysis_pipeline, "evaluate_cpl_rules", lambda *_args, **_kwargs: original)
    monkeypatch.setattr(analysis_pipeline, "_complete_semantic_review", complete)
    monkeypatch.setattr(
        analysis_pipeline,
        "run_cpl",
        lambda *_args, **_kwargs: SimpleNamespace(missing_check_run_id=10),
    )
    monkeypatch.setattr(analysis_pipeline, "request_reason_from_result", lambda *_args: None)
    monkeypatch.setattr(analysis_pipeline, "_run_fit", fit)

    async def no_retrieval(*_args) -> None:
        return None

    monkeypatch.setattr(analysis_pipeline, "_run_retrieval", no_retrieval)

    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret="test-secret-that-is-at-least-32-bytes",
        openai_api_key=None,
    )
    result = asyncio.run(
        analysis_pipeline.run_analysis_pipeline(
            object(),
            object(),
            object(),
            object(),
            settings,
            case_id=1,
            pdf_renderer=object(),
        )
    )

    assert result == "fit-result"
    assert len(complete_calls) == 2
    assert complete_calls[1] == {CplFieldCode.PURPOSE_GOAL}
    assert len(fit_inputs) == 1
    assert any(
        occurrence.axis_code == CplAxisCode.PURPOSE_DIRECTION
        for item in fit_inputs[0].items
        if item.field_code == CplFieldCode.PURPOSE_GOAL
        for occurrence in item.occurrences
    )
