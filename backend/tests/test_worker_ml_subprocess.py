"""Narrow contracts for the isolated external ML child boundary."""

from __future__ import annotations

import sys
import subprocess

import pytest

from worker.adapters.ml_child import _model1_text
from worker.adapters.ml_subprocess import (
    SubprocessJsonRunner,
    SubprocessModelError,
    model1_command,
    normalize_model1_output,
)


def test_ml_child_environment_excludes_parent_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-model")
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-reach-model")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "must-not-reach-model")
    monkeypatch.setenv("HF_TOKEN", "must-not-reach-model")
    monkeypatch.setenv("NVIDIA_API_KEY", "must-not-reach-model")
    monkeypatch.setenv("NVIDIA_LICENSE_KEY", "must-not-reach-model")
    monkeypatch.setenv("CUDA_AUTH_PWD", "must-not-reach-model")
    monkeypatch.setenv("pytorch_registry_pass", "must-not-reach-model")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    captured: dict[str, object] = {}

    def fake_run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = SubprocessJsonRunner(
        (sys.executable, "unused.py"),
        environment={"PREREVIEW_ML_ROOT": "/models"},
    )

    assert runner.run({"input": "text"}) == {}
    child_environment = captured["env"]
    assert isinstance(child_environment, dict)
    assert child_environment["PREREVIEW_ML_ROOT"] == "/models"
    assert child_environment["CUDA_VISIBLE_DEVICES"] == "0"
    assert child_environment["HF_HUB_OFFLINE"] == "1"
    assert child_environment["TRANSFORMERS_OFFLINE"] == "1"
    assert "OPENAI_API_KEY" not in child_environment
    assert "DATABASE_URL" not in child_environment
    assert "SUPABASE_SERVICE_ROLE_KEY" not in child_environment
    assert "HF_TOKEN" not in child_environment
    assert "NVIDIA_API_KEY" not in child_environment
    assert "NVIDIA_LICENSE_KEY" not in child_environment
    assert "CUDA_AUTH_PWD" not in child_environment
    assert "pytorch_registry_pass" not in child_environment


def test_ml_child_rejects_explicit_secret_environment() -> None:
    with pytest.raises(ValueError, match="unsupported keys: OPENAI_API_KEY"):
        SubprocessJsonRunner(
            (sys.executable, "unused.py"),
            environment={"OPENAI_API_KEY": "must-not-reach-model"},
        )


def test_model1_input_preserves_frozen_field_order() -> None:
    assert _model1_text(
        {
            "target_text": "대상",
            "content": "내용",
            "title": "제목",
            "purpose": "목적",
        }
    ) == "제목\n목적\n내용\n대상"


def test_model1_command_is_a_direct_unique_child_invocation() -> None:
    command = model1_command(python_executable=sys.executable)

    assert command[0] == sys.executable
    assert command[-2:] == ("--model", "model1")
    assert command[1].endswith("ml_child.py")


def test_model1_normalizer_accepts_one_serving_row() -> None:
    result = normalize_model1_output(
        {
            "support_type_pred": "판로",
            "confidence": 0.8123,
            "status": "신뢰",
        }
    )

    assert result["support_type_pred"] == "판로"
    assert result["confidence"] == pytest.approx(0.8123)


@pytest.mark.parametrize(
    "raw",
    [
        {"support_type_pred": "판로", "confidence": 1.1, "status": "신뢰"},
        {"support_type_pred": "판로", "confidence": 0.5, "status": "unknown"},
        {"support_type_pred": "", "confidence": 0.5, "status": "신뢰"},
    ],
)
def test_model1_normalizer_rejects_invalid_serving_rows(raw: dict[str, object]) -> None:
    with pytest.raises(SubprocessModelError):
        normalize_model1_output(raw)
