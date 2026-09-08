"""Slice 5 L2: ML subprocess boundary and raw-output adapters."""

from __future__ import annotations

import json
from io import StringIO
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from worker.contracts.ml_result import (
    INPUT_EVIDENCE_MISSING,
    MODEL_EXECUTION_FAILED,
    MlModelId,
)
from worker.cpl import build_cpl_result
from worker.ml_reference import (
    FakeMlModel,
    MlModelInput,
    _run_one,
    run_ml_reference,
)
from worker.adapters.ml_subprocess import (
    Model2SubprocessMlModel,
    Model3SubprocessMlModel,
    SubprocessJsonRunner,
    SubprocessModelError,
    model2_command,
    model3_command,
    normalize_model2_output,
    normalize_model3_output,
)
from worker.adapters import ml_child


_ML_RUNTIME_MODULES = (
    "numpy",
    "pandas",
    "sklearn",
    "xgboost",
    "scipy",
    "joblib",
    "pyarrow",
)
_ML_ROOT = Path(__file__).resolve().parents[2] / "ml"
_REAL_SMOKE_READY = (
    all(importlib.util.find_spec(name) is not None for name in _ML_RUNTIME_MODULES)
    and (_ML_ROOT / "models" / "model2_canonical" / "model2_p3_bundle.joblib").is_file()
    and (_ML_ROOT / "data" / "processed" / "business_taxonomy.parquet").is_file()
    and (_ML_ROOT / "serving" / "model3" / "design_features_v3.parquet").is_file()
)


def _child(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_ml_child.py"
    script.write_text(
        "import json\n"
        "import sys\n"
        "import time\n"
        "payload = json.load(sys.stdin)\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def _input(model_id: MlModelId) -> MlModelInput:
    return MlModelInput(
        model_id=model_id,
        payload={"evidence_text": "원문"},
        sources=["test:evidence"],
    )


def test_model2_child_response_is_unwrapped_to_l1_shape(tmp_path):
    command = _child(
        tmp_path,
        "print(json.dumps({'model': 'm82', 'n': 1, 'predictions': "
        "[{'pred_won': 40000000, 'input_completeness': 'partial', "
        "'missing_features': ['support_ratio']}]}, ensure_ascii=False))",
    )
    model = Model2SubprocessMlModel(command, artifact_version="m82-test")

    output = model.predict({"evidence_text": "원문"})

    assert output == {
        "pred_won": 40000000,
        "input_completeness": "partial",
        "missing_features": ["support_ratio"],
    }
    assert model.model_id is MlModelId.MODEL_2_AMOUNT
    assert model.artifact_version == "m82-test"


@pytest.mark.parametrize(
    "score, expected",
    [
        (0.995, "과거 사업 패턴과 차이가 큼"),
        (0.96, "동일 유형 대비 비전형적"),
        (0.93, "확인 필요"),
        (0.90, "확인 필요"),
        (0.89, "비교군 범위 내"),
    ],
)
def test_model3_dataframe_record_is_mapped_to_allowed_display_values(
    tmp_path, score, expected
):
    command = _child(
        tmp_path,
        "print(json.dumps({'records': [{'row_id': 'r1', 'score': "
        f"{score!r}, 'level': 'L1 사업화 x grant', 'cohort_n': 20, "
        "'top1_axis': 'support_ratio'}]}, ensure_ascii=False))",
    )
    model = Model3SubprocessMlModel(command, artifact_version="m3-test")

    output = model.predict(
        {"support_type": "사업화", "quantities": {"support_ratio": ["70%"]}}
    )

    assert output["level"] == expected
    assert output["cause_axes"] == ["지원비율"]
    assert output["cohort_level"] == "L1 사업화 x grant"
    assert output["score"] == score
    assert model.model_id is MlModelId.MODEL_3_ANOMALY


def test_model3_null_top_axis_is_an_empty_allowed_axis_list(tmp_path):
    command = _child(
        tmp_path,
        "print(json.dumps({'records': [{'score': 0.5, 'level': 'L0 전체', "
        "'top1_axis': None}]}, ensure_ascii=False))",
    )
    output = Model3SubprocessMlModel(command).predict({"support_type": "사업화"})
    assert output["level"] == "비교군 범위 내"
    assert output["cause_axes"] == []


def test_model3_direct_raw_dict_is_normalized_without_cohort_envelope(tmp_path):
    command = _child(
        tmp_path,
        "print(json.dumps({'score': 0.96, 'top1_axis': 'project_duration'}, "
        "ensure_ascii=False))",
    )

    output = Model3SubprocessMlModel(command).predict({"support_type": "사업화"})

    assert output["level"] == "동일 유형 대비 비전형적"
    assert output["cause_axes"] == ["사업기간"]
    assert "cohort_level" not in output


@pytest.mark.parametrize("wire_value", ["[]", "null", "true", "1"])
def test_subprocess_wire_response_must_be_a_json_object(tmp_path, wire_value):
    command = _child(tmp_path, f"print({wire_value!r})")

    with pytest.raises(SubprocessModelError, match="JSON object"):
        SubprocessJsonRunner(command).run({})


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"predictions": []},
        {"predictions": [{"pred_won": 1}, {"pred_won": 2}]},
        {"predictions": ["not-an-object"]},
    ],
)
def test_model2_malformed_envelope_is_rejected(raw):
    with pytest.raises(SubprocessModelError):
        normalize_model2_output(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"records": []},
        {"records": [{"score": 0.5, "level": "L0"}, {}]},
        {"records": [{"score": 0.5, "level": "L0", "top1_axis": "unknown"}]},
        {"records": [{"score": 0.5, "level": "L0"}]},
        {"records": [{"score": float("nan"), "level": "L0"}]},
        {"records": [{"score": 2, "level": "L0"}]},
        {"records": [{"score": "0.5", "level": "L0", "top1_axis": None}]},
        {"records": [{"score": 0.5, "level": ""}]},
    ],
)
def test_model3_malformed_record_is_rejected(raw):
    with pytest.raises(SubprocessModelError):
        normalize_model3_output(raw)


