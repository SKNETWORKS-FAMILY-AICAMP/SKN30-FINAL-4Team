"""PDF rendering of the result screen projection.

화면이 받는 ``CaseReport`` 를 그대로 그린다. 내부 보고서(``ReportJsonV01``)를
그리면 화면에서 뺀 값(확인율·점수·순위·유사도)이 PDF 에만 남고, 화면에 넣기로
한 근거가 PDF 에서 빠지는 일이 다시 생긴다.

항목·관계·상태의 한글 이름은 화면 쪽 소유라 응답에는 코드만 담긴다. 사람이
받아 보는 문서에 코드를 그대로 찍을 수는 없으므로, 프론트에 전달한
``docs/Pre-review_API_결과응답_프론트필드설명.md`` 의 표를 그대로 옮겨 둔다.
그 표가 바뀌면 여기도 함께 고친다.
"""

import asyncio
import html
import io
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.schemas.report import (
    CaseReport,
    CplItemDisplay,
    FitRelationDisplay,
    ReportExcerpt,
    ReportSimAxisDisplay,
    ReportSimCandidateDisplay,
)
from app.schemas.cpl import CPL_TOTAL_FIELDS
from app.schemas.fit import FIT_TOTAL_RELATIONS


FONT_NAME = "PreReviewKorean"
_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
)

CPL_FIELD_LABELS = {
    "REQUEST_TYPE": "사전협의 요청 유형",
    "PURPOSE_GOAL": "사업 목적·목표",
    "IMPLEMENTATION_PLAN": "추진계획",
    "BUSINESS_PERIOD": "사업 기간",
    "NEW_OR_CHANGED_CONTENT": "신설·변경 내용",
    "BUSINESS_NEED": "사업 필요성",
    "LEGAL_BASIS": "법적 근거",
    "LINKED_POLICY": "연계 정책·계획",
    "BUDGET": "예산",
    "TARGET_AND_CONDITIONS": "지원 대상·조건",
    "SUPPORT_CONTENT_AND_SCALE": "지원 내용·규모",
    "DELIVERY_SYSTEM": "수행 체계",
    "EXPECTED_EFFECTS_AND_PERFORMANCE": "기대효과·성과지표",
}

CPL_STATUS_LABELS = {
    "PRESENT": "확인",
    "MISSING": "누락",
    "NOT_APPLICABLE": "해당 없음",
    "NEEDS_CONFIRMATION": "확인 필요",
}

FIT_RELATION_LABELS = {
    "FIT-1": "목적의 대상 조건 ↔ 지원 대상",
    "FIT-2": "목적 방향 ↔ 지원 활동·수단",
    "FIT-3": "목적 방향 ↔ 기대효과·성과지표",
    "FIT-4": "사업 계층 간 비교",
    "FIT-5": "대상군 ↔ 지원 조건",
    "FIT-6": "수행기관 ↔ 절차·역할",
    "FIT-7": "지원 내용 ↔ 지원 규모 정량값",
}

FIT_STATUS_LABELS = {
    "FIT": "연결 관계 확인",
    "NEEDS_REVIEW": "추가 검토 필요",
    "CONFLICT": "관계 충돌",
    "INSUFFICIENT": "비교 정보 부족",
}

SIM_AXIS_LABELS = {
    "purpose": "사업 목적",
    "target": "지원 대상",
    "content": "지원 내용",
    "delivery": "수행 체계",
}

SIM_STATUS_LABELS = {
    "SIMILAR": "공통점 확인",
    "PARTIAL": "일부 공통점 확인",
    "DIFFERENT": "차이 확인",
    "INSUFFICIENT": "비교 정보 부족",
}


class ReportLabPdfRenderer:
    async def render(self, report: CaseReport) -> bytes:
        return await asyncio.to_thread(_render, report)


