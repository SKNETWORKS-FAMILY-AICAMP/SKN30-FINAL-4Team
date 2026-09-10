"""Slice 5 L2: isolated ML serving adapters.

The team serving packages are not imported by the backend process.  Their entry
points mutate ``sys.path`` and use bare module names, so each model is invoked
in a short-lived child process instead.  The child protocol is deliberately
small: one JSON object on stdin and one JSON value on stdout.

This module does not import numpy, pandas, joblib, torch, or the team's ``ml``
package.  Once the real artifacts are available, only the command supplied to
the adapter changes.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Literal, Mapping, Sequence

from ..contracts.ml_result import INPUT_EVIDENCE_MISSING, MlModelId
from ..ml_reference import (
    MODEL_1_ALLOWED_STATUSES,
    MODEL_3_ALLOWED_AXES,
    MODEL_3_ALLOWED_LEVELS,
    MODEL_3_TYPICAL_LEVEL,
    MlModel,
    MlUnavailable,
)

__all__ = [
    "SubprocessModelError",
    "SubprocessJsonRunner",
    "SubprocessMlModel",
    "Model2SubprocessMlModel",
    "Model3SubprocessMlModel",
    "Model1SubprocessMlModel",
    "MODEL1_SERVING_DIR_ENV",
    "model1_command",
    "model2_command",
    "model3_command",
    "normalize_model1_output",
    "normalize_model2_output",
    "normalize_model3_output",
]


_CHILD_ENTRYPOINT = Path(__file__).with_name("ml_child.py")
MODEL1_SERVING_DIR_ENV = "ML_MODEL1_SERVING_DIR"


def _child_command(
    model: Literal["model1", "model2", "model3"],
    *,
    python_executable: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Build the argv for one isolated team-serving invocation.

    The returned argv is consumed directly by :func:`subprocess.run`; keeping
    the executable and arguments separate is the important part of the
    no-shell boundary.  The default interpreter is the backend's interpreter,
    while tests/deployments can provide the ML runtime interpreter explicitly.
    """

    executable = sys.executable if python_executable is None else str(python_executable)
    return (executable, str(_CHILD_ENTRYPOINT), "--model", model)


def model1_command(
    *, python_executable: str | os.PathLike[str] | None = None
) -> tuple[str, ...]:
    """Return the child argv for the external Model 1 serving entrypoint."""

    return _child_command("model1", python_executable=python_executable)


def model2_command(
    *, python_executable: str | os.PathLike[str] | None = None
) -> tuple[str, ...]:
    """Return the child argv for the team's ``predict_document`` entrypoint."""

    return _child_command("model2", python_executable=python_executable)


def model3_command(
    *, python_executable: str | os.PathLike[str] | None = None
) -> tuple[str, ...]:
    """Return the child argv for the team's ``score_document`` entrypoint."""

    return _child_command("model3", python_executable=python_executable)


class SubprocessModelError(RuntimeError):
    """A child invocation or its wire response was unusable.

    The exception intentionally does not contain child stdout/stderr.  Model
    inputs can contain document text and the L1 diagnostic boundary only needs
    a safe failure category, not a copy of the raw response.
    """


def _reject_json_constant(value: str) -> None:
    """Reject JSON extensions such as NaN and Infinity at the wire boundary."""

    raise ValueError(f"non-finite JSON constant: {value}")