@pytest.mark.parametrize(
    "body, expected_fragment",
    [
        ("raise SystemExit(7)", "exit=7"),
        ("print('not-json')", "stdout이 JSON이 아니다"),
        (
            "print(json.dumps({'predictions': [{'pred_won': 1}]}))\n"
            "raise SystemExit(3)",
            "exit=3",
        ),
    ],
)
def test_child_failure_is_safe_and_contains_no_raw_output(
    tmp_path, body, expected_fragment
):
    command = _child(tmp_path, body)
    runner = SubprocessJsonRunner(command)

    with pytest.raises(SubprocessModelError, match=expected_fragment):
        runner.run({"secret_document": "개인정보"})


def test_nonfinite_json_is_rejected_at_the_wire_boundary(tmp_path):
    command = _child(tmp_path, "print('{\"predictions\":[{\"pred_won\":NaN}]}')")
    with pytest.raises(SubprocessModelError, match="JSON이 아니다"):
        SubprocessJsonRunner(command).run({})


def test_timeout_is_model_local_through_existing_l1(tmp_path):
    command = _child(tmp_path, "time.sleep(2)\nprint('{}')")
    model = Model2SubprocessMlModel(command, timeout_seconds=0.05)
    diagnostics = []

    result = _run_one(
        MlModelId.MODEL_2_AMOUNT,
        model,
        _input(MlModelId.MODEL_2_AMOUNT),
        diagnostics,
    )

    assert (result.status, result.reason_code) == ("FAILED", MODEL_EXECUTION_FAILED)
    assert result.reference_text is None
    assert diagnostics and diagnostics[0].reason_code == MODEL_EXECUTION_FAILED


def test_nonzero_and_invalid_json_are_model_local_through_existing_l1(tmp_path):
    for body in ("raise SystemExit(9)", "print('broken')"):
        model = Model2SubprocessMlModel(_child(tmp_path, body))
        diagnostics = []
        result = _run_one(
            MlModelId.MODEL_2_AMOUNT,
            model,
            _input(MlModelId.MODEL_2_AMOUNT),
            diagnostics,
        )
        assert (result.status, result.reason_code) == (
            "FAILED",
            MODEL_EXECUTION_FAILED,
        )
        assert diagnostics[-1].reason_code == MODEL_EXECUTION_FAILED


def test_subprocess_does_not_modify_parent_import_state(tmp_path):
    command = _child(
        tmp_path,
        "print(json.dumps({'predictions': [{'pred_won': 1}]}))",
    )
    model = Model2SubprocessMlModel(command)
    before_path = list(sys.path)
    before_modules = set(sys.modules)

    model.predict({"evidence_text": "원문"})

    assert sys.path == before_path
    assert set(sys.modules) == before_modules
    assert "inference" not in sys.modules
    assert not any(name.startswith("model2_") for name in sys.modules)
    assert not any(name.startswith("model3_") for name in sys.modules)


