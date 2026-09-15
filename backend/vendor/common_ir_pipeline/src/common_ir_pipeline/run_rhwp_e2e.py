#!/usr/bin/env python3
"""Run one original HWP/HWPX file through rhwp and Common IR v1.

This is the portable single-document E2E runner for the transfer snapshot.
It does not use the old batch scripts, whose original RunPod-root-relative
paths are intentionally not valid after transfer.  Install the optional
``hwp`` dependency and configure any FreeType compatibility preload required
by the local rhwp build *before* invoking ``common-ir-rhwp``.  No package-
internal wrapper script is required.

The runner creates only these run-local artifacts:

* ``raw/rhwp_full_ir/<notice>.<kind>.json``
* ``common_ir_v1/<notice>.<kind>.json``
* ``run_manifest.json``

It performs no OCR, PDF processing, GPU work, or network access.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter


HERE = Path(__file__).resolve()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_runtime() -> None:
    try:
        import rhwp  # noqa: F401
    except ImportError as exc:
        raise SystemExit("rhwp-python must be installed and importable; configure its FreeType runtime before invoking this command") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("hwp", "hwpx"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()

    validate_runtime()
    if not args.input.is_file():
        raise SystemExit(f"input does not exist: {args.input}")
    if args.input.suffix.lower() != f".{args.source_kind}":
        raise SystemExit(f"input extension does not match --source-kind: {args.input}")
    if args.run_dir.exists():
        raise SystemExit(f"run directory already exists: {args.run_dir}")

    started_at = utc_now()
    args.run_dir.mkdir(parents=True)
    raw_path = args.run_dir / "raw" / "rhwp_full_ir" / f"{args.notice_id}.{args.source_kind}.json"
    output_path = args.run_dir / "common_ir_v1" / f"{args.notice_id}.{args.source_kind}.json"
    source_hash = sha256(args.input)
    stable_raw_location = str(Path("raw") / "rhwp_full_ir" / raw_path.name)
    rhwp_version = core_version = None
    parse_error = None
    try:
        import rhwp
        document = rhwp.parse(args.input)
        raw_ir = document.to_ir_json()
        rhwp_version = rhwp.version()
        # rhwp_core_version() is the deterministic-if-available half of the
        # lineage contract (common_ir_v1_schema.py's document.provenance
        # docstring): record the literal value rhwp itself reports, and
        # leave it None (never guessed at) if this rhwp build doesn't expose
        # it at all, rather than letting an AttributeError abort the run.
        get_core_version = getattr(rhwp, "rhwp_core_version", None)
        core_version = get_core_version() if callable(get_core_version) else None
        raw_payload = {
            "notice_id": args.notice_id,
            "source_kind": args.source_kind,
            "artifact_role": "production",
            "method": "rhwp",
            "version": rhwp_version,
            "core_version": core_version,
            "source_path": str(args.input.resolve()),
            "source_sha256": source_hash,
            "page_count": getattr(document, "page_count", None),
            "paragraph_count": getattr(document, "paragraph_count", None),
            "ir": json.loads(raw_ir) if isinstance(raw_ir, str) else raw_ir,
        }
        write_json(raw_path, raw_payload)
    except Exception as exc:
        parse_error = f"{type(exc).__name__}: {exc}"
        manifest = {
            "run_id": args.run_dir.name, "status": "failed", "started_at_utc": started_at, "ended_at_utc": utc_now(),
            "notice_id": args.notice_id, "source_kind": args.source_kind,
            "input": {"path": str(args.input.resolve()), "sha256": source_hash},
            "declared_constraints": {"ocr_called": False, "pdf_processed": False, "gpu_used": False, "network_access": False},
            "steps": [{"name": "rhwp_parse", "status": "failed", "error": parse_error, "traceback": traceback.format_exc()}],
        }
        write_json(args.run_dir / "run_manifest.json", manifest)
        raise SystemExit(parse_error)

    command = [
        sys.executable,
        "-m",
        "common_ir_pipeline.adapters.rhwp",
        "--notice-id", args.notice_id,
        "--rhwp", str(raw_path),
        "--source-kind", args.source_kind,
        "--source-path", str(args.input.resolve()),
        "--source-sha256", source_hash,
        "--source-location-base", stable_raw_location,
        "--output", str(output_path),
    ]
    adapter_started = utc_now()
    t0 = perf_counter()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    manifest = {
        "run_id": args.run_dir.name,
        "status": "ok" if completed.returncode == 0 else "failed",
        "started_at_utc": started_at,
        "ended_at_utc": utc_now(),
        "notice_id": args.notice_id,
        "source_kind": args.source_kind,
        "input": {"path": str(args.input.resolve()), "sha256": source_hash},
        "toolchain": {"python": sys.executable, "rhwp": rhwp_version, "rhwp_core": core_version},
        "declared_constraints": {"ocr_called": False, "pdf_processed": False, "gpu_used": False, "network_access": False},
        "steps": [
            {"name": "rhwp_parse", "status": "ok", "output": str(raw_path), "output_sha256": sha256(raw_path)},
            {
                "name": "adapt_rhwp_to_common_ir_v1",
                "status": "ok" if completed.returncode == 0 else "failed",
                "command": command,
                "started_at_utc": adapter_started,
                "duration_seconds": round(perf_counter() - t0, 6),
                "return_code": completed.returncode,
                "summary": json.loads(completed.stdout) if completed.returncode == 0 else None,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "output": str(output_path) if output_path.exists() else None,
                "output_sha256": sha256(output_path) if output_path.exists() else None,
            },
        ],
    }
    write_json(args.run_dir / "run_manifest.json", manifest)
    if completed.returncode:
        sys.stderr.write(completed.stderr)
        return 1
    print(json.dumps({"run_dir": str(args.run_dir), "raw": str(raw_path), "common_ir_v1": str(output_path), "summary": manifest["steps"][1]["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
