#!/usr/bin/env python3
"""Benchmark the existing CPU subprocess and resident Model 1 HTTP paths.

Only the four-field classifier contract and safe prediction metadata are read
or written.  In particular, this command never prints document text or a
bearer token.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import statistics
import sys
import time
from typing import Any, Mapping, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker.adapters.ml_subprocess import Model1SubprocessMlModel, model1_command
from worker.adapters.model1_http import Model1HttpAdapter
from worker.ml_reference import MODEL_1_FIELDS


def _inputs(path: Path) -> list[dict[str, str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("input JSON could not be read") from error
    if not isinstance(value, list) or not value:
        raise ValueError("input JSON must be a non-empty list")
    result: list[dict[str, str]] = []
    for row in value:
        if not isinstance(row, dict) or set(row) != set(MODEL_1_FIELDS):
            raise ValueError("each input must contain exactly four Model 1 fields")
        if any(not isinstance(row[field], str) for field in MODEL_1_FIELDS):
            raise ValueError("Model 1 input fields must be strings")
        if not any(row[field].strip() for field in MODEL_1_FIELDS):
            raise ValueError("Model 1 input must contain text")
        result.append({field: row[field] for field in MODEL_1_FIELDS})
    return result


def _safe_prediction(value: Mapping[str, Any]) -> tuple[str, str, float]:
    return (str(value["support_type_pred"]), str(value["status"]), float(value["confidence"]))


def _stats(samples: Sequence[float]) -> dict[str, float | list[float]]:
    ordered = sorted(samples)

    def percentile(q: float) -> float:
        index = (len(ordered) - 1) * q
        low, high = int(index), min(int(index) + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (index - low)

    result: dict[str, float | list[float]] = {
        "samples_ms": [sample * 1000 for sample in samples],
        "min_ms": min(samples) * 1000,
        "max_ms": max(samples) * 1000,
        "mean_ms": statistics.fmean(samples) * 1000,
        "p50_ms": percentile(0.50) * 1000,
        "p95_ms": percentile(0.95) * 1000,
        "throughput_per_second": len(samples) / sum(samples),
    }
    if len(ordered) >= 3:
        result["trimmed_mean_ms"] = statistics.fmean(ordered[1:-1]) * 1000
    return result


def _run_mode(
    name: str, model: Any, rows: Sequence[dict[str, str]], *, warmups: int, repetitions: int,
) -> tuple[dict[str, Any], list[tuple[str, str, float]]]:
    # A local call includes process/model startup.  Keep it separately visible;
    # remote startup is outside this adapter and therefore has no cold sample.
    cold: float | None = None
    if name == "local":
        start = time.perf_counter()
        first = model.predict(rows[0])
        cold = time.perf_counter() - start
        # The first result is not part of the comparable sequence.  This
        # adapter starts a new subprocess for every prediction, so it is a
        # first-call measurement rather than a one-time model-load metric.
        predictions = []
    else:
        predictions = []
    for index in range(warmups):
        model.predict(rows[index % len(rows)])
    samples: list[float] = []
    for index in range(repetitions):
        row = rows[index % len(rows)]
        start = time.perf_counter()
        value = model.predict(row)
        samples.append(time.perf_counter() - start)
        predictions.append(_safe_prediction(value))
    result: dict[str, Any] = {"samples": repetitions, **_stats(samples)}
    if cold is not None:
        result["first_call_ms"] = cold * 1000
    return result, predictions


def _read_bearer_token(path: Path) -> str:
    """Read a benchmark token without accepting links or shared files."""

    if path.is_symlink():
        raise ValueError("bearer token file is invalid")
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or not 32 <= metadata.st_size <= 513
    ):
        raise ValueError("bearer token file is invalid")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise ValueError("bearer token file is invalid")
    token = lines[0]
    if (
        not 32 <= len(token) <= 512
        or not all(character.isascii() and (character.isalnum() or character in "_-") for character in token)
    ):
        raise ValueError("bearer token file is invalid")
    return token


def run_benchmark(
    rows: Sequence[dict[str, str]], *, mode: str = "both", warmups: int = 1,
    repetitions: int = 5, local_interpreter: str | None = None,
    local_runtime: str | os.PathLike[str] | None = None,
    ml_root: str | os.PathLike[str] | None = None, remote_base_url: str | None = None,
    bearer_token: str | None = None, expected_remote_runtime_manifest: str | None = None,
    allow_loopback_http: bool = False,
) -> dict[str, Any]:
    if mode not in {"local", "remote", "both"} or warmups < 0 or repetitions <= 0:
        raise ValueError("invalid benchmark mode, warmups, or repetitions")
    selected: list[str] = [mode] if mode != "both" else ["local", "remote"]
    models: dict[str, Any] = {}
    if "local" in selected:
        runtime = str(local_runtime or os.environ.get("PREREVIEW_MODEL1_SERVING_DIR", ""))
        root = str(ml_root or os.environ.get("PREREVIEW_ML_ROOT", BACKEND_ROOT.parent / "ml"))
        if not runtime:
            raise ValueError("local runtime is required")
        models["local"] = Model1SubprocessMlModel(
            model1_command(python_executable=local_interpreter),
            environment={"PREREVIEW_MODEL1_SERVING_DIR": runtime, "PREREVIEW_ML_ROOT": root},
        )
    if "remote" in selected:
        if not remote_base_url or bearer_token is None or not expected_remote_runtime_manifest:
            raise ValueError("remote base URL, bearer token, and expected runtime manifest are required")
        models["remote"] = Model1HttpAdapter(
            base_url=remote_base_url, bearer_token=bearer_token,
            expected_runtime_manifest_sha256=expected_remote_runtime_manifest,
            allow_loopback_http=allow_loopback_http,
        )
    report: dict[str, Any] = {"mode": mode, "inputs": len(rows), "warmups": warmups, "repetitions": repetitions, "results": {}}
    predictions: dict[str, list[tuple[str, str, float]]] = {}
    for name in selected:
        metrics, predictions[name] = _run_mode(name, models[name], rows, warmups=warmups, repetitions=repetitions)
        report["results"][name] = metrics
    if set(selected) == {"local", "remote"}:
        count = min(len(predictions["local"]), len(predictions["remote"]))
        report["parity"] = {
            "compared": count,
            "exact_label_status": all(predictions["local"][i][:2] == predictions["remote"][i][:2] for i in range(count)),
            "max_confidence_delta": max((abs(predictions["local"][i][2] - predictions["remote"][i][2]) for i in range(count)), default=0.0),
        }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON list of exact four-field inputs")
    parser.add_argument("--mode", choices=("local", "remote", "both"), default="both")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--local-interpreter", "--local-python", dest="local_interpreter")
    parser.add_argument("--local-runtime", "--model1-serving-dir", dest="local_runtime")
    parser.add_argument("--ml-root", dest="ml_root")
    parser.add_argument("--remote-base-url")
    parser.add_argument("--bearer-token-file", type=Path)
    parser.add_argument("--expected-remote-runtime-manifest", "--expected-runtime-manifest-sha256", dest="expected_manifest")
    parser.add_argument("--allow-loopback-http", action="store_true")
    args = parser.parse_args(argv)
    try:
        token = None if args.bearer_token_file is None else _read_bearer_token(args.bearer_token_file)
        report = run_benchmark(_inputs(args.input), mode=args.mode, warmups=args.warmups, repetitions=args.repetitions,
                               local_interpreter=args.local_interpreter, local_runtime=args.local_runtime, ml_root=args.ml_root,
                               remote_base_url=args.remote_base_url, bearer_token=token,
                               expected_remote_runtime_manifest=args.expected_manifest, allow_loopback_http=args.allow_loopback_http)
    except Exception as error:  # safe CLI boundary: no input/provider diagnostics
        print(json.dumps({"status": "failed", "error": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
