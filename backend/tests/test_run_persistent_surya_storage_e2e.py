"""Offline checks for the accelerator-only persistent-Surya operator command."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import zlib

import pytest

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    PdfCoordinateManifest,
    build_sidecar_binding,
)
from common_ir_pipeline.pdf_fusion.render_manifest import RenderedPageInput, assemble_render_manifest
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact,
    SuryaLayoutPage,
    SuryaProducerIdentity as PortableSuryaProducerIdentity,
)
from scripts import run_persistent_surya_storage_e2e as e2e
from worker.accelerator_coordinator import (
    ArtifactNotFound,
    ArtifactRead,
    CoordinatorDisposition,
    ExistingPdfSuryaOutcome,
)
from worker.contracts.accelerator import (
    AcceleratorJobState,
    AcceleratorJobStatus,
    AcceleratorResourceCaps,
    SignedStorageCapability,
    StorageResourceCaps,
    build_surya_layout_reconciliation_logical_compute_key,
    build_surya_layout_result_object_key,
)
from worker.adapters.supabase_accelerator_input import (
    SupabaseAcceleratorInputUploadError,
)
from worker.signed_storage_capabilities import SignedStorageCapabilityIssuerError


def _digest(value: bytes | str) -> str:
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30\x10\x20\x30"))
        + chunk(b"IEND", b"")
    )


def _artifact_root(root: Path) -> Path:
    source = root / "source" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    image = root / "rendered" / "page-0001.png"
    image.parent.mkdir()
    image.write_bytes(_png())
    coordinate = PdfCoordinateManifest(
        source_sha256=_digest(source.read_bytes()), page=1, page_count=1,
        media_box=(0, 0, 2, 1), crop_box=(0, 0, 2, 1), rotation=0,
        user_unit=1.0, canonical_width_pt=2.0, canonical_height_pt=1.0,
        render_scale_px_per_point=1.0, rendered_width_px=2, rendered_height_px=1,
        pdf_origin="bottom_left", pdf_x_axis="right", pdf_y_axis="up",
        pixel_origin="top_left", pixel_x_axis="right", pixel_y_axis="down",
        user_to_pixel=AffineTransform.from_sequence([1, 0, 0, -1, 0, 1]),
        pixel_to_user=AffineTransform.from_sequence([1, 0, 0, -1, 0, 1]),
        renderer="pdfium", renderer_version="1", renderer_config_sha256=_digest("config"),
        page_image_sha256=_digest(image.read_bytes()),
    )
    manifest = assemble_render_manifest(
        artifact_root=root,
        source_pdf_path=source,
        pages=(RenderedPageInput(image_path=image, coordinate_manifest=coordinate),),
    )
    (root / "render_manifest.json").write_bytes(manifest.canonical_json())
    return root


def _config(
    path: Path,
    *,
    max_ttl_seconds: int = 1800,
    max_execution_timeout_seconds: int = 60,
) -> Path:
    value = {
        "schema_version": "prereview.runpod-surya-worker/v1",
        "storage_scopes": [
            {"method": "GET", "origin": "https://storage.example.test", "endpoint_path_template": "/storage/v1/object/sign/{bucket}/{object_key}", "bucket": "request-temp", "object_key_prefix": "accelerator/input/"},
            {"method": "PUT", "origin": "https://storage.example.test", "endpoint_path_template": "/storage/v1/object/upload/sign/{bucket}/{object_key}", "bucket": "request-temp", "object_key_prefix": "accelerator/surya-layout/"},
        ],
        "dispatch_policy": {"max_ttl_seconds": max_ttl_seconds, "max_execution_timeout_seconds": max_execution_timeout_seconds, "max_page_count": 2, "max_total_input_bytes": 100000, "max_total_rendered_pixels": 100000, "max_capability_bytes": 100000, "allowed_mime_types": ["application/json", "image/png"]},
        "producer": {"engine_id": "surya", "engine_version": "0.1", "model_id": "surya-layout", "model_revision": "1" * 40, "model_weights_sha256": _digest("weights"), "pipeline_revision": "pipeline-r1", "config_sha256": _digest("config"), "worker_image_digest": f"sha256:{_digest('image')}"},
        "http": {"hard_max_bytes": 100000, "connect_timeout_seconds": 1.0, "read_timeout_seconds": 1.0, "write_timeout_seconds": 1.0, "pool_timeout_seconds": 1.0, "proxy_url": None},
        "output": {"max_output_bytes": 10000, "max_terminal_output_bytes": 1000, "max_regions_per_page": 10},
        "surya_endpoint": {"url": "http://127.0.0.1:8000/v1", "allowed_origins": []},
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _api_client_config(
    path: Path,
    *,
    origin: str = "https://runpod-test.example.ts.net",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "prereview.persistent-api-client/v1",
                "origin": origin,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _args(tmp_path: Path) -> object:
    token = tmp_path / "runpod.token"
    token.write_text("bearer-token-that-must-not-print\n", encoding="utf-8")
    token.chmod(stat.S_IRUSR | stat.S_IWUSR)
    service_key = tmp_path / "service.key"
    service_key.write_text("service-role-that-must-not-print\n", encoding="utf-8")
    service_key.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return e2e.build_parser().parse_args([
        "--artifact-root", str(_artifact_root(tmp_path / "artifact")),
        "--runpod-config-json", str(_config(tmp_path / "config.json")),
        "--runpod-api-client-config", str(_api_client_config(tmp_path / "api-client.json")),
        "--runpod-api-base-url", "https://runpod-test.example.ts.net",
        "--runpod-bearer-token-file", str(token),
        "--storage-bucket", "request-temp",
        "--supabase-url", "http://127.0.0.1:8000",
        "--supabase-service-role-key-file", str(service_key),
        "--poll-interval-seconds", "0.1",
    ])


@pytest.mark.parametrize("mode", [0o400, 0o644, 0o660])
def test_bearer_token_requires_private_current_user_file(tmp_path: Path, mode: int) -> None:
    token = tmp_path / "runpod.token"
    token.write_text("token\n", encoding="utf-8")
    token.chmod(mode)

    with pytest.raises(e2e.PersistentSuryaE2EError, match="credential_invalid"):
        e2e._read_bearer_token(token)


def test_bearer_token_rejects_symlinks_and_multiple_links(tmp_path: Path) -> None:
    token = tmp_path / "runpod.token"
    token.write_text("token\n", encoding="utf-8")
    token.chmod(0o600)
    link = tmp_path / "runpod-token-link"
    link.symlink_to(token)
    with pytest.raises(e2e.PersistentSuryaE2EError, match="credential_invalid"):
        e2e._read_bearer_token(link)

    hard_link = tmp_path / "runpod-token-hard-link"
    hard_link.hardlink_to(token)
    with pytest.raises(e2e.PersistentSuryaE2EError, match="credential_invalid"):
        e2e._read_bearer_token(token)


def test_secret_rejects_lstat_open_inode_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "runpod.token"
    token.write_text("token\n", encoding="utf-8")
    token.chmod(0o600)
    actual = e2e.os.lstat(token)
    fake_lstat = SimpleNamespace(
        st_dev=actual.st_dev,
        st_ino=actual.st_ino + 1,
        st_mode=actual.st_mode,
        st_uid=actual.st_uid,
        st_nlink=actual.st_nlink,
    )
    monkeypatch.setattr(e2e.os, "lstat", lambda _: fake_lstat)

    with pytest.raises(e2e.PersistentSuryaE2EError, match="credential_invalid"):
        e2e._read_bearer_token(token)


def test_runtime_supabase_env_requires_private_current_user_file(tmp_path: Path) -> None:
    env_file = tmp_path / "supabase.env"
    env_file.write_text("SERVICE_ROLE_KEY=service-secret\n", encoding="utf-8")
    env_file.chmod(0o644)
    args = type("Args", (), {
        "supabase_runtime_env": env_file,
        "supabase_service_role_key_file": None,
        "supabase_url": None,
    })()

    with pytest.raises(e2e.PersistentSuryaE2EError, match="storage_configuration_invalid"):
        e2e._load_supabase_settings(args)


@pytest.mark.parametrize(
    "origin",
    (
        "https://example.com",
        "http://runpod-test.example.ts.net",
        "https://runpod-test.example.ts.net/path",
        "https://runpod-test.example.ts.net:443",
        "https://RUNPOD-TEST.example.ts.net",
    ),
)
def test_runpod_api_origin_rejects_unpinned_or_noncanonical_destinations(
    tmp_path: Path,
    origin: str,
) -> None:
    path = _api_client_config(tmp_path / "api-client.json", origin=origin)

    with pytest.raises(e2e.PersistentSuryaE2EError, match="configuration_invalid"):
        e2e._load_runpod_api_origin(path, asserted_origin=None)


def test_runpod_api_origin_requires_private_file_and_exact_cli_assertion(
    tmp_path: Path,
) -> None:
    path = _api_client_config(tmp_path / "api-client.json")
    path.chmod(0o644)
    with pytest.raises(e2e.PersistentSuryaE2EError, match="configuration_invalid"):
        e2e._load_runpod_api_origin(path, asserted_origin=None)

    path.chmod(0o600)
    with pytest.raises(e2e.PersistentSuryaE2EError, match="configuration_invalid"):
        e2e._load_runpod_api_origin(
            path,
            asserted_origin="https://different-node.example.ts.net",
        )

    assert e2e._load_runpod_api_origin(
        path,
        asserted_origin="https://runpod-test.example.ts.net",
    ) == "https://runpod-test.example.ts.net"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000/",
        "http://127.0.0.1",
        "http://127.0.0.2:8000",
        "https://127.0.0.1:8000",
    ],
)
def test_service_role_destination_requires_canonical_loopback_origin(
    tmp_path: Path,
    url: str,
) -> None:
    service_key = tmp_path / "service.key"
    service_key.write_text("service-role-that-must-not-print\n", encoding="utf-8")
    service_key.chmod(0o600)
    args = type(
        "Args",
        (),
        {
            "supabase_runtime_env": None,
            "supabase_service_role_key_file": service_key,
            "supabase_url": url,
        },
    )()

    with pytest.raises(e2e.PersistentSuryaE2EError, match="storage_configuration_invalid"):
        e2e._load_supabase_settings(args)


def test_service_role_destination_accepts_canonical_loopback_origin(tmp_path: Path) -> None:
    service_key = tmp_path / "service.key"
    service_key.write_text("service-role-that-must-not-print\n", encoding="utf-8")
    service_key.chmod(0o600)
    args = type(
        "Args",
        (),
        {
            "supabase_runtime_env": None,
            "supabase_service_role_key_file": service_key,
            "supabase_url": "http://127.0.0.1:8000",
        },
    )()

    assert e2e._load_supabase_settings(args) == (
        "http://127.0.0.1:8000",
        "service-role-that-must-not-print",
    )


@pytest.mark.parametrize("mode", [0o400, 0o644, 0o660])
def test_runpod_config_requires_private_current_user_file(
    tmp_path: Path,
    mode: int,
) -> None:
    config = _config(tmp_path / "config.json")
    config.chmod(mode)

    with pytest.raises(e2e.PersistentSuryaE2EError, match="configuration_invalid"):
        e2e._load_runpod_settings(config)


def test_runpod_config_rejects_symlink_and_hard_link(tmp_path: Path) -> None:
    target = _config(tmp_path / "config.json")
    symlink = tmp_path / "config-link.json"
    symlink.symlink_to(target)
    with pytest.raises(e2e.PersistentSuryaE2EError, match="configuration_invalid"):
        e2e._load_runpod_settings(symlink)

    hard_link = tmp_path / "config-hard-link.json"
    hard_link.hardlink_to(target)
    with pytest.raises(e2e.PersistentSuryaE2EError, match="configuration_invalid"):
        e2e._load_runpod_settings(target)


class _Storage:
    def __init__(self, **_: object) -> None:
        self.puts: list[tuple[str, str]] = []

    def put_if_absent(self, *, bucket: str, object_key: str, content: bytes, content_type: str) -> bool:
        self.puts.append((bucket, object_key))
        return True


class _Issuer:
    init_kwargs: dict[str, object] = {}

    def __init__(self, **_: object) -> None:
        type(self).init_kwargs = dict(_)
        self.now = datetime.now(UTC)

    def _cap(self, *, method: str, bucket: str, key: str, expected: str | None, size: int, mime: str) -> SignedStorageCapability:
        operation = "sign" if method == "GET" else "upload/sign"
        return SignedStorageCapability(
            method=method, access_mode="read_only" if method == "GET" else "create_only",
            url=f"https://storage.example.test/storage/v1/object/{operation}/{bucket}/{key}?token=test",
            storage_host="storage.example.test", bucket=bucket,
            path=f"/storage/v1/object/{operation}/{bucket}/{key}", object_key=key,
            expected_sha256=expected, expires_at=self.now + timedelta(minutes=10),
            resource_caps=StorageResourceCaps(max_bytes=size, allowed_mime_types=(mime,)),
        )

    def issue_read(self, *, bucket: str, object_key: str, expected_sha256: str, size_bytes: int, mime_type: str, ttl_seconds: int) -> SignedStorageCapability:
        return self._cap(method="GET", bucket=bucket, key=object_key, expected=expected_sha256, size=size_bytes, mime=mime_type)

    def issue_surya_layout_result_upload(self, *, bucket: str, logical_compute_key: str, max_bytes: int, mime_type: str = "application/json") -> SignedStorageCapability:
        return self._cap(method="PUT", bucket=bucket, key=build_surya_layout_result_object_key(logical_compute_key), expected=None, size=max_bytes, mime=mime_type)


class _HttpAccelerator:
    def __init__(self, **_: object) -> None:
        self.request = None
        self.polls = 0

    def submit(self, request):  # type: ignore[no-untyped-def]
        self.request = request
        return AcceleratorJobStatus(external_job_id="persistent-job-1", state=AcceleratorJobState.QUEUED, logical_compute_key=request.logical_compute_key, request_digest=request.request_digest)

    def get_status(self, external_job_id: str) -> AcceleratorJobStatus:
        assert self.request is not None
        self.polls += 1
        return AcceleratorJobStatus(external_job_id=external_job_id, state=AcceleratorJobState.CANCELLED, logical_compute_key=self.request.logical_compute_key, request_digest=self.request.request_digest)

    def cancel(self, external_job_id: str) -> AcceleratorJobStatus:
        return self.get_status(external_job_id)


class _Reader:
    def __init__(self, **_: object) -> None:
        pass

    def read(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise ArtifactNotFound()


def _existing_artifact(args: object) -> SuryaLayoutArtifact:
    manifest = e2e._load_render_manifest(args.artifact_root)
    settings = e2e._load_runpod_settings(args.runpod_config_json)
    producer = PortableSuryaProducerIdentity(**settings.producer.model_dump())
    logical_key = build_surya_layout_reconciliation_logical_compute_key(
        manifest, settings.producer
    )
    page = manifest.pages[0]
    return SuryaLayoutArtifact(
        logical_compute_key=logical_key,
        source_sha256=manifest.source_pdf_sha256,
        page_count=manifest.page_count,
        render_manifest_schema_version=manifest.schema_version,
        render_manifest_sha256=manifest.manifest_sha256(),
        producer=producer,
        requested_pages=(1,),
        pages=(
            SuryaLayoutPage(
                page=1,
                sidecar_binding=build_sidecar_binding(page.coordinate_manifest),
                pixel_width=page.coordinate_manifest.rendered_width_px,
                pixel_height=page.coordinate_manifest.rendered_height_px,
                rendered_page_px=(
                    0,
                    0,
                    page.coordinate_manifest.rendered_width_px,
                    page.coordinate_manifest.rendered_height_px,
                ),
                regions=(),
            ),
        ),
    )


class _ExistingResultReader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.reads = 0

    def read(self, *args: object, **kwargs: object) -> ArtifactRead:
        self.reads += 1
        return ArtifactRead(content=self.content, content_type="application/json")


def test_reuses_fully_validated_existing_result_before_any_dispatch_side_effect(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    artifact = _existing_artifact(args)
    reader = _ExistingResultReader(artifact.canonical_json())
    accepted: list[SuryaLayoutArtifact] = []

    def forbidden(**kwargs: object) -> object:
        del kwargs
        raise AssertionError("existing-result reuse must not prepare or dispatch")

    outcome = e2e.run_e2e(
        args,
        storage_factory=forbidden,
        issuer_factory=forbidden,
        accelerator_factory=forbidden,
        artifact_reader_factory=lambda **_: reader,
        accepted_artifact=accepted.append,
    )

    assert outcome.disposition == "succeeded"
    assert outcome.reason_code == "completed"
    assert reader.reads == 1
    assert accepted == [artifact]


def test_existing_mismatched_result_fails_closed_without_dispatch(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    payload = _existing_artifact(args).to_dict()
    payload["logical_compute_key"] = _digest("wrong-logical-key")
    content = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    reader = _ExistingResultReader(content)
    accepted: list[SuryaLayoutArtifact] = []

    def forbidden(**kwargs: object) -> object:
        del kwargs
        raise AssertionError("mismatched immutable result must not be overwritten")

    outcome = e2e.run_e2e(
        args,
        storage_factory=forbidden,
        issuer_factory=forbidden,
        accelerator_factory=forbidden,
        artifact_reader_factory=lambda **_: reader,
        accepted_artifact=accepted.append,
    )

    assert outcome.disposition == "content_failed"
    assert outcome.reason_code == "content_failed"
    assert reader.reads == 1
    assert accepted == []


def test_runs_offline_through_real_coordinator_polling_without_leaking_secrets(tmp_path: Path) -> None:
    args = _args(tmp_path)
    storages: list[_Storage] = []
    storage_options: list[dict[str, object]] = []
    accelerators: list[_HttpAccelerator] = []

    def storage_factory(**kwargs: object) -> _Storage:
        storage_options.append(kwargs)
        storage = _Storage(**kwargs)
        storages.append(storage)
        return storage

    def accelerator_factory(**kwargs: object) -> _HttpAccelerator:
        accelerator = _HttpAccelerator(**kwargs)
        accelerators.append(accelerator)
        return accelerator

    outcome = e2e.run_e2e(
        args,
        storage_factory=storage_factory,
        issuer_factory=_Issuer,
        accelerator_factory=accelerator_factory,
        artifact_reader_factory=_Reader,
        monotonic=iter((0.0, 0.0, 1.0)).__next__,
        sleep=lambda _: None,
    )

    assert outcome.disposition == "cancelled"
    assert outcome.provider_state == "cancelled"
    assert len(storages[0].puts) == 2
    manifest = e2e._load_render_manifest(args.artifact_root)
    expected_input_bytes = len(manifest.canonical_json()) + sum(
        page.image_size_bytes for page in manifest.pages
    )
    assert storage_options == [
        {
            "supabase_url": "http://127.0.0.1:8000",
            "service_role_key": "service-role-that-must-not-print",
            "allowed_bucket": "request-temp",
            "object_key_prefix": "accelerator/input/",
            "max_object_bytes": expected_input_bytes,
            "timeout_seconds": 30.0,
        }
    ]
    assert accelerators[0].polls == 1
    rendered = outcome.as_json()
    assert "bearer-token-that-must-not-print" not in rendered
    assert "service-role-that-must-not-print" not in rendered
    assert "https://storage.example.test" not in rendered
    assert _Issuer.init_kwargs["max_capability_ttl_seconds"] == 1800


def test_maps_input_upload_failure_to_safe_reason(tmp_path: Path) -> None:
    args = _args(tmp_path)

    class FailingStorage(_Storage):
        def put_if_absent(self, **_: object) -> bool:
            raise SupabaseAcceleratorInputUploadError()

    with pytest.raises(e2e.PersistentSuryaE2EError, match="storage_upload_failed"):
        e2e.run_e2e(
            args,
            storage_factory=FailingStorage,
            issuer_factory=_Issuer,
            accelerator_factory=_HttpAccelerator,
            artifact_reader_factory=_Reader,
        )


def test_maps_capability_issuance_failure_to_safe_reason(tmp_path: Path) -> None:
    args = _args(tmp_path)

    class FailingIssuer(_Issuer):
        def issue_read(self, **_: object) -> SignedStorageCapability:
            raise SignedStorageCapabilityIssuerError()

    with pytest.raises(
        e2e.PersistentSuryaE2EError,
        match="storage_capability_issue_failed",
    ):
        e2e.run_e2e(
            args,
            storage_factory=_Storage,
            issuer_factory=FailingIssuer,
            accelerator_factory=_HttpAccelerator,
            artifact_reader_factory=_Reader,
        )


def test_request_ttl_and_capability_ceiling_keep_dispatch_slack(tmp_path: Path) -> None:
    args = _args(tmp_path)
    settings = e2e._load_runpod_settings(args.runpod_config_json)
    manifest = e2e._load_render_manifest(args.artifact_root)
    caps = e2e._resource_caps(settings, manifest)

    assert caps.ttl_seconds == 1_200
    assert e2e._capability_ttl_seconds(settings) == 1_800
    assert e2e._DISPATCH_SLACK_SECONDS == 600


def test_rejects_invalid_local_artifact_before_storage_side_effects(tmp_path: Path) -> None:
    args = _args(tmp_path)
    (args.artifact_root / "rendered" / "page-0001.png").write_bytes(b"not-a-png")
    called = False

    def storage_factory(**kwargs: object) -> _Storage:
        nonlocal called
        called = True
        return _Storage(**kwargs)

    with pytest.raises(e2e.PersistentSuryaE2EError, match="local_artifact_invalid"):
        e2e.run_e2e(args, storage_factory=storage_factory)

    assert called is False


def test_preflights_deployment_ceiling_before_constructing_storage_ports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    called = False

    def outside_deployment_ceiling(
        _settings: object,
        _manifest: object,
    ) -> AcceleratorResourceCaps:
        return AcceleratorResourceCaps(
            max_page_count=2,
            max_total_input_bytes=100_000,
            max_total_rendered_pixels=100_000,
            max_output_bytes=100_001,
            execution_timeout_seconds=60,
            ttl_seconds=300,
        )

    def storage_factory(**kwargs: object) -> _Storage:
        nonlocal called
        called = True
        return _Storage(**kwargs)

    monkeypatch.setattr(e2e, "_resource_caps", outside_deployment_ceiling)

    with pytest.raises(e2e.PersistentSuryaE2EError, match="configuration_invalid"):
        e2e.run_e2e(args, storage_factory=storage_factory)

    assert called is False


@pytest.mark.parametrize(
    "config_kwargs",
    [
        {"max_ttl_seconds": 1_799},
        {"max_execution_timeout_seconds": 1_201},
    ],
)
def test_rejects_low_ttl_or_execution_ceiling_before_storage_side_effects(
    tmp_path: Path,
    config_kwargs: dict[str, int],
) -> None:
    args = _args(tmp_path)
    args.runpod_config_json = _config(tmp_path / "low-ceiling-config.json", **config_kwargs)
    called = False

    def storage_factory(**_: object) -> _Storage:
        nonlocal called
        called = True
        return _Storage()

    with pytest.raises(e2e.PersistentSuryaE2EError, match="configuration_invalid"):
        e2e.run_e2e(args, storage_factory=storage_factory)
    assert called is False


def test_uses_credential_free_reconciliation_after_uncertain_poll(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)

    class ReconciliationCoordinator:
        reconciliations = 0

        def advance(self, *args: object, **kwargs: object) -> ExistingPdfSuryaOutcome:
            return ExistingPdfSuryaOutcome(
                disposition=CoordinatorDisposition.INFRA_RETRYABLE,
                external_job_id="persistent-job-1",
                reason_code="provider_poll_failed",
            )

        def reconcile_result_artifact(
            self,
            *args: object,
            **kwargs: object,
        ) -> ExistingPdfSuryaOutcome:
            self.reconciliations += 1
            return ExistingPdfSuryaOutcome(
                disposition=CoordinatorDisposition.SUCCEEDED,
                external_job_id="persistent-job-1",
                artifact=object(),  # type: ignore[arg-type]
            )

    coordinator = ReconciliationCoordinator()
    accepted: list[object] = []

    outcome = e2e.run_e2e(
        args,
        storage_factory=_Storage,
        issuer_factory=_Issuer,
        accelerator_factory=_HttpAccelerator,
        artifact_reader_factory=_Reader,
        coordinator_factory=lambda **_: coordinator,  # type: ignore[arg-type]
        accepted_artifact=accepted.append,  # type: ignore[arg-type]
    )

    assert outcome.disposition == "succeeded"
    assert coordinator.reconciliations == 1
    assert len(accepted) == 1


def test_reconciliation_callback_receives_only_successful_artifacts(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)

    class CancelledReconciliationCoordinator:
        def advance(self, *args: object, **kwargs: object) -> ExistingPdfSuryaOutcome:
            return ExistingPdfSuryaOutcome(
                disposition=CoordinatorDisposition.INFRA_RETRYABLE,
                external_job_id="persistent-job-1",
                reason_code="provider_poll_failed",
            )

        def reconcile_result_artifact(
            self, *args: object, **kwargs: object
        ) -> ExistingPdfSuryaOutcome:
            return ExistingPdfSuryaOutcome(
                disposition=CoordinatorDisposition.CANCELLED,
                external_job_id="persistent-job-1",
            )

    accepted: list[object] = []
    outcome = e2e.run_e2e(
        args,
        storage_factory=_Storage,
        issuer_factory=_Issuer,
        accelerator_factory=_HttpAccelerator,
        artifact_reader_factory=_Reader,
        coordinator_factory=lambda **_: CancelledReconciliationCoordinator(),  # type: ignore[arg-type]
        accepted_artifact=accepted.append,  # type: ignore[arg-type]
    )

    assert outcome.disposition == "cancelled"
    assert accepted == []


def test_real_coordinator_recovers_lost_initial_submit_response_by_logical_key(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)

    class AcceptedThenResponseLost:
        def __init__(self, **_: object) -> None:
            self.request = None
            self.polls: list[str] = []

        def submit(self, request):  # type: ignore[no-untyped-def]
            self.request = request
            raise TimeoutError("response lost after remote acceptance")

        def get_status(self, external_job_id: str) -> AcceleratorJobStatus:
            assert self.request is not None
            assert external_job_id == self.request.logical_compute_key
            self.polls.append(external_job_id)
            return AcceleratorJobStatus(
                external_job_id=external_job_id,
                state=AcceleratorJobState.CANCELLED,
                logical_compute_key=self.request.logical_compute_key,
                request_digest=self.request.request_digest,
            )

        def cancel(self, external_job_id: str) -> AcceleratorJobStatus:
            raise AssertionError("operator recovery must not cancel provider work")

    accelerator = AcceptedThenResponseLost()

    outcome = e2e.run_e2e(
        args,
        storage_factory=_Storage,
        issuer_factory=_Issuer,
        accelerator_factory=lambda **_: accelerator,
        artifact_reader_factory=_Reader,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    assert outcome.disposition == "cancelled"
    assert accelerator.request is not None
    assert accelerator.polls == [accelerator.request.logical_compute_key]


def test_initial_retry_without_job_id_uses_logical_key_until_deadline(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    args.poll_timeout_seconds = 1.0

    class LostSubmitCoordinator:
        def __init__(self) -> None:
            self.advance_ids: list[str | None] = []
            self.reconcile_ids: list[str] = []
            self.logical_key: str | None = None

        def advance(self, *args: object, **kwargs: object) -> ExistingPdfSuryaOutcome:
            request = args[0]
            assert hasattr(request, "logical_compute_key")
            self.logical_key = request.logical_compute_key
            prior = kwargs["prior_external_job_id"]
            assert isinstance(prior, (str, type(None)))
            self.advance_ids.append(prior)
            if len(self.advance_ids) == 1:
                return ExistingPdfSuryaOutcome(
                    disposition=CoordinatorDisposition.INFRA_RETRYABLE,
                    reason_code="provider_submit_failed",
                )
            return ExistingPdfSuryaOutcome(
                disposition=CoordinatorDisposition.PENDING,
                external_job_id=self.logical_key,
                provider_state=AcceleratorJobState.QUEUED,
            )

        def reconcile_result_artifact(
            self,
            *_: object,
            **kwargs: object,
        ) -> ExistingPdfSuryaOutcome:
            job_id = kwargs["external_job_id"]
            assert isinstance(job_id, str)
            self.reconcile_ids.append(job_id)
            return ExistingPdfSuryaOutcome(
                disposition=CoordinatorDisposition.PENDING,
                external_job_id=job_id,
                reason_code="artifact_not_found",
            )

    coordinator = LostSubmitCoordinator()
    clock = [0.0]

    def monotonic() -> float:
        return clock[0]

    def advance_clock(seconds: float) -> None:
        clock[0] += seconds

    outcome = e2e.run_e2e(
        args,
        storage_factory=_Storage,
        issuer_factory=_Issuer,
        accelerator_factory=_HttpAccelerator,
        artifact_reader_factory=_Reader,
        coordinator_factory=lambda **_: coordinator,  # type: ignore[arg-type]
        monotonic=monotonic,
        sleep=advance_clock,
    )

    assert outcome.disposition == "timed_out"
    assert coordinator.advance_ids[0] is None
    assert coordinator.logical_key is not None
    assert coordinator.advance_ids[1:] == [coordinator.logical_key] * len(coordinator.advance_ids[1:])
    assert coordinator.reconcile_ids
    assert set(coordinator.reconcile_ids) == {coordinator.logical_key}


def test_provider_reason_is_collapsed_to_local_category() -> None:
    outcome = e2e._safe_coordinator_outcome(
        ExistingPdfSuryaOutcome(
            disposition=CoordinatorDisposition.INFRA_RETRYABLE,
            external_job_id="provider-job",
            reason_code="provider_poll_failed",
        )
    )

    assert outcome.reason_code == "infra_retryable"
    assert "provider_poll_failed" not in outcome.as_json()
