"""Focused contract tests for the resident GPU Model 1 service."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import time

import httpx
import pytest

import prereview_model1_service.app as model1_app
import prereview_model1_service.runtime as model1_runtime
from prereview_model1_service.app import create_app
from prereview_model1_service.contract import MODEL1_DEFAULT_MAX_REQUEST_BYTES
from prereview_model1_service.runtime import EXPECTED_WEIGHT_SHA256, canonical_input_digest
from prereview_model1_service.settings import (
    Model1ServiceConfigurationError,
    Model1ServiceSettings,
    load_settings,
)


TOKEN = "a" * 32
INPUT = {
    "title": "스마트 제조 지원",
    "purpose": "중소기업 기술 사업화",
    "content": "시제품 제작 비용 지원",
    "target_text": "국내 중소기업",
}


class FakeRuntime:
    runtime_manifest_sha256 = "b" * 64

    def __init__(self, *, delay: float = 0.0) -> None:
        self.loaded = 0
        self.delay = delay
        self.inputs: list[dict[str, str]] = []

    def load_and_warm(self) -> None:
        self.loaded += 1

    def predict(self, fields: dict[str, str]) -> dict[str, object]:
        self.inputs.append(dict(fields))
        if self.delay:
            time.sleep(self.delay)
        return {"support_type_pred": "사업화", "confidence": 0.75, "status": "신뢰"}


@pytest.fixture(autouse=True)
def inline_model_threadpool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep contract tests CPU/GPU-free while preserving an async yield point."""

    async def inline(function: object, *args: object, **kwargs: object) -> object:
        if getattr(function, "__name__", "") == "predict":
            await asyncio.sleep(0.05)
        return function(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(model1_app, "to_thread", inline)


def _settings() -> Model1ServiceSettings:
    return Model1ServiceSettings(
        bearer_token=TOKEN,
        runtime_directory=Path("/runtime/model1"),
        preprocessor_path=Path("/worker/ml/pipelines/model1/dl07_m1_apply.py"),
        require_cuda=False,
        device="cpu",
    )


def _headers(fields: dict[str, str] = INPUT) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Idempotency-Key": canonical_input_digest(fields),
    }


def _payload(fields: dict[str, str] = INPUT) -> dict[str, object]:
    return {"input": fields}


def test_ready_and_predict_envelope_are_authenticated_and_deterministic() -> None:
    runtime = FakeRuntime()
    app = create_app(settings=_settings(), runtime=runtime)  # type: ignore[arg-type]
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return (
                    await client.get("/docs"),
                    await client.get("/v1/model1/ready"),
                    await client.get("/v1/model1/ready", headers=_headers()),
                    await client.post(
                        "/v1/model1/predict", headers=_headers(), json=_payload()
                    ),
                )

    docs, unauthorized, ready, response = asyncio.run(scenario())
    assert runtime.loaded == 1
    assert docs.status_code == 404
    assert unauthorized.status_code == 401
    assert ready.status_code == 200
    assert ready.json() == {
        "ready": True,
        "max_concurrency": 1,
        "max_request_bytes": MODEL1_DEFAULT_MAX_REQUEST_BYTES,
        "device": "cpu",
        "producer": {
            "service": "prereview-model1",
            "weight_sha256": EXPECTED_WEIGHT_SHA256,
            "runtime_manifest_sha256": "b" * 64,
        },
    }
    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "prereview.model1-predict-result/v1",
        "input_digest": canonical_input_digest(INPUT),
        "producer": {
            "service": "prereview-model1",
            "weight_sha256": EXPECTED_WEIGHT_SHA256,
            "runtime_manifest_sha256": "b" * 64,
        },
        "prediction": {"support_type_pred": "사업화", "confidence": 0.75, "status": "신뢰"},
    }
    assert runtime.inputs == [INPUT]