def test_subprocess_forces_child_bytecode_cleanup_environment(monkeypatch):
    captured = {}

    class Completed:
        returncode = 0
        stdout = "{}"

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessJsonRunner(
        [sys.executable, "fake-child.py"],
        environment={"PYTHONDONTWRITEBYTECODE": "0"},
    ).run({})

    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_child_entrypoint_contains_team_stdout(monkeypatch, capsys):
    """A team debug print cannot corrupt the one-object stdout protocol."""

    monkeypatch.setattr(
        ml_child.sys,
        "stdin",
        StringIO('{"evidence_text":"원문"}'),
    )

    def fake_model2(request):
        assert request == {"evidence_text": "원문"}
        print("team diagnostic")
        return {"predictions": [{"pred_won": 1}]}

    monkeypatch.setattr(ml_child, "_run_model2", fake_model2)

    assert ml_child.main(["--model", "model2"]) == 0
    captured = capsys.readouterr()
    assert captured.out == '{"predictions":[{"pred_won":1}]}\n'
    assert "team diagnostic" not in captured.out


def test_child_layout_requires_the_tracked_data_root_without_creating_it(
    monkeypatch, tmp_path
):
    missing_root = tmp_path / "ml"
    monkeypatch.setattr(ml_child, "ML_ROOT", missing_root)

    with pytest.raises(RuntimeError, match="data layout is missing"):
        ml_child._ensure_layout()

    assert not missing_root.exists()


def test_child_model2_calls_the_frozen_predict_document(monkeypatch):
    calls = {}

    class TeamModel2:
        @staticmethod
        def predict_document(text, *, base):
            calls.update(text=text, base=base)
            return {"predictions": [{"pred_won": 10}]}

    def fake_load(name, path):
        assert name == "team_model2_predict"
        assert path.name == "predict.py"
        return TeamModel2

    monkeypatch.setattr(ml_child, "_load_module", fake_load)
    payload = {
        "evidence_text": "원문",
        "title": "제목",
        "support_type": "grant",
        "quantities": {"support_ratio": ["70%"]},
    }

    assert ml_child._run_model2(payload) == {"predictions": [{"pred_won": 10}]}
    assert calls == {
        "text": "원문",
        "base": {"title": "제목", "support_type": "grant"},
    }


def test_child_model3_unwraps_score_document_result(monkeypatch):
    calls = {}
    meta = {"features": {"support_type": "grant"}}

    class TeamModel3:
        @staticmethod
        def score_document(received):
            calls["meta"] = received
            return {
                "scored": True,
                "result": {"score": 0.5, "top1_axis": "support_ratio"},
            }

    def fake_load(name, path):
        assert name == "team_model3_score"
        assert path.name == "score.py"
        return TeamModel3

    monkeypatch.setattr(ml_child, "_load_module", fake_load)
    monkeypatch.setattr(ml_child, "_adapt_for_model3", lambda payload: meta)

    assert ml_child._run_model3({"meta": meta}) == {
        "score": 0.5,
        "top1_axis": "support_ratio",
    }
    assert calls == {"meta": meta}


def test_model3_l1_quantities_are_reassembled_as_team_adapter_evidence(monkeypatch):
    calls = {}

    class TeamAdapter:
        @staticmethod
        def adapt(text, *, base):
            calls.update(text=text, base=base)
            return {"features": {"support_type": base["support_type"]}}

    def fake_load(name, path):
        assert name == "team_preconsultation_adapter"
        return TeamAdapter

    monkeypatch.setattr(ml_child, "_load_module", fake_load)
    payload = {
        "support_type": "사업화",
        "quantities": {
            "support_scale": ["기업당 400만원"],
            "cost_sharing": ["30%"],
            "support_period": ["12개월"],
            "total_budget": ["1억원"],
        },
    }

    assert ml_child._adapt_for_model3(payload) == {
        "features": {"support_type": "사업화"}
    }
    assert calls == {
        "text": "지원규모: 기업당 400만원\n자부담: 30%\n지원기간: 12개월\n총사업비: 1억원",
        "base": {"support_type": "사업화"},
    }


def test_model_commands_are_direct_argv_without_shell_interpolation():
    model2 = model2_command(python_executable="python312")
    model3 = model3_command(python_executable="python312")

    assert model2[0] == model3[0] == "python312"
    assert model2[-2:] == ("--model", "model2")
    assert model3[-2:] == ("--model", "model3")
    assert model2[1].endswith("backend\\worker\\adapters\\ml_child.py")


def test_child_jsonable_maps_nonfinite_pandas_values_to_json_null():
    assert ml_child._jsonable({"missing": float("nan")}) == {"missing": None}


