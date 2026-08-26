import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import Engine, text

from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.schemas.cpl import CplAxisCode, CplResult
from app.schemas.sim import (
    SIM_AXIS_IDS,
    SimAxes,
    SimAxis,
    SimAxisResult,
    SimComparisonResult,
    SimEvidence,
    SimReviewGrade,
    SimScoringPolicy,
    SimSemanticResponse,
    SimStatus,
)
from app.services.retrieval.retrieval import RetrievalResult


_REQUEST_PROFILE_KEYS: dict[CplAxisCode, tuple[SimAxis, str]] = {
    CplAxisCode.PURPOSE_PROBLEM_DOMAIN: (SimAxis.PURPOSE, "problem_domain"),
    CplAxisCode.PURPOSE_DIRECTION: (SimAxis.PURPOSE, "direction"),
    CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE: (
        SimAxis.PURPOSE,
        "specific_objective",
    ),
    CplAxisCode.TARGET_GROUP: (SimAxis.TARGET, "target_group"),
    CplAxisCode.COND_COMPANY_TYPE: (SimAxis.TARGET, "company_type"),
    CplAxisCode.COND_INDUSTRY: (SimAxis.TARGET, "industry"),
    CplAxisCode.COND_REGION: (SimAxis.TARGET, "region"),
    CplAxisCode.COND_BUSINESS_AGE: (SimAxis.TARGET, "business_age"),
    CplAxisCode.COND_REVENUE: (SimAxis.TARGET, "revenue"),
    CplAxisCode.COND_HEADCOUNT: (SimAxis.TARGET, "headcount"),
    CplAxisCode.COND_CERTIFICATION: (SimAxis.TARGET, "certification_or_status"),
    CplAxisCode.COND_OTHER: (SimAxis.TARGET, "other_condition"),
    CplAxisCode.COND_EXCLUSION: (SimAxis.TARGET, "exclusion"),
    CplAxisCode.SUPPORT_ACTIVITY: (SimAxis.CONTENT, "activity"),
    CplAxisCode.SUPPORT_INSTRUMENT: (SimAxis.CONTENT, "instrument"),
    CplAxisCode.SUPPORT_ITEM: (SimAxis.CONTENT, "item"),
    CplAxisCode.DELIVERY_ORG_NAME: (SimAxis.DELIVERY, "organization"),
    CplAxisCode.DELIVERY_METHOD_TYPE: (SimAxis.DELIVERY, "method"),
    CplAxisCode.DELIVERY_PROCEDURE_STEP: (SimAxis.DELIVERY, "procedure"),
    CplAxisCode.DELIVERY_STEP_ROLE: (SimAxis.DELIVERY, "step_role"),
}

_CANDIDATE_CONTAINER_KEYS: dict[SimAxis, tuple[str, ...]] = {
    SimAxis.PURPOSE: ("problem_domain", "direction", "specific_objective"),
    SimAxis.TARGET: (
        "target_group",
        "company_type",
        "industry",
        "region",
        "business_age",
        "revenue",
        "headcount",
        "certification_or_status",
        "other_condition",
        "exclusion",
    ),
    SimAxis.CONTENT: ("activity", "instrument", "item"),
    SimAxis.DELIVERY: ("organization",),
}


@dataclass(frozen=True, slots=True)
class _Candidate:
    retrieval_candidate_id: int
    rank: int
    announcement_id: str
    announcement_version_id: int
    title: str
    source_url: str
    semantic_similarity: float
    semantic_similarity_display: int
    purpose: str
    target: str
    content: str
    target_name: str | None
    jurisdiction_name: str | None
    executing_name: str | None
    detail_ref_fields: tuple[str, ...]


def load_sim_scoring(path: Path) -> SimScoringPolicy:
    return SimScoringPolicy.model_validate_json(path.read_text(encoding="utf-8"))


def load_sim_prompt(path: Path) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("SIM prompt must not be blank")
    return prompt


