"""Contract tests for the Tailscale-only persistent RunPod job API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import gc
import json
import stat
import threading
import time
import weakref

import httpx
import pytest

from prereview_runpod_worker.persistent_api.app import create_persistent_app
from prereview_runpod_worker.persistent_api.entrypoint import serve_persistent_app
from prereview_runpod_worker.persistent_api.settings import (
    PersistentApiConfigurationError,
    PersistentApiSettings,
    load_persistent_api_settings,
)
from prereview_runpod_worker.persistent_api.service import (
    JobSubmissionInvalid,
    PersistentJobService,
)
from prereview_runpod_worker.persistent_api.store import (
    DurableJobStore,
    JobRecord,
    JobStoreError,
)
from worker.adapters.persistent_surya_http import PersistentSuryaHttpAdapter
from worker.contracts.accelerator import (
    AcceleratorJobState,
    AcceleratorJobStatus,
    SuryaLayoutRequest,
)


TOKEN = "test-bearer-token-that-is-long-enough-0001"
OTHER_TOKEN = "other-bearer-token-that-is-long-enough-0002"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
AUTH_ONLY = {"Authorization": f"Bearer {TOKEN}"}
AUTH = {**AUTH_ONLY, "Idempotency-Key": DIGEST_A}


class RaisingHandler:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, event: object) -> object:
        self.calls += 1
        raise RuntimeError("signed=https://secret.example/?token=must-not-leak")


class BlockingHandler:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def handle(self, event: dict[str, object]) -> dict[str, object]:
        self.started.set()
        assert self.release.wait(timeout=5)
        payload = event["input"]
        assert isinstance(payload, dict)
        number = payload["number"]
        assert isinstance(number, int) and not isinstance(number, bool)
        return AcceleratorJobStatus(
            external_job_id=str(event["id"]),
            state=AcceleratorJobState.CANCELLED,
            logical_compute_key=f"{number:064x}",
            request_digest=DIGEST_B,
        ).model_dump(mode="json")


def _settings(tmp_path: Path, **overrides: object) -> PersistentApiSettings:
    values: dict[str, object] = {
        "bearer_token": TOKEN,
        "state_directory": tmp_path / "jobs",
        "queue_capacity": 2,
        "max_request_bytes": 4096,
        "max_job_records": 100,
        "shutdown_grace_seconds": 2.0,
        "port": 8787,
    }
    values.update(overrides)
    return PersistentApiSettings(**values)  # type: ignore[arg-type]


def _store(settings: PersistentApiSettings) -> DurableJobStore:
    return DurableJobStore(
        settings.state_directory,
        max_records=settings.max_job_records,
        mac_key=settings.journal_mac_key,
    )


@pytest.fixture
def accept_fake_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def parse(_: type[SuryaLayoutRequest], payload: object) -> SimpleNamespace:
        if not isinstance(payload, dict):
            raise ValueError
        number = payload.get("number")
        logical_compute_key = (
            f"{number:064x}"
            if isinstance(number, int) and not isinstance(number, bool)
            else DIGEST_A
        )
        return SimpleNamespace(
            logical_compute_key=logical_compute_key,
            request_digest=DIGEST_B,
        )

    monkeypatch.setattr(SuryaLayoutRequest, "from_wire_payload", classmethod(parse))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _wait_for_terminal(api: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    for _ in range(100):
        response = await api.get(f"/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        if body["state"] not in {"queued", "running"}:
            return body
        await __import__("asyncio").sleep(0.01)
    raise AssertionError("job did not reach a terminal state")


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    )


def test_settings_require_a_long_bearer_and_default_to_port_8787(tmp_path: Path) -> None:
    with pytest.raises(PersistentApiConfigurationError):
        load_persistent_api_settings({"PREREVIEW_SURYA_API_BEARER_TOKEN": "short"})
    with pytest.raises(PersistentApiConfigurationError):
        load_persistent_api_settings({})
    settings = load_persistent_api_settings(
        {"PREREVIEW_SURYA_API_BEARER_TOKEN": TOKEN}
    )
    assert settings.port == 8787
    assert settings.state_directory == Path("/workspace/persistent/prereview/surya-jobs")
    assert settings.shutdown_grace_seconds == 210.0
    assert "bearer" not in repr(settings).lower()
    assert TOKEN not in repr(settings)

    with pytest.raises(PersistentApiConfigurationError):
        load_persistent_api_settings(
            {"PREREVIEW_SURYA_API_BEARER_TOKEN": "$" * 32}
        )


@pytest.mark.parametrize(
    "state_directory",
    (
        "/run/prereview-surya/jobs",
        "/tmp/prereview-surya/jobs",
        "/workspace/persistent/prereview/../surya-jobs",
    ),
)
def test_deployed_settings_reject_ephemeral_or_noncanonical_journal_paths(
    state_directory: str,
) -> None:
    with pytest.raises(PersistentApiConfigurationError):
        load_persistent_api_settings(
            {
                "PREREVIEW_SURYA_API_BEARER_TOKEN": TOKEN,
                "PREREVIEW_SURYA_API_STATE_DIRECTORY": state_directory,
            }
        )

    settings = load_persistent_api_settings(
        {
            "PREREVIEW_SURYA_API_BEARER_TOKEN": TOKEN,
            "PREREVIEW_SURYA_API_STATE_DIRECTORY": (
                "/workspace/persistent/prereview/surya-jobs"
            ),
        }
    )
    assert settings.state_directory == Path(
        "/workspace/persistent/prereview/surya-jobs"
    )


def test_launcher_is_one_worker_on_loopback_only(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def runner(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    app = object()
    serve_persistent_app(app, settings=_settings(tmp_path), runner=runner)

    assert captured["app"] is app
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8787
    assert captured["workers"] == 1
    assert captured["access_log"] is False
    assert captured["proxy_headers"] is False


@pytest.mark.anyio
async def test_health_is_minimal_and_jobs_require_exact_bearer(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    app = create_persistent_app(handler=RaisingHandler(), settings=_settings(tmp_path))
    async with app.router.lifespan_context(app):
      async with _client(app) as api:
        health = await api.get("/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "ready": True,
            "queue_depth": 0,
            "queue_capacity": 2,
            "gpu_concurrency": 1,
        }
        assert (await api.post("/jobs", json={"input": {}})).status_code == 401
        assert (await api.post(
            "/jobs",
            json={"input": {}},
            headers={"Authorization": "Bearer wrong-wrong-wrong-wrong-wrong"},
        )).status_code == 401
        missing_key = await api.post(
            "/jobs",
            json={"input": {}},
            headers=AUTH_ONLY,
        )
        assert missing_key.status_code == 400
        assert missing_key.json() == {"detail": "idempotency_key_invalid"}
        wrong_key = await api.post(
            "/jobs",
            json={"input": {}},
            headers={**AUTH_ONLY, "Idempotency-Key": DIGEST_B},
        )
        assert wrong_key.status_code == 400
        assert wrong_key.json() == {"detail": "idempotency_key_invalid"}
        duplicate_key = await api.post(
            "/jobs",
            json={"input": {}},
            headers=[
                ("Authorization", f"Bearer {TOKEN}"),
                ("Idempotency-Key", DIGEST_A),
                ("Idempotency-Key", DIGEST_A),
            ],
        )
        assert duplicate_key.status_code == 400
        assert duplicate_key.json() == {"detail": "idempotency_key_invalid"}


@pytest.mark.anyio
async def test_request_capability_material_is_memory_only_and_failures_are_sanitized(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    secret = "https://storage.example/?signature=top-secret"
    app = create_persistent_app(handler=RaisingHandler(), settings=_settings(tmp_path))
    async with app.router.lifespan_context(app):
      async with _client(app) as api:
        response = await api.post(
            "/jobs",
            json={"input": {"capability": {"url": secret}}},
            headers=AUTH,
        )
        assert response.status_code == 202
        job_id = response.json()["id"]
        terminal = await _wait_for_terminal(api, job_id)
        assert terminal["state"] == "infra_retryable"
        assert terminal["reason_code"] == "worker_runtime_failed"
        assert terminal["output"] is None

    durable = b"".join(path.read_bytes() for path in (tmp_path / "jobs").glob("*.json"))
    assert secret.encode() not in durable
    assert b"top-secret" not in durable


@pytest.mark.anyio
async def test_last_completed_job_releases_memory_only_capability_payload(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    class CapabilityPayload(dict[str, object]):
        pass

    settings = _settings(tmp_path)
    store = _store(settings)
    store.open()
    service = PersistentJobService(
        handler=RaisingHandler(),
        store=store,
        settings=settings,
    )
    service.start()
    payload = CapabilityPayload(
        capability={"url": "https://storage.example/?signature=memory-only"}
    )
    payload_ref = weakref.ref(payload)
    try:
        service.submit(payload, requested_job_id=DIGEST_A)
        del payload
        for _ in range(100):
            record = service.get(DIGEST_A)
            if record is not None and record.state.is_terminal:
                break
            await __import__("asyncio").sleep(0.01)
        else:
            raise AssertionError("job did not reach a terminal state")

        gc.collect()
        assert payload_ref() is None
    finally:
        assert await service.shutdown()
        store.close()


@pytest.mark.anyio
async def test_service_requires_logical_compute_key_as_requested_job_id(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    store.open()
    service = PersistentJobService(
        handler=RaisingHandler(),
        store=store,
        settings=settings,
    )
    service.start()
    try:
        with pytest.raises(JobSubmissionInvalid):
            service.submit({}, requested_job_id=None)
        with pytest.raises(JobSubmissionInvalid):
            service.submit({}, requested_job_id=DIGEST_B)
    finally:
        assert await service.shutdown()
        store.close()


@pytest.mark.anyio
async def test_body_parser_rejects_duplicate_keys_oversize_and_compression(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    app = create_persistent_app(
        handler=RaisingHandler(),
        settings=_settings(tmp_path, max_request_bytes=128),
    )
    async with app.router.lifespan_context(app):
      async with _client(app) as api:
        duplicate = await api.post(
            "/jobs",
            content=b'{"input":{},"input":{}}',
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert duplicate.status_code == 400
        compressed = await api.post(
            "/jobs",
            content=b"{}",
            headers={
                **AUTH,
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )
        assert compressed.status_code == 415
        oversized = await api.post(
            "/jobs",
            content=json.dumps({"input": {"pad": "x" * 200}}),
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert oversized.status_code == 413


@pytest.mark.anyio
async def test_bounded_fifo_allows_cancel_only_while_queued(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    def headers_for(number: int) -> dict[str, str]:
        return {**AUTH_ONLY, "Idempotency-Key": f"{number:064x}"}

    handler = BlockingHandler()
    app = create_persistent_app(
        handler=handler,
        settings=_settings(tmp_path, queue_capacity=1),
    )
    async with app.router.lifespan_context(app):
      async with _client(app) as api:
        first = await api.post(
            "/jobs", json={"input": {"number": 1}}, headers=headers_for(1)
        )
        assert first.status_code == 202
        assert (
            PersistentSuryaHttpAdapter._parse_public_status(first.json()).state
            == AcceleratorJobState.QUEUED
        )
        for _ in range(100):
            if handler.started.is_set():
                break
            await __import__("asyncio").sleep(0.01)
        assert handler.started.is_set()

        second = await api.post(
            "/jobs", json={"input": {"number": 2}}, headers=headers_for(2)
        )
        assert second.status_code == 202
        third = await api.post(
            "/jobs", json={"input": {"number": 3}}, headers=headers_for(3)
        )
        assert third.status_code == 429
        assert third.headers["retry-after"] == "5"

        second_id = second.json()["id"]
        cancelled = await api.post(f"/jobs/{second_id}/cancel", headers=AUTH)
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        assert (
            PersistentSuryaHttpAdapter._parse_public_status(cancelled.json()).state
            == AcceleratorJobState.CANCELLED
        )
        repeated_cancel = await api.post(f"/jobs/{second_id}/cancel", headers=AUTH)
        assert repeated_cancel.status_code == 200
        assert repeated_cancel.json() == cancelled.json()
        assert (await api.get(f"/jobs/{second_id}", headers=AUTH)).json()["state"] == "cancelled"

        # Cancellation releases both the bounded slot and the only in-memory
        # reference to its capability-bearing body immediately.
        replacement = await api.post(
            "/jobs", json={"input": {"number": 4}}, headers=headers_for(4)
        )
        assert replacement.status_code == 202

        first_id = first.json()["id"]
        assert (await api.delete(f"/jobs/{first_id}", headers=AUTH)).status_code == 409
        handler.release.set()
        assert (await _wait_for_terminal(api, first_id))["state"] == "cancelled"


@pytest.mark.anyio
async def test_idempotency_key_returns_same_job_and_conflict_is_fixed(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    key = DIGEST_A
    app = create_persistent_app(handler=RaisingHandler(), settings=_settings(tmp_path))
    headers = {**AUTH, "Idempotency-Key": key}
    async with app.router.lifespan_context(app):
      async with _client(app) as api:
        first = await api.post("/jobs", json={"input": {"a": 1}}, headers=headers)
        second = await api.post("/jobs", json={"input": {"a": 1}}, headers=headers)
        assert first.status_code == 202
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"] == key

        def changed(_: type[SuryaLayoutRequest], payload: object) -> SimpleNamespace:
            return SimpleNamespace(
                logical_compute_key=DIGEST_A,
                request_digest="c" * 64,
            )

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                SuryaLayoutRequest,
                "from_wire_payload",
                classmethod(changed),
            )
            conflict = await api.post(
                "/jobs", json={"input": {"a": 2}}, headers=headers
            )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "idempotency_conflict"}


@pytest.mark.anyio
async def test_infra_retryable_idempotent_submit_requeues_with_fresh_memory_input(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    key = DIGEST_A
    handler = RaisingHandler()
    app = create_persistent_app(handler=handler, settings=_settings(tmp_path))
    headers = {**AUTH, "Idempotency-Key": key}
    async with app.router.lifespan_context(app):
        async with _client(app) as api:
            first = await api.post(
                "/jobs", json={"input": {"capability_version": 1}}, headers=headers
            )
            assert first.status_code == 202
            assert (await _wait_for_terminal(api, key))["state"] == "infra_retryable"

            retry = await api.post(
                "/jobs", json={"input": {"capability_version": 2}}, headers=headers
            )
            assert retry.status_code == 202
            assert retry.json()["state"] == "queued"
            assert (await _wait_for_terminal(api, key))["state"] == "infra_retryable"
    assert handler.calls == 2


@pytest.mark.anyio
async def test_startup_fences_incomplete_jobs_without_recovering_request_body(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    store.open()
    created_at = datetime.now(UTC)
    store.create(
        JobRecord(
            job_id="queued_recovery_0001",
            state=AcceleratorJobState.QUEUED,
            logical_compute_key=DIGEST_A,
            request_digest=DIGEST_B,
            created_at=created_at,
        )
    )
    store.close()

    app = create_persistent_app(handler=RaisingHandler(), settings=settings)
    async with app.router.lifespan_context(app):
      async with _client(app) as api:
        response = await api.get("/jobs/queued_recovery_0001", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["state"] == "infra_retryable"
        assert response.json()["reason_code"] == "worker_restarted"


def test_store_refuses_second_writer_but_quarantines_corrupt_record(tmp_path: Path) -> None:
    directory = tmp_path / "jobs"
    settings = _settings(tmp_path, state_directory=directory, max_job_records=10)
    first = _store(settings)
    second = _store(settings)
    first.open()
    try:
        with pytest.raises(JobStoreError):
            second.open()
    finally:
        first.close()

    directory.joinpath("bad.json").write_text('{"job_id":"bad","job_id":"other"}')
    third = _store(settings)
    third.open()
    try:
        assert third.count() == 0
        assert not directory.joinpath("bad.json").exists()
        assert directory.joinpath(".quarantine", "bad.json").exists()
    finally:
        third.close()


def test_store_does_not_rely_on_network_volume_directory_mode(tmp_path: Path) -> None:
    directory = tmp_path / "jobs"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    settings = _settings(tmp_path, state_directory=directory, max_job_records=10)
    store = _store(settings)

    store.open()
    try:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o777
    finally:
        store.close()


@pytest.mark.anyio
async def test_tampered_record_is_quarantined_and_api_reports_not_found(
    tmp_path: Path,
    accept_fake_request: None,
) -> None:
    settings = _settings(tmp_path)
    record = JobRecord(
        job_id="tampered_record_0001",
        state=AcceleratorJobState.QUEUED,
        logical_compute_key=DIGEST_A,
        request_digest=DIGEST_B,
        created_at=datetime.now(UTC),
    )
    store = _store(settings)
    store.open()
    store.create(record)
    store.close()

    path = settings.state_directory / f"{record.job_id}.json"
    envelope = json.loads(path.read_text())
    envelope["record"]["request_digest"] = DIGEST_A
    path.write_text(json.dumps(envelope))

    app = create_persistent_app(handler=RaisingHandler(), settings=settings)
    async with app.router.lifespan_context(app):
        async with _client(app) as api:
            response = await api.get(f"/jobs/{record.job_id}", headers=AUTH)
            assert response.status_code == 404
            assert response.json() == {"detail": "job_not_found"}
    assert (settings.state_directory / ".quarantine" / path.name).exists()


def test_store_quarantines_unsigned_legacy_record_and_ignores_stray_files(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    directory = settings.state_directory
    directory.mkdir()
    legacy = JobRecord(
        job_id="unsigned_legacy_0001",
        state=AcceleratorJobState.QUEUED,
        logical_compute_key=DIGEST_A,
        request_digest=DIGEST_B,
        created_at=datetime.now(UTC),
    )
    legacy_path = directory / f"{legacy.job_id}.json"
    legacy_path.write_bytes(legacy.model_dump_json(exclude_none=False).encode())
    directory.joinpath("operator-note.txt").write_text("not a record")
    directory.joinpath("interrupted.tmp").write_text("not a record")

    store = _store(settings)
    store.open()
    try:
        assert store.count() == 0
    finally:
        store.close()

    assert not legacy_path.exists()
    assert (directory / ".quarantine" / legacy_path.name).exists()
    assert directory.joinpath("operator-note.txt").exists()
    assert directory.joinpath("interrupted.tmp").exists()


def test_store_rejects_a_record_signed_with_a_rotated_bearer(tmp_path: Path) -> None:
    original = _settings(tmp_path)
    record = JobRecord(
        job_id="rotated_bearer_0001",
        state=AcceleratorJobState.QUEUED,
        logical_compute_key=DIGEST_A,
        request_digest=DIGEST_B,
        created_at=datetime.now(UTC),
    )
    writer = _store(original)
    writer.open()
    writer.create(record)
    writer.close()

    rotated = _settings(
        tmp_path,
        bearer_token=OTHER_TOKEN,
        state_directory=original.state_directory,
    )
    reader = _store(rotated)
    reader.open()
    try:
        assert reader.get(record.job_id) is None
    finally:
        reader.close()
    assert (original.state_directory / ".quarantine" / f"{record.job_id}.json").exists()


def test_store_poisoned_after_ambiguous_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, max_job_records=10)
    store = _store(settings)
    store.open()
    real_fsync = __import__("os").fsync
    calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr("os.fsync", fail_directory_fsync)
    with pytest.raises(JobStoreError):
        store.create(
            JobRecord(
                job_id="ambiguous_write_0001",
                state=AcceleratorJobState.QUEUED,
                logical_compute_key=DIGEST_A,
                request_digest=DIGEST_B,
                created_at=datetime.now(UTC),
            )
        )
    assert store.available is False
    with pytest.raises(JobStoreError):
        store.get("ambiguous_write_0001")
    store.close()
