import hashlib
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy import Engine, text

from app.core.config import Settings
from app.ports.object_storage import ObjectStorage
from app.ports.pdf_renderer import PdfRenderer
from app.schemas.cpl import CplItem, CplOccurrence, CplResult, CplStatus
from app.schemas.fit import FitResult, FitStatus
from app.schemas.report import (
    REPORT_SCHEMA_VERSION,
    ReportCase,
    ReportCaseDisplay,
    ReportEvidence,
    ReportExcerpt,
    FitAvailabilityDisplay,
    FitDisplay,
    FitRelationDisplay,
    ReportJsonV01,
    AnalysisReport,
    CaseReport,
    ReportSimAxes,
    ReportSimAxis,
    ReportSimCandidate,
    ReportSimAxesDisplay,
    ReportSimAxisDisplay,
    ReportSimCandidateDisplay,
    CplDisplay,
    CplItemDisplay,
    ReviewIssue,
    SelfCheck,
    SelfCheckItem,
    StructuralConsistency,
    StructuralRelation,
    StructuralScore,
)
from app.schemas.sim import SimAxisResult, SimComparisonResult, SimEvidence


logger = logging.getLogger(__name__)
PDF_TEMPLATE_VERSION = "alpha-pdf-v0.1"
REPORT_FAILURE_CODE = "REPORT_GENERATION_FAILED"
REPORT_FAILURE_MESSAGE = "The analysis report could not be generated"


class ReportNotFoundError(LookupError):
    pass


class ReportNotReadyError(ValueError):
    pass


class ReportFileUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReportFile:
    content: BinaryIO
    filename: str


async def finalize_report(
    engine: Engine,
    storage: ObjectStorage,
    renderer: PdfRenderer,
    settings: Settings,
    *,
    case_id: int,
    missing_check_run_id: int,
    retrieval_run_id: int,
    cpl_result: CplResult,
    fit_result: FitResult | None,
    sim_results: list[SimComparisonResult],
    expected_candidate_count: int,
) -> ReportJsonV01:
    case = _claim_reporting(engine, case_id)
    storage_key = (
        f"users/{case['owner_user_id']}/cases/{case_id}/reports/"
        f"{uuid4().hex}.pdf"
    )
    stored = None
    try:
        finalized_at = datetime.now(timezone.utc)
        report = compose_report(
            settings,
            case_id=case_id,
            title=case["title"],
            created_at=case["created_at"],
            completed_at=finalized_at,
            cpl_result=cpl_result,
            fit_result=fit_result,
            sim_results=sim_results,
            expected_candidate_count=expected_candidate_count,
        )
        pdf_bytes = await renderer.render(report)
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("PDF renderer returned invalid content")
        stored = await storage.put(storage_key, io.BytesIO(pdf_bytes))
        if stored.key != storage_key or stored.size_bytes != len(pdf_bytes):
            raise RuntimeError("Stored PDF metadata does not match rendered content")
        _persist_final_report(
            engine,
            case_id=case_id,
            owner_user_id=case["owner_user_id"],
            missing_check_run_id=missing_check_run_id,
            retrieval_run_id=retrieval_run_id,
            report=report,
            finalized_at=finalized_at,
            storage_key=storage_key,
            pdf_bytes=pdf_bytes,
        )
    except BaseException:
        if stored is not None:
            try:
                await storage.delete(storage_key)
            except Exception:
                logger.exception("Failed to compensate report PDF: %s", storage_key)
        _mark_reporting_failure(engine, case_id)
        raise
    return report


