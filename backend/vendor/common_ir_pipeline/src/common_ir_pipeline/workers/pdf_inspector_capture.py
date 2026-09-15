"""Capture one complete PDF as pinned, path-neutral native evidence.

This is a deliberately small child-process entry point.  The parent replay
runner owns its private working directory, allowlisted environment, timeout
and process-group termination.  This child reads no environment variables
and accepts neither credentials nor a page selector.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from common_ir_pipeline.pdf_fusion.native_capture import (
    NativeCaptureError,
    canonical_json_bytes,
    capture_pdf_to_native,
)


def write_capture_exclusive(path: Path, data: bytes) -> None:
    """Create ``path`` exactly once; never replace an archived capture."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise NativeCaptureError("capture output already exists; refusing to overwrite it") from error
    except OSError as error:
        raise NativeCaptureError("failed to write native capture output") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument(
        "--source-relative-path",
        required=True,
        help="stable POSIX path stored in the artifact; absolute host paths are rejected",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        capture = capture_pdf_to_native(
            args.pdf,
            notice_id=args.notice_id,
            source_relative_path=args.source_relative_path,
        )
        encoded = canonical_json_bytes(capture) + b"\n"
        write_capture_exclusive(args.output, encoded)
    except NativeCaptureError as error:
        parser.error(str(error))
    print(json.dumps({
        "output": str(args.output),
        "source_sha256": capture["source_sha256"],
        "source_size_bytes": capture["source_size_bytes"],
        "page_count": capture["process_result"]["page_count"],
        "text_items": len(capture["text_items"]),
        "extraction_scope": "full_document",
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