async def analyze_sim_candidates(
    engine: Engine,
    retrieval: RetrievalResult,
    cpl_result: CplResult,
    llm_client: LLMClient | None,
    *,
    scoring: SimScoringPolicy,
    prompt: str,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
) -> list[SimComparisonResult]:
    if not prompt.strip() or not ruleset_version.strip() or not prompt_version.strip():
        raise ValueError("SIM prompt and versions must not be blank")
    if retrieval.top_k_used != scoring.retrieval.top_k:
        raise ValueError("Retrieval and SIM Top-K contracts do not match")

    request = _request_profile(cpl_result)
    candidates = _load_candidates(engine, retrieval)
    pending = []
    for candidate in candidates:
        candidate_profile, source_warnings = _candidate_profile(candidate)
        pending.append(
            _compare_candidate(
                candidate,
                request,
                candidate_profile,
                source_warnings,
                llm_client,
                scoring,
                prompt,
                ruleset_version,
                prompt_version,
                model_profile,
            )
        )
    comparisons = await asyncio.gather(*pending)
    results = [
        (candidate.retrieval_candidate_id, result)
        for candidate, result in zip(candidates, comparisons, strict=True)
    ]
    _persist_results(engine, results)
    return [result for _, result in results]


def _request_profile(cpl_result: CplResult) -> dict[SimAxis, dict[str, SimEvidence]]:
    profile = {axis: {} for axis in SimAxis}
    for item in cpl_result.items:
        for index, occurrence in enumerate(item.occurrences):
            mapping = _REQUEST_PROFILE_KEYS.get(occurrence.axis_code)
            if mapping is None or not occurrence.raw_text.strip():
                continue
            axis, profile_key = mapping
            reference = f"request:{item.field_code.value}:{index}"
            profile[axis][reference] = SimEvidence(
                evidence_ref=reference,
                source_id=f"request-block:{occurrence.block_id}",
                profile_key=profile_key,
                excerpt=occurrence.raw_text.strip(),
                normalized_value=occurrence.normalized_value,
                page_no=occurrence.page_no,
                section_path=list(occurrence.section_path),
                source_locator={
                    **occurrence.source_locator,
                    "block_id": occurrence.block_id,
                    "source_role": occurrence.source_role,
                },
                extraction_method=occurrence.extraction_method,
                extraction_version=(
                    cpl_result.prompt_version or cpl_result.ruleset_version
                    if occurrence.extraction_method == "LLM"
                    else cpl_result.ruleset_version
                ),
            )
    return profile


def _candidate_profile(
    candidate: _Candidate,
) -> tuple[dict[SimAxis, dict[str, SimEvidence]], list[str]]:
    profile = {axis: {} for axis in SimAxis}
    source_values = {
        SimAxis.PURPOSE: ("purpose", candidate.purpose),
        SimAxis.TARGET: ("target", candidate.target),
        SimAxis.CONTENT: ("content", candidate.content),
    }
    for axis, (source_field, raw_text) in source_values.items():
        if not raw_text.strip():
            continue
        for profile_key in _CANDIDATE_CONTAINER_KEYS[axis]:
            reference = (
                f"candidate:{candidate.announcement_version_id}:"
                f"{source_field}:{profile_key}"
            )
            profile[axis][reference] = _source_evidence(
                candidate,
                reference=reference,
                source_field=source_field,
                profile_key=profile_key,
                excerpt=raw_text,
            )

    for source_field, value in (
        ("target_name", candidate.target_name),
        ("jurisdiction_name", candidate.jurisdiction_name),
        ("executing_name", candidate.executing_name),
    ):
        if value is None or not value.strip():
            continue
        axis = (
            SimAxis.TARGET
            if source_field == "target_name"
            else SimAxis.DELIVERY
        )
        profile_key = (
            "target_group"
            if source_field == "target_name"
            else "organization"
        )
        reference = (
            f"candidate:{candidate.announcement_version_id}:"
            f"{axis.value}:{source_field}"
        )
        profile[axis][reference] = _source_evidence(
            candidate,
            reference=reference,
            source_field=source_field,
            profile_key=profile_key,
            excerpt=value,
        )

    warnings = []
    if candidate.detail_ref_fields:
        warnings.append(
            "Candidate summary refers to an unparsed detail attachment for: "
            + ", ".join(candidate.detail_ref_fields)
        )
    return profile, warnings


