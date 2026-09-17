from __future__ import annotations

from typing import Any

import pytest

from worker.contracts.ml_result import MlModelId
from worker.ml_reference import MlUnavailable
from worker.ml_retry import RetryingMlModel


class _UnavailableModel:
    model_id = MlModelId.MODEL_2_AMOUNT
    artifact_version = "test-model-2"

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise MlUnavailable("ML_RUNTIME_MISSING", "runtime missing")


def test_retrying_model_does_not_retry_permanent_unavailability() -> None:
    model = _UnavailableModel()

    with pytest.raises(MlUnavailable):
        RetryingMlModel(model).predict({})

    assert model.calls == 1
