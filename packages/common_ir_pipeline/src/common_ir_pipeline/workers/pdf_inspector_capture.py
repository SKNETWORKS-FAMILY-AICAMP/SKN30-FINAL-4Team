"""Capture the immutable native PDF artifact consumed by the PDF adapter."""
from __future__ import annotations

import argparse
import dataclasses
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


def serialise(value: Any, seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return serialise(dataclasses.asdict(value), seen)
    if isinstance(value, dict):
        return {str(key): serialise(item, seen) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialise(item, seen) for item in value]
    tracked = set() if seen is None else seen
    if id(value) in tracked:
        return None
    tracked.add(id(value))
    return {name: serialise(getattr(value, name), tracked) for name in dir(value) if not name.startswith("_") and not callable(getattr(value, name, None))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.pdf.is_file():
        parser.error(f"PDF does not exist: {args.pdf}")
    try:
        import pdf_inspector
    except ImportError as error:
        raise SystemExit("Install the optional [pdf] dependency to capture native PDF text.") from error
    try:
        inspector_version = version("pdf-inspector")
    except PackageNotFoundError:
        inspector_version = None
    pdf = str(args.pdf)
    payload = {"notice_id": args.notice_id, "source_kind": "pdf", "artifact_role": "production", "method": "pdf_inspector", "version": inspector_version, "source_path": pdf, "process_result": serialise(pdf_inspector.process_pdf(pdf)), "pages_markdown_result": serialise(pdf_inspector.extract_pages_markdown(pdf)), "text_items": serialise(pdf_inspector.extract_text_with_positions(pdf)), "structure_elements": serialise(pdf_inspector.extract_structure_elements(pdf))}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "text_items": len(payload["text_items"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