def _source_evidence(
    candidate: _Candidate,
    *,
    reference: str,
    source_field: str,
    profile_key: str,
    excerpt: str,
) -> SimEvidence:
    return SimEvidence(
        evidence_ref=reference,
        source_id=f"announcement-version:{candidate.announcement_version_id}",
        profile_key=profile_key,
        excerpt=excerpt.strip(),
        source_locator={
            "announcement_version_id": candidate.announcement_version_id,
            "source_field": source_field,
            "source_url": candidate.source_url,
        },
        extraction_method="SOURCE",
        extraction_version="bizinfo-open-api-summary-v1",
    )


async def _compare_candidate(
    candidate: _Candidate,
    request: dict[SimAxis, dict[str, SimEvidence]],
    candidate_profile: dict[SimAxis, dict[str, SimEvidence]],
    source_warnings: list[str],
    llm_client: LLMClient | None,
    scoring: SimScoringPolicy,
    prompt: str,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
) -> SimComparisonResult:
    if llm_client is None:
        return _failed_comparison(
            candidate,
            request,
            candidate_profile,
            source_warnings,
            "LLM_UNAVAILABLE",
            scoring,
            ruleset_version,
            prompt_version,
            model_profile,
        )
    try:
        response = await llm_client.generate_structured(
            task_name="sim_candidate_comparison",
            messages=[
                Message(role="developer", content=prompt),
                Message(
                    role="user",
                    content=_semantic_input_json(request, candidate_profile),
                ),
            ],
            response_schema=SimSemanticResponse,
            model_profile=model_profile,
        )
        if not isinstance(response, SimSemanticResponse):
            raise LLMInvalidResponseError("Unexpected structured response type")
        return _ground_response(
            candidate,
            response,
            request,
            candidate_profile,
            source_warnings,
            scoring,
            ruleset_version,
            prompt_version,
            model_profile,
        )
    except LLMTimeoutError:
        failure_code = "LLM_TIMEOUT"
    except LLMUnavailableError:
        failure_code = "LLM_UNAVAILABLE"
    except (LLMInvalidResponseError, ValidationError, ValueError):
        failure_code = "LLM_INVALID_RESPONSE"
    return _failed_comparison(
        candidate,
        request,
        candidate_profile,
        source_warnings,
        failure_code,
        scoring,
        ruleset_version,
        prompt_version,
        model_profile,
    )


def _ground_response(
    candidate: _Candidate,
    response: SimSemanticResponse,
    request: dict[SimAxis, dict[str, SimEvidence]],
    candidate_profile: dict[SimAxis, dict[str, SimEvidence]],
    warnings: list[str],
    scoring: SimScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
) -> SimComparisonResult:
    results: dict[SimAxis, SimAxisResult] = {}
    for axis in SimAxis:
        semantic = getattr(response.axes, axis.value)
        request_refs = semantic.request_evidence_refs
        candidate_refs = semantic.candidate_evidence_refs
        if len(request_refs) != len(set(request_refs)):
            raise ValueError("SIM response contains duplicate request evidence")
        if len(candidate_refs) != len(set(candidate_refs)):
            raise ValueError("SIM response contains duplicate candidate evidence")
        if not set(request_refs).issubset(request[axis]):
            raise ValueError("SIM response contains invalid request evidence")
        if not set(candidate_refs).issubset(candidate_profile[axis]):
            raise ValueError("SIM response contains invalid candidate evidence")
        if semantic.status != SimStatus.INSUFFICIENT and (
            not request_refs or not candidate_refs
        ):
            raise ValueError("Assessable SIM response requires evidence on both sides")
        if semantic.status != SimStatus.SIMILAR and not semantic.reason_code:
            raise ValueError("Non-SIMILAR SIM response requires a reason code")
        results[axis] = _axis_result(
            axis,
            semantic.status,
            semantic.summary,
            semantic.common_points,
            semantic.differences,
            [request[axis][reference] for reference in request_refs],
            [candidate_profile[axis][reference] for reference in candidate_refs],
            semantic.reason_code,
            scoring,
        )
    return _comparison_result(
        candidate,
        results,
        response.comparison_summary,
        warnings,
        scoring,
        ruleset_version,
        prompt_version,
        model_profile,
    )


