"""Offline tests for strict RunPod Surya worker cold-start settings."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any

import pytest

from prereview_runpod_worker.surya_layout_worker.settings import (
    PREREVIEW_SURYA_WORKER_CONFIG_JSON,
    SURYA_WORKER_CONFIG_SCHEMA_VERSION,
    SuryaWorkerConfigurationError,
    build_worker_composition,
    load_worker_settings,
)
from prereview_runpod_worker.surya_layout_worker.surya_inference import (
    SuryaDeploymentAttestation,
)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _config() -> dict[str, Any]:
    return {
        "schema_version": SURYA_WORKER_CONFIG_SCHEMA_VERSION,
        "storage_scopes": [
            {
                "method": "GET",
                "origin": "https://storage.example.test",
                "endpoint_path_template": "/storage/v1/object/sign/{bucket}/{object_key}",
                "bucket": "request-temp",
                "object_key_prefix": "input/",
            },
            {
                "method": "PUT",
                "origin": "https://storage.example.test",
                "endpoint_path_template": "/storage/v1/object/upload/sign/{bucket}/{object_key}",
                "bucket": "request-temp",
                "object_key_prefix": "accelerator/surya-layout/",
            },
        ],
        "dispatch_policy": {
            "max_ttl_seconds": 120,
            "max_execution_timeout_seconds": 60,
            "max_page_count": 5,
            "max_total_input_bytes": 1_000_000,
            "max_total_rendered_pixels": 10_000_000,
            "max_capability_bytes": 1_000_000,
            "allowed_mime_types": ["application/json", "image/png"],
        },
        "producer": {
            "engine_id": "surya",
            "engine_version": "0.22.1",
            "model_id": "surya-layout",
            "model_revision": "1" * 40,
            "model_weights_sha256": _hash("weights"),
            "pipeline_revision": "pipeline-r1",
            "config_sha256": _hash("configuration"),
            "worker_image_digest": f"sha256:{_hash('worker-image')}",
        },
        "http": {
            "hard_max_bytes": 1_000_000,
            "connect_timeout_seconds": 5.0,
            "read_timeout_seconds": 30.0,
            "write_timeout_seconds": 30.0,
            "pool_timeout_seconds": 5.0,
        },
        "output": {
            "max_output_bytes": 200_000,
            "max_terminal_output_bytes": 20_000,
            "max_regions_per_page": 200,
        },
        "surya_endpoint": {"url": "http://127.0.0.1:8000/v1"},
    }


def _attestation(config: dict[str, Any]) -> SuryaDeploymentAttestation:
    producer = config["producer"]
    return SuryaDeploymentAttestation(
        endpoint_url=config["surya_endpoint"]["url"],
        producer_model_id=producer["model_id"],
        producer_model_revision=producer["model_revision"],
        producer_model_weights_sha256=producer["model_weights_sha256"],
        vllm_served_model_id="datalab-to/surya-ocr-2",
        server_pid=os.getpid(),
    )


def _environment(config: dict[str, Any]) -> dict[str, str]:
    return {PREREVIEW_SURYA_WORKER_CONFIG_JSON: json.dumps(config)}


def test_loads_one_strict_document_into_deployment_owned_contracts() -> None:
    settings = load_worker_settings(_environment(_config()))

    assert tuple(scope.method for scope in settings.dispatch_policy.allowed_scopes) == (
        "GET",
        "PUT",
    )
    assert settings.dispatch_policy.allowed_mime_types == ("application/json", "image/png")
    assert settings.producer.model_revision == "1" * 40
    assert settings.endpoint.url == "http://127.0.0.1:8000/v1"
    assert settings.endpoint_policy is None
    assert settings.http.proxy_url is None


def test_loader_accepts_only_loopback_storage_proxy() -> None:
    config = _config()
    config["http"]["proxy_url"] = "http://127.0.0.1:1055"
    settings = load_worker_settings(_environment(config))
    assert settings.http.proxy_url == "http://127.0.0.1:1055"

    for unsafe in (
        "http://100.64.0.2:1055",
        "http://localhost:1055",
        "http://user:secret@127.0.0.1:1055",
    ):
        config["http"]["proxy_url"] = unsafe
        with pytest.raises(SuryaWorkerConfigurationError):
            load_worker_settings(_environment(config))


def test_shipped_example_is_valid_after_replacing_identity_placeholders() -> None:
    example_path = (
        Path(__file__).resolve().parents[1]
        / "prereview_runpod_worker"
        / "config.example.json"
    )
    config = json.loads(example_path.read_text(encoding="utf-8"))
    config["producer"]["pipeline_revision"] = "a" * 40
    config["producer"]["config_sha256"] = "b" * 64
    config["producer"]["worker_image_digest"] = f"sha256:{'c' * 64}"

    settings = load_worker_settings(_environment(config))

    assert settings.output.max_regions_per_page == 200
    assert settings.dispatch_policy.max_page_count == 20
    assert settings.dispatch_policy.max_ttl_seconds == 1800
    assert settings.http.proxy_url == "http://127.0.0.1:1056"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "[]",
        '{"storage_scopes":[],"storage_scopes":[]}',
        '{"storage_scopes": NaN}',
        '{"x":{"x":{"x":{"x":{"x":{"x":{"x":{"x":{"x":{"x":{"x":{"x":{"x":0}}}}}}}}}}}}}',
    ],
)
def test_loader_rejects_missing_duplicate_nonfinite_or_deep_json_without_echoing_it(
    raw: str | None,
) -> None:
    environment = {} if raw is None else {PREREVIEW_SURYA_WORKER_CONFIG_JSON: raw}

    with pytest.raises(SuryaWorkerConfigurationError) as raised:
        load_worker_settings(environment)

    assert str(raised.value) == "surya_worker_configuration_invalid"
    if raw:
        assert raw not in str(raised.value)


def test_loader_rejects_unknown_key_and_mutable_model_revision() -> None:
    config = _config()
    config["unexpected"] = "ignored values are unsafe"
    with pytest.raises(SuryaWorkerConfigurationError):
        load_worker_settings(_environment(config))

    config = _config()
    config["schema_version"] = "prereview.runpod-surya-worker/v0"
    with pytest.raises(SuryaWorkerConfigurationError):
        load_worker_settings(_environment(config))

    config = _config()
    config["producer"]["model_weights_sha256"] = "0" * 64
    with pytest.raises(SuryaWorkerConfigurationError):
        load_worker_settings(_environment(config))

    config = _config()
    config["producer"]["model_revision"] = "main"
    with pytest.raises(SuryaWorkerConfigurationError):
        load_worker_settings(_environment(config))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["dispatch_policy"].update(
            {"allowed_mime_types": ["application/json"]}
        ),
        lambda value: value["dispatch_policy"].update({"max_capability_bytes": 2_000_000}),
        lambda value: value["output"].update({"max_output_bytes": 2_000_000}),
        lambda value: value["output"].update({"max_terminal_output_bytes": 300_000}),
    ],
)
def test_loader_rejects_widened_or_incoherent_worker_caps(mutate: Any) -> None:
    config = _config()
    mutate(config)

    with pytest.raises(SuryaWorkerConfigurationError):
        load_worker_settings(_environment(config))


@pytest.mark.parametrize(
    "page_count,regions_per_page",
    [
        (257, 1),
        (1, 201),
        (51, 200),
    ],
)
def test_loader_rejects_page_region_limits_outside_portable_artifact_contract(
    page_count: int,
    regions_per_page: int,
) -> None:
    config = _config()
    config["dispatch_policy"]["max_page_count"] = page_count
    config["output"]["max_regions_per_page"] = regions_per_page

    with pytest.raises(SuryaWorkerConfigurationError):
        load_worker_settings(_environment(config))


def test_loader_rejects_output_cap_above_portable_artifact_byte_limit() -> None:
    config = _config()
    widened = 16 * 1024 * 1024 + 1
    config["http"]["hard_max_bytes"] = widened
    config["dispatch_policy"]["max_capability_bytes"] = widened
    config["output"]["max_output_bytes"] = widened

    with pytest.raises(SuryaWorkerConfigurationError):
        load_worker_settings(_environment(config))


def test_loader_requires_exact_non_loopback_endpoint_allowlist() -> None:
    config = _config()
    config["surya_endpoint"] = {"url": "https://gpu.example.test:8443/v1"}
    settings = load_worker_settings(_environment(config))

    with pytest.raises(SuryaWorkerConfigurationError):
        build_worker_composition(settings)

    config["surya_endpoint"]["allowed_origins"] = ["https://gpu.example.test:8443"]
    settings = load_worker_settings(_environment(config))
    assert settings.endpoint_policy is not None
    assert settings.endpoint_policy.allows("https://gpu.example.test:8443")


def test_composition_builds_each_adapter_once_with_hard_limits() -> None:
    calls: dict[str, list[dict[str, Any]]] = {"storage": [], "inference": [], "handler": []}

    class Storage:
        def __init__(self, **kwargs: Any) -> None:
            calls["storage"].append(kwargs)

    class Inference:
        def __init__(self, **kwargs: Any) -> None:
            calls["inference"].append(kwargs)

        def initialize(self) -> None:
            calls.setdefault("initialize", []).append({})

    class Handler:
        def __init__(self, **kwargs: Any) -> None:
            calls["handler"].append(kwargs)

    config = _config()
    composition = build_worker_composition(
        load_worker_settings(_environment(config)),
        storage_factory=Storage,
        inference_factory=Inference,
        handler_factory=Handler,  # type: ignore[arg-type]
        attestation_loader=lambda: _attestation(config),
    )

    assert len(calls["storage"]) == len(calls["inference"]) == len(calls["handler"]) == 1
    assert calls["storage"][0]["hard_max_bytes"] == 1_000_000
    assert calls["storage"][0]["proxy_url"] is None
    assert calls["inference"][0]["max_png_bytes"] == 1_000_000
    assert calls["inference"][0]["max_image_pixels"] == 10_000_000
    assert calls["initialize"] == [{}]
    assert composition.policy.max_regions_per_page == 200


def test_composition_rejects_missing_or_mismatched_launcher_attestation_before_handler() -> None:
    config = _config()
    settings = load_worker_settings(_environment(config))

    with pytest.raises(SuryaWorkerConfigurationError):
        build_worker_composition(settings, attestation_loader=lambda: (_ for _ in ()).throw(FileNotFoundError()))

    mismatched = _attestation(config)
    object.__setattr__(mismatched, "producer_model_revision", "f" * 40)
    with pytest.raises(SuryaWorkerConfigurationError):
        build_worker_composition(settings, attestation_loader=lambda: mismatched)
