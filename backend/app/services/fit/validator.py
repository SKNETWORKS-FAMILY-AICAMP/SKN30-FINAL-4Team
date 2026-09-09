"""FIT No-Loop Validator & Aggregator 모듈 (Architecture v2.2).

7개 축의 완전성, 소유권, 원문 접지, 정책 중립성을 검증하고 FitResult로 집계합니다.
"""
import logging
import re
from typing import Any

from app.schemas.cpl import CplResult
from app.schemas.fit import (
    FIT_RELATIONS,
    FitRelationId,
    FitRelationResult,
    FitResult,
    FitScoringPolicy,
    FitStatus,
)
from app.services.fit.fit_engine import _relation_result, _score_summary

logger = logging.getLogger(__name__)

_POLICY_JUDGMENT_PATTERN = re.compile(r"(부적격|중복사업|위반|탈락|폐지|부적정)")

SUBAGENT_OWNERSHIP = {
    1: {FitRelationId.FIT_1, FitRelationId.FIT_2, FitRelationId.FIT_3},
    2: {FitRelationId.FIT_4, FitRelationId.FIT_6},
    3: {FitRelationId.FIT_5, FitRelationId.FIT_7},
}


def validate_and_aggregate_fit(
    subagent_outputs: list[Any],
    actual_cpl: CplResult,
    *,
    scoring: FitScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
) -> FitResult:
    """3개 서브에이전트 출력을 수집하여 No-Loop 검증 후 FitResult로 조립."""
    collected_relations: dict[FitRelationId, FitRelationResult] = {}
    accumulated_warnings: list[str] = []

    # 1. 서브에이전트 결과 수집 및 예외 언패킹 (소유권 검증 포함)
    for idx, output in enumerate(subagent_outputs, start=1):
        if isinstance(output, Exception):
            logger.error("FIT Subagent %s raised unhandled exception: %s", idx, output)
            accumulated_warnings.append(f"FIT Subagent {idx} crashed: {type(output).__name__}")
            continue
        if isinstance(output, tuple) and len(output) == 2:
            res_dict, warns = output
            allowed_axes = SUBAGENT_OWNERSHIP.get(idx, set())
            for rel_id, rel_res in res_dict.items():
                if allowed_axes and rel_id not in allowed_axes:
                    logger.warning(
                        "FIT Subagent %s produced unauthorized axis %s; ignored",
                        idx,
                        rel_id.value,
                    )
                    accumulated_warnings.append(f"Unauthorized axis {rel_id.value} from Subagent {idx} ignored")
                    continue
                collected_relations[rel_id] = rel_res
            accumulated_warnings.extend(warns)

    # 2. No-Loop 결손 축 격리 (누락된 축은 자동 재추론 없이 즉시 UNAVAILABLE)
    for rel_id in FIT_RELATIONS:
        if rel_id not in collected_relations:
            logger.warning("FIT relation %s missing from subagent outputs; marking UNAVAILABLE", rel_id.value)
            collected_relations[rel_id] = _relation_result(
                rel_id,
                FitStatus.INSUFFICIENT,
                "평가 결과가 누락되어 분석이 보류되었습니다.",
                [],
                [],
                "UNAVAILABLE",
                scoring,
                ruleset_version,
                prompt_version,
            )
            accumulated_warnings.append(f"FIT relation {rel_id.value}:UNAVAILABLE")

    # 3. 정책 중립성 가드레일 및 reason_code 무결성 검증
    for rel_id, rel in collected_relations.items():
        summary = rel.summary or ""
        sanitized = False
        if _POLICY_JUDGMENT_PATTERN.search(summary):
            logger.warning("Non-neutral policy judgment detected in %s summary: %s", rel_id.value, summary)
            summary = _POLICY_JUDGMENT_PATTERN.sub("확인 필요", summary)
            sanitized = True
            accumulated_warnings.append(f"Policy judgment sanitized in {rel_id.value}")

        # reason_code 필요조건 검증 (Non-FIT 축은 반드시 reason_code 필요)
        reason_code = rel.reason_code
        if rel.status != FitStatus.FIT and not reason_code:
            reason_code = "REASON_CODE_MISSING"
            sanitized = True
            accumulated_warnings.append(f"Validator: {rel_id.value} missing reason_code rectified")

        if sanitized:
            collected_relations[rel_id] = _relation_result(
                rel_id,
                rel.status,
                summary,
                rel.left_evidence,
                rel.right_evidence,
                reason_code,
                scoring,
                ruleset_version,
                prompt_version,
            )

    # 4. 정렬 및 스코어 집계 (9대 네거티브 제약 준수: INSUFFICIENT는 score=None 유지)
    ordered_relations = [collected_relations[rel_id] for rel_id in FIT_RELATIONS]
    score_summary = _score_summary(ordered_relations, scoring)

    # 5. FitResult 인스턴스 반환
    return FitResult(
        relations=ordered_relations,
        score=score_summary,
        warnings=accumulated_warnings,
        ruleset_version=ruleset_version,
        prompt_version=prompt_version,
        scoring_version=scoring.version,
        model_profile=model_profile,
    )
