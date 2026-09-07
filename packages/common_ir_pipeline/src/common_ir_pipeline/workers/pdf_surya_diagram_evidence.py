"""Create a SHA-bound Surya Diagram HTML sidecar for explicit-edge checking.

This is a diagnostic artifact, not Common IR.  It retains HTML only for
literal-arrow verification by the explicit diagram-edge promoter.  The HTML
is never projected as Common IR semantic text or CandidatePack input.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import time
from pathlib import Path
from typing import Any

from .pdf_surya_layout import _configure_explicit_endpoint, _load_runtime, _parse_pages, sha256_file


def dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if dataclasses.is_dataclass(value):
        return dump(dataclasses.asdict(value))
    if isinstance(value, list):
        return [dump(item) for item in value]
    if isinstance(value, dict):
        return {key: dump(item) for key, item in value.items()}
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pages", help="comma-separated 1-based pages; default: all")
    parser.add_argument("--scale", type=float, default=2.0)
    args = parser.parse_args()
    if not args.pdf.is_file() or args.scale <= 0:
        parser.error("--pdf must exist and --scale must be positive")
    endpoint = _configure_explicit_endpoint()
    try:
        fitz, image_cls, manager_cls, _layout_predictor = _load_runtime()
        import surya.recognition as recognition
        from surya.recognition import RecognitionPredictor
    except ImportError as error:
        raise SystemExit("Surya Diagram evidence requires the [surya] environment.") from error
    document = fitz.open(args.pdf)
    pages_to_scan = _parse_pages(args.pages, len(document))
    # Surya normally skips Diagram blocks.  This process-local override only
    # exposes raw Diagram HTML to the conservative explicit-edge checker.
    recognition.SKIP_CANON_LABELS.discard("Diagram")
    manager = manager_cls()
    predictor = RecognitionPredictor(manager)
    matrix = fitz.Matrix(args.scale, args.scale)
    pages = []
    started = time.perf_counter()
    for page_number in pages_to_scan:
        page_started = time.perf_counter()
        pixmap = document[page_number - 1].get_pixmap(matrix=matrix, alpha=False)
        mode = "RGB" if pixmap.n == 3 else "RGBA"
        image = image_cls.frombytes(mode, [pixmap.width, pixmap.height], pixmap.samples).convert("RGB")
        result = dump(predictor([image], full_page=True)[0])
        blocks = [{"label": block.get("label"), "bbox": block.get("bbox"), "html": block.get("html", "")} for block in result.get("blocks", []) if block.get("label") == "Diagram"]
        pages.append({"page": page_number, "image_size": {"width": pixmap.width, "height": pixmap.height}, "high_accuracy_seconds": round(time.perf_counter() - page_started, 6), "high_accuracy_result": {"blocks": blocks}})
    sidecar = {"sidecar_schema_version": "common_ir_v1_pdf_surya_diagram_evidence_v1", "source": {"source_location": str(args.pdf), "source_sha256": sha256_file(args.pdf), "page_count": len(document)}, "worker": {"name": "common_ir_pdf_surya_diagram_evidence", "version": "1.0.0", "surya_package": "surya-ocr==0.22.1", "inference_endpoint": endpoint, "render_scale": args.scale, "coordinate_space": "rendered_page_px"}, "policy": {"semantic_text_source": "native_pdf_only", "diagram_html_purpose": "explicit_arrow_validation_only", "diagram_html_allowed_in_common_ir_text": False, "does_not_infer": ["llm_candidate_edges", "ambiguous_edges", "business_facts"]}, "pages": pages, "total_seconds": round(time.perf_counter() - started, 6)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "pages": len(pages), "diagram_blocks": sum(len(page["high_accuracy_result"]["blocks"]) for page in pages)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