def _render(report: CaseReport) -> bytes:
    font_name = _register_font()
    styles = _styles(font_name)

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Pre-review - {report.case.title}",
    )

    story = [
        Paragraph("Pre-review 분석 보고서", styles["title"]),
        Spacer(1, 6 * mm),
        _paragraph(f"분석 문서: {report.case.title}", styles["body"]),
        _paragraph(
            f"완료 시각: {report.case.completed_at.isoformat()}", styles["body"]
        ),
        Spacer(1, 5 * mm),
    ]
    story += _cpl_story(report, styles)
    story += _fit_story(report, styles)
    story += _sim_story(report, styles)

    document.build(
        story,
        onFirstPage=lambda canvas, doc: _page_number(canvas, doc, font_name),
        onLaterPages=lambda canvas, doc: _page_number(canvas, doc, font_name),
    )
    return output.getvalue()


def _cpl_story(report: CaseReport, styles: dict) -> list:
    cpl = report.report.cpl
    return [
        Paragraph("요청자료 완전성·기초구조 점검", styles["heading"]),
        _paragraph(
            f"확인 {cpl.confirmed_count} / {CPL_TOTAL_FIELDS}", styles["body"]
        ),
        Spacer(1, 2 * mm),
        _table(
            [_header_row(("항목", "상태", "원문 근거"), styles)]
            + [_cpl_row(item, styles) for item in cpl.items],
            [42 * mm, 24 * mm, 108 * mm],
        ),
        Spacer(1, 7 * mm),
    ]


def _cpl_row(item: CplItemDisplay, styles: dict) -> list:
    return [
        _paragraph(CPL_FIELD_LABELS.get(item.field_code, item.field_code), styles["body"]),
        _paragraph(CPL_STATUS_LABELS.get(item.status.value, item.status.value), styles["body"]),
        _evidence_paragraph(item.evidence, styles),
    ]


def _fit_story(report: CaseReport, styles: dict) -> list:
    fit = report.report.fit
    story = [Paragraph("내부 정합성 점검", styles["heading"])]
    if fit.module_status != "AVAILABLE":
        story.append(_paragraph("정합성 점검을 수행하지 못했습니다.", styles["body"]))
        story.append(Spacer(1, 7 * mm))
        return story
    story += [
        _paragraph(
            f"판단 가능 {fit.availability.assessable_count} / "
            f"{FIT_TOTAL_RELATIONS}",
            styles["body"],
        ),
        Spacer(1, 2 * mm),
        _table(
            [_header_row(("관계", "상태", "설명", "원문 근거"), styles)]
            + [_fit_row(relation, styles) for relation in fit.relations],
            [40 * mm, 24 * mm, 50 * mm, 60 * mm],
        ),
        Spacer(1, 7 * mm),
    ]
    return story


def _fit_row(relation: FitRelationDisplay, styles: dict) -> list:
    label = FIT_RELATION_LABELS.get(relation.relation_id.value, relation.relation_id.value)
    return [
        _paragraph(label, styles["body"]),
        _paragraph(
            FIT_STATUS_LABELS.get(relation.status.value, relation.status.value),
            styles["body"],
        ),
        _paragraph(relation.summary, styles["body"]),
        _evidence_paragraph(
            list(relation.left_evidence) + list(relation.right_evidence), styles
        ),
    ]


def _sim_story(report: CaseReport, styles: dict) -> list:
    candidates = report.report.similar_candidates
    story = [Paragraph("유사 공고 비교", styles["heading"])]
    if not candidates:
        # 왜 비었는지 적지 않으면 검색이 실패한 것과 구분되지 않는다.
        story.append(
            _paragraph(
                "비교할 유사 공고를 찾지 못했습니다. 접수 중인 공고 중 비교 대상이 "
                "없었던 경우에도 이렇게 표시됩니다.",
                styles["body"],
            )
        )
        story.append(Spacer(1, 7 * mm))
        return story
    for index, candidate in enumerate(candidates, 1):
        story.append(_sim_candidate(index, candidate, styles))
        story.append(Spacer(1, 5 * mm))
    return story


