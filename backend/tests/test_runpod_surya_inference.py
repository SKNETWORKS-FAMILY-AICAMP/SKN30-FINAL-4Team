"""Offline tests for the pinned Surya inference adapter.

The tests intentionally provide fake Pillow and Surya surfaces: importing or
running them must never require a model download, network access, GPU, or the
optional RunPod dependency set.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from prereview_runpod_worker.surya_layout_worker.handler import (
    InferenceContentFailure,
    InferenceInfrastructureFailure,
)
from prereview_runpod_worker.surya_layout_worker.surya_inference import (
    SURYA_DEPLOYMENT_ATTESTATION_SCHEMA_VERSION,
    SuryaDeploymentAttestation,
    SuryaEndpointAllowlistPolicy,
    SuryaLayoutInferenceAdapter,
    SuryaVllmEndpointSettings,
    load_deployment_attestation,
    verify_deployment_attestation,
)
from worker.contracts.accelerator import SuryaProducerIdentity


@pytest.fixture(autouse=True)
def _clean_surya_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SURYA_INFERENCE_URL",
        "SURYA_INFERENCE_BACKEND",
        "SURYA_INFERENCE_AUTOSTART",
    ):
        monkeypatch.delenv(name, raising=False)


def _producer() -> SuryaProducerIdentity:
    return SuryaProducerIdentity(
        engine_id="surya",
        engine_version="0.22.1",
        model_id="datalab-to/surya-ocr-2",
        model_revision="1" * 40,
        model_weights_sha256="2" * 64,
        pipeline_revision="pipeline-r1",
        config_sha256="3" * 64,
        worker_image_digest=f"sha256:{'4' * 64}",
    )


def _attestation(
    endpoint_url: str = "http://127.0.0.1:8000/v1",
) -> SuryaDeploymentAttestation:
    return SuryaDeploymentAttestation(
        endpoint_url=endpoint_url,
        producer_model_id="datalab-to/surya-ocr-2",
        producer_model_revision="1" * 40,
        producer_model_weights_sha256="2" * 64,
        vllm_served_model_id="datalab-to/surya-ocr-2",
        server_pid=os.getpid(),
    )


class _BombWarning(RuntimeWarning):
    pass


class _BombError(Exception):
    pass


class _FakeImage:
    def __init__(
        self,
        *,
        size: tuple[int, int] = (20, 10),
        mode: str = "RGBA",
        image_format: str | None = "PNG",
    ) -> None:
        self.size = size
        self.mode = mode
        self.format = image_format
        self.n_frames = 1
        self.is_animated = False
        self.loaded = False
        self.closed = False

    def load(self) -> None:
        self.loaded = True

    def convert(self, mode: str) -> "_FakeImage":
        assert self.loaded
        return _FakeImage(size=self.size, mode=mode, image_format=self.format)

    def close(self) -> None:
        self.closed = True


class _FakeImageModule:
    DecompressionBombWarning = _BombWarning
    DecompressionBombError = _BombError

    def __init__(self, *, size: tuple[int, int] = (20, 10)) -> None:
        self.size = size
        self.open_calls = 0

    def open(self, stream: object) -> _FakeImage:
        assert stream is not None
        self.open_calls += 1
        return _FakeImage(size=self.size)


@dataclass
class _Region:
    label: str
    bbox: object
    polygon: object
    position: int
    confidence: object = 0.75
    model_fields_set: set[str] | None = None
    text: str = "must-not-cross-boundary"

    def __post_init__(self) -> None:
        if self.model_fields_set is None:
            self.model_fields_set = {"label", "bbox", "polygon", "position", "confidence"}


@dataclass
class _OfficialLikeLayoutBox:
    """Surya 0.22.1 LayoutBox shape, including its computed bbox property."""

    polygon: list[list[float]]
    label: str
    raw_label: str
    position: int
    count: int
    confidence: float | None
    model_fields_set: set[str]

    @property
    def bbox(self) -> list[float]:
        xs = [point[0] for point in self.polygon]
        ys = [point[1] for point in self.polygon]
        return [min(xs), min(ys), max(xs), max(ys)]


def _region(
    *,
    label: str = "Text",
    position: int = 0,
    x0: int = 0,
    x1: int = 10,
    confidence: object = 0.75,
    confidence_supplied: bool = True,
) -> _Region:
    return _Region(
        label=label,
        bbox=[x0, 0, x1, 10],
        polygon=[[x0, 0], [x1, 0], [x1, 10], [x0, 10]],
        position=position,
        confidence=confidence,
        model_fields_set={"label", "bbox", "polygon", "position"}
        | ({"confidence"} if confidence_supplied else set()),
    )


def _layout(*regions: object, error: bool = False, image_bbox: object = (0, 0, 20, 10)) -> object:
    return SimpleNamespace(
        error=error,
        image_bbox=image_bbox,
        bboxes=list(regions),
        raw="provider diagnostics must be discarded",
        text="recognized text must be discarded",
    )


class _Runtime:
    def __init__(self, results: list[object]) -> None:
        self.image_module = _FakeImageModule()
        self.results = results
        self.loader_calls = 0
        self.manager_calls = 0
        self.predictor_calls = 0
        self.inference_calls = 0
        self.before_predict: object | None = None

    def loader(self) -> tuple[object, object, object]:
        self.loader_calls += 1

        def manager_factory() -> object:
            self.manager_calls += 1
            return object()

        def predictor_factory(manager: object) -> object:
            assert manager is not None
            self.predictor_calls += 1

            def predict(images: list[object]) -> list[object]:
                assert len(images) == 1
                self.inference_calls += 1
                if callable(self.before_predict):
                    self.before_predict(self.inference_calls)
                result = self.results[min(self.inference_calls - 1, len(self.results) - 1)]
                if isinstance(result, BaseException):
                    raise result
                return [result]

            return predict

        return self.image_module, manager_factory, predictor_factory


def _adapter(runtime: _Runtime, **kwargs: Any) -> SuryaLayoutInferenceAdapter:
    model_probe = kwargs.pop(
        "model_probe", lambda _: frozenset({"datalab-to/surya-ocr-2"})
    )
    live_identity = SimpleNamespace(
        start_time_ticks=123,
        actual_model_id="datalab-to/surya-ocr-2",
        served_model_ids=("datalab-to/surya-ocr-2",),
        revision="1" * 40,
    )
    live_process_probe = kwargs.pop("live_process_probe", lambda _: live_identity)
    return SuryaLayoutInferenceAdapter(
        producer=_producer(),
        endpoint=SuryaVllmEndpointSettings(url="http://127.0.0.1:8000/v1"),
        attestation=_attestation(),
        runtime_loader=runtime.loader,
        model_probe=model_probe,
        attestation_verifier=lambda _: None,
        live_process_probe=live_process_probe,
        max_png_bytes=1_000,
        max_image_pixels=1_000,
        max_regions_per_page=10,
        **kwargs,
    )


def _infer(adapter: SuryaLayoutInferenceAdapter) -> object:
    return adapter.layout(
        page_number=1,
        png_bytes=b"already-portably-validated-png",
        pixel_width=20,
        pixel_height=10,
    )


def test_success_uses_one_lazy_predictor_sorts_positions_and_discards_extra_fields() -> None:
    runtime = _Runtime(
        [
            _layout(
                _region(label="Table", position=1, x0=10, x1=20),
                _region(label="Text", position=0, x0=0, x1=10),
            )
        ]
    )
    adapter = _adapter(runtime)

    first = _infer(adapter)
    second = _infer(adapter)

    assert runtime.loader_calls == runtime.manager_calls == runtime.predictor_calls == 1
    assert runtime.inference_calls == 2
    assert [region["position"] for region in first.regions] == [0, 1]
    assert [region["label"] for region in first.regions] == ["Text", "Table"]
    assert first.regions[0]["confidence"] == 0.75
    assert all("text" not in region and "raw" not in region for region in first.regions)
    assert first.confidence_marker is not None
    assert first.confidence_marker.producer == _producer()
    assert second.confidence_marker == first.confidence_marker


def test_initialize_builds_runtime_and_checks_the_attested_vllm_model_once() -> None:
    runtime = _Runtime([_layout()])
    probes: list[str] = []
    adapter = _adapter(runtime, model_probe=lambda endpoint: probes.append(endpoint) or frozenset({"datalab-to/surya-ocr-2"}))

    adapter.initialize()
    adapter.initialize()

    assert runtime.loader_calls == runtime.manager_calls == runtime.predictor_calls == 1
    assert probes == ["http://127.0.0.1:8000/v1"]


def test_initialize_rejects_a_vllm_model_id_not_bound_by_the_launcher_attestation() -> None:
    runtime = _Runtime([_layout()])
    adapter = _adapter(runtime, model_probe=lambda _: frozenset({"different-model"}))

    with pytest.raises(InferenceInfrastructureFailure, match="^surya_vllm_identity_mismatch$"):
        adapter.initialize()


def test_every_inference_revalidates_process_start_identity_and_served_model() -> None:
    runtime = _Runtime([_layout(), _layout()])
    stable = SimpleNamespace(
        start_time_ticks=123,
        actual_model_id="datalab-to/surya-ocr-2",
        served_model_ids=("datalab-to/surya-ocr-2",),
        revision="1" * 40,
    )
    replaced = SimpleNamespace(
        start_time_ticks=124,
        actual_model_id="datalab-to/surya-ocr-2",
        served_model_ids=("datalab-to/surya-ocr-2",),
        revision="1" * 40,
    )
    live_results = iter((stable, stable, replaced))
    model_results = iter(
        (
            frozenset({"datalab-to/surya-ocr-2"}),
            frozenset({"datalab-to/surya-ocr-2"}),
        )
    )
    adapter = _adapter(
        runtime,
        live_process_probe=lambda _: next(live_results),
        model_probe=lambda _: next(model_results),
    )

    assert _infer(adapter).error is False
    with pytest.raises(InferenceInfrastructureFailure, match="^surya_attestation_invalid$"):
        _infer(adapter)
    assert runtime.inference_calls == 1


def test_layout_installs_the_remaining_absolute_budget_as_surya_request_timeout() -> None:
    runtime = _Runtime([_layout()])
    installed: list[float] = []

    @contextmanager
    def timeout_scope(seconds: float) -> Any:
        installed.append(seconds)
        yield

    adapter = _adapter(
        runtime,
        monotonic=lambda: 10.0,
        timeout_override_factory=timeout_scope,
    )
    result = adapter.layout(
        page_number=1,
        png_bytes=b"already-portably-validated-png",
        pixel_width=20,
        pixel_height=10,
        deadline_monotonic=14.5,
    )

    assert result.error is False
    assert installed == [4.5]
    assert runtime.inference_calls == 1


def test_layout_does_not_start_inference_when_absolute_deadline_has_expired() -> None:
    runtime = _Runtime([_layout()])
    adapter = _adapter(runtime, monotonic=lambda: 11.0)

    with pytest.raises(InferenceInfrastructureFailure, match="^surya_inference_timeout$"):
        adapter.layout(
            page_number=1,
            png_bytes=b"already-portably-validated-png",
            pixel_width=20,
            pixel_height=10,
            deadline_monotonic=10.0,
        )
    assert runtime.inference_calls == 0


def test_keyboard_interrupt_from_runtime_or_vllm_probe_is_not_redacted() -> None:
    runtime = _Runtime([_layout()])
    interrupting = _adapter(runtime, model_probe=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        interrupting.initialize()

    def interrupted_loader() -> tuple[object, object, object]:
        raise KeyboardInterrupt()

    adapter = SuryaLayoutInferenceAdapter(
        producer=_producer(),
        endpoint=SuryaVllmEndpointSettings(url="http://127.0.0.1:8000/v1"),
        attestation=_attestation(),
        runtime_loader=interrupted_loader,
        model_probe=lambda _: frozenset({"datalab-to/surya-ocr-2"}),
        attestation_verifier=lambda _: None,
        live_process_probe=lambda _: SimpleNamespace(
            start_time_ticks=123,
            actual_model_id="datalab-to/surya-ocr-2",
            served_model_ids=("datalab-to/surya-ocr-2",),
            revision="1" * 40,
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        adapter.initialize()


def test_launcher_attestation_is_strict_regular_file_and_binds_identity(tmp_path: Any) -> None:
    path = tmp_path / "surya-vllm.json"
    payload = {
        "schema_version": SURYA_DEPLOYMENT_ATTESTATION_SCHEMA_VERSION,
        "endpoint_url": "http://127.0.0.1:8000/v1",
        "producer": {
            "model_id": "datalab-to/surya-ocr-2",
            "model_revision": "1" * 40,
            "model_weights_sha256": "2" * 64,
        },
        "vllm_served_model_id": "datalab-to/surya-ocr-2",
        "server_pid": os.getpid(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    attestation = load_deployment_attestation(path)
    attestation.assert_matches(
        producer=_producer(),
        endpoint=SuryaVllmEndpointSettings(url="http://127.0.0.1:8000/v1"),
    )

    payload["producer"]["model_weights_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="^deployment attestation unavailable$"):
        load_deployment_attestation(path)


def test_live_attestation_requires_exact_process_revision_and_rehashed_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _attestation()
    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._read_process_command_line",
        lambda _: ("vllm", "serve", "datalab-to/surya-ocr-2", "--revision", "1" * 40),
    )
    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._read_process_start_time",
        lambda _: 123,
    )
    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._resolve_attested_weights_path",
        lambda _: "/fixed/cache/blob",
    )
    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._sha256_file",
        lambda _: "2" * 64,
    )

    verify_deployment_attestation(attestation)

    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._read_process_command_line",
        lambda _: (
            "/usr/local/bin/python3",
            "/fixed/venv/bin/vllm",
            "serve",
            "datalab-to/surya-ocr-2",
            "--served-model-name",
            "datalab-to/surya-ocr-2",
            "--revision",
            "1" * 40,
        ),
    )
    verify_deployment_attestation(attestation)

    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._sha256_file",
        lambda _: "f" * 64,
    )
    with pytest.raises(InferenceInfrastructureFailure, match="^surya_attestation_invalid$"):
        verify_deployment_attestation(attestation)


def test_live_attestation_distinguishes_actual_model_from_served_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _attestation()
    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._read_process_start_time",
        lambda _: 123,
    )
    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._resolve_attested_weights_path",
        lambda _: "/fixed/cache/blob",
    )
    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._sha256_file",
        lambda _: "2" * 64,
    )
    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._read_process_command_line",
        lambda _: (
            "vllm",
            "serve",
            "attacker/model",
            "--served-model-name",
            "datalab-to/surya-ocr-2",
            "--revision",
            "1" * 40,
        ),
    )

    with pytest.raises(InferenceInfrastructureFailure, match="^surya_attestation_invalid$"):
        verify_deployment_attestation(attestation)

    monkeypatch.setattr(
        "prereview_runpod_worker.surya_layout_worker.surya_inference._read_process_command_line",
        lambda _: (
            "python",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model=datalab-to/surya-ocr-2",
            "--served-model-name",
            "datalab-to/surya-ocr-2",
            "--revision=" + "1" * 40,
        ),
    )
    verify_deployment_attestation(attestation)


def test_shared_predictor_calls_are_serialized() -> None:
    runtime = _Runtime([_layout()])
    first_entered = Event()
    second_entered = Event()
    release_first = Event()

    def block_first(call_number: int) -> None:
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()

    runtime.before_predict = block_first
    adapter = _adapter(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_infer, adapter)
        assert first_entered.wait(timeout=1)
        second = pool.submit(_infer, adapter)
        assert not second_entered.wait(timeout=0.05)
        release_first.set()
        assert first.result(timeout=1).error is False
        assert second.result(timeout=1).error is False
    assert second_entered.is_set()


def test_official_0221_computed_bbox_shape_is_adapted_without_raw_fields() -> None:
    region = _OfficialLikeLayoutBox(
        polygon=[[0, 0], [20, 0], [20, 10], [0, 10]],
        label="Diagram",
        raw_label="Diagram",
        position=0,
        count=150,
        confidence=0.81,
        model_fields_set={
            "polygon",
            "label",
            "raw_label",
            "position",
            "count",
            "confidence",
        },
    )
    runtime = _Runtime([_layout(region)])

    result = _infer(_adapter(runtime))

    assert result.regions == (
        {
            "label": "Diagram",
            "bbox": (0.0, 0.0, 20.0, 10.0),
            "polygon": ((0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)),
            "position": 0,
            "confidence": 0.81,
        },
    )


def test_confidence_without_source_field_attestation_is_omitted() -> None:
    runtime = _Runtime([_layout(_region(confidence=1.0, confidence_supplied=False))])

    result = _infer(_adapter(runtime))

    assert "confidence" not in result.regions[0]


def test_surya_0221_synthetic_one_confidence_is_conservatively_omitted() -> None:
    # Surya 0.22.1 explicitly passes confidence=1.0 into LayoutBox when the
    # provider omitted mean_token_prob, so Pydantic's fields-set is not enough
    # to prove that this specific value came from the model.
    runtime = _Runtime([_layout(_region(confidence=1.0, confidence_supplied=True))])

    result = _infer(_adapter(runtime))

    assert "confidence" not in result.regions[0]


def test_in_band_layout_error_is_retryable_infrastructure_failure() -> None:
    runtime = _Runtime([_layout(error=True)])

    with pytest.raises(
        InferenceInfrastructureFailure,
        match="^surya_inference_rejected$",
    ):
        _infer(_adapter(runtime))


def test_image_bbox_must_match_decoded_canvas() -> None:
    runtime = _Runtime([_layout(image_bbox=(0, 0, 19, 10))])

    with pytest.raises(InferenceContentFailure, match="^surya_result_canvas_mismatch$"):
        _infer(_adapter(runtime))


@pytest.mark.parametrize(
    "regions",
    [
        (_region(position=0), _region(position=0, x0=10, x1=20)),
        (_region(position=0), _region(position=2, x0=10, x1=20)),
    ],
)
def test_duplicate_or_noncontiguous_positions_fail_closed(regions: tuple[object, ...]) -> None:
    runtime = _Runtime([_layout(*regions)])

    with pytest.raises(InferenceContentFailure, match="^surya_result_invalid$"):
        _infer(_adapter(runtime))


@pytest.mark.parametrize(
    "region",
    [
        _region(label="UnreviewedLabel"),
        _Region(
            label="Text",
            bbox=[0, 0, 10, 10],
            polygon=[[0, 0], [11, 0], [11, 10], [0, 10]],
            position=0,
        ),
        _Region(
            label="Text",
            bbox=[0, 0, 21, 10],
            polygon=[[0, 0], [21, 0], [21, 10], [0, 10]],
            position=0,
        ),
    ],
)
def test_wrong_label_or_geometry_is_content_failure(region: object) -> None:
    runtime = _Runtime([_layout(region)])

    with pytest.raises(InferenceContentFailure, match="^surya_result_invalid$"):
        _infer(_adapter(runtime))


def test_endpoint_defaults_to_loopback_and_non_loopback_requires_exact_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime([_layout()])
    with pytest.raises(ValueError, match="non-loopback"):
        SuryaLayoutInferenceAdapter(
            producer=_producer(),
            endpoint=SuryaVllmEndpointSettings(url="https://gpu.example.test:8443/v1"),
            attestation=_attestation("https://gpu.example.test:8443/v1"),
            runtime_loader=runtime.loader,
        )

    policy = SuryaEndpointAllowlistPolicy(
        allowed_origins=("https://gpu.example.test:8443",)
    )
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://proxy.example.test:8080")
    adapter = SuryaLayoutInferenceAdapter(
        producer=_producer(),
        endpoint=SuryaVllmEndpointSettings(url="https://gpu.example.test:8443"),
        endpoint_policy=policy,
        attestation=_attestation("https://gpu.example.test:8443/v1"),
        runtime_loader=runtime.loader,
        model_probe=lambda _: frozenset({"datalab-to/surya-ocr-2"}),
    )
    assert adapter is not None
    assert os.environ["SURYA_INFERENCE_URL"] == "https://gpu.example.test:8443/v1"
    assert os.environ["SURYA_INFERENCE_BACKEND"] == "vllm"
    assert os.environ["SURYA_INFERENCE_AUTOSTART"] == "false"
    assert os.environ["NO_PROXY"] == "*"
    assert os.environ["no_proxy"] == "*"
    assert all(
        name not in os.environ
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        )
    )


def test_non_loopback_endpoint_rejects_plain_http_even_when_allowlisted() -> None:
    runtime = _Runtime([_layout()])
    policy = SuryaEndpointAllowlistPolicy(
        allowed_origins=("http://gpu.example.test:8000",)
    )

    with pytest.raises(ValueError, match="must use HTTPS"):
        SuryaLayoutInferenceAdapter(
            producer=_producer(),
            endpoint=SuryaVllmEndpointSettings(
                url="http://gpu.example.test:8000/v1"
            ),
            endpoint_policy=policy,
            attestation=_attestation("http://gpu.example.test:8000/v1"),
            runtime_loader=runtime.loader,
        )


def test_provider_failure_is_infrastructure_failure_with_no_message_url_or_cause() -> None:
    secret = "https://gpu.example.test/v1?token=do-not-leak"
    runtime = _Runtime([RuntimeError(secret)])

    with pytest.raises(InferenceInfrastructureFailure) as captured:
        _infer(_adapter(runtime))

    rendered = str(captured.value)
    assert rendered == "surya_inference_unavailable"
    assert "gpu.example" not in rendered
    assert "token" not in rendered
    assert captured.value.__cause__ is None


def test_decoded_geometry_mismatch_is_content_failure_before_inference() -> None:
    runtime = _Runtime([_layout()])
    runtime.image_module.size = (21, 10)

    with pytest.raises(InferenceContentFailure, match="^surya_png_decode_rejected$"):
        _infer(_adapter(runtime))

    assert runtime.inference_calls == 0
