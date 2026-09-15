"""Worker adapters. Model calls go through the noop gateway; OCR engines stay off."""

from app.models.gateway import NoopModelGateway
from app.workers.ocr_layout import (
    ENGINE_RUN_ALLOWED,
    OcrLayoutResult,
    OcrLayoutWorkerAdapter,
    OcrRuntimeProbe,
    probe_ocr_runtime,
)

__all__ = [
    "ENGINE_RUN_ALLOWED",
    "NoopModelGateway",
    "OcrLayoutResult",
    "OcrLayoutWorkerAdapter",
    "OcrRuntimeProbe",
    "probe_ocr_runtime",
]
