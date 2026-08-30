import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.schemas.cpl import (
    CplAxisCode,
    CplFieldCode,
    CplItem,
    CplOccurrence,
    CplResult,
    CplSourceRole,
    CplStatus,
)
from app.services.cpl.logic_validator import CPL_INCOMPLETE_REASON_CODES
from app.schemas.fit import (
    FIT_RELATIONS,
    FitRelationId,
    FitRelationResult,
    FitResult,
    FitInputFeedback,
    FitInputFeedbackReason,
    FitScoreSummary,
    FitScoringPolicy,
    FitSemanticRelation,
    FitSemanticResponse,
    FitStatus,
)


FitLlmFailureCode = Literal[
    "LLM_UNAVAILABLE",
    "LLM_TIMEOUT",
    "LLM_INVALID_RESPONSE",
]


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EvidenceSelector:
    field_code: CplFieldCode
    axes: frozenset[CplAxisCode]
    roles: frozenset[CplSourceRole | None] | None = None


_RELATION_SELECTORS: dict[
    FitRelationId,
    tuple[_EvidenceSelector, _EvidenceSelector],
] = {
    # 사업목적에는 구역 role 이 없다. 좌우가 서로 다른 필드라 role 로 갈라낼
    # 것도 없으므로 축과 필드만으로 근거를 고른다.
    FitRelationId.FIT_1: (
        _EvidenceSelector(
            CplFieldCode.PURPOSE_GOAL,
            frozenset({CplAxisCode.PURPOSE_TARGET_CONDITION}),
        ),
        _EvidenceSelector(
            CplFieldCode.TARGET_AND_CONDITIONS,
            frozenset({CplAxisCode.TARGET_GROUP}),
            frozenset({CplSourceRole.TARGET}),
        ),
    ),
    FitRelationId.FIT_2: (
        _EvidenceSelector(
            CplFieldCode.PURPOSE_GOAL,
            frozenset({CplAxisCode.PURPOSE_DIRECTION}),
        ),
        _EvidenceSelector(
            CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
            frozenset(
                {
                    CplAxisCode.SUPPORT_ACTIVITY,
                    CplAxisCode.SUPPORT_INSTRUMENT,
                }
            ),
            frozenset({CplSourceRole.SUPPORT_CONTENT}),
        ),
    ),
    FitRelationId.FIT_3: (
        _EvidenceSelector(
            CplFieldCode.PURPOSE_GOAL,
            frozenset({CplAxisCode.PURPOSE_DIRECTION}),
        ),
        _EvidenceSelector(
            CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
            frozenset(
                {
                    CplAxisCode.EFFECT_SUBJECT,
                    CplAxisCode.EFFECT_CONTENT,
                    CplAxisCode.EFFECT_DIRECTION,
                    CplAxisCode.KPI_NAME,
                    CplAxisCode.KPI_TARGET_VALUE,
                    CplAxisCode.KPI_UNIT,
                    CplAxisCode.KPI_BASE_YEAR,
                    CplAxisCode.KPI_FORMULA,
                }
            ),
            frozenset(
                {
                    CplSourceRole.EXPECTED_EFFECT,
                    CplSourceRole.PERFORMANCE_INDICATOR,
                }
            ),
        ),
    ),
    FitRelationId.FIT_5: (
        _EvidenceSelector(
            CplFieldCode.TARGET_AND_CONDITIONS,
            frozenset({CplAxisCode.TARGET_GROUP}),
            frozenset({CplSourceRole.TARGET}),
        ),
        _EvidenceSelector(
            CplFieldCode.TARGET_AND_CONDITIONS,
            frozenset(
                axis
                for axis in CplAxisCode
                if axis.value.startswith("COND_")
            ),
            frozenset({CplSourceRole.CONDITION}),
        ),
    ),
    FitRelationId.FIT_6: (
        _EvidenceSelector(
            CplFieldCode.DELIVERY_SYSTEM,
            frozenset({CplAxisCode.DELIVERY_ORG_NAME}),
            frozenset({CplSourceRole.DELIVERY_ORG}),
        ),
        _EvidenceSelector(
            CplFieldCode.DELIVERY_SYSTEM,
            frozenset(
                {
                    CplAxisCode.DELIVERY_PROCEDURE_STEP,
                    CplAxisCode.DELIVERY_STEP_ROLE,
                }
            ),
            frozenset({CplSourceRole.DELIVERY_PROCEDURE}),
        ),
    ),
}

