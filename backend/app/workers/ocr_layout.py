"""CPU OCR/layout worker adapter around the handed-off vendor worker.

This adapter never downloads model weights and never invokes PaddleOCR or
EasyOCR. It may import the vendor module's pure geometry helpers
(`geometry_regions`, `build_sidecar`) and probe engine *presence* with
`importlib.util.find_spec`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.models.outcomes import ErrorCode, OutcomeCode
from app.pipelines.availability import module_available, probe_modules
from app.pipelines.hashing import sha256_file

VENDOR_OCR_WORKER = (
    Path(__file__).resolve().parents[2]
    / "vendor"
    / "common_ir_pipeline"
    / "src"
    / "common_ir_pipeline"
    / "workers"
    / "pdf_ocr_layout.py"
)

# OCR is opt-in because PaddleOCR/EasyOCR can fetch weights on a first run.
# The handed-off worker itself is CPU-only (`use_gpu=False`); setting this
# flag only permits an already-provisioned host to launch it.
ENGINE_RUN_ALLOWED = os.getenv("PREREVIEW_ENABLE_OCR", "false").lower() in {"1", "true", "yes"}

OCR_ENGINE_MODULES = ("paddle", "paddleocr", "easyocr", "fitz", "numpy")


@dataclass(frozen=True, slots=True)
class OcrRuntimeProbe:
    worker_source_present: bool
    worker_name: str | None
    worker_version: str | None
    sidecar_schema_version: str | None
    engines: dict[str, bool]
    engine_run_allowed: bool = ENGINE_RUN_ALLOWED


@dataclass(frozen=True, slots=True)
class OcrLayoutResult:
    outcome: OutcomeCode
    review_status: OutcomeCode
    error_code: str | None
    message: str
    sidecar: dict[str, Any] | None = None
    probe: OcrRuntimeProbe | None = None


def _load_vendor_worker() -> Any | None:
    if not VENDOR_OCR_WORKER.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "prereview_vendor_pdf_ocr_layout",
        VENDOR_OCR_WORKER,
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_ocr_runtime() -> OcrRuntimeProbe:
    module = _load_vendor_worker()
    engines = {item.name: item.available for item in probe_modules(*OCR_ENGINE_MODULES)}
    return OcrRuntimeProbe(
        worker_source_present=module is not None,
        worker_name=getattr(module, "WORKER_NAME", None) if module else None,
        worker_version=getattr(module, "WORKER_VERSION", None) if module else None,
        sidecar_schema_version=getattr(module, "SIDECAR_SCHEMA_VERSION", None) if module else None,
        engines=engines,
        engine_run_allowed=ENGINE_RUN_ALLOWED,
    )


class OcrLayoutWorkerAdapter:
    """Contract around common-ir-pdf-ocr-layout with engines disabled."""

    def probe(self) -> OcrRuntimeProbe:
        return probe_ocr_runtime()

    def run(
        self,
        pdf_path: str | Path,
        *,
        engine: str = "both",
        pages: str | None = None,
        scale: float = 1.5,
    ) -> OcrLayoutResult:
        probe = self.probe()
        if engine not in {"paddle", "easy", "both"}:
            return OcrLayoutResult(
                outcome=OutcomeCode.FAILED,
                review_status=OutcomeCode.NEEDS_REVIEW,
                error_code=ErrorCode.UNSUPPORTED_FORMAT,
                message=f"unsupported OCR engine: {engine}",
                probe=probe,
            )
        if not Path(pdf_path).is_file():
            return OcrLayoutResult(
                outcome=OutcomeCode.FAILED,
                review_status=OutcomeCode.NEEDS_REVIEW,
                error_code=ErrorCode.UNSUPPORTED_FORMAT,
                message=f"PDF does not exist: {pdf_path}",
                probe=probe,
            )
        if not ENGINE_RUN_ALLOWED:
            return OcrLayoutResult(
                outcome=OutcomeCode.UNAVAILABLE,
                review_status=OutcomeCode.NEEDS_REVIEW,
                error_code=ErrorCode.OCR_EXECUTION_DISABLED,
                message=(
                    "OCR engine execution is disabled; this adapter does not download "
                    "weights or run PaddleOCR/EasyOCR"
                ),
                probe=probe,
            )
        required = {"fitz", "numpy"}
        if engine in {"paddle", "both"}:
            required.update({"paddle", "paddleocr"})
        if engine in {"easy", "both"}:
            required.add("easyocr")
        missing = [name for name in sorted(required) if not probe.engines.get(name, False)]
        if missing:
            return OcrLayoutResult(
                outcome=OutcomeCode.UNAVAILABLE,
                review_status=OutcomeCode.NEEDS_REVIEW,
                error_code=ErrorCode.OCR_RUNTIME_UNAVAILABLE,
                message=f"OCR runtime unavailable: {', '.join(missing)}",
                probe=probe,
            )
        with tempfile.TemporaryDirectory(prefix="prereview-ocr-") as directory:
            output = Path(directory) / "layout.json"
            command = [
                sys.executable,
                str(VENDOR_OCR_WORKER),
                "--pdf",
                str(Path(pdf_path).resolve()),
                "--output",
                str(output),
                "--engine",
                engine,
                "--scale",
                str(scale),
            ]
            if pages:
                command.extend(["--pages", pages])
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                return OcrLayoutResult(
                    outcome=OutcomeCode.FAILED,
                    review_status=OutcomeCode.NEEDS_REVIEW,
                    error_code=ErrorCode.OCR_RUNTIME_UNAVAILABLE,
                    message="OCR layout worker exceeded its 300-second timeout",
                    probe=probe,
                )
            if completed.returncode != 0 or not output.is_file():
                return OcrLayoutResult(
                    outcome=OutcomeCode.FAILED,
                    review_status=OutcomeCode.NEEDS_REVIEW,
                    error_code=ErrorCode.OCR_RUNTIME_UNAVAILABLE,
                    message="OCR layout worker failed; inspect worker logs on the provisioned host",
                    probe=probe,
                )
            try:
                sidecar = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return OcrLayoutResult(
                    outcome=OutcomeCode.FAILED,
                    review_status=OutcomeCode.NEEDS_REVIEW,
                    error_code=ErrorCode.OCR_RUNTIME_UNAVAILABLE,
                    message="OCR layout worker returned an invalid sidecar",
                    probe=probe,
                )
        if '"text"' in _stable_dump(sidecar):
            return OcrLayoutResult(
                outcome=OutcomeCode.FAILED,
                review_status=OutcomeCode.NEEDS_REVIEW,
                error_code=ErrorCode.OCR_EXECUTION_DISABLED,
                message="OCR sidecar contained forbidden recognized text",
                sidecar=sidecar,
                probe=probe,
            )
        return OcrLayoutResult(
            outcome=OutcomeCode.SUCCEEDED,
            review_status=OutcomeCode.SUCCEEDED,
            error_code=None,
            message="CPU OCR/layout sidecar generated without persisting OCR text",
            sidecar=sidecar,
            probe=probe,
        )

    def materialize_textless_sidecar(
        self,
        pdf_path: str | Path,
        pages: list[dict[str, Any]],
        *,
        page_count: int,
        render_scale: float = 1.5,
        engine_records: dict[str, Iterable[object]] | None = None,
    ) -> OcrLayoutResult:
        """Build a textless sidecar from already-sanitized geometry.

        `engine_records` are passed through the vendor `geometry_regions`
        helper, which discards recognized strings. No OCR model is invoked.
        """
        probe = self.probe()
        module = _load_vendor_worker()
        if module is None:
            return OcrLayoutResult(
                outcome=OutcomeCode.UNAVAILABLE,
                review_status=OutcomeCode.NEEDS_REVIEW,
                error_code=ErrorCode.OCR_RUNTIME_UNAVAILABLE,
                message="handed-off OCR worker source is not present",
                probe=probe,
            )
        source = Path(pdf_path)
        built_pages = list(pages)
        if engine_records:
            regions: list[dict[str, Any]] = []
            for engine, records in engine_records.items():
                for region in module.geometry_regions(engine, records):
                    regions.append({"engine": engine, **region})
            if built_pages:
                built_pages[0] = {**built_pages[0], "regions": regions}
            else:
                built_pages.append(
                    {
                        "page": 1,
                        "image_size": {"width": 1, "height": 1},
                        "render_seconds": 0.0,
                        "total_seconds": 0.0,
                        "engines": {engine: {"seconds": 0.0, "region_count": 0} for engine in engine_records},
                        "regions": regions,
                    }
                )
        sidecar = module.build_sidecar(source, page_count, render_scale, built_pages)
        if sidecar.get("source", {}).get("source_sha256") != sha256_file(source):
            return OcrLayoutResult(
                outcome=OutcomeCode.FAILED,
                review_status=OutcomeCode.NEEDS_REVIEW,
                error_code=ErrorCode.SOURCE_HASH_MISMATCH,
                message="sidecar source hash does not match PDF",
                sidecar=sidecar,
                probe=probe,
            )
        rendered = _stable_dump(sidecar)
        if '"text"' in rendered:
            return OcrLayoutResult(
                outcome=OutcomeCode.FAILED,
                review_status=OutcomeCode.NEEDS_REVIEW,
                error_code=ErrorCode.OCR_EXECUTION_DISABLED,
                message="sidecar persisted OCR text, which is forbidden",
                sidecar=sidecar,
                probe=probe,
            )
        return OcrLayoutResult(
            outcome=OutcomeCode.SUCCEEDED,
            review_status=OutcomeCode.SUCCEEDED,
            error_code=None,
            message="textless layout sidecar materialized without OCR engine run",
            sidecar=sidecar,
            probe=probe,
        )


def _stable_dump(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def engines_were_imported() -> bool:
    """Test helper: engine modules must not be imported by probing."""
    import sys

    return any(name in sys.modules for name in ("paddleocr", "easyocr", "paddle"))


# Presence of fitz/numpy is allowed only if the host already had them; this
# adapter must not import them itself during probe().
assert not module_available("this_module_does_not_exist")