def test_rejects_wrong_or_duplicated_auth_and_idempotency() -> None:
    app = create_app(settings=_settings(), runtime=FakeRuntime())  # type: ignore[arg-type]
    async def scenario() -> tuple[int, int, int]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                missing = await client.post("/v1/model1/predict", json=_payload())
                wrong = _headers()
                wrong["Idempotency-Key"] = "c" * 64
                wrong_key = await client.post(
                    "/v1/model1/predict", headers=wrong, json=_payload()
                )
                duplicate_auth = [
                    ("Authorization", f"Bearer {TOKEN}"),
                    ("Authorization", f"Bearer {TOKEN}"),
                    ("Content-Type", "application/json"),
                    ("Idempotency-Key", canonical_input_digest(INPUT)),
                ]
                duplicate = await client.post(
                    "/v1/model1/predict",
                    headers=duplicate_auth,
                    content=json.dumps(_payload()),
                )
                return missing.status_code, wrong_key.status_code, duplicate.status_code

    missing, wrong_key, duplicate = asyncio.run(scenario())
    assert missing == 401
    assert wrong_key == 400
    assert duplicate == 401


def test_request_shape_is_closed_and_bounded() -> None:
    app = create_app(settings=_settings(), runtime=FakeRuntime())  # type: ignore[arg-type]
    async def scenario() -> tuple[int, int, int, int, int]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                bad = {"input": {**INPUT, "unexpected": "x"}}
                bad_response = await client.post(
                    "/v1/model1/predict", headers=_headers(), json=bad
                )
                blank = {"input": {key: "  " for key in INPUT}}
                blank_response = await client.post(
                    "/v1/model1/predict", headers=_headers(blank["input"]), json=blank
                )
                duplicate = b'{"input":{"title":"x","title":"y","purpose":"","content":"","target_text":""}}'
                digest = canonical_input_digest(
                    {"title": "x", "purpose": "", "content": "", "target_text": ""}
                )
                headers = {
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": digest,
                }
                duplicate_response = await client.post(
                    "/v1/model1/predict", headers=headers, content=duplicate
                )
                media_response = await client.post(
                    "/v1/model1/predict",
                    headers={**_headers(), "Content-Type": "text/plain"},
                    content=b"x",
                )
                oversized = "x" * 70_000
                huge = {"input": {**INPUT, "content": oversized}}
                huge_response = await client.post(
                    "/v1/model1/predict", headers=_headers(huge["input"]), json=huge
                )
                return (
                    bad_response.status_code,
                    blank_response.status_code,
                    duplicate_response.status_code,
                    media_response.status_code,
                    huge_response.status_code,
                )

    assert asyncio.run(scenario()) == (400, 400, 400, 415, 413)


