"""Focused contracts for the combined resident Model 2/3 CPU service."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

import prereview_model23_service.app as model23_app
import prereview_model23_service.runtime as model23_runtime
from prereview_model23_service.app import create_app
from prereview_model23_service.contract import (
    MODEL23_DEFAULT_MAX_REQUEST_BYTES,
    MODEL23_MAX_CONCURRENCY,
    MODEL2_RESULT_SCHEMA_VERSION,
    MODEL3_RESULT_SCHEMA_VERSION,
)
from prereview_model23_service.runtime import (
    EXPECTED_MODEL2_BUNDLE_SHA256,
    EXPECTED_MODEL3_POOL_SHA256,
    Model23Runtime,
    Model23RuntimeError,
    canonical_input_digest,
)
from prereview_model23_service.settings import (
    Model23ServiceConfigurationError,
    Model23ServiceSettings,
    load_settings,
)


TOKEN = "resident_model23_test_token_0123456789"
MANIFEST = "b" * 64
MODEL2_INPUT: dict[str, object] = {
    "title": "스마트 사업화 지원",
    "evidence_text": "지원기간 12개월, 기업당 최대 1억원, 사업비의 70% 지원",
    "quantities": {"support_scale": ["기업당 최대 1억원"]},
    "support_type": "사업화",
    "support_type_status": "신뢰",
    "support_type_model_version": "model1-external-serving-v1",
}
MODEL3_INPUT: dict[str, object] = {
    "evidence_text": "지원기간 12개월, 기업당 최대 1억원, 사업비의 70% 지원",
    "quantities": {
        "support_scale": ["기업당 최대 1억원"],
        "support_period": ["지원기간 12개월"],
    },
    "support_type": "사업화",
    "support_type_status": "신뢰",
    "support_type_model_version": "model1-external-serving-v1",
}


def _model2_raw() -> dict[str, object]:
    return {
        "model": "model2_p3",
        "n": 1,
        "context_digit_residue": 0,
        "predictions": [
            {
                "row_id": None,
                "input_completeness": "partial",
                "missing_features": ["support_count"],
                "status": "참고",
                "pred_log10": 8.0,
                "pred_won": 100_000_000,
                "bucket_proba": {"Low": 0.1, "Mid": 0.8, "High": 0.1},
                "bucket": "Mid",
                "bucket_edges_won": [10_000_000, 500_000_000],
                "proximity": {
                    "prox_support_rate": 70.0,
                    "prox_self_burden_rate": None,
                    "prox_selected_count": None,
                    "prox_duration_months": 12.0,
                },
                "evidence_quality": "medium",
            }
        ],
        "adapter": {
            "program_duration_years": None,
            "project_duration_years": 1.0,
            "duration_evidence": {},
            "support_count_candidates": [],
            "support_count_basis": "no_new_project_count",
            "amounts": {},
            "review": [],
        },
    }


def _model3_row() -> dict[str, object]:
    return {
        "row_id": "REQ00000",
        "score": 0.94,
        "level": "L1",
        "cohort_key": ["사업화", "grant"],
        "cohort_n": 50,
        "top1_axis": "support_ratio",
    }


class FakeRuntime:
    runtime_manifest_sha256 = MANIFEST

    def __init__(self, *, delayed: bool = False) -> None:
        self.loaded = 0
        self.delayed = delayed
        self.model2_inputs: list[dict[str, object]] = []
        self.model3_inputs: list[dict[str, object]] = []

    def load_and_warm(self) -> None:
        self.loaded += 1

    def predict_model2(self, payload: dict[str, object]) -> dict[str, object]:
        self.model2_inputs.append(dict(payload))
        return _model2_raw()

    def predict_model3(self, payload: dict[str, object]) -> dict[str, object]:
        self.model3_inputs.append(dict(payload))
        return _model3_row()


@pytest.fixture(autouse=True)
def inline_model_threadpool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep contract tests independent of the host executor."""

    async def inline(function: object, *args: object, **kwargs: object) -> object:
        await asyncio.sleep(0)
        return function(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(model23_app, "to_thread", inline)


def _settings(*, max_request_bytes: int = MODEL23_DEFAULT_MAX_REQUEST_BYTES) -> Model23ServiceSettings:
    return Model23ServiceSettings(
        bearer_token=TOKEN,
        ml_root=Path("/app/ml"),
        max_request_bytes=max_request_bytes,
    )


def _headers(payload: dict[str, object]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Idempotency-Key": canonical_input_digest(payload),
    }


def _producer() -> dict[str, str]:
    return {
        "service": "prereview-model23",
        "runtime_manifest_sha256": MANIFEST,
        "model2_bundle_sha256": EXPECTED_MODEL2_BUNDLE_SHA256,
        "model3_pool_sha256": EXPECTED_MODEL3_POOL_SHA256,
    }


def test_ready_and_both_prediction_envelopes_are_closed_and_authenticated() -> None:
    runtime = FakeRuntime()
    app = create_app(settings=_settings(), runtime=runtime)  # type: ignore[arg-type]

    async def scenario() -> tuple[httpx.Response, ...]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return (
                    await client.get("/docs"),
                    await client.get("/v1/model23/ready"),
                    await client.get(
                        "/v1/model23/ready", headers={"Authorization": f"Bearer {TOKEN}"}
                    ),
                    await client.post(
                        "/v1/model2/predict",
                        headers=_headers(MODEL2_INPUT),
                        json={"input": MODEL2_INPUT},
                    ),
                    await client.post(
                        "/v1/model3/predict",
                        headers=_headers(MODEL3_INPUT),
                        json={"input": MODEL3_INPUT},
                    ),
                )

    docs, unauthorized, ready, model2, model3 = asyncio.run(scenario())
    assert runtime.loaded == 1
    assert docs.status_code == 404
    assert unauthorized.status_code == 401
    assert ready.json() == {
        "ready": True,
        "max_concurrency": MODEL23_MAX_CONCURRENCY,
        "max_request_bytes": MODEL23_DEFAULT_MAX_REQUEST_BYTES,
        "device": "cpu",
        "producer": _producer(),
    }
    assert model2.json() == {
        "schema_version": MODEL2_RESULT_SCHEMA_VERSION,
        "input_digest": canonical_input_digest(MODEL2_INPUT),
        "producer": _producer(),
        "prediction": _model2_raw(),
    }
    assert model3.json() == {
        "schema_version": MODEL3_RESULT_SCHEMA_VERSION,
        "input_digest": canonical_input_digest(MODEL3_INPUT),
        "producer": _producer(),
        "prediction": _model3_row(),
    }
    assert runtime.model2_inputs == [MODEL2_INPUT]
    assert runtime.model3_inputs == [MODEL3_INPUT]


def test_model2_without_model1_carry_remains_valid() -> None:
    payload = {key: MODEL2_INPUT[key] for key in ("title", "evidence_text", "quantities")}
    runtime = FakeRuntime()
    app = create_app(settings=_settings(), runtime=runtime)  # type: ignore[arg-type]

    async def scenario() -> int:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/model2/predict",
                    headers=_headers(payload),
                    json={"input": payload},
                )
                return response.status_code

    assert asyncio.run(scenario()) == 200
    assert runtime.model2_inputs == [payload]


