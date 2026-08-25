import json
from dataclasses import dataclass
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
from app.schemas.cpl import CplFieldCode, CplItem, CplOccurrence, CplResult
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

_RELATION_FIELDS: dict[
    FitRelationId,
    tuple[tuple[CplFieldCode, ...], tuple[CplFieldCode, ...]],
] = {
    FitRelationId.FIT_1: (
        (CplFieldCode.PURPOSE_GOAL,),
        (CplFieldCode.TARGET_AND_CONDITIONS,),
    ),
    FitRelationId.FIT_2: (
        (CplFieldCode.PURPOSE_GOAL,),
        (CplFieldCode.SUPPORT_CONTENT_AND_SCALE,),
    ),
    FitRelationId.FIT_3: (
        (CplFieldCode.PURPOSE_GOAL,),
        (CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,),
    ),
    FitRelationId.FIT_4: (
        (CplFieldCode.REQUEST_TYPE,),
        (CplFieldCode.IMPLEMENTATION_PLAN,),
    ),
    FitRelationId.FIT_5: (
        (CplFieldCode.TARGET_AND_CONDITIONS,),
        (CplFieldCode.TARGET_AND_CONDITIONS,),
    ),
    FitRelationId.FIT_6: (
        (CplFieldCode.DELIVERY_SYSTEM,),
        (CplFieldCode.DELIVERY_SYSTEM,),
    ),
    FitRelationId.FIT_7: (
        (CplFieldCode.SUPPORT_CONTENT_AND_SCALE,),
        (CplFieldCode.SUPPORT_CONTENT_AND_SCALE,),
    ),
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

    inputs = _relation_inputs(cpl_result)
    results: dict[FitRelationId, FitRelationResult] = {}
    pending: dict[FitRelationId, _RelationInput] = {}
    for relation_id in FIT_RELATIONS:
        relation_input = inputs[relation_id]
        if not relation_input.left or not relation_input.right:
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
        semantic_results, warnings = await _semantic_results(
            pending,
            llm_client,
            scoring,
            prompt,
            ruleset_version,
            prompt_version,
            model_profile,
        )
        results.update(semantic_results)
    else:
        warnings = []

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


def _relation_inputs(cpl_result: CplResult) -> dict[FitRelationId, _RelationInput]:
    items = {item.field_code: item for item in cpl_result.items}
    return {
        relation_id: _RelationInput(
            left=_evidence(items, left_fields),
            right=_evidence(items, right_fields),
        )
        for relation_id, (left_fields, right_fields) in _RELATION_FIELDS.items()
    }


def _evidence(
    items: dict[CplFieldCode, CplItem],
    fields: tuple[CplFieldCode, ...],
) -> dict[str, CplOccurrence]:
    return {
        f"{field_code.value}:{index}": occurrence
        for field_code in fields
        for index, occurrence in enumerate(items[field_code].occurrences)
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
        }
        for reference, occurrence in evidence.items()
    ]


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
