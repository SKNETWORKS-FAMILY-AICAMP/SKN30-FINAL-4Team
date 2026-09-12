from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sys
from typing import Any, Mapping

import pytest

from worker.contracts.ml_result import MlModelId
from worker.existing_model1 import (
    ExistingModel1Backfill,
    ExistingModel1Prediction,
    ExistingModel1Profile,
    assemble_existing_model1_input,
    normalize_existing_model1_prediction,
)
from scripts.classify_existing_model1 import (
    _configured_ml_python,
    _model1_serving_root,
    _verify_model1_artifact,
)


def _profile(profile_id: str = "profile-1") -> ExistingModel1Profile:
    # Deliberately unordered fields: persisted ordinal, not dict/list encounter
    # order, is the exact Existing evidence order sent to Model 1.
    return ExistingModel1Profile(
        profile_version_id=profile_id,
        portal_metadata={"title": "  지역   성장 사업  "},
        facts=[
            {"field_name": "support_content", "value_raw": "후순위 내용", "status": "identified", "ordinal": 30},
            {"field_name": "purpose_goal", "value_raw": "기업 성장", "status": "identified", "ordinal": 10},
            {"field_name": "support_activities", "value_raw": "컨설팅", "status": "partial", "ordinal": 20},
            {"field_name": "support_target", "value_raw": "중소기업", "status": "identified", "ordinal": 40},
            {"field_name": "exclusions", "value_raw": "중복수혜 제외", "status": "identified", "ordinal": 50},
            {"field_name": "support_methods", "value_raw": "무시", "status": "not_identified", "ordinal": 15},
            # Component-scoped facts are stored in the same table and must be
            # included without a separate component traversal.
            {"field_name": "support_items", "value_raw": "시제품", "status": "identified", "ordinal": 25},
        ],
    )


def test_existing_model1_input_uses_only_approved_facts_in_frozen_field_order() -> None:
    assembled = assemble_existing_model1_input(_profile())

    assert assembled.payload == {
        "title": "지역 성장 사업",
        "purpose": "기업 성장",
        "content": "컨설팅\n시제품\n후순위 내용",
        "target_text": "중소기업\n중복수혜 제외",
    }
    assert assembled.missing_fields == ()
    assert assembled.input_sha256 == assemble_existing_model1_input(_profile()).input_sha256


def test_existing_model1_input_hash_distinguishes_field_boundaries() -> None:
    first = assemble_existing_model1_input(
        ExistingModel1Profile(
            profile_version_id="one",
            portal_metadata={"title": "A"},
            facts=[{"field_name": "purpose_goal", "value_raw": "B\nC", "status": "identified", "ordinal": 0}],
        )
    )
    second = assemble_existing_model1_input(
        ExistingModel1Profile(
            profile_version_id="two",
            portal_metadata={"title": "A\nB"},
            facts=[{"field_name": "purpose_goal", "value_raw": "C", "status": "identified", "ordinal": 0}],
        )
    )
    assert first.input_sha256 != second.input_sha256


def test_withheld_prediction_retains_raw_label_but_has_no_effective_label() -> None:
    prediction = normalize_existing_model1_prediction(
        {"support_type_pred": "판로", "confidence": 0.19, "status": "판단보류"}
    )

    assert prediction.support_type_pred == "판로"
    assert prediction.effective_support_type is None


class _FakeModel:
    model_id = MlModelId.MODEL_1_SUPPORT_TYPE
    artifact_version = "a" * 64

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(inputs))
        return {"support_type_pred": "판로", "confidence": 0.91, "status": "신뢰"}


class _FakeRepository:
    def __init__(self, profiles: list[ExistingModel1Profile]) -> None:
        self.profiles = profiles
        self.results: dict[tuple[str, str, str], ExistingModel1Prediction] = {}
        self.invocations: list[dict[str, Any]] = []
        self.finished: list[tuple[str, bool, str | None]] = []
        self.promotions: list[dict[str, Any]] = []

    def current_profiles(self) -> list[ExistingModel1Profile]:
        return list(self.profiles)

    def has_result(self, *, profile_version_id: str, configuration_id: str, input_sha256: str) -> bool:
        return (profile_version_id, configuration_id, input_sha256) in self.results

    def start_run(self, *, configuration_id: str, profile_count: int) -> str:
        assert configuration_id == "config-1"
        assert profile_count == len(self.profiles)
        return "run-1"

    def record_invocation(self, **kwargs: Any) -> None:
        self.invocations.append(kwargs)

    def write_result(self, **kwargs: Any) -> None:
        self.results[(kwargs["profile_version_id"], kwargs["configuration_id"], kwargs["input_sha256"])] = kwargs["prediction"]

    def write_failure(self, **kwargs: Any) -> None:
        self.results[(kwargs["profile_version_id"], kwargs["configuration_id"], kwargs["input_sha256"])] = kwargs["reason_code"]

    def verify_and_promote(self, **kwargs: Any) -> None:
        expected = kwargs["expected_inputs"]
        assert len(expected) == len(self.profiles)
        assert all((profile_id, kwargs["configuration_id"], digest) in self.results for profile_id, digest in expected.items())
        self.promotions.append(kwargs)

    def finish_run(self, *, processing_run_id: str, succeeded: bool, error_code: str | None = None) -> None:
        self.finished.append((processing_run_id, succeeded, error_code))


