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
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.schemas.report import ReportJsonV01


FONT_NAME = "PreReviewKorean"
_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
)


class ReportLabPdfRenderer:
    async def render(self, report: ReportJsonV01) -> bytes:
        return await asyncio.to_thread(_render, report)


def _render(report: ReportJsonV01) -> bytes:
    font_name = _register_font()

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
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "KoreanBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=13,
    )
    heading = ParagraphStyle(
        "KoreanHeading",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#17324D"),
        spaceBefore=8,
        spaceAfter=8,
    )
    title = ParagraphStyle(
        "KoreanTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=20,
        leading=26,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#102A43"),
    )
    table_header = ParagraphStyle(
        "KoreanTableHeader",
        parent=body,
        fontName=font_name,
        textColor=colors.white,
        alignment=TA_CENTER,
    )

    story = [
        Paragraph("Pre-review 분석 보고서", title),
        Spacer(1, 6 * mm),
        _paragraph(f"분석 문서: {report.case.title}", body),
        _paragraph(f"완료 시각: {report.case.completed_at.isoformat()}", body),
        _paragraph(f"보고서 스키마: {report.schema_version}", body),
        Spacer(1, 5 * mm),
        Paragraph("자체 점검 (CPL)", heading),
        _paragraph(
            f"확인 {report.self_check.confirmed_count}/{report.self_check.total_count} "
            f"({report.self_check.confirmation_rate:.1f}%)",
            body,
        ),
        _table(
            [[_paragraph("항목", table_header), _paragraph("상태", table_header), _paragraph("설명", table_header)]]
            + [
                [
                    _paragraph(item.field_code, body),
                    _paragraph(item.status.value, body),
                    _paragraph(item.explanation or item.reason_code or "-", body),
                ]
                for item in report.self_check.items
            ],
            [45 * mm, 35 * mm, 94 * mm],
        ),
        PageBreak(),
        Paragraph("구조적 정합성 (FIT)", heading),
        _paragraph(
            "모듈 상태: " + report.structural_consistency.module_status,
            body,
        ),
        _paragraph(
            "점수: "
            + (
                f"{report.structural_consistency.score.value:.1f}"
                if report.structural_consistency.score.value is not None
                else "판단 불가"
            )
            + f" / 판단 가능 {report.structural_consistency.score.assessable_count}/7",
            body,
        ),
        _table(
            [[_paragraph("관계", table_header), _paragraph("상태", table_header), _paragraph("요약", table_header)]]
            + [
                [
                    _paragraph(item.relation_id.value, body),
                    _paragraph(item.status.value, body),
                    _paragraph(item.summary, body),
                ]
                for item in report.structural_consistency.relations
            ],
            [28 * mm, 35 * mm, 111 * mm],
        ),
        Spacer(1, 6 * mm),
        Paragraph("유사사업 후보 (SIM)", heading),
    ]
    if report.similar_candidates:
        story.append(
            _table(
                [[_paragraph("순위", table_header), _paragraph("공고", table_header), _paragraph("유사도", table_header), _paragraph("검토 등급", table_header)]]
                + [
                    [
                        _paragraph(str(item.rank), body),
                        _paragraph(item.title, body),
                        _paragraph(str(item.semantic_similarity_display), body),
                        _paragraph(item.review_grade.value, body),
                    ]
                    for item in report.similar_candidates
                ],
                [18 * mm, 92 * mm, 28 * mm, 36 * mm],
            )
        )
    else:
        story.append(_paragraph("유사사업 후보가 없습니다.", body))

    story.extend([Spacer(1, 6 * mm), Paragraph("검토 쟁점", heading)])
    if report.review_issues:
        for issue in report.review_issues:
            story.append(
                _paragraph(
                    f"[{issue.source}/{issue.status}] {issue.summary}",
                    body,
                )
            )
    else:
        story.append(_paragraph("추가 검토 쟁점이 없습니다.", body))

    if report.warnings:
        story.extend([Spacer(1, 6 * mm), Paragraph("실행 경고", heading)])
        story.extend(_paragraph(f"- {warning}", body) for warning in report.warnings)

    document.build(
        story,
        onFirstPage=lambda canvas, doc: _page_number(canvas, doc, font_name),
        onLaterPages=lambda canvas, doc: _page_number(canvas, doc, font_name),
    )
    return output.getvalue()


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(html.escape(str(value)).replace("\n", "<br/>"), style)


def _table(rows: list[list[object]], widths: list[float]) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
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
