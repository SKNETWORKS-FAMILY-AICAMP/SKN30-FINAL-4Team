#!/usr/bin/env python3
"""Render every page of a PDF once for a fixed multi-model OCR comparison."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pypdfium2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--scale", type=float, default=2.7777778)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    document = pypdfium2.PdfDocument(str(args.pdf))
    pages = []
    started_all = time.perf_counter()
    for index in range(len(document)):
        page_started = time.perf_counter()
        image = document[index].render(scale=args.scale).to_pil().convert("RGB")
        output = args.output_dir / f"page_{index + 1:04d}.png"
        image.save(output)
        pages.append(
            {
                "page": index + 1,
                "path": str(output),
                "width": image.width,
                "height": image.height,
                "render_seconds": round(time.perf_counter() - page_started, 6),
            }
        )
    manifest = {
        "source_pdf": str(args.pdf),
        "renderer": "pypdfium2",
        "render_scale": args.scale,
        "dpi_equivalent": round(72 * args.scale, 4),
        "manual_crop": False,
        "pages": pages,
        "total_render_seconds": round(time.perf_counter() - started_all, 6),
    }
    (args.output_dir / "render_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
