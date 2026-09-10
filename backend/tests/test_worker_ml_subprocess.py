"""Narrow contracts for the isolated external ML child boundary."""

from __future__ import annotations

import sys

import pytest

from worker.adapters.ml_child import _model1_text
from worker.adapters.ml_subprocess import (
    SubprocessModelError,
    model1_command,
    normalize_model1_output,
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