_FIT_7_AXES = frozenset(
    {
        CplAxisCode.PER_COMPANY_LIMIT,
        CplAxisCode.COMPANY_COUNT,
        CplAxisCode.SUBSIDY_RATE,
        CplAxisCode.SELF_BURDEN_RATE,
        CplAxisCode.TOTAL_SCALE,
    }
)
_FIT_7_VALUE_FIELDS: dict[CplAxisCode, tuple[str, str]] = {
    CplAxisCode.PER_COMPANY_LIMIT: ("amount_won", "KRW"),
    CplAxisCode.COMPANY_COUNT: ("count", "COMPANY"),
    CplAxisCode.SUBSIDY_RATE: ("ratio", "PERCENT"),
    CplAxisCode.SELF_BURDEN_RATE: ("ratio", "PERCENT"),
    CplAxisCode.TOTAL_SCALE: ("amount_won", "KRW"),
}


@dataclass(frozen=True)
class _RelationInput:
    left: dict[str, CplOccurrence]
    right: dict[str, CplOccurrence]


def load_fit_scoring(path: Path) -> FitScoringPolicy:
    return FitScoringPolicy.model_validate_json(path.read_text(encoding="utf-8"))


def load_fit_prompt(path: Path) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("FIT prompt must not be blank")
    return prompt


def inspect_fit_inputs(cpl_result: CplResult) -> list[FitInputFeedback]:
    """Return deterministic FIT input gaps that CPL may be able to repair.

    This preflight deliberately covers only semantic FIT relations. FIT-4 is
    contractually unavailable and FIT-7 is rule-owned quantitative comparison.
    FIT-5 requests a recheck only when CPL itself is incomplete; a reliable
    absence of conditions remains ``NO_CONDITIONS_SPECIFIED``.
    """

    items = {item.field_code: item for item in cpl_result.items}
    feedback: list[FitInputFeedback] = []
    for relation_id, (left_selector, right_selector) in _RELATION_SELECTORS.items():
        for side, selector in (("left", left_selector), ("right", right_selector)):
            item = items[selector.field_code]
            axis_occurrences = [
                occurrence
                for occurrence in item.occurrences
                if occurrence.axis_code in selector.axes
            ]
            present_axes = {
                occurrence.axis_code
                for occurrence in axis_occurrences
                if occurrence.axis_code is not None
            }
            missing_axes = selector.axes - present_axes

            # source_role is assigned from the cited document range by Rule.
            # It is not an LLM-repairable semantic axis, so a role-only gap
            # remains unassessable without creating another CPL call.
            if selector.roles is not None and axis_occurrences and not any(
                occurrence.source_role in selector.roles
                for occurrence in axis_occurrences
            ):
                continue
            if not missing_axes:
                continue
            if (
                relation_id == FitRelationId.FIT_5
                and side == "right"
                and not _conditions_unresolved(item)
            ):
                continue
            # A missing axis after a normal, successful extraction is simply
            # unassessable. Recheck only an actual extraction failure or an
            # axis-less piece of source evidence that still needs classifying.
            if not _cpl_input_incomplete(item):
                continue
            feedback.append(
                FitInputFeedback(
                    relation_id=relation_id,
                    side=side,
                    field_code=selector.field_code,
                    reason_code=FitInputFeedbackReason.REQUIRED_AXIS_MISSING,
                    required_axis_codes=_ordered_axes(missing_axes),
                    required_source_roles=_ordered_roles(selector.roles),
                )
            )
    return feedback


def _cpl_input_incomplete(item: CplItem) -> bool:
    # 원인이 무엇이든 CPL 이 비교 가능한 형태로 확정하지 못한 것은 같다.
    # 원인별 구분은 기록으로 남기고 여기서는 미완료 여부만 본다.
    if (
        item.status == CplStatus.PARSE_FAILED
        or item.reason_code in CPL_INCOMPLETE_REASON_CODES
    ):
        return True
    return any(
        occurrence.axis_code is None
        and not _is_explicit_absence_occurrence(occurrence)
        for occurrence in item.occurrences
    )


