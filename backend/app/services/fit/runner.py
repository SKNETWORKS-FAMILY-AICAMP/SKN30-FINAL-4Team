"""FIT 병렬 러너 모듈 (Architecture v2.2).

`asyncio.gather`를 통해 Sub 1, Sub 2, Sub 3을 완전 병렬 구동합니다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.config import Settings
from app.ports.llm_client import LLMClient
from app.schemas.cpl import CplResult
from app.schemas.fit import FitResult, FitScoringPolicy
# Load functions removed; using cached Settings properties
from app.services.fit.subagents import (
    run_fit_subagent_1,
    run_fit_subagent_2,
    run_fit_subagent_3,
)
from app.services.fit.validator import validate_and_aggregate_fit

logger = logging.getLogger(__name__)


async def run_fit_subagents_parallel(
    cpl_result: CplResult | Any,
    llm_client: LLMClient | None,
    *,
    scoring: FitScoringPolicy | None = None,
    prompt: str | None = None,
    ruleset_version: str | None = None,
    prompt_version: str | None = None,
    model_profile: str | None = None,
    settings: Settings | None = None,
    case_id: int | None = None,
    subagent_timeout_seconds: float = 30.0,
) -> FitResult:
    """3개 FIT 서브에이전트를 병렬 실행하고 No-Loop 검증을 거쳐 FitResult 반환."""
    # 1. CplResult / FrozenInspectionContext 정규화
    if hasattr(cpl_result, "cpl_result") and isinstance(cpl_result.cpl_result, CplResult):
        actual_cpl = cpl_result.cpl_result
    elif hasattr(cpl_result, "cpl_results") and isinstance(cpl_result.cpl_results, dict) and cpl_result.cpl_results:
        actual_cpl = CplResult.model_validate(cpl_result.cpl_results)
    elif isinstance(cpl_result, dict):
        actual_cpl = CplResult.model_validate(cpl_result)
    elif isinstance(cpl_result, CplResult):
        actual_cpl = cpl_result
    else:
        actual_cpl = cpl_result

    # 2. Config 자동 바인딩
    if settings is not None:
        if scoring is None:
            scoring = settings.fit_scoring
        if prompt is None:
            prompt = settings.fit_prompt
        if ruleset_version is None:
            ruleset_version = settings.fit_ruleset_version
        if prompt_version is None:
            prompt_version = settings.fit_prompt_version
        if model_profile is None:
            model_profile = settings.fit_model_profile

    if scoring is None or prompt is None or ruleset_version is None or prompt_version is None:
        raise ValueError("FIT scoring, prompt, and versions must be provided or resolved from settings")
    if model_profile is None:
        model_profile = "default"

    logger.info("Starting FIT 3 subagents parallel execution for case_id=%s", case_id)

    # 3. 3개 서브에이전트 동시 기동 (Fault-isolated with return_exceptions=True)
    sub1_future = run_fit_subagent_1(
        actual_cpl,
        llm_client,
        scoring=scoring,
        prompt=prompt,
        ruleset_version=ruleset_version,
        prompt_version=prompt_version,
        model_profile=model_profile,
        timeout=subagent_timeout_seconds,
    )
    sub2_future = run_fit_subagent_2(
        actual_cpl,
        llm_client,
        scoring=scoring,
        prompt=prompt,
        ruleset_version=ruleset_version,
        prompt_version=prompt_version,
        model_profile=model_profile,
        timeout=subagent_timeout_seconds,
    )
    sub3_future = run_fit_subagent_3(
        actual_cpl,
        llm_client,
        scoring=scoring,
        prompt=prompt,
        ruleset_version=ruleset_version,
        prompt_version=prompt_version,
        model_profile=model_profile,
        timeout=subagent_timeout_seconds,
    )

    outputs = await asyncio.gather(
        sub1_future,
        sub2_future,
        sub3_future,
        return_exceptions=True,
    )

    # 4. No-Loop Validator & Aggregator 실행
    return validate_and_aggregate_fit(
        subagent_outputs=list(outputs),
        actual_cpl=actual_cpl,
        scoring=scoring,
        ruleset_version=ruleset_version,
        prompt_version=prompt_version,
        model_profile=model_profile,
    )
