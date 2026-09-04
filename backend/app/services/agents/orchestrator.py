"""Deterministic coordination between the CPL and FIT analyzers.

The analyzers remain independent domain services.  This module owns the small
amount of workflow state needed when FIT discovers that a CPL occurrence is
not consumable for one of its relations.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.schemas.cpl import CplFieldCode, CplResult
from app.schemas.fit import FitInputFeedback
from app.services.fit.fit_engine import inspect_fit_inputs


logger = logging.getLogger(__name__)

# A second open-ended debate between agents would make results and cost
# unpredictable.  One targeted pass is the alpha contract.
MAX_CPL_FIT_RECHECKS = 1


@dataclass(frozen=True, slots=True)
class CplFitReconciliation:
    """Outcome of the optional, bounded CPL→FIT input reconciliation."""

    result: CplResult
    initial_feedback: tuple[FitInputFeedback, ...]
    remaining_feedback: tuple[FitInputFeedback, ...]
    requested_fields: frozenset[CplFieldCode]
    recheck_count: int


RecheckCpl = Callable[
    [CplResult, tuple[FitInputFeedback, ...]],
    Awaitable[CplResult],
]


async def reconcile_cpl_for_fit(
    cpl_result: CplResult,
    *,
    recheck: RecheckCpl | None,
    max_rechecks: int = MAX_CPL_FIT_RECHECKS,
) -> CplFitReconciliation:
    """Let FIT request one targeted CPL evidence pass, if needed.

    ``recheck`` receives the current CPL snapshot and FIT's typed feedback.
    Keeping the required axes and source roles prevents a deterministic second
    pass from merely repeating the same broad CPL request.  It must return
    another complete ``CplResult``; CPL remains the source of truth and FIT
    never mutates that result directly.  Failures in this optional pass retain
    the first CPL result so normal LLM failure semantics still apply.
    """

    initial_feedback = tuple(inspect_fit_inputs(cpl_result))
    if not initial_feedback or recheck is None or max_rechecks <= 0:
        return CplFitReconciliation(
            result=cpl_result,
            initial_feedback=initial_feedback,
            remaining_feedback=initial_feedback,
            requested_fields=frozenset(),
            recheck_count=0,
        )

    # Keep the public function defensive even if a caller passes a larger
    # budget.  The alpha workflow is always at most one additional pass.
    requested_fields = frozenset(
        feedback.field_code for feedback in initial_feedback
    )
    current = cpl_result
    recheck_count = 0
    try:
        current = await recheck(current, initial_feedback)
        if not isinstance(current, CplResult):
            raise TypeError("CPL recheck must return CplResult")
        recheck_count = 1
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # The primary CPL result is already usable.  Do not turn an optional
        # feedback pass into a case-wide failure, but keep the event visible.
        logger.warning(
            "CPL/FIT reconciliation unavailable error=%s",
            type(error).__name__,
        )
        recheck_count = 1
        current = _append_warning(
            cpl_result,
            "CPL/FIT reconciliation recheck unavailable; original CPL evidence retained",
        )
        remaining_feedback = initial_feedback
        return CplFitReconciliation(
            result=current,
            initial_feedback=initial_feedback,
            remaining_feedback=remaining_feedback,
            requested_fields=requested_fields,
            recheck_count=recheck_count,
        )

    remaining_feedback = tuple(inspect_fit_inputs(current))
    requested = ",".join(
        sorted(field_code.value for field_code in requested_fields)
    )
    if remaining_feedback:
        unresolved = ",".join(
            sorted(
                {
                    f"{feedback.relation_id.value}:{feedback.field_code.value}"
                    for feedback in remaining_feedback
                }
            )
        )
        outcome = f"unresolved={unresolved}"
    else:
        outcome = "resolved"
    current = _append_warning(
        current,
        f"CPL/FIT reconciliation recheck 1 requested for {requested} ({outcome})",
    )
    return CplFitReconciliation(
        result=current,
        initial_feedback=initial_feedback,
        remaining_feedback=remaining_feedback,
        requested_fields=requested_fields,
        recheck_count=recheck_count,
    )


def _append_warning(result: CplResult, warning: str) -> CplResult:
    if warning in result.warnings:
        return result
    snapshot = result.model_dump(mode="python")
    snapshot["warnings"] = [*result.warnings, warning]
    return CplResult.model_validate(snapshot)