def test_default_request_cap_accepts_exact_boundary_and_rejects_one_more_byte() -> None:
    def request_for_size(size: int) -> tuple[dict[str, str], bytes]:
        fields = {"title": "", "purpose": "", "content": "", "target_text": ""}
        empty = json.dumps(
            {"input": fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fields["content"] = "x" * (size - len(empty))
        body = json.dumps(
            {"input": fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert len(body) == size
        return fields, body

    boundary_fields, boundary_body = request_for_size(
        MODEL1_DEFAULT_MAX_REQUEST_BYTES
    )
    oversized_fields, oversized_body = request_for_size(
        MODEL1_DEFAULT_MAX_REQUEST_BYTES + 1
    )
    runtime = FakeRuntime()
    app = create_app(settings=_settings(), runtime=runtime)  # type: ignore[arg-type]

    async def scenario() -> tuple[int, int]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                accepted = await client.post(
                    "/v1/model1/predict",
                    headers=_headers(boundary_fields),
                    content=boundary_body,
                )
                rejected = await client.post(
                    "/v1/model1/predict",
                    headers=_headers(oversized_fields),
                    content=oversized_body,
                )
                return accepted.status_code, rejected.status_code

    assert asyncio.run(scenario()) == (200, 413)
    assert runtime.inputs == [boundary_fields]


def test_predict_returns_fast_429_while_one_gpu_call_is_active() -> None:
    runtime = FakeRuntime()
    app = create_app(settings=_settings(), runtime=runtime)  # type: ignore[arg-type]
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = asyncio.create_task(
                    client.post(
                        "/v1/model1/predict", headers=_headers(), json=_payload()
                    )
                )
                await asyncio.sleep(0.02)
                second = asyncio.create_task(
                    client.post(
                        "/v1/model1/predict", headers=_headers(), json=_payload()
                    )
                )
                a, b = await asyncio.gather(first, second)
                return a, b

    responses = asyncio.run(scenario())
    assert sorted(response.status_code for response in responses) == [200, 429]
    busy = next(response for response in responses if response.status_code == 429)
    assert busy.headers["retry-after"] == "1"


@pytest.mark.parametrize("fail_after_release", [False, True])
def test_cancelled_request_keeps_capacity_until_inference_really_finishes(
    monkeypatch: pytest.MonkeyPatch,
    fail_after_release: bool,
) -> None:
    runtime = FakeRuntime()
    app = create_app(settings=_settings(), runtime=runtime)  # type: ignore[arg-type]

    async def scenario() -> httpx.Response:
        started = asyncio.Event()
        release = asyncio.Event()

        async def controlled(function: object, *args: object, **kwargs: object) -> object:
            if getattr(function, "__name__", "") == "predict":
                started.set()
                await release.wait()
                if fail_after_release:
                    raise RuntimeError("synthetic inference failure")
            return function(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(model1_app, "to_thread", controlled)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                first = asyncio.create_task(
                    client.post(
                        "/v1/model1/predict",
                        headers=_headers(),
                        json=_payload(),
                    )
                )
                await started.wait()
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                await asyncio.sleep(0)
                second = await client.post(
                    "/v1/model1/predict",
                    headers=_headers(),
                    json=_payload(),
                )
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await first
                return second

    response = asyncio.run(scenario())
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert runtime.inputs == ([] if fail_after_release else [INPUT])


def test_canonical_digest_is_only_the_exact_fixed_input_object() -> None:
    expected = sha256(
        json.dumps(INPUT, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert canonical_input_digest(dict(reversed(list(INPUT.items())))) == expected


def test_device_setting_is_closed_and_cpu_is_explicit() -> None:
    assert _settings().device == "cpu"
    with pytest.raises(ValueError):
        Model1ServiceSettings(
            bearer_token=TOKEN,
            runtime_directory=Path("/runtime/model1"),
            preprocessor_path=Path("/worker/helper.py"),
            require_cuda=True,
            device="cpu",
        )
    with pytest.raises(ValueError):
        Model1ServiceSettings(
            bearer_token=TOKEN,
            runtime_directory=Path("/runtime/model1"),
            preprocessor_path=Path("/worker/helper.py"),
            require_cuda=False,
            device="auto",
        )


def test_runpod_profile_remains_the_default() -> None:
    settings = load_settings({"PREREVIEW_MODEL1_API_BEARER_TOKEN": TOKEN})

    assert settings.runtime_directory == Path("/workspace/project/prereview-model1/model1")
    assert settings.preprocessor_path == Path(
        "/workspace/project/prereview-model1/worker/ml/pipelines/model1/dl07_m1_apply.py"
    )
    assert settings.device == "cuda"
    assert settings.require_cuda is True


def test_backend_cpu_profile_uses_fixed_paths_and_protected_token_file(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "model1-token"
    token_file.write_text(f"{TOKEN}\n", encoding="ascii")
    token_file.chmod(0o600)

    settings = load_settings(
        {
            "PREREVIEW_MODEL1_DEPLOYMENT_PROFILE": "backend-cpu",
            "PREREVIEW_MODEL1_API_BEARER_TOKEN_FILE": str(token_file),
        }
    )

    assert settings.bearer_token == TOKEN
    assert settings.runtime_directory == Path("/opt/prereview/model1")
    assert settings.preprocessor_path == Path(
        "/app/ml/pipelines/model1/dl07_m1_apply.py"
    )
    assert settings.device == "cpu"
    assert settings.require_cuda is False


def test_service_request_cap_uses_shared_default_and_accepts_bounded_override() -> None:
    default = load_settings({"PREREVIEW_MODEL1_API_BEARER_TOKEN": TOKEN})
    configured = load_settings(
        {
            "PREREVIEW_MODEL1_API_BEARER_TOKEN": TOKEN,
            "PREREVIEW_MODEL1_MAX_REQUEST_BYTES": "32768",
        }
    )

    assert default.max_request_bytes == MODEL1_DEFAULT_MAX_REQUEST_BYTES
    assert configured.max_request_bytes == 32_768


def test_runtime_identity_covers_shared_contract_and_healthcheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "model1"
    for relative in model1_runtime._RUNTIME_FILES:
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    preprocessor = tmp_path / "dl07_m1_apply.py"
    preprocessor.write_text("# fixture\n", encoding="utf-8")
    visited: list[Path] = []

    def digest(path: Path) -> str:
        visited.append(path)
        if path == runtime / "model" / "model.safetensors":
            return EXPECTED_WEIGHT_SHA256
        return "c" * 64

    monkeypatch.setattr(model1_runtime, "_file_sha256", digest)

    result = model1_runtime.verified_runtime_manifest_sha256(runtime, preprocessor)

    assert len(result) == 64
    service_root = Path(model1_runtime.__file__).resolve().parent
    assert service_root / "contract.py" in visited
    assert service_root / "healthcheck.py" in visited


@pytest.mark.parametrize(
    "override",
    [
        {"PREREVIEW_MODEL1_DEPLOYMENT_PROFILE": "unknown"},
        {
            "PREREVIEW_MODEL1_DEPLOYMENT_PROFILE": "backend-cpu",
            "PREREVIEW_MODEL1_DEVICE": "cuda",
        },
        {
            "PREREVIEW_MODEL1_DEPLOYMENT_PROFILE": "backend-cpu",
            "PREREVIEW_MODEL1_RUNTIME_DIR": "/tmp/model1",
        },
        {
            "PREREVIEW_MODEL1_DEPLOYMENT_PROFILE": "backend-cpu",
            "PREREVIEW_MODEL1_PREPROCESSOR": "/tmp/helper.py",
        },
        {"PREREVIEW_MODEL1_MAX_REQUEST_BYTES": "511"},
        {"PREREVIEW_MODEL1_MAX_REQUEST_BYTES": "1048577"},
    ],
)
def test_deployment_profiles_reject_unknown_or_incompatible_settings(
    override: dict[str, str],
) -> None:
    with pytest.raises(Model1ServiceConfigurationError):
        load_settings({"PREREVIEW_MODEL1_API_BEARER_TOKEN": TOKEN, **override})


def test_bearer_sources_are_exclusive_and_token_file_is_fail_closed(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "model1-token"
    token_file.write_text(TOKEN, encoding="ascii")
    token_file.chmod(0o600)
    common = {"PREREVIEW_MODEL1_DEPLOYMENT_PROFILE": "backend-cpu"}

    with pytest.raises(Model1ServiceConfigurationError):
        load_settings(common)
    with pytest.raises(Model1ServiceConfigurationError):
        load_settings(
            {
                **common,
                "PREREVIEW_MODEL1_API_BEARER_TOKEN": TOKEN,
                "PREREVIEW_MODEL1_API_BEARER_TOKEN_FILE": str(token_file),
            }
        )

    token_file.chmod(0o640)
    with pytest.raises(Model1ServiceConfigurationError):
        load_settings(
            {**common, "PREREVIEW_MODEL1_API_BEARER_TOKEN_FILE": str(token_file)}
        )

    token_file.chmod(0o600)
    symlink = tmp_path / "model1-token-link"
    symlink.symlink_to(token_file)
    with pytest.raises(Model1ServiceConfigurationError):
        load_settings(
            {**common, "PREREVIEW_MODEL1_API_BEARER_TOKEN_FILE": str(symlink)}
        )

    hardlink = tmp_path / "model1-token-hardlink"
    hardlink.hardlink_to(token_file)
    with pytest.raises(Model1ServiceConfigurationError):
        load_settings(
            {**common, "PREREVIEW_MODEL1_API_BEARER_TOKEN_FILE": str(token_file)}
        )