def compose_report(
    settings: Settings,
    *,
    case_id: int,
    title: str,
    created_at: datetime,
    completed_at: datetime,
    cpl_result: CplResult,
    fit_result: FitResult | None,
    sim_results: list[SimComparisonResult],
    expected_candidate_count: int,
) -> ReportJsonV01:
    self_check = SelfCheck(
        confirmed_count=cpl_result.confirmed_count,
        confirmation_rate=cpl_result.confirmation_rate,
        items=[
            SelfCheckItem(
                field_code=item.field_code.value,
                status=item.status,
                reason_code=item.reason_code,
                explanation=item.explanation,
                occurrences=_cpl_evidence(case_id, item, cpl_result),
            )
            for item in cpl_result.items
        ],
        ruleset_version=cpl_result.ruleset_version,
        prompt_version=cpl_result.prompt_version or "none",
        model_profile=cpl_result.model_profile or "none",
        warnings=list(cpl_result.warnings),
    )
    structural = _structural_consistency(case_id, fit_result, settings)
    candidates = [_sim_candidate(result) for result in sim_results]
    warnings = _unique(
        [
            *cpl_result.warnings,
            *(fit_result.warnings if fit_result is not None else ["FIT result unavailable"]),
            *(warning for result in sim_results for warning in result.warnings),
            *(
                ["SIM result unavailable for one or more retrieval candidates"]
                if len(sim_results) != expected_candidate_count
                else []
            ),
        ]
    )
    issues = _review_issues(self_check, structural, candidates)
    return ReportJsonV01(
        case=ReportCase(
            case_id=case_id,
            title=title,
            created_at=created_at,
            completed_at=completed_at,
        ),
        self_check=self_check,
        structural_consistency=structural,
        review_issues=issues,
        similar_candidates=candidates,
        ben_references=[],
        differences=[],
        warnings=warnings,
    )


def get_report(engine: Engine, owner_user_id: int, case_id: int) -> CaseReport:
    with engine.connect() as connection:
        case_exists = connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM sims.inspection_case "
                "WHERE id = :case_id AND owner_user_id = :owner_user_id)"
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        )
        if not case_exists:
            raise ReportNotFoundError
        row = connection.execute(
            text(
                """
                SELECT r.report_json
                FROM sims.inspection_report r
                WHERE r.inspection_case_id = :case_id
                """
            ),
            {"case_id": case_id},
        ).mappings().one_or_none()
    if row is None:
        raise ReportNotReadyError
    return _report_response(ReportJsonV01.model_validate(row["report_json"]))