def test_requests_are_closed_bounded_and_bound_to_idempotency_key() -> None:
    app = create_app(
        settings=_settings(max_request_bytes=65_536), runtime=FakeRuntime()  # type: ignore[arg-type]
    )

    async def scenario() -> tuple[int, ...]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                extra = {**MODEL2_INPUT, "unexpected": True}
                partial_carry = {
                    key: value
                    for key, value in MODEL2_INPUT.items()
                    if key != "support_type_model_version"
                }
                unknown_quantity = {
                    **MODEL3_INPUT,
                    "quantities": {"untrusted": ["x"]},
                }
                wrong_key = {**_headers(MODEL2_INPUT), "Idempotency-Key": "c" * 64}
                oversized = {
                    **MODEL2_INPUT,
                    "evidence_text": "x" * 70_000,
                }
                responses = (
                    await client.post(
                        "/v1/model2/predict",
                        headers=_headers(extra),
                        json={"input": extra},
                    ),
                    await client.post(
                        "/v1/model2/predict",
                        headers=_headers(partial_carry),
                        json={"input": partial_carry},
                    ),
                    await client.post(
                        "/v1/model3/predict",
                        headers=_headers(unknown_quantity),
                        json={"input": unknown_quantity},
                    ),
                    await client.post(
                        "/v1/model2/predict",
                        headers=wrong_key,
                        json={"input": MODEL2_INPUT},
                    ),
                    await client.post(
                        "/v1/model2/predict",
                        headers=_headers(oversized),
                        json={"input": oversized},
                    ),
                )
                return tuple(response.status_code for response in responses)

    assert asyncio.run(scenario()) == (400, 400, 400, 400, 413)


