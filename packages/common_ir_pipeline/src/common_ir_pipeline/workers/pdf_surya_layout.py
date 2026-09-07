"""Run the optional Surya GPU layout/structure diagnostic worker for a PDF.

This worker has two deliberately narrow modes:

* ``layout`` (default): render selected pages and preserve Surya layout block
  labels and geometry.
* ``block``: additionally send selected layout crops through Surya block
  recognition to measure that route.  Recognition output is discarded before
  the sidecar is written; only a success flag and duration remain.

The output is a SHA-bound, textless diagnostic sidecar.  It is not Common IR
by itself.  ``common-ir-pdf-native --ocr-layout-diagnostic`` may project it
only as blank ``layout_candidate`` blocks.  Therefore Surya text can never
become Common IR semantic text, a CandidatePack text basis, an exact span, or
a Structured Profile ``value_raw``.

No table cells, row/column relations, diagram edges, or business facts are
inferred here.  The worker is intentionally optional because it needs a
GPU-compatible Torch/CUDA deployment and downloads Surya model weights on
first use.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


WORKER_NAME = "common_ir_pdf_surya_layout_diagnostic"
WORKER_VERSION = "1.0.0"
SIDECAR_SCHEMA_VERSION = "common_ir_v1_pdf_surya_layout_diagnostic_v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _value(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _point(point: Any) -> list[float]:
    if isinstance(point, dict):
        x, y = point.get("x"), point.get("y")
    else:
        x, y = point[0], point[1]
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        raise ValueError("Surya polygon contains a non-numeric point")
    return [float(x), float(y)]


def _bbox_from_polygon(polygon: Any) -> list[float]:
    points = [_point(point) for point in polygon]
    if len(points) < 2:
        raise ValueError("Surya layout block has no usable polygon")
    xs, ys = zip(*points)
    return [min(xs), min(ys), max(xs), max(ys)]


def _block_bbox(block: Any) -> list[float]:
    polygon = _value(block, "polygon")
    if polygon:
        return _bbox_from_polygon(polygon)
    bbox = _value(block, "bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
        return [float(value) for value in bbox]
    raise ValueError("Surya layout block has neither polygon nor numeric bbox")


def layout_regions(layout: Any) -> list[dict]:
    """Return textless layout geometry from a Surya layout result.

    The helper deliberately ignores any recognizer output a runtime object may
    carry.  Keeping it pure allows a no-text invariant test without models.
    """
    boxes = _value(layout, "bboxes", []) or []
    regions: list[dict] = []
    for index, block in enumerate(boxes):
        label = _value(block, "label", "Unknown")
        regions.append({
            "region_id": f"surya-block-{index}",
            "label": str(label),
            "bbox": _block_bbox(block),
        })
    return regions


def build_sidecar(source_pdf: Path, page_count: int, render_scale: float, pages: list[dict], mode: str) -> dict:
    return {
        "sidecar_schema_version": SIDECAR_SCHEMA_VERSION,
        "policy": {
            "semantic_text_source": "native_pdf_only",
            "surya_text_persisted": False,
            "surya_text_allowed_in_common_ir": False,
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
            "surya_package": "surya-ocr==0.22.1",
            "mode": mode,
            "render_scale": render_scale,
            "coordinate_space": "rendered_page_px",
        },
        "pages": pages,
    }


def _parse_pages(raw: str | None, page_count: int) -> list[int]:
    if raw is None:
        return list(range(1, page_count + 1))
    pages: set[int] = set()
    for token in raw.split(","):
        try:
            page = int(token.strip())
        except ValueError as error:
            raise ValueError("--pages must be comma-separated 1-based integers") from error
        if not 1 <= page <= page_count:
            raise ValueError(f"page {page} is outside 1..{page_count}")
        pages.add(page)
    if not pages:
        raise ValueError("--pages selected no pages")
    return sorted(pages)


def _parse_targets(raw_targets: list[str], selected_pages: set[int]) -> dict[int, set[int]]:
    targets: dict[int, set[int]] = {}
    for raw in raw_targets:
        try:
            page_raw, block_raw = raw.split(":", 1)
            page, block = int(page_raw), int(block_raw)
        except ValueError as error:
            raise ValueError("--target must be PAGE:BLOCK_INDEX, e.g. --target 2:3") from error
        if page not in selected_pages or block < 0:
            raise ValueError("--target page must be selected by --pages and block index must be non-negative")
        targets.setdefault(page, set()).add(block)
    return targets


def _load_runtime():
    try:
        import fitz
        from PIL import Image
        from surya.inference import SuryaInferenceManager
        from surya.layout import LayoutPredictor
    except ImportError as error:
        raise RuntimeError(
            "Surya worker requires the optional [surya] dependencies and a GPU-compatible Torch deployment; "
            "see scripts/setup_surya_gpu.sh and README.md."
        ) from error
    return fitz, Image, SuryaInferenceManager, LayoutPredictor


def _configure_explicit_endpoint() -> str:
    """Require a caller-managed Surya/vLLM endpoint.

    This package must never start Docker, vLLM, or a local model server as an
    incidental effect of scanning a PDF.  Surya's client reads these settings
    from the environment before ``SuryaInferenceManager`` is created.
    """
    url = os.environ.get("SURYA_INFERENCE_URL", "").strip().rstrip("/")
    if not url:
        raise RuntimeError(
            "SURYA_INFERENCE_URL is required (an already-running, caller-managed Surya/vLLM endpoint); "
            "this worker never autostarts a server."
        )
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    if os.environ.get("SURYA_INFERENCE_AUTOSTART", "false").lower() not in {"false", "0", "no"}:
        raise RuntimeError("SURYA_INFERENCE_AUTOSTART must be false; this worker never starts an inference server")
    os.environ["SURYA_INFERENCE_URL"] = url
    os.environ["SURYA_INFERENCE_BACKEND"] = os.environ.get("SURYA_INFERENCE_BACKEND", "vllm")
    os.environ["SURYA_INFERENCE_AUTOSTART"] = "false"
    return url


def _render_page(page: Any, image_cls: Any, scale: float):
    pixmap = page.get_pixmap(matrix=page.parent.Matrix(scale, scale) if hasattr(page.parent, "Matrix") else None, alpha=False)
    # PyMuPDF's Matrix lives on the module, not document/page; this fallback
    # only makes a fake page testable.  The real path is set by main below.
    mode = "RGB" if pixmap.n == 3 else "RGBA"
    image = image_cls.frombytes(mode, [pixmap.width, pixmap.height], pixmap.samples)
    return image.convert("RGB"), pixmap.width, pixmap.height


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True, help="source PDF; never modified")
    parser.add_argument("--output", type=Path, required=True, help="textless Surya layout diagnostic JSON")
    parser.add_argument("--pages", help="comma-separated 1-based pages; defaults to all pages")
    parser.add_argument("--scale", type=float, default=1.5, help="PDF render scale in rendered_page_px")
    parser.add_argument("--mode", choices=("layout", "block"), default="layout")
    parser.add_argument("--target", action="append", default=[], help="for --mode block: PAGE:BLOCK_INDEX; repeatable, defaults to all blocks")
    parser.add_argument("--max-tokens", type=int, default=1024, help="for --mode block only; recognition output is discarded")
    args = parser.parse_args()
    if args.scale <= 0 or args.max_tokens <= 0:
        parser.error("--scale and --max-tokens must be positive")
    if not args.pdf.is_file():
        parser.error(f"PDF does not exist: {args.pdf}")
    try:
        endpoint = _configure_explicit_endpoint()
        fitz, image_cls, manager_cls, predictor_cls = _load_runtime()
        document = fitz.open(args.pdf)
        selected_pages = _parse_pages(args.pages, len(document))
        targets = _parse_targets(args.target, set(selected_pages))
    except (RuntimeError, ValueError) as error:
        parser.error(str(error))

    manager = manager_cls()
    predictor = predictor_cls(manager)
    pages: list[dict] = []
    matrix = fitz.Matrix(args.scale, args.scale)
    for page_number in selected_pages:
        started = time.perf_counter()
        page = document[page_number - 1]
        render_started = time.perf_counter()
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        mode = "RGB" if pixmap.n == 3 else "RGBA"
        image = image_cls.frombytes(mode, [pixmap.width, pixmap.height], pixmap.samples).convert("RGB")
        render_seconds = round(time.perf_counter() - render_started, 6)
        layout_started = time.perf_counter()
        layout = predictor([image])[0]
        regions = layout_regions(layout)
        page_record: dict = {
            "page": page_number,
            "image_size": {"width": pixmap.width, "height": pixmap.height},
            "render_seconds": render_seconds,
            "layout_seconds": round(time.perf_counter() - layout_started, 6),
            "regions": regions,
        }
        if args.mode == "block":
            try:
                from surya.inference.schema import BatchInputItem
                from surya.recognition import _crop_block
            except ImportError as error:
                raise SystemExit("Installed Surya runtime does not expose block recognition APIs required by --mode block") from error
            boxes = _value(layout, "bboxes", []) or []
            chosen = targets.get(page_number, set(range(len(boxes))))
            scan_records = []
            for index in sorted(chosen):
                if index >= len(boxes):
                    raise SystemExit(f"--target {page_number}:{index} is outside detected layout blocks (0..{len(boxes)-1})")
                crop = _crop_block(image, _value(boxes[index], "polygon"))
                block_started = time.perf_counter()
                manager.generate([BatchInputItem(image=crop, prompt_type="block", max_tokens=args.max_tokens)])
                # Deliberately do not capture or serialize the returned text/HTML.
                scan_records.append({"region_id": f"surya-block-{index}", "recognition_seconds": round(time.perf_counter() - block_started, 6), "completed": True})
            page_record["block_scan"] = {"attempted": len(scan_records), "records": scan_records}
        page_record["total_seconds"] = round(time.perf_counter() - started, 6)
        pages.append(page_record)
    sidecar = build_sidecar(args.pdf, len(document), args.scale, pages, args.mode)
    sidecar["worker"]["inference_endpoint"] = endpoint
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "pages": len(pages), "mode": args.mode, "surya_text_persisted": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