async def open_report_file(
    engine: Engine,
    storage: ObjectStorage,
    owner_user_id: int,
    case_id: int,
) -> ReportFile:
    with engine.connect() as connection:
        case_exists = connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM sims.inspection_case "
                "WHERE id = :case_id AND owner_user_id = :owner_user_id)"
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        )
        if not case_exists:
            raise ReportNotFoundError
        row = connection.execute(
            text(
                """
                SELECT f.storage_key, f.original_filename
                FROM sims.inspection_report r
                JOIN sims.output_artifact a ON a.inspection_report_id = r.id
                JOIN sims.file_asset f
                  ON f.id = a.file_asset_id
                 AND f.inspection_case_id = r.inspection_case_id
                 AND f.owner_user_id = :owner_user_id
                WHERE r.inspection_case_id = :case_id
                """
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        ).mappings().one_or_none()
    if row is None:
        raise ReportNotReadyError
    try:
        content = await storage.open(row["storage_key"])
    except FileNotFoundError as error:
        raise ReportFileUnavailableError from error
    return ReportFile(content=content, filename=row["original_filename"])


def _claim_reporting(engine: Engine, case_id: int) -> dict:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT c.owner_user_id, c.created_at, f.original_filename
                FROM sims.inspection_case c
                JOIN sims.uploaded_document d ON d.inspection_case_id = c.id
                JOIN sims.file_asset f ON f.id = d.file_asset_id
                WHERE c.id = :case_id AND c.status = 'RETRIEVING'
                FOR UPDATE OF c
                """
            ),
            {"case_id": case_id},
        ).mappings().one_or_none()
        if row is None:
            raise ReportNotReadyError("Case is not ready for reporting")
        connection.execute(
            text(
                "UPDATE sims.inspection_case SET status = 'REPORTING' "
                "WHERE id = :case_id"
            ),
            {"case_id": case_id},
        )
    return {
        "owner_user_id": row["owner_user_id"],
        "created_at": row["created_at"],
        "title": row["original_filename"],
    }


def _persist_final_report(
    engine: Engine,
    *,
    case_id: int,
    owner_user_id: int,
    missing_check_run_id: int,
    retrieval_run_id: int,
    report: ReportJsonV01,
    finalized_at: datetime,
    storage_key: str,
    pdf_bytes: bytes,
) -> None:
    with engine.begin() as connection:
        case_status = connection.scalar(
            text(
                "SELECT status FROM sims.inspection_case "
                "WHERE id = :case_id AND owner_user_id = :owner_user_id "
                "FOR UPDATE"
            ),
            {"case_id": case_id, "owner_user_id": owner_user_id},
        )
        if case_status != "REPORTING":
            raise ReportNotReadyError("Case is not in REPORTING status")
        report_id = connection.scalar(
            text(
                """
                INSERT INTO sims.inspection_report (
                    inspection_case_id, missing_check_run_id, retrieval_run_id,
                    report_schema_version, report_json, finalized_at
                )
                SELECT :case_id, :missing_check_run_id, :retrieval_run_id,
                       :schema_version, CAST(:report_json AS jsonb), :finalized_at
                WHERE EXISTS (
                    SELECT 1 FROM sims.missing_check_run
                    WHERE id = :missing_check_run_id
                      AND inspection_case_id = :case_id AND status = 'SUCCESS'
                ) AND EXISTS (
                    SELECT 1 FROM sims.retrieval_run
                    WHERE id = :retrieval_run_id
                      AND inspection_case_id = :case_id AND status = 'SUCCESS'
                )
                RETURNING id
                """
            ),
            {
                "case_id": case_id,
                "missing_check_run_id": missing_check_run_id,
                "retrieval_run_id": retrieval_run_id,
                "schema_version": REPORT_SCHEMA_VERSION,
                "report_json": report.model_dump_json(),
                "finalized_at": finalized_at,
            },
        )
        if report_id is None:
            raise ReportNotReadyError("Report source runs are inconsistent")
        file_asset_id = connection.scalar(
            text(
                """
                INSERT INTO sims.file_asset (
                    asset_scope, owner_user_id, inspection_case_id,
                    storage_key, original_filename, detected_mime_type,
                    extension, size_bytes, sha256_hex
                ) VALUES (
                    'USER', :owner_user_id, :case_id,
                    :storage_key, :original_filename, 'application/pdf',
                    'pdf', :size_bytes, :sha256_hex
                ) RETURNING id
                """
            ),
            {
                "owner_user_id": owner_user_id,
                "case_id": case_id,
                "storage_key": storage_key,
                "original_filename": f"Pre-review_{case_id}.pdf",
                "size_bytes": len(pdf_bytes),
                "sha256_hex": hashlib.sha256(pdf_bytes).hexdigest(),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO sims.output_artifact (
                    inspection_report_id, file_asset_id,
                    output_format, template_version, generated_at
                ) VALUES (
                    :report_id, :file_asset_id, 'PDF',
                    :template_version, :finalized_at
                )
                """
            ),
            {
                "report_id": report_id,
                "file_asset_id": file_asset_id,
                "template_version": PDF_TEMPLATE_VERSION,
                "finalized_at": finalized_at,
            },
        )
        updated = connection.execute(
            text(
                """
                UPDATE sims.inspection_case
                SET status = 'COMPLETED', completed_at = :finalized_at,
                    result_frozen_at = :finalized_at,
                    failure_code = NULL, failure_message = NULL
                WHERE id = :case_id AND status = 'REPORTING'
                """
            ),
            {"case_id": case_id, "finalized_at": finalized_at},
        )
        if updated.rowcount != 1:
            raise ReportNotReadyError("Case completion transition failed")


