from datetime import datetime, timezone
import re

from app.infrastructure import reportlab_pdf_renderer as renderer
from app.schemas.report import (
    AnalysisReport,
    CaseReport,
    CplDisplay,
    FitAvailabilityDisplay,
    FitDisplay,
    ReportCaseDisplay,
    ReportExcerpt,
    ReportSimAxesDisplay,
    ReportSimAxisDisplay,
    ReportSimCandidateDisplay,
)
from app.schemas.sim import SimStatus


def _axis(evidence: list[ReportExcerpt]) -> ReportSimAxisDisplay:
    return ReportSimAxisDisplay(
        status=SimStatus.SIMILAR,
        summary="공통점이 확인되었습니다.",
        request_evidence=evidence,
        candidate_evidence=evidence,
    )


def test_long_sim_evidence_is_split_across_pdf_pages() -> None:
    long_excerpt = [ReportExcerpt(excerpt="긴 원문 근거 " * 1_000)]
    candidate = ReportSimCandidateDisplay(
        title="긴 근거 공고",
        source_url="https://example.com/announcement",
        comparison_summary="긴 근거를 포함한 비교 결과입니다.",
        axes=ReportSimAxesDisplay(
            purpose=_axis(long_excerpt),
            target=_axis([ReportExcerpt(excerpt="대상 근거")]),
            content=_axis([ReportExcerpt(excerpt="내용 근거")]),
            delivery=_axis([ReportExcerpt(excerpt="수행 근거")]),
        ),
    )
    report = CaseReport(
        case=ReportCaseDisplay(
            case_id=1,
            title="요청서.hwpx",
            completed_at=datetime.now(timezone.utc),
        ),
        report=AnalysisReport(
            cpl=CplDisplay(confirmed_count=0, items=[]),
            fit=FitDisplay(
                module_status="UNAVAILABLE",
                availability=FitAvailabilityDisplay(assessable_count=0),
                relations=[],
            ),
            similar_candidates=[candidate],
        ),
    )

    content = renderer._render(report)

    assert content.startswith(b"%PDF-")
    assert len(re.findall(rb"/Type\s*/Page\b", content)) > 1
