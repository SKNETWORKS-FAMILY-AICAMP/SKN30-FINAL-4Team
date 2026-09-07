"""Render a PDF and write the coordinate manifest used by visual workers.

Rendered images are diagnostic artifacts, never semantic Common IR text.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scale", type=float, default=2.0)
    args = parser.parse_args()
    if not args.pdf.is_file() or args.scale <= 0:
        parser.error("--pdf must exist and --scale must be positive")
    try:
        import fitz
    except ImportError as error:
        raise SystemExit("PDF rendering requires PyMuPDF; install common-ir-pipeline[surya] or PyMuPDF.") from error
    args.output_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(args.pdf)
    started = time.perf_counter()
    pages = []
    matrix = fitz.Matrix(args.scale, args.scale)
    for index, page in enumerate(document):
        page_started = time.perf_counter()
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        path = args.output_dir / f"page_{index + 1:04d}.png"
        pixmap.save(path)
        pages.append({"page": index + 1, "path": str(path), "width": pixmap.width, "height": pixmap.height, "render_seconds": round(time.perf_counter() - page_started, 6)})
    manifest = {"source_pdf": str(args.pdf), "renderer": "pymupdf", "render_scale": args.scale, "dpi_equivalent": round(72 * args.scale, 4), "manual_crop": False, "pages": pages, "total_render_seconds": round(time.perf_counter() - started, 6)}
    manifest_path = args.output_dir / "render_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(manifest_path), "pages": len(pages)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