def test_backfill_is_idempotent_and_promotes_only_after_complete_verification() -> None:
    repository = _FakeRepository([_profile("profile-1"), _profile("profile-2")])
    model = _FakeModel()
    backfill = ExistingModel1Backfill(repository, model, configuration_id="config-1")

    first = backfill.run()
    second = backfill.run()

    assert (first.predicted, first.skipped, first.promoted) == (2, 0, True)
    assert (second.predicted, second.skipped, second.promoted) == (0, 2, True)
    assert len(model.calls) == 2
    assert [item["status"] for item in repository.invocations] == ["succeeded", "succeeded"]
    assert repository.finished == [("run-1", True, None), ("run-1", True, None)]


def test_dry_run_neither_calls_model_nor_writes() -> None:
    repository = _FakeRepository([_profile()])
    model = _FakeModel()

    result = ExistingModel1Backfill(repository, model, configuration_id="config-1").run(dry_run=True)

    assert result.dry_run is True
    assert model.calls == []
    assert repository.results == {}
    assert repository.promotions == []


def test_backfill_rejects_an_empty_current_corpus() -> None:
    with pytest.raises(ValueError, match="empty"):
        ExistingModel1Backfill(_FakeRepository([]), _FakeModel(), configuration_id="config-1").run()


def test_failed_model_call_is_audited_and_persisted_without_prediction() -> None:
    class _BrokenModel(_FakeModel):
        def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("serving unavailable")

    repository = _FakeRepository([_profile()])
    with pytest.raises(RuntimeError, match="serving unavailable"):
        ExistingModel1Backfill(repository, _BrokenModel(), configuration_id="config-1").run()

    assert repository.invocations[0]["status"] == "failed"
    assert list(repository.results.values()) == ["MODEL_EXECUTION_FAILED"]
    assert repository.finished == [("run-1", False, "RuntimeError")]


def _fake_serving_root(tmp_path: Path, *, parent_layout: bool) -> tuple[Path, bytes]:
    root = tmp_path / "serving"
    model_root = root / "model1" if parent_layout else root
    (model_root / "model").mkdir(parents=True)
    (model_root / "inference.py").write_text("# test wrapper\n", encoding="utf-8")
    payload = b"not a real safetensors file; never loaded"
    (model_root / "model" / "model.safetensors").write_bytes(payload)
    return root, payload


@pytest.mark.parametrize("parent_layout", [False, True])
def test_runtime_verification_accepts_worker_model1_mount_layouts(
    tmp_path: Path, parent_layout: bool
) -> None:
    configured, payload = _fake_serving_root(tmp_path, parent_layout=parent_layout)
    env = {"PREREVIEW_MODEL1_SERVING_DIR": str(configured)}
    config = {"artifact_sha256": sha256(payload).hexdigest()}

    root, actual = _verify_model1_artifact(config, env)

    assert root == (configured / "model1" if parent_layout else configured)
    assert actual == config["artifact_sha256"]
    assert _model1_serving_root(env) == root


def test_runtime_verification_rejects_weight_digest_mismatch_before_inference(
    tmp_path: Path,
) -> None:
    configured, _ = _fake_serving_root(tmp_path, parent_layout=False)
    with pytest.raises(RuntimeError, match="SHA-256"):
        _verify_model1_artifact(
            {"artifact_sha256": "0" * 64},
            {"PREREVIEW_MODEL1_SERVING_DIR": str(configured)},
        )


def test_configured_ml_python_uses_verified_external_interpreter() -> None:
    assert _configured_ml_python({"PREREVIEW_ML_PYTHON_EXECUTABLE": sys.executable}) == sys.executable
    with pytest.raises(RuntimeError, match="PREREVIEW_ML_PYTHON_EXECUTABLE"):
        _configured_ml_python({"PREREVIEW_ML_PYTHON_EXECUTABLE": "/missing/ml-python"})
