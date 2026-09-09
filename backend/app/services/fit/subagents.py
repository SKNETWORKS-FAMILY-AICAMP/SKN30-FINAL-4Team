"""FIT 3개 서브에이전트 구현 모듈 (Architecture v2.2).

- Subagent 1: 목적 연결성 3축 (FIT-1, FIT-2, FIT-3)
- Subagent 2: 사업 체계 2축 (FIT-4, FIT-6)
- Subagent 3: 대상 조건 및 규모 2축 (FIT-5, FIT-7)
"""
import asyncio
import logging
from typing import Any

from pydantic import ValidationError

from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.schemas.cpl import CplFieldCode, CplResult
from app.schemas.fit import (
    FitRelationId,
    FitRelationResult,
    FitScoringPolicy,
    FitSemanticResponse,
    FitStatus,
)
from app.services.fit.fit_engine import (
    _conditions_unresolved,
    _fit_7_result,
    _ground_semantic_response,
    _relation_inputs,
    _relation_result,
    _semantic_input_json,
)

logger = logging.getLogger(__name__)

SUBAGENT_1_RELATIONS = (
    FitRelationId.FIT_1,
    FitRelationId.FIT_2,
    FitRelationId.FIT_3,
)
SUBAGENT_2_RELATIONS = (
    FitRelationId.FIT_4,
    FitRelationId.FIT_6,
)
SUBAGENT_3_RELATIONS = (
    FitRelationId.FIT_5,
    FitRelationId.FIT_7,
)


async def run_fit_subagent_1(
    cpl_result: CplResult,
    llm_client: LLMClient | None,
    *,
    scoring: FitScoringPolicy,
    prompt: str,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
    timeout: float = 30.0,
) -> tuple[dict[FitRelationId, FitRelationResult], list[str]]:
    """Subagent 1: 목적 연결성 3축 (FIT-1, FIT-2, FIT-3) 평가."""
    results: dict[FitRelationId, FitRelationResult] = {}
    warnings: list[str] = []

    try:
        async with asyncio.timeout(timeout):
            items = {item.field_code: item for item in cpl_result.items}
            all_inputs = _relation_inputs(items)
            pending = {}

            for rel_id in SUBAGENT_1_RELATIONS:
                rel_input = all_inputs.get(rel_id)
                if not rel_input or not rel_input.left or not rel_input.right:
                    left = list(rel_input.left.values()) if rel_input else []
                    right = list(rel_input.right.values()) if rel_input else []
                    results[rel_id] = _relation_result(
                        rel_id,
                        FitStatus.INSUFFICIENT,
                        "비교에 필요한 요청서 정보가 부족합니다.",
                        left,
                        right,
                        "COMPARISON_EVIDENCE_MISSING",
                        scoring,
                        ruleset_version,
                        prompt_version,
                    )
                else:
                    pending[rel_id] = rel_input

            if pending:
                if llm_client is None:
                    for rel_id, inp in pending.items():
                        results[rel_id] = _relation_result(
                            rel_id,
                            FitStatus.INSUFFICIENT,
                            "LLM 클라이언트를 사용할 수 없습니다.",
                            list(inp.left.values()),
                            list(inp.right.values()),
                            "LLM_UNAVAILABLE",
                            scoring,
                            ruleset_version,
                            prompt_version,
                        )
                    warnings.append("FIT Subagent 1 incomplete: LLM_UNAVAILABLE")
                else:
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
                        raise LLMInvalidResponseError("Invalid structured response type")

                    grounded, ground_warns = _ground_semantic_response(
                        response, pending, scoring, ruleset_version, prompt_version
                    )
                    results.update(grounded)
                    warnings.extend(ground_warns)

    except (TimeoutError, asyncio.TimeoutError, LLMTimeoutError):
        logger.warning("FIT Subagent 1 timed out after %ss", timeout)
        warnings.append("FIT Subagent 1 incomplete: LLM_TIMEOUT")
        _apply_subagent_fallback(
            SUBAGENT_1_RELATIONS, results, "LLM_TIMEOUT", scoring, ruleset_version, prompt_version
        )
    except LLMUnavailableError:
        logger.warning("FIT Subagent 1 LLM unavailable")
        warnings.append("FIT Subagent 1 incomplete: LLM_UNAVAILABLE")
        _apply_subagent_fallback(
            SUBAGENT_1_RELATIONS, results, "LLM_UNAVAILABLE", scoring, ruleset_version, prompt_version
        )
    except (LLMInvalidResponseError, ValidationError, ValueError) as err:
        logger.warning("FIT Subagent 1 LLM response error: %s", err)
        warnings.append("FIT Subagent 1 incomplete: LLM_INVALID_RESPONSE")
        _apply_subagent_fallback(
            SUBAGENT_1_RELATIONS, results, "LLM_INVALID_RESPONSE", scoring, ruleset_version, prompt_version
        )
    except Exception as ex:
        logger.error("FIT Subagent 1 unhandled error: %s", ex, exc_info=True)
        warnings.append(f"FIT Subagent 1 error: {type(ex).__name__}")
        _apply_subagent_fallback(
            SUBAGENT_1_RELATIONS, results, "UNAVAILABLE", scoring, ruleset_version, prompt_version
        )

    return results, warnings


