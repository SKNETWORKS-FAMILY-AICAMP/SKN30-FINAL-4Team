import json
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
)
from app.schemas.fit import (
    FIT_RELATIONS,
    FitRelationId,
    FitRelationResult,
    FitResult,
    FitScoreSummary,
    FitScoringPolicy,
    FitSemanticResponse,
    FitStatus,
)


FitLlmFailureCode = Literal[
    "LLM_UNAVAILABLE",
    "LLM_TIMEOUT",
    "LLM_INVALID_RESPONSE",
]


@dataclass(frozen=True)
class _EvidenceSelector:
    field_code: CplFieldCode
    axes: frozenset[CplAxisCode]
    roles: frozenset[CplSourceRole | None] | None = None


_RELATION_SELECTORS: dict[
    FitRelationId,
    tuple[_EvidenceSelector, _EvidenceSelector],
] = {
    FitRelationId.FIT_1: (
        _EvidenceSelector(
            CplFieldCode.PURPOSE_GOAL,
            frozenset({CplAxisCode.PURPOSE_TARGET_CONDITION}),
            frozenset({None}),
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
            frozenset({None}),
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
            frozenset({None}),
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
        if relation_id == FitRelationId.FIT_5 and not relation_input.right:
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
        return (
            _ground_semantic_response(
                response,
                pending,
                scoring,
                ruleset_version,
                prompt_version,
            ),
            [],
        )
    except LLMTimeoutError:
        failure_code: FitLlmFailureCode = "LLM_TIMEOUT"
    except LLMUnavailableError:
        failure_code = "LLM_UNAVAILABLE"
    except (LLMInvalidResponseError, ValidationError, ValueError):
        failure_code = "LLM_INVALID_RESPONSE"
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

    shared_axes = set(left_values) & set(right_values)
    if any(left_values[axis] != right_values[axis] for axis in shared_axes):
        status = FitStatus.NEEDS_REVIEW
        reason_code = "NUMERIC_MISMATCH"
        summary = "동일 지원 개념의 정량값이 문서 안에서 서로 다릅니다."
    elif set(left_values) != set(right_values):
        status = FitStatus.FIT
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
) -> dict[FitRelationId, FitRelationResult]:
    ids = [relation.relation_id for relation in response.relations]
    if len(ids) != len(set(ids)) or set(ids) != set(pending):
        raise ValueError("FIT response relations do not match the request")

    grounded: dict[FitRelationId, FitRelationResult] = {}
    for relation in response.relations:
        source = pending[relation.relation_id]
        if not relation.summary.strip():
            raise ValueError("FIT summary must not be blank")
        if len(relation.left_evidence_refs) != len(set(relation.left_evidence_refs)):
            raise ValueError("FIT response contains duplicate left evidence")
        if len(relation.right_evidence_refs) != len(set(relation.right_evidence_refs)):
            raise ValueError("FIT response contains duplicate right evidence")
        if not set(relation.left_evidence_refs).issubset(source.left):
            raise ValueError("FIT response contains invalid left evidence")
        if not set(relation.right_evidence_refs).issubset(source.right):
            raise ValueError("FIT response contains invalid right evidence")
        if relation.status != FitStatus.INSUFFICIENT and (
            not relation.left_evidence_refs or not relation.right_evidence_refs
        ):
            raise ValueError("Assessable FIT response requires evidence on both sides")
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
    return grounded


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