def _is_explicit_absence_occurrence(occurrence: CplOccurrence) -> bool:
    normalized = occurrence.normalized_value
    return (
        isinstance(normalized, dict)
        and normalized.get("explicit_absence") is True
    )


def _conditions_unresolved(item: CplItem) -> bool:
    """조건 축 부재가 '조건 부재'인지 '구조화 실패'인지 가른다.

    조건 근거 자체가 있는데 축이 없으면 문서에 조건이 없는 것이 아니라 CPL 이
    비교 가능한 형태로 확정하지 못한 것이다.
    """

    condition_occurrences = [
        occurrence
        for occurrence in item.occurrences
        if occurrence.source_role == CplSourceRole.CONDITION
    ]
    if condition_occurrences:
        return any(
            not _is_explicit_absence_occurrence(occurrence)
            for occurrence in condition_occurrences
        )
    if item.status == CplStatus.NEEDS_CONFIRMATION and item.reason_code:
        return True
    return _cpl_input_incomplete(item)


def _ordered_axes(
    axes: frozenset[CplAxisCode] | set[CplAxisCode | None],
) -> list[CplAxisCode]:
    return sorted(
        (axis for axis in axes if axis is not None),
        key=lambda axis: axis.value,
    )


def _ordered_roles(
    roles: frozenset[CplSourceRole | None] | None,
) -> list[CplSourceRole | None]:
    if roles is None:
        return []
    return sorted(
        roles,
        key=lambda role: "" if role is None else role.value,
    )


async def analyze_fit(
    cpl_result: CplResult,
    llm_client: LLMClient | None,
    *,
    scoring: FitScoringPolicy,
    prompt: str,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
) -> FitResult:
    if not prompt.strip() or not ruleset_version.strip() or not prompt_version.strip():
        raise ValueError("FIT prompt and versions must not be blank")

    items = {item.field_code: item for item in cpl_result.items}
    inputs = _relation_inputs(items)
    results: dict[FitRelationId, FitRelationResult] = {
        FitRelationId.FIT_4: _relation_result(
            FitRelationId.FIT_4,
            FitStatus.INSUFFICIENT,
            "사업 계층별 비교 기준이 아직 제공되지 않았습니다.",
            [],
            [],
            "HIERARCHY_COMPARISON_NOT_AVAILABLE",
            scoring,
            ruleset_version,
            prompt_version,
        )
    }
    fit_7, warnings = _fit_7_result(
        items[CplFieldCode.SUPPORT_CONTENT_AND_SCALE],
        scoring,
        ruleset_version,
        prompt_version,
    )
    results[FitRelationId.FIT_7] = fit_7
    pending: dict[FitRelationId, _RelationInput] = {}
    for relation_id, relation_input in inputs.items():
        if (
            relation_id == FitRelationId.FIT_5
            and relation_input.left
            and not relation_input.right
            and not _conditions_unresolved(items[CplFieldCode.TARGET_AND_CONDITIONS])
        ):
            results[relation_id] = _relation_result(
                relation_id,
                FitStatus.INSUFFICIENT,
                "별도로 명시된 지원조건이 없습니다.",
                list(relation_input.left.values()),
                [],
                "NO_CONDITIONS_SPECIFIED",
                scoring,
                ruleset_version,
                prompt_version,
            )
        elif not relation_input.left or not relation_input.right:
            results[relation_id] = _relation_result(
                relation_id,
                FitStatus.INSUFFICIENT,
                "비교에 필요한 요청서 정보가 부족합니다.",
                list(relation_input.left.values()),
                list(relation_input.right.values()),
                "COMPARISON_EVIDENCE_MISSING",
                scoring,
                ruleset_version,
                prompt_version,
            )
        else:
            pending[relation_id] = relation_input

    if pending:
        semantic_results, semantic_warnings = await _semantic_results(
            pending,
            llm_client,
            scoring,
            prompt,
            ruleset_version,
            prompt_version,
            model_profile,
        )
        results.update(semantic_results)
        warnings.extend(semantic_warnings)

    ordered = [results[relation_id] for relation_id in FIT_RELATIONS]
    score = _score_summary(ordered, scoring)
    return FitResult(
        relations=ordered,
        score=score,
        warnings=warnings,
        ruleset_version=ruleset_version,
        prompt_version=prompt_version,
        scoring_version=scoring.version,
        model_profile=model_profile,
    )


