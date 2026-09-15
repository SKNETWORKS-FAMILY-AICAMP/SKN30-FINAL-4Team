"""Run optional CPU OCR only to create a textless PDF layout sidecar.

This worker is intentionally *not* a Common IR adapter.  OCR recognition is
used to locate rendered text regions and measure page-level work, but the
recognized strings are discarded before any artifact is written.  Its output
therefore cannot become Common IR semantic text, a CandidatePack text basis,
an exact span, or a Structured Profile ``value_raw``.

The sidecar is useful for selecting pages/regions for visual inspection or a
separate, explicitly audited table/diagram layout pipeline.  It does not
infer table cells, row/column relations, diagram edges, or business facts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Iterable


WORKER_NAME = "common_ir_pdf_ocr_layout_diagnostic"
WORKER_VERSION = "1.0.0"
SIDECAR_SCHEMA_VERSION = "common_ir_v1_pdf_ocr_layout_diagnostic_v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bbox_from_polygon(polygon: Iterable[Iterable[float]]) -> list[float]:
    points = [[float(x), float(y)] for x, y in polygon]
    if len(points) < 2:
        raise ValueError("OCR geometry needs at least two polygon points")
    xs, ys = zip(*points)
    return [min(xs), min(ys), max(xs), max(ys)]


def geometry_regions(engine: str, results: Iterable[object]) -> list[dict]:
    """Convert engine records into textless geometry.

    Accepted records are ``(polygon, text, confidence)`` for EasyOCR or
    ``(polygon, (text, confidence))`` for PaddleOCR.  The text element is
    purposefully read only to unpack the engine result and is never returned
    or logged.  This pure function permits tests with fakes and makes the
    no-OCR-text invariant independently testable without heavyweight models.
    """
    regions: list[dict] = []
    for index, record in enumerate(results):
        if not isinstance(record, (list, tuple)) or len(record) != 2 and len(record) != 3:
            raise ValueError(f"unsupported {engine} OCR record at index {index}")
        polygon = record[0]
        payload = record[1] if len(record) == 2 else (record[1], record[2])
        if isinstance(payload, (list, tuple)) and len(payload) == 2:
            # Paddle: (polygon, (text, confidence)); Easy: (polygon, text, confidence)
            confidence = payload[1]
        else:
            raise ValueError(f"unsupported {engine} OCR payload at index {index}")
        regions.append({
            "region_id": f"layout:{engine}:{index}",
            "bbox": _bbox_from_polygon(polygon),
            "confidence": round(float(confidence), 6),
        })
    return regions


def _dedupe_regions(regions_by_engine: dict[str, list[dict]]) -> list[dict]:
    """Keep engine-specific geometry; no text-based matching is performed."""
    return [
        {"engine": engine, **region}
        for engine, regions in regions_by_engine.items()
        for region in regions
    ]


def build_sidecar(source_pdf: Path, page_count: int, render_scale: float, pages: list[dict]) -> dict:
    """Build a schema-stable, textless sidecar from already-sanitized pages."""
    source_pdf = Path(source_pdf)
    return {
        "sidecar_schema_version": SIDECAR_SCHEMA_VERSION,
        "policy": {
            "semantic_text_source": "native_pdf_only",
            "ocr_text_persisted": False,
            "ocr_text_allowed_in_common_ir": False,
            "purpose": "layout_diagnostic_only",
            "does_not_infer": ["table_cells", "row_column_relations", "diagram_edges", "business_facts"],
        },
        "source": {
            "source_location": str(source_pdf),
            "source_sha256": sha256_file(source_pdf),
            "page_count": page_count,
        },
        "worker": {
            "name": WORKER_NAME,
            "version": WORKER_VERSION,
            "render_scale": render_scale,
            "coordinate_space": "rendered_page_px",
        },
        "pages": pages,
    }


def _parse_pages(raw: str | None, page_count: int) -> list[int]:
    if raw is None:
        return list(range(1, page_count + 1))
    selected: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            page = int(token)
        except ValueError as error:
            raise ValueError("--pages must be comma-separated 1-based integers") from error
        if not 1 <= page <= page_count:
            raise ValueError(f"page {page} is outside 1..{page_count}")
        selected.add(page)
    if not selected:
        raise ValueError("--pages selected no pages")
    return sorted(selected)


def _load_runtime(engine: str):
    try:
        import fitz  # PyMuPDF
        import numpy as np
    except ImportError as error:
        raise RuntimeError("OCR layout worker requires the optional [ocr] dependencies; install with `uv pip install -e '.[ocr]'`.") from error

    models = {}
    if engine in ("paddle", "both"):
        try:
            import paddle  # noqa: F401 -- explicit runtime availability check
        except ImportError as error:
            raise RuntimeError(
                "PaddlePaddle CPU runtime is unavailable; install the optional [ocr] dependencies "
                "or install a platform-compatible PaddlePaddle build before running PaddleOCR."
            ) from error
        try:
            from paddleocr import PaddleOCR
        except ImportError as error:
            raise RuntimeError("PaddleOCR is unavailable; install the optional [ocr] dependencies after PaddlePaddle.") from error
        models["paddle"] = PaddleOCR(use_angle_cls=True, lang="korean", use_gpu=False, show_log=False)
    if engine in ("easy", "both"):
        try:
            from easyocr import Reader
        except ImportError as error:
            raise RuntimeError("EasyOCR is unavailable; install the optional [ocr] dependencies.") from error
        models["easy"] = Reader(["ko", "en"], gpu=False)
    return fitz, np, models


def _paddle_records(model, image):
    return (model.ocr(image, cls=True)[0] or [])


def _easy_records(model, image):
    return model.readtext(image, detail=1, paragraph=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True, help="source PDF; never modified")
    parser.add_argument("--output", type=Path, required=True, help="textless layout diagnostic JSON")
    parser.add_argument("--engine", choices=("paddle", "easy", "both"), default="both")
    parser.add_argument("--pages", help="comma-separated 1-based pages; defaults to all pages")
    parser.add_argument("--scale", type=float, default=1.5, help="render scale in rendered_page_px")
    args = parser.parse_args()
    if args.scale <= 0:
        parser.error("--scale must be positive")
    if not args.pdf.is_file():
        parser.error(f"PDF does not exist: {args.pdf}")

    try:
        fitz, np, models = _load_runtime(args.engine)
        document = fitz.open(args.pdf)
        selected_pages = _parse_pages(args.pages, len(document))
    except (RuntimeError, ValueError) as error:
        parser.error(str(error))

    pages: list[dict] = []
    matrix = fitz.Matrix(args.scale, args.scale)
    for page_number in selected_pages:
        page_started = time.perf_counter()
        page = document[page_number - 1]
        render_started = time.perf_counter()
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
        if pixmap.n == 4:
            image = image[:, :, :3]
        render_seconds = round(time.perf_counter() - render_started, 6)
        engine_results: dict[str, list[dict]] = {}
        engine_metrics: dict[str, dict] = {}
        for engine, model in models.items():
            started = time.perf_counter()
            records = _paddle_records(model, image) if engine == "paddle" else _easy_records(model, image)
            engine_metrics[engine] = {
                "seconds": round(time.perf_counter() - started, 6),
                "region_count": len(records),
            }
            engine_results[engine] = geometry_regions(engine, records)
        pages.append({
            "page": page_number,
            "image_size": {"width": pixmap.width, "height": pixmap.height},
            "render_seconds": render_seconds,
            "total_seconds": round(time.perf_counter() - page_started, 6),
            "engines": engine_metrics,
            "regions": _dedupe_regions(engine_results),
        })
    sidecar = build_sidecar(args.pdf, len(document), args.scale, pages)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "pages": len(pages), "ocr_text_persisted": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
