from typing import Protocol

from app.schemas.report import ReportJsonV01


class PdfRenderer(Protocol):
    async def render(self, report: ReportJsonV01) -> bytes: ...