async def _semantic_results(
    pending: dict[FitRelationId, _RelationInput],
    llm_client: LLMClient | None,
    scoring: FitScoringPolicy,
    prompt: str,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
) -> tuple[dict[FitRelationId, FitRelationResult], list[str]]:
    if llm_client is None:
        return _failed_semantic_results(
            pending,
            "LLM_UNAVAILABLE",
            scoring,
            ruleset_version,
            prompt_version,
        )
    try:
        response = await llm_client.generate_structured(
            task_name="fit_internal_consistency",
            messages=[
                Message(role="developer", content=prompt),
                Message(role="user", content=_semantic_input_json(pending)),
            ],
            response_schema=FitSemanticResponse,
            model_profile=model_profile,
        )
        if not isinstance(response, FitSemanticResponse):
            raise LLMInvalidResponseError("Unexpected structured response type")
        return _ground_semantic_response(
            response,
            pending,
            scoring,
            ruleset_version,
            prompt_version,
        )
    except LLMTimeoutError as error:
        failure_code: FitLlmFailureCode = "LLM_TIMEOUT"
        failure_error: Exception = error
    except LLMUnavailableError as error:
        failure_code = "LLM_UNAVAILABLE"
        failure_error = error
    except (LLMInvalidResponseError, ValidationError, ValueError) as error:
        failure_code = "LLM_INVALID_RESPONSE"
        failure_error = error
    detail = None
    if isinstance(failure_error, ValidationError):
        detail = ",".join(
            f"{'.'.join(map(str, item['loc']))}:{item['type']}"
            for item in failure_error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        )
    elif type(failure_error) is ValueError:
        detail = str(failure_error)
    logger.warning(
        "FIT semantic batch failed failure=%s error=%s detail=%s",
        failure_code,
        type(failure_error).__name__,
        detail,
    )
    return _failed_semantic_results(
        pending,
        failure_code,
        scoring,
        ruleset_version,
        prompt_version,
    )


def _relation_inputs(
    items: dict[CplFieldCode, CplItem],
) -> dict[FitRelationId, _RelationInput]:
    return {
        relation_id: _RelationInput(
            left=_evidence(items, left_selector),
            right=_evidence(items, right_selector),
        )
        for relation_id, (
            left_selector,
            right_selector,
        ) in _RELATION_SELECTORS.items()
    }


def _evidence(
    items: dict[CplFieldCode, CplItem],
    selector: _EvidenceSelector,
) -> dict[str, CplOccurrence]:
    return {
        f"{selector.field_code.value}:{index}": occurrence
        for index, occurrence in enumerate(items[selector.field_code].occurrences)
        if occurrence.axis_code in selector.axes
        and (
            selector.roles is None
            or occurrence.source_role in selector.roles
        )
    }


def _semantic_input_json(pending: dict[FitRelationId, _RelationInput]) -> str:
    return json.dumps(
        {
            "requested_relations": [
                {
                    "relation_id": relation_id.value,
                    "left_evidence": _prompt_evidence(relation_input.left),
                    "right_evidence": _prompt_evidence(relation_input.right),
                }
                for relation_id, relation_input in pending.items()
            ]
        },
        ensure_ascii=False,
    )


def _prompt_evidence(evidence: dict[str, CplOccurrence]) -> list[dict]:
    return [
        {
            "ref": reference,
            "raw_text": occurrence.raw_text,
            "normalized_value": occurrence.normalized_value,
            "axis_code": occurrence.axis_code,
            "source_role": occurrence.source_role,
        }
        for reference, occurrence in evidence.items()
    ]