def _sim_candidate(
    index: int, candidate: ReportSimCandidateDisplay, styles: dict
) -> KeepTogether:
    rows = [_header_row(("비교 축", "상태", "요약", "원문 근거"), styles)]
    for axis_key, label in SIM_AXIS_LABELS.items():
        rows.append(_sim_axis_row(label, getattr(candidate.axes, axis_key), styles))
    return KeepTogether(
        [
            _paragraph(f"{index}. {candidate.title}", styles["subheading"]),
            _paragraph(candidate.source_url, styles["link"]),
            _paragraph(candidate.comparison_summary, styles["body"]),
            Spacer(1, 2 * mm),
            _table(
                rows,
                [26 * mm, 24 * mm, 60 * mm, 64 * mm],
                split_in_row=True,
            ),
        ]
    )


def _sim_axis_row(label: str, axis: ReportSimAxisDisplay, styles: dict) -> list:
    summary = [axis.summary]
    if axis.common_points:
        summary.append("공통: " + " / ".join(axis.common_points))
    if axis.differences:
        summary.append("차이: " + " / ".join(axis.differences))
    return [
        _paragraph(label, styles["body"]),
        _paragraph(SIM_STATUS_LABELS.get(axis.status.value, axis.status.value), styles["body"]),
        _paragraph("\n".join(summary), styles["body"]),
        _evidence_paragraph(
            list(axis.request_evidence) + list(axis.candidate_evidence), styles
        ),
    ]


def _evidence_paragraph(evidence: list[ReportExcerpt], styles: dict) -> Paragraph:
    if not evidence:
        return _paragraph("-", styles["body"])
    return _paragraph(
        "\n\n".join(item.excerpt for item in evidence), styles["excerpt"]
    )


def _header_row(labels: tuple[str, ...], styles: dict) -> list:
    return [_paragraph(label, styles["table_header"]) for label in labels]


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(html.escape(str(value)).replace("\n", "<br/>"), style)


def _styles(font_name: str) -> dict:
    sample = getSampleStyleSheet()
    body = ParagraphStyle(
        "KoreanBody",
        parent=sample["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=13,
    )
    return {
        "body": body,
        "excerpt": ParagraphStyle(
            "KoreanExcerpt",
            parent=body,
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#3D4C5A"),
        ),
        "link": ParagraphStyle(
            "KoreanLink",
            parent=body,
            fontSize=8,
            textColor=colors.HexColor("#2874A6"),
        ),
        "heading": ParagraphStyle(
            "KoreanHeading",
            parent=sample["Heading2"],
            fontName=font_name,
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#17324D"),
            spaceBefore=8,
            spaceAfter=8,
        ),
        "subheading": ParagraphStyle(
            "KoreanSubheading",
            parent=sample["Heading3"],
            fontName=font_name,
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#17324D"),
            spaceBefore=4,
            spaceAfter=4,
        ),
        "title": ParagraphStyle(
            "KoreanTitle",
            parent=sample["Title"],
            fontName=font_name,
            fontSize=20,
            leading=26,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#102A43"),
        ),
        "table_header": ParagraphStyle(
            "KoreanTableHeader",
            parent=body,
            textColor=colors.white,
            alignment=TA_CENTER,
        ),
    }


def _table(
    rows: list[list[object]],
    widths: list[float],
    *,
    split_in_row: bool = False,
) -> Table:
    table = Table(
        rows,
        colWidths=widths,
        repeatRows=1,
        hAlign="LEFT",
        splitInRow=split_in_row,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2874A6")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8C4CE")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7F9")]),
            ]
        )
    )
    return table


def _page_number(canvas, document, font_name: str) -> None:
    canvas.saveState()
    canvas.setFont(font_name, 8)
    canvas.setFillColor(colors.HexColor("#607080"))
    canvas.drawCentredString(A4[0] / 2, 9 * mm, f"{document.page}")
    canvas.restoreState()


def _register_font() -> str:
    if FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return FONT_NAME
    for path in _FONT_CANDIDATES:
        if path.is_file():
            pdfmetrics.registerFont(TTFont(FONT_NAME, path))
            return FONT_NAME
    fallback = "HYSMyeongJo-Medium"
    if fallback not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(fallback))
    return fallback
