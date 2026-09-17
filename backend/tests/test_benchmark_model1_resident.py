from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import benchmark_model1_resident as benchmark


ROWS = [{"title": "t", "purpose": "p", "content": "c", "target_text": "g"}]


class _FakeModel:
    def __init__(self, result: dict[str, object]):
        self.result = result

    def predict(self, _row: dict[str, str]) -> dict[str, object]:
        return dict(self.result)


def test_input_loader_rejects_extra_fields_without_echoing_text(tmp_path: Path) -> None:
    path = tmp_path / "inputs.json"
    secret = "private-document-marker"
    path.write_text(json.dumps([{**ROWS[0], "extra": secret}]), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        benchmark._inputs(path)
    assert secret not in str(error.value)


def test_run_mode_reports_percentiles_and_cold_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 0.2, 1.0, 1.1, 2.0, 2.3])
    monkeypatch.setattr(benchmark.time, "perf_counter", lambda: next(clock))
    metrics, predictions = benchmark._run_mode(
        "local", _FakeModel({"support_type_pred": "사업화", "status": "신뢰", "confidence": 0.8}),
        ROWS, warmups=0, repetitions=2,
    )
    assert metrics["first_call_ms"] == pytest.approx(200.0)
    assert metrics["samples"] == 2
    assert metrics["samples_ms"] == pytest.approx([100.0, 300.0])
    assert "trimmed_mean_ms" not in metrics
    assert metrics["p50_ms"] == pytest.approx(200.0)
    assert len(predictions) == 2


def test_stats_reports_five_raw_samples_and_middle_three_mean() -> None:
    metrics = benchmark._stats((0.005, 0.001, 0.003, 0.002, 0.004))

    assert metrics["samples_ms"] == pytest.approx([5.0, 1.0, 3.0, 2.0, 4.0])
    assert metrics["trimmed_mean_ms"] == pytest.approx(3.0)


def test_both_parity_compares_only_steady_state(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeLocal(_FakeModel):
        def __init__(self, *args, **kwargs):
            super().__init__({"support_type_pred": "사업화", "status": "신뢰", "confidence": 0.80})

    class FakeRemote(_FakeModel):
        def __init__(self, *args, **kwargs):
            super().__init__({"support_type_pred": "사업화", "status": "신뢰", "confidence": 0.83})

    monkeypatch.setattr(benchmark, "Model1SubprocessMlModel", FakeLocal)
    monkeypatch.setattr(benchmark, "Model1HttpAdapter", FakeRemote)
    report = benchmark.run_benchmark(
        ROWS, mode="both", warmups=0, repetitions=2, local_runtime="/runtime",
        remote_base_url="https://model1.example", bearer_token="secret-token",
        expected_remote_runtime_manifest="a" * 64,
    )
    assert report["parity"] == {
        "compared": 2,
        "exact_label_status": True,
        "max_confidence_delta": pytest.approx(0.03),
    }
    rendered = json.dumps(report)
    assert "secret-token" not in rendered
    assert "private-document-marker" not in rendered