def _fit_7_result(
    item: CplItem,
    scoring: FitScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
) -> tuple[FitRelationResult, list[str]]:
    left = [
        occurrence
        for occurrence in item.occurrences
        if occurrence.axis_code in _FIT_7_AXES
        and occurrence.source_role == CplSourceRole.SUPPORT_CONTENT
    ]
    right = [
        occurrence
        for occurrence in item.occurrences
        if occurrence.axis_code in _FIT_7_AXES
        and occurrence.source_role == CplSourceRole.SUPPORT_SCALE
    ]
    left_values, left_invalid = _quantitative_values(left)
    right_values, right_invalid = _quantitative_values(right)
    invalid_count = left_invalid + right_invalid
    warnings = (
        [f"FIT-7 excluded {invalid_count} invalid quantitative occurrence(s)"]
        if invalid_count
        else []
    )

    if not left_values and not right_values:
        reason_code = (
            "COMPARISON_VALUE_INVALID"
            if invalid_count
            else "COMPARISON_EVIDENCE_MISSING"
        )
        return (
            _relation_result(
                FitRelationId.FIT_7,
                FitStatus.INSUFFICIENT,
                "비교 가능한 지원내용·지원규모 정량정보가 없습니다.",
                left,
                right,
                reason_code,
                scoring,
                ruleset_version,
                prompt_version,
            ),
            warnings,
        )

    if invalid_count and (not left_values or not right_values):
        return (
            _relation_result(
                FitRelationId.FIT_7,
                FitStatus.INSUFFICIENT,
                "정량정보의 단위 또는 값 정규화에 실패해 비교할 수 없습니다.",
                left,
                right,
                "COMPARISON_VALUE_INVALID",
                scoring,
                ruleset_version,
                prompt_version,
            ),
            warnings,
        )

    shared_axes = set(left_values) & set(right_values)
    if any(left_values[axis] != right_values[axis] for axis in shared_axes):
        status = FitStatus.NEEDS_REVIEW
        reason_code = "NUMERIC_MISMATCH"
        summary = "동일 지원 개념의 정량값이 문서 안에서 서로 다릅니다."
    elif set(left_values) != set(right_values):
        status = FitStatus.INSUFFICIENT
        reason_code = "SINGLE_SIDED_NO_CONFLICT"
        summary = "한쪽에만 있는 정량정보에서 명시적 충돌은 확인되지 않았습니다."
    else:
        status = FitStatus.FIT
        reason_code = None
        summary = "지원내용과 지원규모의 정량정보가 서로 일치합니다."
    return (
        _relation_result(
            FitRelationId.FIT_7,
            status,
            summary,
            left,
            right,
            reason_code,
            scoring,
            ruleset_version,
            prompt_version,
        ),
        warnings,
    )


def _quantitative_values(
    occurrences: list[CplOccurrence],
) -> tuple[dict[CplAxisCode, set[Decimal]], int]:
    values: dict[CplAxisCode, set[Decimal]] = {}
    invalid_count = 0
    for occurrence in occurrences:
        axis = occurrence.axis_code
        if axis not in _FIT_7_VALUE_FIELDS:
            continue
        value_key, expected_unit = _FIT_7_VALUE_FIELDS[axis]
        normalized = occurrence.normalized_value
        if not isinstance(normalized, dict) or normalized.get("unit") != expected_unit:
            invalid_count += 1
            continue
        value = normalized.get(value_key)
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            invalid_count += 1
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            invalid_count += 1
            continue
        if not number.is_finite():
            invalid_count += 1
            continue
        values.setdefault(axis, set()).add(number)
    return values, invalid_count