def _mark_reporting_failure(engine: Engine, case_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE sims.inspection_case
                SET status = 'FAILED', failure_code = :failure_code,
                    failure_message = :failure_message
                WHERE id = :case_id AND status = 'REPORTING'
                """
            ),
            {
                "case_id": case_id,
                "failure_code": REPORT_FAILURE_CODE,
                "failure_message": REPORT_FAILURE_MESSAGE,
            },
        )


def _cpl_evidence(
    case_id: int,
    item: CplItem,
    result: CplResult,
) -> list[ReportEvidence]:
    return [
        _occurrence_evidence(
            case_id,
            occurrence,
            evidence_ref=f"request:{item.field_code.value}:{index}",
            field_code=item.field_code.value,
            extraction_version=(
                result.prompt_version or result.ruleset_version
                if occurrence.extraction_method == "LLM"
                else result.ruleset_version
            ),
        )
        for index, occurrence in enumerate(item.occurrences)
    ]


def _report_response(report: ReportJsonV01) -> CaseReport:
    """PDF 링크를 담지 않는다. 완료된 건은 PDF 가 항상 있고 경로가 고정이다."""
    structural = report.structural_consistency
    return CaseReport(
        case=ReportCaseDisplay(
            case_id=report.case.case_id,
            title=report.case.title,
            completed_at=report.case.completed_at,
        ),
        report=AnalysisReport(
            cpl=CplDisplay(
                confirmed_count=report.self_check.confirmed_count,
                items=[
                    _self_check_item(item)
                    for item in report.self_check.items
                    if item.status != CplStatus.PARSE_FAILED
                ],
            ),
            fit=FitDisplay(
                module_status=structural.module_status,
                availability=FitAvailabilityDisplay(
                    assessable_count=structural.score.assessable_count,
                ),
                relations=[
                    FitRelationDisplay(
                        relation_id=relation.relation_id,
                        status=relation.status,
                        summary=relation.summary,
                        left_evidence=_display_evidence(relation.left_evidence),
                        right_evidence=_display_evidence(relation.right_evidence),
                    )
                    for relation in structural.relations
                ],
            ),
            similar_candidates=[
                _sim_candidate_display(candidate)
                for candidate in report.similar_candidates
            ],
        ),
    )


def _self_check_item(item: SelfCheckItem) -> CplItemDisplay:
    show_evidence = item.status in {
        CplStatus.MISSING,
        CplStatus.NEEDS_CONFIRMATION,
    }
    return CplItemDisplay(
        field_code=item.field_code,
        status=item.status,
        evidence=_display_evidence(item.occurrences) if show_evidence else [],
    )


def _display_evidence(evidence: list[ReportEvidence]) -> list[ReportExcerpt]:
    """Keep source passages, not every extraction fragment from a passage."""
    unique = []
    for item in evidence:
        normalized = _normalized_excerpt(item.excerpt)
        if not normalized or any(
            normalized in _normalized_excerpt(saved.excerpt) for saved in unique
        ):
            continue
        unique = [
            saved
            for saved in unique
            if _normalized_excerpt(saved.excerpt) not in normalized
        ]
        unique.append(ReportExcerpt(excerpt=item.excerpt))
    return [ReportExcerpt(excerpt="\n\n".join(item.excerpt for item in unique))] if unique else []


def _normalized_excerpt(value: str) -> str:
    return " ".join(value.split())


def _sim_candidate_display(candidate: ReportSimCandidate) -> ReportSimCandidateDisplay:
    return ReportSimCandidateDisplay(
        title=candidate.title,
        source_url=candidate.source_url,
        comparison_summary=candidate.comparison_summary,
        axes=ReportSimAxesDisplay(
            purpose=_sim_axis_display(candidate.axes.purpose),
            target=_sim_axis_display(candidate.axes.target),
            content=_sim_axis_display(candidate.axes.content),
            delivery=_sim_axis_display(candidate.axes.delivery),
        ),
    )


def _sim_axis_display(axis: ReportSimAxis) -> ReportSimAxisDisplay:
    return ReportSimAxisDisplay(
        status=axis.status,
        summary=axis.summary,
        common_points=axis.common_points,
        differences=axis.differences,
        request_evidence=_display_evidence(axis.request_evidence),
        candidate_evidence=_display_evidence(axis.candidate_evidence),
    )


def _occurrence_evidence(
    case_id: int,
    occurrence: CplOccurrence,
    *,
    evidence_ref: str,
    field_code: str | None,
    extraction_version: str,
) -> ReportEvidence:
    return ReportEvidence(
        evidence_ref=evidence_ref,
        source_side="REQUEST",
        source_id=f"case:{case_id}",
        field_code=field_code,
        axis_code=occurrence.axis_code,
        source_role=occurrence.source_role,
        excerpt=occurrence.raw_text,
        normalized_value=occurrence.normalized_value,
        block_id=occurrence.block_id,
        page_no=occurrence.page_no,
        section_path=list(occurrence.section_path),
        source_locator=dict(occurrence.source_locator),
        extraction_method=occurrence.extraction_method,
        extraction_version=extraction_version,
    )


def _structural_consistency(
    case_id: int,
    fit: FitResult | None,
    settings: Settings,
) -> StructuralConsistency:
    if fit is None:
        return StructuralConsistency(
            module_status="UNAVAILABLE",
            score=StructuralScore(
                value=None,
                numerator=0,
                denominator=0,
                assessable_count=0,
                scoring_version="unavailable",
            ),
            relations=[],
            ruleset_version=settings.fit_ruleset_version,
            prompt_version=settings.fit_prompt_version,
            scoring_version="unavailable",
            model_profile=settings.fit_model_profile,
            warnings=["FIT result unavailable"],
        )
    relations = []
    for relation in fit.relations:
        left = [
            _occurrence_evidence(
                case_id,
                occurrence,
                evidence_ref=f"fit:{relation.relation_id.value}:left:{index}",
                field_code=None,
                extraction_version=(
                    fit.prompt_version
                    if occurrence.extraction_method == "LLM"
                    else fit.ruleset_version
                ),
            )
            for index, occurrence in enumerate(relation.left_evidence)
        ]
        right = [
            _occurrence_evidence(
                case_id,
                occurrence,
                evidence_ref=f"fit:{relation.relation_id.value}:right:{index}",
                field_code=None,
                extraction_version=(
                    fit.prompt_version
                    if occurrence.extraction_method == "LLM"
                    else fit.ruleset_version
                ),
            )
            for index, occurrence in enumerate(relation.right_evidence)
        ]
        relations.append(
            StructuralRelation(
                relation_id=relation.relation_id,
                status=relation.status,
                score=relation.score,
                summary=relation.summary,
                reason_code=relation.reason_code,
                left_evidence=left,
                right_evidence=right,
                rule_version=relation.rule_version,
                prompt_version=relation.prompt_version,
            )
        )
    return StructuralConsistency(
        module_status="AVAILABLE",
        score=StructuralScore(**fit.score.model_dump()),
        relations=relations,
        ruleset_version=fit.ruleset_version,
        prompt_version=fit.prompt_version,
        scoring_version=fit.scoring_version,
        model_profile=fit.model_profile,
        warnings=list(fit.warnings),
    )


def _sim_candidate(result: SimComparisonResult) -> ReportSimCandidate:
    return ReportSimCandidate(
        rank=result.rank,
        announcement_id=result.announcement_id,
        announcement_version_id=result.announcement_version_id,
        title=result.title,
        source_url=result.source_url,
        semantic_similarity=result.semantic_similarity,
        semantic_similarity_display=result.semantic_similarity_display,
        weighted_score=result.weighted_score,
        assessable_axis_count=result.assessable_axis_count,
        review_grade=result.review_grade,
        comparison_summary=result.comparison_summary,
        axes=ReportSimAxes(
            purpose=_sim_axis(result.axes.purpose),
            target=_sim_axis(result.axes.target),
            content=_sim_axis(result.axes.content),
            delivery=_sim_axis(result.axes.delivery),
        ),
        warnings=list(result.warnings),
        ruleset_version=result.ruleset_version,
        prompt_version=result.prompt_version,
        scoring_version=result.scoring_version,
        model_profile=result.model_profile,
    )


def _sim_axis(axis: SimAxisResult) -> ReportSimAxis:
    return ReportSimAxis(
        axis_id=axis.axis_id,
        status=axis.status,
        score=axis.score,
        summary=axis.summary,
        common_points=list(axis.common_points),
        differences=list(axis.differences),
        request_evidence=[_sim_evidence(item, "REQUEST") for item in axis.request_evidence],
        candidate_evidence=[
            _sim_evidence(item, "ANNOUNCEMENT") for item in axis.candidate_evidence
        ],
        reason_code=axis.reason_code,
    )


def _sim_evidence(
    evidence: SimEvidence,
    source_side: str,
) -> ReportEvidence:
    locator = dict(evidence.source_locator)
    return ReportEvidence(
        evidence_ref=evidence.evidence_ref,
        source_side=source_side,
        source_id=evidence.source_id,
        field_code=evidence.profile_key,
        axis_code=None,
        source_role=locator.get("source_role"),
        excerpt=evidence.excerpt,
        normalized_value=evidence.normalized_value,
        block_id=locator.get("block_id"),
        page_no=evidence.page_no,
        section_path=list(evidence.section_path),
        source_locator=locator,
        extraction_method=evidence.extraction_method,
        extraction_version=evidence.extraction_version,
    )


def _review_issues(
    cpl: SelfCheck,
    fit: StructuralConsistency,
    candidates: list[ReportSimCandidate],
) -> list[ReviewIssue]:
    issues = [
        ReviewIssue(
            issue_id=f"CPL:{item.field_code}",
            source="CPL",
            reference_id=item.field_code,
            status=item.status.value,
            summary=item.explanation or item.reason_code or "확인이 필요한 CPL 항목입니다.",
            reason_code=item.reason_code,
            evidence=item.occurrences,
        )
        for item in cpl.items
        if item.status
        in {CplStatus.MISSING, CplStatus.NEEDS_CONFIRMATION, CplStatus.PARSE_FAILED}
    ]
    issues.extend(
        ReviewIssue(
            issue_id=f"FIT:{item.relation_id.value}",
            source="FIT",
            reference_id=item.relation_id.value,
            status=item.status.value,
            summary=item.summary,
            reason_code=item.reason_code,
            evidence=[*item.left_evidence, *item.right_evidence],
        )
        for item in fit.relations
        if item.status
        in {FitStatus.NEEDS_REVIEW, FitStatus.CONFLICT, FitStatus.INSUFFICIENT}
    )
    issues.extend(
        ReviewIssue(
            issue_id=f"SIM:{item.announcement_id}",
            source="SIM",
            reference_id=item.announcement_id,
            status=item.review_grade.value,
            summary=item.comparison_summary,
            evidence=_candidate_evidence(item),
        )
        for item in candidates
        if item.review_grade.value in {"FOCUS_REVIEW", "GENERAL_REVIEW"}
    )
    return issues


def _candidate_evidence(candidate: ReportSimCandidate) -> list[ReportEvidence]:
    values = []
    seen = set()
    for axis in (
        candidate.axes.purpose,
        candidate.axes.target,
        candidate.axes.content,
        candidate.axes.delivery,
    ):
        for evidence in [*axis.request_evidence, *axis.candidate_evidence]:
            key = (evidence.source_side, evidence.evidence_ref)
            if key not in seen:
                seen.add(key)
                values.append(evidence)
    return values


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
