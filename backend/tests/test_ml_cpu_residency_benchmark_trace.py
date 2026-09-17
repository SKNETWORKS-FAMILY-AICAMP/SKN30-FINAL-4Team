from __future__ import annotations

import json
from pathlib import Path
import statistics


TRACE = (
    Path(__file__).resolve().parents[1]
    / "fastapi/docs/benchmarks/ml_cpu_residency_20260917.json"
)


def _measurement_rows(value: object):
    if not isinstance(value, dict):
        return
    if "samples_ms" in value:
        yield value
    for nested in value.values():
        yield from _measurement_rows(nested)


def test_cpu_residency_trace_has_exact_repetitions_and_correct_trimmed_means() -> None:
    trace = json.loads(TRACE.read_text(encoding="utf-8"))

    assert trace["schema_version"] == "prereview.ml-cpu-residency-benchmark/v1"
    assert trace["method"]["repetitions"] == 5
    rows = list(_measurement_rows(trace["measurements"]))
    assert rows
    for row in rows:
        samples = row["samples_ms"]
        assert len(samples) == 5
        expected = statistics.fmean(sorted(samples)[1:-1])
        assert row["trimmed_mean_ms"] == round(expected, 3)


def test_cpu_residency_trace_pins_real_input_and_runtime_artifacts() -> None:
    trace = json.loads(TRACE.read_text(encoding="utf-8"))

    assert trace["input"]["notice_id"] == "PBLN_000000000117383"
    for key in (
        "source_sha256",
        "common_ir_sha256",
        "profile_sha256",
        "model1_input_sha256",
        "model2_proxy_input_sha256",
        "model3_proxy_input_sha256",
        "model2_resident_http_input_sha256",
        "model3_resident_http_input_sha256",
    ):
        assert len(trace["input"][key]) == 64
    for artifact in trace["artifacts"].values():
        assert len(artifact["sha256"]) == 64
        assert artifact["size_bytes"] > 0

    validation = trace["implementation_validation"]
    assert validation["model23_service"] == "prereview-model23"
    assert len(validation["runtime_manifest_sha256"]) == 64
    assert validation["worker_preflight"] == "passed"
    assert validation["sidecar_health"] == "healthy"
    assert validation["subprocess_resident_output_sha256_equal"] is True

    for model in ("model2", "model3"):
        measurements = trace["measurements"][model]
        one_shot = measurements["implementation_one_shot_same_input"]
        resident = measurements["implementation_resident_http_same_input"]
        assert len(one_shot["output_sha256"]) == 64
        assert resident["output_sha256"] == one_shot["output_sha256"]
        assert resident["trimmed_mean_ms"] < one_shot["trimmed_mean_ms"]