def _failed_comparison(
    candidate: _Candidate,
    request: dict[SimAxis, dict[str, SimEvidence]],
    candidate_profile: dict[SimAxis, dict[str, SimEvidence]],
    warnings: list[str],
    failure_code: str,
    scoring: SimScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
) -> SimComparisonResult:
    results = {
        axis: _axis_result(
            axis,
            SimStatus.INSUFFICIENT,
            "의미 비교를 완료하지 못했습니다.",
            [],
            [],
            list(request[axis].values()),
            list(candidate_profile[axis].values()),
            failure_code,
            scoring,
        )
        for axis in SimAxis
    }
    return _comparison_result(
        candidate,
        results,
        "후보의 구조 비교를 완료하지 못했습니다.",
        [*warnings, f"SIM semantic comparison incomplete: {failure_code}"],
        scoring,
        ruleset_version,
        prompt_version,
        model_profile,
    )


def _axis_result(
    axis: SimAxis,
    status: SimStatus,
    summary: str,
    common_points: list[str],
    differences: list[str],
    request_evidence: list[SimEvidence],
    candidate_evidence: list[SimEvidence],
    reason_code: str | None,
    scoring: SimScoringPolicy,
) -> SimAxisResult:
    return SimAxisResult(
        axis_id=SIM_AXIS_IDS[axis],
        status=status,
        score=scoring.status_scores[status],
        summary=summary,
        common_points=common_points,
        differences=differences,
        request_evidence=request_evidence,
        candidate_evidence=candidate_evidence,
        reason_code=reason_code,
    )


def _comparison_result(
    candidate: _Candidate,
    results: dict[SimAxis, SimAxisResult],
    comparison_summary: str,
    warnings: list[str],
    scoring: SimScoringPolicy,
    ruleset_version: str,
    prompt_version: str,
    model_profile: str,
) -> SimComparisonResult:
    assessable = {
        axis: result for axis, result in results.items() if result.score is not None
    }
    if assessable:
        denominator = sum(scoring.axis_weights[axis] for axis in assessable)
        weighted_score = sum(
            result.score * scoring.axis_weights[axis]
            for axis, result in assessable.items()
            if result.score is not None
        ) / denominator
        if weighted_score >= scoring.grades.focus_review_min:
            grade = SimReviewGrade.FOCUS_REVIEW
        elif weighted_score >= scoring.grades.general_review_min:
            grade = SimReviewGrade.GENERAL_REVIEW
        else:
            grade = SimReviewGrade.LOW_PRIORITY
    else:
        weighted_score = None
        grade = SimReviewGrade.ON_HOLD
    axes = SimAxes(
        purpose=results[SimAxis.PURPOSE],
        target=results[SimAxis.TARGET],
        content=results[SimAxis.CONTENT],
        delivery=results[SimAxis.DELIVERY],
    )
    return SimComparisonResult(
        rank=candidate.rank,
        announcement_id=candidate.announcement_id,
        announcement_version_id=candidate.announcement_version_id,
        title=candidate.title,
        source_url=candidate.source_url,
        semantic_similarity=candidate.semantic_similarity,
        semantic_similarity_display=candidate.semantic_similarity_display,
        axes=axes,
        weighted_score=weighted_score,
        assessable_axis_count=len(assessable),
        review_grade=grade,
        comparison_summary=comparison_summary,
        warnings=warnings,
        ruleset_version=ruleset_version,
        prompt_version=prompt_version,
        scoring_version=scoring.version,
        model_profile=model_profile,
    )


def _semantic_input_json(
    request: dict[SimAxis, dict[str, SimEvidence]],
    candidate: dict[SimAxis, dict[str, SimEvidence]],
) -> str:
    return json.dumps(
        {
            "axes": {
                axis.value: {
                    "axis_id": SIM_AXIS_IDS[axis],
                    "request_evidence": [
                        evidence.model_dump(mode="json")
                        for evidence in request[axis].values()
                    ],
                    "candidate_evidence": [
                        evidence.model_dump(mode="json")
                        for evidence in candidate[axis].values()
                    ],
                }
                for axis in SimAxis
            }
        },
        ensure_ascii=False,
    )