def _ground_semantic_response(
    response: FitSemanticResponse,
    pending: dict[FitRelationId, _RelationInput],
    scoring: FitScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
) -> tuple[dict[FitRelationId, FitRelationResult], list[str]]:
    response_by_id: dict[FitRelationId, list[FitSemanticRelation]] = {}
    for relation in response.relations:
        response_by_id.setdefault(relation.relation_id, []).append(relation)

    grounded: dict[FitRelationId, FitRelationResult] = {}
    warnings: list[str] = []

    unexpected = sorted(
        set(response_by_id) - set(pending),
        key=lambda relation_id: relation_id.value,
    )
    if unexpected:
        unexpected_ids = ",".join(relation_id.value for relation_id in unexpected)
        warnings.append(f"FIT semantic relation ignored: {unexpected_ids}")
        logger.warning(
            "FIT semantic response contained unexpected relations=%s",
            unexpected_ids,
        )

    def mark_invalid(
        relation_id: FitRelationId,
        source: _RelationInput,
        reason: str,
    ) -> None:
        logger.warning(
            "FIT semantic relation incomplete relation=%s reason=%s",
            relation_id.value,
            reason,
        )
        grounded[relation_id] = _relation_result(
            relation_id,
            FitStatus.INSUFFICIENT,
            "해당 관계의 의미 분석 응답을 검증하지 못했습니다.",
            list(source.left.values()),
            list(source.right.values()),
            "LLM_INVALID_RESPONSE",
            scoring,
            ruleset_version,
            prompt_version,
        )
        warnings.append(
            "FIT semantic relation incomplete: "
            f"{relation_id.value}:LLM_INVALID_RESPONSE"
        )

    for relation_id, source in pending.items():
        candidates = response_by_id.get(relation_id, [])
        if not candidates:
            mark_invalid(relation_id, source, "MISSING")
            continue
        if len(candidates) != 1:
            mark_invalid(relation_id, source, "DUPLICATED")
            continue
        relation = candidates[0]
        try:
            if not relation.summary.strip():
                raise ValueError("FIT summary must not be blank")
            if len(relation.left_evidence_refs) != len(
                set(relation.left_evidence_refs)
            ):
                raise ValueError("FIT response contains duplicate left evidence")
            if len(relation.right_evidence_refs) != len(
                set(relation.right_evidence_refs)
            ):
                raise ValueError("FIT response contains duplicate right evidence")
            if not set(relation.left_evidence_refs).issubset(source.left):
                raise ValueError("FIT response contains invalid left evidence")
            if not set(relation.right_evidence_refs).issubset(source.right):
                raise ValueError("FIT response contains invalid right evidence")
            if relation.status != FitStatus.INSUFFICIENT and (
                not relation.left_evidence_refs or not relation.right_evidence_refs
            ):
                raise ValueError(
                    "Assessable FIT response requires evidence on both sides"
                )
            if relation.status != FitStatus.FIT and not relation.reason_code:
                raise ValueError("Non-FIT response requires a reason")

            grounded[relation.relation_id] = _relation_result(
                relation.relation_id,
                relation.status,
                relation.summary,
                [source.left[reference] for reference in relation.left_evidence_refs],
                [source.right[reference] for reference in relation.right_evidence_refs],
                relation.reason_code,
                scoring,
                ruleset_version,
                prompt_version,
            )
        except ValueError as error:
            logger.warning(
                "FIT relation grounding failed relation=%s error=%s",
                relation.relation_id.value,
                error,
            )
            mark_invalid(relation.relation_id, source, "GROUNDING_FAILED")
    return grounded, warnings


def _failed_semantic_results(
    pending: dict[FitRelationId, _RelationInput],
    failure_code: FitLlmFailureCode,
    scoring: FitScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
) -> tuple[dict[FitRelationId, FitRelationResult], list[str]]:
    results = {
        relation_id: _relation_result(
            relation_id,
            FitStatus.INSUFFICIENT,
            "의미 관계 분석을 완료하지 못했습니다.",
            list(relation_input.left.values()),
            list(relation_input.right.values()),
            failure_code,
            scoring,
            ruleset_version,
            prompt_version,
        )
        for relation_id, relation_input in pending.items()
    }
    return results, [f"FIT semantic analysis incomplete: {failure_code}"]


def _relation_result(
    relation_id: FitRelationId,
    status: FitStatus,
    summary: str,
    left_evidence: list[CplOccurrence],
    right_evidence: list[CplOccurrence],
    reason_code: str | None,
    scoring: FitScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
) -> FitRelationResult:
    return FitRelationResult(
        relation_id=relation_id,
        status=status,
        score=scoring.status_scores[status],
        summary=summary,
        left_evidence=left_evidence,
        right_evidence=right_evidence,
        reason_code=reason_code,
        rule_version=ruleset_version,
        prompt_version=prompt_version,
    )


def _score_summary(
    relations: list[FitRelationResult],
    scoring: FitScoringPolicy,
) -> FitScoreSummary:
    assessable = [relation for relation in relations if relation.score is not None]
    numerator = sum(
        relation.score * scoring.weights[relation.relation_id]
        for relation in assessable
        if relation.score is not None
    )
    denominator = sum(
        100 * scoring.weights[relation.relation_id] for relation in assessable
    )
    return FitScoreSummary(
        value=(numerator / denominator * 100 if denominator else None),
        numerator=numerator,
        denominator=denominator,
        assessable_count=len(assessable),
        scoring_version=scoring.version,
    )
