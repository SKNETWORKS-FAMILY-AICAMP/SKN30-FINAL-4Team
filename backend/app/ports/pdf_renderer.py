from typing import Protocol

from app.schemas.report import CaseReport


class PdfRenderer(Protocol):
    async def render(self, report: CaseReport) -> bytes:
        """Render the same projection the result screen receives."""