def _load_candidates(
    engine: Engine,
    retrieval: RetrievalResult,
) -> list[_Candidate]:
    expected = {
        candidate.announcement_version_id: candidate
        for candidate in retrieval.candidates
    }
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT rc.id AS retrieval_candidate_id, rc.rank_no,
                       a.pblanc_id, av.id AS announcement_version_id,
                       av.pblanc_nm, av.pblanc_url, av.purpose,
                       av.target, av.content, av.target_name, av.jrsd_instt_nm,
                       av.exc_instt_nm, av.detail_ref_fields
                FROM sims.retrieval_candidate rc
                JOIN sims.announcement_version av
                  ON av.id = rc.announcement_version_id
                JOIN sims.announcement a ON a.id = av.announcement_id
                WHERE rc.retrieval_run_id = :retrieval_run_id
                ORDER BY rc.rank_no
                """
            ),
            {"retrieval_run_id": retrieval.retrieval_run_id},
        ).mappings().all()
    if len(rows) != len(expected):
        raise ValueError("Persisted retrieval candidates do not match retrieval result")
    candidates = []
    for row in rows:
        source = expected.get(row["announcement_version_id"])
        if source is None or source.rank != row["rank_no"]:
            raise ValueError("Persisted retrieval candidate identity is inconsistent")
        candidates.append(
            _Candidate(
                retrieval_candidate_id=row["retrieval_candidate_id"],
                rank=source.rank,
                announcement_id=row["pblanc_id"],
                announcement_version_id=source.announcement_version_id,
                title=source.title,
                source_url=source.url,
                semantic_similarity=source.semantic_similarity,
                semantic_similarity_display=source.semantic_similarity_display,
                purpose=row["purpose"],
                target=row["target"],
                content=row["content"],
                target_name=row["target_name"],
                jurisdiction_name=row["jrsd_instt_nm"],
                executing_name=row["exc_instt_nm"],
                detail_ref_fields=tuple(row["detail_ref_fields"] or ()),
            )
        )
    return candidates


def _persist_results(
    engine: Engine,
    results: list[tuple[int, SimComparisonResult]],
) -> None:
    with engine.begin() as connection:
        for candidate_id, result in results:
            connection.execute(
                text(
                    """
                    UPDATE sims.retrieval_candidate
                    SET comparison_summary = :summary,
                        comparison_result = CAST(:result AS jsonb)
                    WHERE id = :candidate_id
                    """
                ),
                {
                    "candidate_id": candidate_id,
                    "summary": result.comparison_summary,
                    "result": result.model_dump_json(),
                },
            )
            connection.execute(
                text(
                    "DELETE FROM sims.candidate_evidence "
                    "WHERE retrieval_candidate_id = :candidate_id"
                ),
                {"candidate_id": candidate_id},
            )
            seen: set[tuple[str, str]] = set()
            for axis, axis_result in (
                (SimAxis.PURPOSE, result.axes.purpose),
                (SimAxis.TARGET, result.axes.target),
                (SimAxis.CONTENT, result.axes.content),
                (SimAxis.DELIVERY, result.axes.delivery),
            ):
                for side, evidence_items in (
                    ("REQUEST", axis_result.request_evidence),
                    ("ANNOUNCEMENT", axis_result.candidate_evidence),
                ):
                    for evidence in evidence_items:
                        key = (side, evidence.evidence_ref)
                        if key in seen:
                            continue
                        seen.add(key)
                        locator = {
                            **evidence.source_locator,
                            "evidence_ref": evidence.evidence_ref,
                            "source_id": evidence.source_id,
                            "section_path": evidence.section_path,
                            "extraction_method": evidence.extraction_method,
                            "extraction_version": evidence.extraction_version,
                        }
                        connection.execute(
                            text(
                                """
                                INSERT INTO sims.candidate_evidence (
                                    retrieval_candidate_id, evidence_side,
                                    field_code, page_no, source_locator,
                                    excerpt, explanation
                                ) VALUES (
                                    :candidate_id, :side, :field_code, :page_no,
                                    CAST(:source_locator AS jsonb), :excerpt,
                                    :explanation
                                )
                                """
                            ),
                            {
                                "candidate_id": candidate_id,
                                "side": side,
                                "field_code": evidence.profile_key,
                                "page_no": evidence.page_no,
                                "source_locator": json.dumps(
                                    locator,
                                    ensure_ascii=False,
                                    default=str,
                                ),
                                "excerpt": evidence.excerpt,
                                "explanation": f"{SIM_AXIS_IDS[axis]} evidence",
                            },
                        )
