"""One report job: validate projection, render PDF, store immutable bytes."""

from __future__ import annotations

from hashlib import sha256
from typing import Protocol
from uuid import UUID

from worker.runtime import ClaimedJob

from .contracts import ReportPayloadV1
from .renderer import PersistentChromiumRenderer


PDF_MIME_TYPE = "application/pdf"
REPORT_BUCKET = "analysis-reports"
DEFAULT_REPORT_MAX_BYTES = 25 * 1024 * 1024


class ReportStorage(Protocol):
    def put_if_absent(
        self, *, bucket: str, object_key: str, content: bytes, content_type: str
    ) -> bool: ...


class ReportJobHandler:
    def __init__(
        self,
        *,
        renderer: PersistentChromiumRenderer,
        storage: ReportStorage,
        max_pdf_bytes: int = DEFAULT_REPORT_MAX_BYTES,
    ) -> None:
        if max_pdf_bytes < 1:
            raise ValueError("max_pdf_bytes must be positive")
        self._renderer = renderer
        self._storage = storage
        self._max_pdf_bytes = max_pdf_bytes

    def handle(self, job: ClaimedJob) -> dict[str, object]:
        payload = ReportPayloadV1.from_projection(
            result_payload=job.payload.get("result_payload"),
            sim_details=job.payload.get("sim_details"),
            source_analysis_run_id=job.payload.get("source_analysis_run_id"),
        )
        owner_id = UUID(str(job.payload.get("owner_id")))
        claim_case_id = UUID(str(job.payload.get("analysis_case_id")))
        case_id = UUID(str(payload.case.analysis_case_id))
        if case_id != claim_case_id:
            raise ValueError("report payload case does not match the claimed job")
        pdf = self._renderer.render(payload)
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("renderer did not return a PDF document")
        if len(pdf) > self._max_pdf_bytes:
            raise ValueError("rendered PDF exceeds the configured byte limit")
        digest = sha256(pdf).hexdigest()
        object_key = str(job.payload.get("storage_object_key") or "")
        expected_object_key = (
            f"{owner_id}/{case_id}/pdf/{job.processing_run_pk}.pdf"
        )
        if object_key != expected_object_key:
            raise ValueError("claimed report storage object key is invalid")
        self._storage.put_if_absent(
            bucket=REPORT_BUCKET,
            object_key=object_key,
            content=pdf,
            content_type=PDF_MIME_TYPE,
        )
        return {
            "storage_bucket": REPORT_BUCKET,
            "storage_object_key": object_key,
            "content_sha256": digest,
            "mime_type": PDF_MIME_TYPE,
            "size_bytes": len(pdf),
        }