def test_model2_and_model3_share_one_capacity_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    app = create_app(settings=_settings(), runtime=runtime)  # type: ignore[arg-type]

    async def delayed(function: object, *args: object, **kwargs: object) -> object:
        if getattr(function, "__name__", "").startswith("predict_model"):
            await asyncio.sleep(0.08)
        return function(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(model23_app, "to_thread", delayed)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = asyncio.create_task(
                    client.post(
                        "/v1/model2/predict",
                        headers=_headers(MODEL2_INPUT),
                        json={"input": MODEL2_INPUT},
                    )
                )
                await asyncio.sleep(0.02)
                second = asyncio.create_task(
                    client.post(
                        "/v1/model3/predict",
                        headers=_headers(MODEL3_INPUT),
                        json={"input": MODEL3_INPUT},
                    )
                )
                return await asyncio.gather(first, second)

    responses = asyncio.run(scenario())
    assert sorted(response.status_code for response in responses) == [200, 429]
    busy = next(response for response in responses if response.status_code == 429)
    assert busy.headers["retry-after"] == "1"
    assert runtime.model2_inputs == [MODEL2_INPUT]
    assert runtime.model3_inputs == []


@pytest.mark.parametrize("fail_after_release", [False, True])
def test_cancelled_model2_request_keeps_shared_slot_until_thread_finishes(
    monkeypatch: pytest.MonkeyPatch,
    fail_after_release: bool,
) -> None:
    runtime = FakeRuntime()
    app = create_app(settings=_settings(), runtime=runtime)  # type: ignore[arg-type]

    async def scenario() -> httpx.Response:
        started = asyncio.Event()
        release = asyncio.Event()

        async def controlled(function: object, *args: object, **kwargs: object) -> object:
            if getattr(function, "__name__", "") == "predict_model2":
                started.set()
                await release.wait()
                if fail_after_release:
                    raise RuntimeError("synthetic inference failure")
            return function(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(model23_app, "to_thread", controlled)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                first = asyncio.create_task(
                    client.post(
                        "/v1/model2/predict",
                        headers=_headers(MODEL2_INPUT),
                        json={"input": MODEL2_INPUT},
                    )
                )
                await started.wait()
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                await asyncio.sleep(0)
                second = await client.post(
                    "/v1/model3/predict",
                    headers=_headers(MODEL3_INPUT),
                    json={"input": MODEL3_INPUT},
                )
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await first
                return second

    response = asyncio.run(scenario())
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert runtime.model2_inputs == ([] if fail_after_release else [MODEL2_INPUT])
    assert runtime.model3_inputs == []


def test_runtime_loads_each_model_once_with_collision_safe_module_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Adapter:
        @staticmethod
        def adapt(text: str, *, base: dict[str, object]) -> dict[str, object]:
            assert text
            return {"features": dict(base), "duration_evidence": {}}

    class Model2:
        PA = Adapter
        load_count = 0

        @classmethod
        def load(cls) -> object:
            cls.load_count += 1
            return object()

        @staticmethod
        def predict_document(text: str, *, base: dict[str, object]) -> dict[str, object]:
            assert text and isinstance(base, dict)
            return _model2_raw()

    class Implementation:
        pool_load_count = 0

        @classmethod
        def _get_pool(cls) -> list[object]:
            cls.pool_load_count += 1
            return [object()]

    class Model3:
        _impl = Implementation

        @staticmethod
        def score_document(meta: dict[str, object]) -> dict[str, object]:
            assert isinstance(meta, dict)
            return {"scored": True, "result": _model3_row()}

    def fake_load(name: str, _path: Path) -> object:
        calls.append(name)
        return Model2 if name.endswith("model2_predict") else Model3

    monkeypatch.setattr(model23_runtime, "verified_runtime_manifest_sha256", lambda _: MANIFEST)
    monkeypatch.setattr(model23_runtime, "_verify_runtime_dependencies", lambda: None)
    monkeypatch.setattr(model23_runtime, "_load_module", fake_load)
    runtime = Model23Runtime(ml_root=tmp_path)

    runtime.load_and_warm()
    runtime.load_and_warm()
    model2_result = runtime.predict_model2(MODEL2_INPUT)
    model3_result = runtime.predict_model3(MODEL3_INPUT)

    assert calls == [
        "prereview_resident_model2_predict",
        "prereview_resident_model3_score",
    ]
    assert Model2.load_count == 1
    assert Implementation.pool_load_count == 1
    # The resident boundary preserves the exact serving shapes consumed by
    # the existing worker normalizers.
    assert model2_result == _model2_raw()
    assert model3_result == _model3_row()
    assert runtime.runtime_manifest_sha256 == MANIFEST


def test_runtime_dependencies_are_exactly_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = dict(model23_runtime._REQUIRED_RUNTIME_DISTRIBUTIONS)
    observed: list[str] = []

    def exact_version(distribution: str) -> str:
        observed.append(distribution)
        return expected[distribution]

    monkeypatch.setattr(model23_runtime.metadata, "version", exact_version)
    model23_runtime._verify_runtime_dependencies()
    assert observed == list(expected)

    monkeypatch.setattr(
        model23_runtime.metadata,
        "version",
        lambda distribution: "0" if distribution == "numpy" else expected[distribution],
    )
    with pytest.raises(Model23RuntimeError):
        model23_runtime._verify_runtime_dependencies()


def test_runtime_manifest_verifies_artifact_bytes_before_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "registered.bin"
    source = tmp_path / "runtime.py"
    artifact.write_bytes(b"registered artifact")
    source.write_text("VALUE = 1\n", encoding="utf-8")
    expected = sha256(artifact.read_bytes()).hexdigest()

    monkeypatch.setattr(model23_runtime, "_ARTIFACTS", {"registered.bin": expected})
    monkeypatch.setattr(
        model23_runtime,
        "_runtime_source_paths",
        lambda _root: [("runtime.py", source)],
    )
    identity = model23_runtime.verified_runtime_manifest_sha256(tmp_path)
    assert len(identity) == 64

    artifact.write_bytes(b"tampered artifact")
    with pytest.raises(Model23RuntimeError):
        model23_runtime.verified_runtime_manifest_sha256(tmp_path)


def test_settings_use_fixed_cpu_root_port_cap_and_protected_token(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "model23-token"
    token_file.write_text(f"{TOKEN}\n", encoding="ascii")
    token_file.chmod(0o600)

    settings = load_settings(
        {"PREREVIEW_MODEL23_API_BEARER_TOKEN_FILE": str(token_file)}
    )
    assert settings.ml_root == Path("/app/ml")
    assert settings.port == 8792
    assert settings.max_request_bytes == MODEL23_DEFAULT_MAX_REQUEST_BYTES
    assert TOKEN not in repr(settings)

    with pytest.raises(Model23ServiceConfigurationError):
        load_settings(
            {
                "PREREVIEW_MODEL23_API_BEARER_TOKEN": TOKEN,
                "PREREVIEW_ML_ROOT": "/unregistered/ml",
            }
        )


def test_canonical_digest_hashes_payload_not_http_envelope() -> None:
    expected = sha256(
        json.dumps(
            MODEL2_INPUT,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert canonical_input_digest(dict(reversed(list(MODEL2_INPUT.items())))) == expected


def test_actual_resident_outputs_match_legacy_one_shot_boundary_when_ml_runtime_exists() -> None:
    """Differential check executed inside the ML image; lightweight CI skips it."""

    dependencies = ("joblib", "numpy", "pandas", "pyarrow", "scipy", "sklearn", "xgboost")
    missing = [name for name in dependencies if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip("ML runtime dependencies are not installed: " + ", ".join(missing))

    repository_root = Path(__file__).resolve().parents[2]
    ml_root = repository_root / "ml"
    evidence = (
        ml_root / "serving" / "shared" / "fixtures" / "preconsultation_example.txt"
    ).read_text(encoding="utf-8")
    model2_payload = {**MODEL2_INPUT, "evidence_text": evidence}
    model3_payload = {**MODEL3_INPUT, "evidence_text": evidence}
    child = repository_root / "backend" / "worker" / "adapters" / "ml_child.py"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PREREVIEW_ML_ROOT": str(ml_root),
    }

    def legacy(model: str, payload: dict[str, object]) -> object:
        completed = subprocess.run(
            (sys.executable, str(child), "--model", model),
            input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=environment,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    expected2 = legacy("model2", model2_payload)
    expected3 = legacy("model3", model3_payload)
    resident = Model23Runtime(ml_root=ml_root)
    resident.load_and_warm()

    assert resident.predict_model2(model2_payload) == expected2
    assert resident.predict_model3(model3_payload) == expected3