class SubprocessJsonRunner:
    """Run one model command once per prediction.

    No serving package is imported in the parent.  ``command`` is executed
    without a shell and inherits the environment unless explicit overrides are
    provided.  A fresh process per call is intentional for this checkpoint:
    model-specific import caches and path mutations die with the child.
    """

    def __init__(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        timeout_seconds: float = 60.0,
        cwd: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("subprocess command must not be empty")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("subprocess timeout must be a finite positive number")
        self.command = tuple(str(part) for part in command)
        self.timeout_seconds = float(timeout_seconds)
        self.cwd = None if cwd is None else str(cwd)
        self.environment = None if environment is None else dict(environment)

    def run(self, payload: Mapping[str, Any]) -> Any:
        try:
            request = json.dumps(
                dict(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        except (TypeError, ValueError) as error:
            raise SubprocessModelError("입력 JSON 직렬화에 실패했다") from error

        # JSON is a UTF-8 wire contract even on Windows, where a Python child
        # otherwise inherits the console code page for stdout.
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if self.environment is not None:
            env.update(self.environment)
        # Team imports can be numerous and mutable; never leave bytecode next
        # to vendored serving sources in the repository from a child call.
        # Keep this assignment after caller overrides so the cleanup contract
        # cannot be disabled accidentally.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            completed = subprocess.run(
                self.command,
                input=request,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                cwd=self.cwd,
                env=env,
            )
        except subprocess.TimeoutExpired as error:
            raise SubprocessModelError(
                f"모델 subprocess가 {self.timeout_seconds:g}초 안에 끝나지 않았다"
            ) from error
        except OSError as error:
            raise SubprocessModelError(
                f"모델 subprocess를 시작하지 못했다: {type(error).__name__}"
            ) from error

        if completed.returncode != 0:
            raise SubprocessModelError(
                f"모델 subprocess가 비정상 종료했다: exit={completed.returncode}"
            )
        if not completed.stdout or not completed.stdout.strip():
            raise SubprocessModelError("모델 subprocess stdout이 비어 있다")
        try:
            response = json.loads(
                completed.stdout,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError) as error:
            raise SubprocessModelError("모델 subprocess stdout이 JSON이 아니다") from error
        if not isinstance(response, dict):
            raise SubprocessModelError("모델 subprocess stdout이 JSON object가 아니다")
        return response


Normalizer = Callable[[Any], dict[str, Any]]


class SubprocessMlModel:
    """Adapt a JSON child command to the L1 ``MlModel`` port."""

    def __init__(
        self,
        model_id: MlModelId,
        runner: SubprocessJsonRunner,
        normalizer: Normalizer,
        *,
        artifact_version: str | None = None,
    ) -> None:
        self.model_id = model_id
        self._runner = runner
        self._normalizer = normalizer
        self.artifact_version = artifact_version

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        raw = self._runner.run(inputs)
        try:
            normalized = self._normalizer(raw)
        except SubprocessModelError:
            raise
        except Exception as error:  # noqa: BLE001 - L1 isolates this model
            raise SubprocessModelError(
                f"모델 raw 응답 정규화에 실패했다: {type(error).__name__}"
            ) from error
        if not isinstance(normalized, dict):
            raise SubprocessModelError("모델 정규화 결과가 object가 아니다")
        return normalized


def _single_prediction(raw: Any, *, model_name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SubprocessModelError(f"{model_name} raw envelope가 object가 아니다")
    predictions = raw.get("predictions")
    if not isinstance(predictions, list) or len(predictions) != 1:
        raise SubprocessModelError(
            f"{model_name} predictions가 단일 행이 아니다"
        )
    row = predictions[0]
    if not isinstance(row, dict):
        raise SubprocessModelError(f"{model_name} prediction row가 object가 아니다")
    return dict(row)


def normalize_model2_output(raw: Any) -> dict[str, Any]:
    """Unwrap team Model 2 ``predictions[0]`` for the L1 contract."""

    return _single_prediction(raw, model_name="model2")


def normalize_model1_output(raw: Any) -> dict[str, Any]:
    """Validate the one-row Model 1 serving contract.

    The child unwraps ``inference.predict([text])`` before writing JSON, so the
    parent receives one object rather than a list.  Class/status validation is
    repeated by the L1 reference boundary; this adapter only rejects malformed
    wire values and non-finite confidence values at the process boundary.
    """

    if not isinstance(raw, dict):
        raise SubprocessModelError("model1 raw result가 object가 아니다")
    label = raw.get("support_type_pred")
    if not isinstance(label, str) or not label.strip():
        raise SubprocessModelError("model1 support_type_pred가 문자열이 아니다")
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise SubprocessModelError("model1 confidence가 숫자가 아니다")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise SubprocessModelError("model1 confidence가 0과 1 사이의 유한값이 아니다")
    status = raw.get("status")
    if not isinstance(status, str) or status not in MODEL_1_ALLOWED_STATUSES:
        raise SubprocessModelError("model1 status가 허용 값 밖이다")
    normalized = dict(raw)
    normalized["support_type_pred"] = label.strip()
    normalized["confidence"] = confidence
    return normalized


def _model3_record(raw: Any) -> dict[str, Any]:
    """Read one JSON representation of a pandas ``to_dict('records')`` result."""

    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict) and isinstance(raw.get("records"), list):
        records = raw["records"]
    elif isinstance(raw, dict) and isinstance(raw.get("record"), dict):
        records = [raw["record"]]
    elif isinstance(raw, dict):
        # The small child protocol may emit the single serving row directly;
        # do not require a pandas-specific envelope at this boundary.
        return dict(raw)
    else:
        raise SubprocessModelError("model3 raw result가 records 형태가 아니다")
    if len(records) != 1 or not isinstance(records[0], dict):
        raise SubprocessModelError("model3 raw result가 단일 행이 아니다")
    return dict(records[0])


def _model3_display_level(score: Any) -> str:
    if isinstance(score, bool):
        raise SubprocessModelError("model3 score가 bool이다")
    if not isinstance(score, (int, float)):
        raise SubprocessModelError("model3 score가 숫자가 아니다")
    try:
        numeric = float(score)
    except (TypeError, ValueError, OverflowError) as error:
        raise SubprocessModelError("model3 score가 숫자가 아니다") from error
    if not math.isfinite(numeric) or not 0 <= numeric <= 1:
        raise SubprocessModelError("model3 score가 0과 1 사이의 유한값이 아니다")
    percentile = numeric * 100
    if percentile >= 99:
        return MODEL_3_ALLOWED_LEVELS[0]
    if percentile >= 95:
        return MODEL_3_ALLOWED_LEVELS[2]
    if percentile >= 90:
        return MODEL_3_ALLOWED_LEVELS[3]
    return MODEL_3_TYPICAL_LEVEL


def normalize_model3_output(raw: Any) -> dict[str, Any]:
    """Convert a team's DataFrame row to the L1 display contract.

    Team Model 3 returns cohort level names such as ``L1 ...`` and an English
    ``top1_axis``.  The user-facing level is derived from the team's frozen
    percentile thresholds; the source cohort level remains under a distinct
    key for internal diagnostics.  No free-form model text is copied.
    """

    row = _model3_record(raw)
    source_level = row.get("level")
    if source_level is not None and (
        not isinstance(source_level, str) or not source_level.strip()
    ):
        raise SubprocessModelError("model3 cohort level이 올바르지 않다")

    display_level = _model3_display_level(row.get("score"))
    if "top1_axis" not in row:
        raise SubprocessModelError("model3 top1_axis가 없다")
    top1_axis = row["top1_axis"]
    if top1_axis is None:
        cause_axes: list[str] = []
    elif isinstance(top1_axis, str):
        axis = top1_axis.strip()
        if axis in MODEL_3_ALLOWED_AXES:
            cause_axes = [axis]
        else:
            # Team serving uses English feature names; translate them here.  The
            # import-free table is the same mapping fixed by L1.
            from ..ml_reference import MODEL_3_AXIS_LABELS

            label = MODEL_3_AXIS_LABELS.get(axis)
            if label is None:
                raise SubprocessModelError(f"model3 top1_axis가 허용 축 밖이다: {axis!r}")
            cause_axes = [label]
    else:
        raise SubprocessModelError("model3 top1_axis가 문자열이 아니다")

    normalized = dict(row)
    if isinstance(source_level, str):
        normalized["cohort_level"] = source_level.strip()
    normalized["level"] = display_level
    normalized["cause_axes"] = cause_axes
    return normalized


class Model2SubprocessMlModel(SubprocessMlModel):
    """L1 model-2 port backed by one isolated child invocation."""

    def __init__(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        timeout_seconds: float = 60.0,
        cwd: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
        artifact_version: str | None = None,
    ) -> None:
        super().__init__(
            MlModelId.MODEL_2_AMOUNT,
            SubprocessJsonRunner(
                command,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
                environment=environment,
            ),
            normalize_model2_output,
            artifact_version=artifact_version,
        )


class Model1SubprocessMlModel(SubprocessMlModel):
    """L1 model-1 port backed by one isolated child invocation."""

    def __init__(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        timeout_seconds: float = 180.0,
        cwd: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
        artifact_version: str | None = None,
    ) -> None:
        super().__init__(
            MlModelId.MODEL_1_SUPPORT_TYPE,
            SubprocessJsonRunner(
                command,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
                environment=environment,
            ),
            normalize_model1_output,
            artifact_version=artifact_version,
        )


class Model3SubprocessMlModel(SubprocessMlModel):
    """L1 model-3 port backed by one isolated child invocation."""

    def __init__(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        timeout_seconds: float = 60.0,
        cwd: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
        artifact_version: str | None = None,
    ) -> None:
        super().__init__(
            MlModelId.MODEL_3_ANOMALY,
            SubprocessJsonRunner(
                command,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
                environment=environment,
            ),
            normalize_model3_output,
            artifact_version=artifact_version,
        )

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Do not invoke team ``prepare`` without a carried support type.

        The frozen team pipeline drops rows whose ``support_type`` is missing.
        Calling it anyway only turns a known L1 input gap into a misleading
        execution failure.  A withheld Model 1 result is equally unusable for
        cohort selection, so both cases use the existing unavailable boundary.
        """

        support_type = inputs.get("support_type")
        if not isinstance(support_type, str) or not support_type.strip():
            raise MlUnavailable(
                INPUT_EVIDENCE_MISSING,
                "모델 3 비교군을 정할 지원유형 근거가 없다.",
            )
        if inputs.get("support_type_status") == "판단보류":
            raise MlUnavailable(
                INPUT_EVIDENCE_MISSING,
                "모델 1 지원유형이 판단보류라 모델 3 비교군을 정할 수 없다.",
            )
        return super().predict(inputs)