async def run_fit_subagent_2(
    cpl_result: CplResult,
    llm_client: LLMClient | None,
    *,
    scoring: FitScoringPolicy,
    prompt: str,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
    timeout: float = 30.0,
) -> tuple[dict[FitRelationId, FitRelationResult], list[str]]:
    """Subagent 2: 사업 체계 2축 (FIT-4 계층 규칙, FIT-6 수행기관·절차) 평가."""
    results: dict[FitRelationId, FitRelationResult] = {}
    warnings: list[str] = []

    # FIT-4: 규칙 기반 고정 반환 (Rule-owned)
    results[FitRelationId.FIT_4] = _relation_result(
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

    try:
        async with asyncio.timeout(timeout):
            items = {item.field_code: item for item in cpl_result.items}
            all_inputs = _relation_inputs(items)
            rel_input = all_inputs.get(FitRelationId.FIT_6)

            if not rel_input or not rel_input.left or not rel_input.right:
                left = list(rel_input.left.values()) if rel_input else []
                right = list(rel_input.right.values()) if rel_input else []
                results[FitRelationId.FIT_6] = _relation_result(
                    FitRelationId.FIT_6,
                    FitStatus.INSUFFICIENT,
                    "비교에 필요한 수행체계 정보가 부족합니다.",
                    left,
                    right,
                    "COMPARISON_EVIDENCE_MISSING",
                    scoring,
                    ruleset_version,
                    prompt_version,
                )
            else:
                pending = {FitRelationId.FIT_6: rel_input}
                if llm_client is None:
                    results[FitRelationId.FIT_6] = _relation_result(
                        FitRelationId.FIT_6,
                        FitStatus.INSUFFICIENT,
                        "LLM 클라이언트를 사용할 수 없습니다.",
                        list(rel_input.left.values()),
                        list(rel_input.right.values()),
                        "LLM_UNAVAILABLE",
                        scoring,
                        ruleset_version,
                        prompt_version,
                    )
                    warnings.append("FIT Subagent 2 incomplete: LLM_UNAVAILABLE")
                else:
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
                        raise LLMInvalidResponseError("Invalid structured response type")

                    grounded, ground_warns = _ground_semantic_response(
                        response, pending, scoring, ruleset_version, prompt_version
                    )
                    results.update(grounded)
                    warnings.extend(ground_warns)

    except (TimeoutError, asyncio.TimeoutError, LLMTimeoutError):
        logger.warning("FIT Subagent 2 timed out after %ss", timeout)
        warnings.append("FIT Subagent 2 incomplete: LLM_TIMEOUT")
        results[FitRelationId.FIT_6] = _relation_result(
            FitRelationId.FIT_6,
            FitStatus.INSUFFICIENT,
            "수행체계 의미 분석 시간 초과",
            [],
            [],
            "LLM_TIMEOUT",
            scoring,
            ruleset_version,
            prompt_version,
        )
    except Exception as ex:
        logger.error("FIT Subagent 2 unhandled error: %s", ex, exc_info=True)
        warnings.append(f"FIT Subagent 2 error: {type(ex).__name__}")
        results[FitRelationId.FIT_6] = _relation_result(
            FitRelationId.FIT_6,
            FitStatus.INSUFFICIENT,
            "수행체계 분석 노드 장애로 평가 보류",
            [],
            [],
            "UNAVAILABLE",
            scoring,
            ruleset_version,
            prompt_version,
        )

    return results, warnings


async def run_fit_subagent_3(
    cpl_result: CplResult,
    llm_client: LLMClient | None,
    *,
    scoring: FitScoringPolicy,
    prompt: str,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
    timeout: float = 30.0,
) -> tuple[dict[FitRelationId, FitRelationResult], list[str]]:
    """Subagent 3: 대상 조건 및 규모 2축 (FIT-5 지원조건, FIT-7 지원규모 산출식 규칙) 평가."""
    results: dict[FitRelationId, FitRelationResult] = {}
    warnings: list[str] = []
    items = {item.field_code: item for item in cpl_result.items}

    # FIT-7: 완전한 코드 기반 정량 산출 규칙 (LLM 절대 미호출)
    fit_7, fit_7_warns = _fit_7_result(
        items[CplFieldCode.SUPPORT_CONTENT_AND_SCALE],
        scoring,
        ruleset_version,
        prompt_version,
    )
    results[FitRelationId.FIT_7] = fit_7
    warnings.extend(fit_7_warns)

    # FIT-5: 지원대상 ↔ 지원조건
    try:
        async with asyncio.timeout(timeout):
            all_inputs = _relation_inputs(items)
            rel_input = all_inputs.get(FitRelationId.FIT_5)

            if not rel_input:
                results[FitRelationId.FIT_5] = _relation_result(
                    FitRelationId.FIT_5,
                    FitStatus.INSUFFICIENT,
                    "비교에 필요한 지원대상·조건 정보가 부족합니다.",
                    [],
                    [],
                    "COMPARISON_EVIDENCE_MISSING",
                    scoring,
                    ruleset_version,
                    prompt_version,
                )
            elif (
                rel_input.left
                and not rel_input.right
                and not _conditions_unresolved(items[CplFieldCode.TARGET_AND_CONDITIONS])
            ):
                results[FitRelationId.FIT_5] = _relation_result(
                    FitRelationId.FIT_5,
                    FitStatus.INSUFFICIENT,
                    "별도로 명시된 지원조건이 없습니다.",
                    list(rel_input.left.values()),
                    [],
                    "NO_CONDITIONS_SPECIFIED",
                    scoring,
                    ruleset_version,
                    prompt_version,
                )
            elif not rel_input.left or not rel_input.right:
                results[FitRelationId.FIT_5] = _relation_result(
                    FitRelationId.FIT_5,
                    FitStatus.INSUFFICIENT,
                    "비교에 필요한 지원대상·조건 정보가 부족합니다.",
                    list(rel_input.left.values()),
                    list(rel_input.right.values()),
                    "COMPARISON_EVIDENCE_MISSING",
                    scoring,
                    ruleset_version,
                    prompt_version,
                )
            else:
                pending = {FitRelationId.FIT_5: rel_input}
                if llm_client is None:
                    results[FitRelationId.FIT_5] = _relation_result(
                        FitRelationId.FIT_5,
                        FitStatus.INSUFFICIENT,
                        "LLM 클라이언트를 사용할 수 없습니다.",
                        list(rel_input.left.values()),
                        list(rel_input.right.values()),
                        "LLM_UNAVAILABLE",
                        scoring,
                        ruleset_version,
                        prompt_version,
                    )
                    warnings.append("FIT Subagent 3 incomplete: LLM_UNAVAILABLE")
                else:
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
                        raise LLMInvalidResponseError("Invalid structured response type")

                    grounded, ground_warns = _ground_semantic_response(
                        response, pending, scoring, ruleset_version, prompt_version
                    )
                    results.update(grounded)
                    warnings.extend(ground_warns)

    except (TimeoutError, asyncio.TimeoutError, LLMTimeoutError):
        logger.warning("FIT Subagent 3 timed out after %ss", timeout)
        warnings.append("FIT Subagent 3 incomplete: LLM_TIMEOUT")
        results[FitRelationId.FIT_5] = _relation_result(
            FitRelationId.FIT_5,
            FitStatus.INSUFFICIENT,
            "지원조건 의미 분석 시간 초과",
            [],
            [],
            "LLM_TIMEOUT",
            scoring,
            ruleset_version,
            prompt_version,
        )
    except Exception as ex:
        logger.error("FIT Subagent 3 unhandled error: %s", ex, exc_info=True)
        warnings.append(f"FIT Subagent 3 error: {type(ex).__name__}")
        results[FitRelationId.FIT_5] = _relation_result(
            FitRelationId.FIT_5,
            FitStatus.INSUFFICIENT,
            "지원조건 분석 노드 장애로 평가 보류",
            [],
            [],
            "UNAVAILABLE",
            scoring,
            ruleset_version,
            prompt_version,
        )

    return results, warnings


def _apply_subagent_fallback(
    relation_ids: tuple[FitRelationId, ...],
    results: dict[FitRelationId, FitRelationResult],
    reason_code: str,
    scoring: FitScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
) -> None:
    """결과가 채워지지 않은 축에 대해 표준 격리 Fallback 적용."""
    for rel_id in relation_ids:
        if rel_id not in results:
            results[rel_id] = _relation_result(
                rel_id,
                FitStatus.INSUFFICIENT,
                f"분석 장애로 평가 보류 ({reason_code})",
                [],
                [],
                reason_code,
                scoring,
                ruleset_version,
                prompt_version,
            )