@pytest.mark.parametrize(
    "payload",
    [
        {"quantities": {"support_scale": ["기업당 400만원"]}},
        {
            "support_type": "사업화",
            "support_type_status": "판단보류",
            "quantities": {"support_scale": ["기업당 400만원"]},
        },
    ],
)
def test_model3_missing_or_withheld_support_type_is_unavailable_before_child(
    tmp_path, payload
):
    """Known L1 cohort-input gaps do not become team execution failures."""

    command = _child(tmp_path, "raise SystemExit(99)")
    model = Model3SubprocessMlModel(command)
    diagnostics = []
    model_input = MlModelInput(
        model_id=MlModelId.MODEL_3_ANOMALY,
        payload=payload,
        sources=["test:quantity"],
    )

    result = _run_one(
        MlModelId.MODEL_3_ANOMALY, model, model_input, diagnostics
    )

    assert (result.status, result.reason_code) == (
        "UNAVAILABLE",
        INPUT_EVIDENCE_MISSING,
    )
    assert diagnostics[-1].reason_code == INPUT_EVIDENCE_MISSING


@pytest.mark.skipif(not _REAL_SMOKE_READY, reason="pinned ML runtime/artifacts are unavailable")
def test_run_ml_reference_reaches_real_model3_from_l1_quantities():
    """The L1 builder's quantity-only input is executable by the real child."""

    root = Path(__file__).resolve().parents[2]
    profile_path = (
        root
        / "packages"
        / "profile_structuring"
        / "examples"
        / "request"
        / "structured_profile_v012.json"
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    # Keep this fixture at the same boundary as L1: each quantity is an
    # evidence-bearing value_raw fragment, and the child/team adapter owns all
    # parsing.  The values intentionally provide three numeric axes so this is
    # a real score smoke rather than an insufficient-evidence assertion.
    profile["comparison_profile"].update(
        {
            "support_scale": [
                {"fact_id": "fact:scale", "value_raw": "기업당 400만원"}
            ],
            "cost_sharing": [
                {"fact_id": "fact:burden", "value_raw": "자부담 30%"}
            ],
            "support_period": [
                {"fact_id": "fact:period", "value_raw": "지원기간 12개월"}
            ],
            "total_budget": [
                {"fact_id": "fact:budget", "value_raw": "총사업비 1억원"}
            ],
        }
    )
    model3 = Model3SubprocessMlModel(
        model3_command(python_executable=sys.executable),
        artifact_version="model3-team-frozen",
    )
    models = {
        MlModelId.MODEL_1_SUPPORT_TYPE: FakeMlModel(
            MlModelId.MODEL_1_SUPPORT_TYPE,
            {"support_type_pred": "사업화", "status": "신뢰"},
        ),
        MlModelId.MODEL_2_AMOUNT: FakeMlModel(
            MlModelId.MODEL_2_AMOUNT,
            {"pred_won": 1},
        ),
        MlModelId.MODEL_3_ANOMALY: model3,
    }

    result = run_ml_reference(
        profile,
        models,
        cpl_result=build_cpl_result(profile),
        common_ir=None,
        title="L1 quantity smoke",
    )
    third = result.result(MlModelId.MODEL_3_ANOMALY)

    assert third.status == "OK"
    assert third.reason_code is None
    assert third.internal["top1_axis"]


@pytest.mark.skipif(not _REAL_SMOKE_READY, reason="pinned ML runtime/artifacts are unavailable")
def test_real_children_smoke_sequentially_and_keep_parent_import_state_unchanged():
    """Run both frozen artifacts once per cold child and record their timings."""

    root = Path(__file__).resolve().parents[2]
    entry = root / "backend" / "worker" / "adapters" / "ml_child.py"
    environment = dict(os.environ)
    environment.update(
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    requests = (
        (
            "model2",
            {
                "title": "테스트 사업",
                "support_type": "사업화",
                "evidence_text": "기업당 400만원, 지원비율 70%, 지원기간 12개월, 총사업비 1억원",
            },
        ),
        (
            "model3",
            {
                "support_type": "사업화",
                "quantities": {
                    "support_scale": ["기업당 400만원"],
                    "cost_sharing": ["30%"],
                    "support_period": ["12개월"],
                    "total_budget": ["1억원"],
                },
            },
        ),
    )
    before_path = list(sys.path)
    before_modules = set(sys.modules)
    timings = {}

    for model_name, payload in requests:
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, str(entry), "--model", model_name],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=root,
            env=environment,
            timeout=180,
            check=False,
        )
        timings[model_name] = time.perf_counter() - started
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        result = json.loads(completed.stdout)
        assert isinstance(result, dict)
        if model_name == "model2":
            assert result["predictions"][0]["pred_won"] > 0
        else:
            assert isinstance(result["score"], (int, float))
            assert result["top1_axis"]

    print("cold child seconds:", {k: round(v, 3) for k, v in timings.items()})
    assert sys.path == before_path
    assert set(sys.modules) == before_modules
