"""Bounded retry wrapper for transient ML execution failures."""

from __future__ import annotations

from typing import Any

from .ml_reference import MlModel, MlUnavailable


class RetryingMlModel:
    """Retry a concrete model without changing the model result contract.

    MlUnavailable represents a missing artifact, runtime, or other durable
    precondition and is therefore never retried. Other execution exceptions
    are retried up to max_attempts and the final exception is re-raised so
    the existing model-local failure isolation records it normally.
    """

    def __init__(self, delegate: MlModel, *, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._delegate = delegate
        self.max_attempts = max_attempts
        self.model_id = delegate.model_id
        self.artifact_version = delegate.artifact_version

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._delegate.predict(inputs)
            except MlUnavailable:
                raise
            except Exception:
                if attempt == self.max_attempts:
                    raise
        raise AssertionError("unreachable")
