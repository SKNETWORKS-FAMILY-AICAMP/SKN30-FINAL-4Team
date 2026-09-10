"""Contract tests for upload -> private storage -> durable queue -> polling."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    AnalysisRunFinalizationRejected,
    AnalysisRunFinalizationUncertain,
    AnalysisRunRecord,
    ObjectStorageUnavailable,
    ObjectStorageWriteUncertain,
    SourceObject,
    UploadCleanupObject,
    UploadReservation,
)
from app.services.analysis_runs import AnalysisRunService
from main import create_app


ORIGIN = "http://frontend.test"
USER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
THIRD_USER_ID = "33333333-3333-3333-3333-333333333333"
HWP = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1request"
HWPX = b"PK\x03\x04request"


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.events: list[str] = []
        self.fail_put: Exception | None = None
        self.fail_delete: Exception | None = None

    async def put(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> None:
        self.events.append("storage.put")
        if self.fail_put:
            raise self.fail_put
        self.objects[(bucket, object_key)] = (content, content_type)

    async def delete(self, *, bucket: str, object_key: str) -> None:
        self.events.append("storage.delete")
        if self.fail_delete:
            raise self.fail_delete
        self.deleted.append((bucket, object_key))
        self.objects.pop((bucket, object_key), None)


class FakeRepository:
    def __init__(self) -> None:
        self.records: dict[str, tuple[str, AnalysisRunRecord, SourceObject]] = {}
        self.events: list[str] = []
        self.reserve_fail_with: Exception | None = None
        self.finalize_fail_with: Exception | None = None
        self.mark_cleanup_fail_with: Exception | None = None
        self.mark_cleanup_returns_none = False
        self.cleanup_candidates: tuple[UploadCleanupObject, ...] = ()

    async def reserve_uploading(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
    ) -> UploadReservation:
        self.events.append("repository.reserve")
        if self.reserve_fail_with:
            raise self.reserve_fail_with
        existing = self.records.get(analysis_run_id)
        if existing is not None:
            if existing[0] == owner_id and existing[2] == source:
                return UploadReservation(
                    record=existing[1],
                    replayed=existing[1].status != "uploading",
                )
            raise ActiveAnalysisRunExists("idempotency key already exists")
        if any(
            item_owner == owner_id
            and item_record.status in {"uploading", "queued", "running"}
            for item_owner, item_record, _source in self.records.values()
        ):
            raise ActiveAnalysisRunExists("already active")
        now = datetime.now(timezone.utc)
        record = AnalysisRunRecord(
            analysis_run_id=analysis_run_id,
            status="uploading",
            analysis_case_id=None,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        self.records[analysis_run_id] = (owner_id, record, source)
        return UploadReservation(
            record=record,
            cleanup_objects=self.cleanup_candidates,
        )

    async def finalize_queued(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
    ) -> AnalysisRunRecord:
        self.events.append("repository.finalize")
        if self.finalize_fail_with:
            raise self.finalize_fail_with
        item_owner, record, item_source = self.records[analysis_run_id]
        assert item_owner == owner_id
        assert item_source == source
        queued = replace(record, status="queued", updated_at=datetime.now(timezone.utc))
        self.records[analysis_run_id] = (item_owner, queued, item_source)
        return queued

    async def mark_upload_cleanup_pending(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
        error_code: str,
        error_message: str,
    ) -> UploadCleanupObject | None:
        self.events.append("repository.mark_cleanup")
        if self.mark_cleanup_fail_with:
            raise self.mark_cleanup_fail_with
        if self.mark_cleanup_returns_none:
            return None
        item = self.records.get(analysis_run_id)
        if item is None or item[0] != owner_id or item[1].status != "uploading":
            return None
        assert item[2] == source
        pending = replace(
            item[1],
            status="cleanup_pending",
            error_code=error_code,
            error_message=error_message,
            updated_at=datetime.now(timezone.utc),
        )
        self.records[analysis_run_id] = (item[0], pending, item[2])
        return UploadCleanupObject(
            analysis_run_id=analysis_run_id,
            bucket=item[2].bucket,
            object_key=item[2].object_key,
        )

    async def complete_upload_cleanup(self, *, analysis_run_id: str) -> bool:
        self.events.append("repository.complete_cleanup")
        item = self.records.get(analysis_run_id)
        if item is None or item[1].status != "cleanup_pending":
            # A stale cleanup candidate need not be materialised in this fake.
            return any(
                candidate.analysis_run_id == analysis_run_id
                for candidate in self.cleanup_candidates
            )
        failed = replace(
            item[1],
            status="failed",
            updated_at=datetime.now(timezone.utc),
        )
        self.records[analysis_run_id] = (item[0], failed, item[2])
        return True

    async def get_for_owner(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
    ) -> AnalysisRunRecord | None:
        item = self.records.get(analysis_run_id)
        return item[1] if item and item[0] == owner_id else None


def configured_client(
    repository: FakeRepository | None = None,
    storage: FakeStorage | None = None,
) -> tuple[TestClient, FakeRepository, FakeStorage]:
    repo = repository or FakeRepository()
    objects = storage or FakeStorage()
    events: list[str] = []
    repo.events = events
    objects.events = events
    app = create_app()
    app.state.analysis_run_service = AnalysisRunService(repo, objects)
    app.state.auth_allowed_origins = frozenset({ORIGIN})
    app.state.upload_max_bytes = 50 * 1024 * 1024
    return TestClient(app), repo, objects


def headers(
    user_id: str = USER_ID,
    *,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "X-PreReview-Dev-User": user_id,
        "Idempotency-Key": idempotency_key or str(uuid4()),
    }


def test_hwp_and_hwpx_create_durable_pollable_runs() -> None:
    api, repository, storage = configured_client()
    with api:
        for user_id, filename, payload, mime in (
            (USER_ID, "요청서.hwp", HWP, "application/x-hwp"),
            (OTHER_USER_ID, "요청서.hwpx", HWPX, "application/vnd.hancom.hwpx"),
        ):
            response = api.post(
                "/api/v1/analysis-runs",
                headers=headers(user_id),
                files={"file": (filename, payload, mime)},
            )
            assert response.status_code == 202
            body = response.json()
            assert body["status"] == "queued"
            run_id = body["analysis_run_id"]

            owner, _record, source = repository.records[run_id]
            assert owner == user_id
            assert source.filename == filename
            assert source.bucket == "request-temp"
            assert source.object_key.startswith(f"{run_id}/source/")
            assert source.object_key.endswith(filename[filename.rfind(".") :])
            assert source.mime_type in {
                "application/x-hwp",
                "application/vnd.hancom.hwpx",
            }
            assert source.declared_mime_type == mime
            assert storage.objects[(source.bucket, source.object_key)][0] == payload

            polled = api.get(
                f"/api/v1/analysis-runs/{run_id}",
                headers=headers(user_id),
            )
            assert polled.status_code == 200
            assert polled.json() == {
                "analysis_run_id": run_id,
                "status": "queued",
                "analysis_case_id": None,
                "error_code": None,
                "error_message": None,
            }
        assert repository.events == [
            "repository.reserve",
            "storage.put",
            "repository.finalize",
            "repository.reserve",
            "storage.put",
            "repository.finalize",
        ]


def test_polling_hides_another_users_run() -> None:
    api, _repository, _storage = configured_client()
    with api:
        created = api.post(
            "/api/v1/analysis-runs",
            headers=headers(),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
        run_id = created.json()["analysis_run_id"]
        hidden = api.get(
            f"/api/v1/analysis-runs/{run_id}",
            headers=headers(OTHER_USER_ID),
        )
        assert hidden.status_code == 404


def test_idempotency_key_replay_returns_the_same_run_without_second_upload() -> None:
    api, repository, storage = configured_client()
    key = "55555555-5555-4555-8555-555555555555"
    request_headers = headers(idempotency_key=key)
    with api:
        first = api.post(
            "/api/v1/analysis-runs",
            headers=request_headers,
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
        replay = api.post(
            "/api/v1/analysis-runs",
            headers=request_headers,
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["analysis_run_id"] == key
    assert len(storage.objects) == 1
    assert repository.events == [
        "repository.reserve",
        "storage.put",
        "repository.finalize",
        "repository.reserve",
    ]


def test_idempotency_key_resumes_exact_uploading_reservation() -> None:
    repository = FakeRepository()
    key = "77777777-7777-4777-8777-777777777777"
    digest = sha256(HWP).hexdigest()
    source = SourceObject(
        bucket="request-temp",
        object_key=f"{key}/source/{digest}.hwp",
        content_sha256=digest,
        filename="request.hwp",
        mime_type="application/x-hwp",
        declared_mime_type="application/x-hwp",
        size_bytes=len(HWP),
    )
    now = datetime.now(timezone.utc)
    repository.records[key] = (
        USER_ID,
        AnalysisRunRecord(
            analysis_run_id=key,
            status="uploading",
            analysis_case_id=None,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        ),
        source,
    )
    api, repository, storage = configured_client(repository=repository)

    with api:
        response = api.post(
            "/api/v1/analysis-runs",
            headers=headers(idempotency_key=key),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )

    assert response.status_code == 202
    assert response.json() == {"analysis_run_id": key, "status": "queued"}
    assert len(storage.objects) == 1
    assert repository.events == [
        "repository.reserve",
        "storage.put",
        "repository.finalize",
    ]


def test_idempotency_key_cannot_be_reused_for_different_source() -> None:
    api, repository, storage = configured_client()
    key = "66666666-6666-4666-8666-666666666666"
    request_headers = headers(idempotency_key=key)
    with api:
        first = api.post(
            "/api/v1/analysis-runs",
            headers=request_headers,
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
        mismatch = api.post(
            "/api/v1/analysis-runs",
            headers=request_headers,
            files={"file": ("request.hwpx", HWPX, "application/vnd.hancom.hwpx")},
        )
    assert first.status_code == 202
    assert mismatch.status_code == 409
    assert len(storage.objects) == 1
    assert repository.events[-1] == "repository.reserve"


def test_active_run_conflict_happens_before_storage_is_touched() -> None:
    repository = FakeRepository()
    repository.reserve_fail_with = ActiveAnalysisRunExists("already active")
    api, _repository, storage = configured_client(repository=repository)
    with api:
        response = api.post(
            "/api/v1/analysis-runs",
            headers=headers(),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
        assert response.status_code == 409
        assert not storage.objects
        assert not storage.deleted
        assert repository.events == ["repository.reserve"]


def test_storage_failure_is_recorded_and_cleaned_before_run_fails() -> None:
    storage = FakeStorage()
    storage.fail_put = ObjectStorageUnavailable("storage unavailable")
    api, repository, _storage = configured_client(storage=storage)
    with api:
        response = api.post(
            "/api/v1/analysis-runs",
            headers=headers(),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
    assert response.status_code == 503
    record = next(iter(repository.records.values()))[1]
    assert record.status == "failed"
    assert record.error_code == "SOURCE_UPLOAD_FAILED"
    assert repository.events == [
        "repository.reserve",
        "storage.put",
        "repository.mark_cleanup",
        "storage.delete",
        "repository.complete_cleanup",
    ]


@pytest.mark.parametrize("cleanup_outcome", ["error", "not-acquired"])
def test_storage_failure_never_deletes_without_cleanup_fence(
    cleanup_outcome: str,
) -> None:
    repository = FakeRepository()
    if cleanup_outcome == "error":
        repository.mark_cleanup_fail_with = RuntimeError("database unavailable")
    else:
        repository.mark_cleanup_returns_none = True
    storage = FakeStorage()
    storage.fail_put = ObjectStorageUnavailable("storage unavailable")
    api, repository, storage = configured_client(
        repository=repository,
        storage=storage,
    )

    with api:
        response = api.post(
            "/api/v1/analysis-runs",
            headers=headers(),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )

    assert response.status_code == 503
    assert storage.deleted == []
    assert "storage.delete" not in repository.events
    assert next(iter(repository.records.values()))[1].status == "uploading"


def test_uncertain_storage_write_preserves_reservation_without_cleanup() -> None:
    storage = FakeStorage()
    storage.fail_put = ObjectStorageWriteUncertain("write acknowledgement lost")
    api, repository, storage = configured_client(storage=storage)

    with api:
        response = api.post(
            "/api/v1/analysis-runs",
            headers=headers(),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )

    assert response.status_code == 503
    assert repository.events == ["repository.reserve", "storage.put"]
    assert storage.deleted == []
    assert next(iter(repository.records.values()))[1].status == "uploading"


def test_known_finalize_rollback_cleans_source_but_uncertain_outcome_does_not() -> None:
    rejected_repository = FakeRepository()
    rejected_repository.finalize_fail_with = AnalysisRunFinalizationRejected(
        "rolled back"
    )
    rejected_api, rejected_repository, rejected_storage = configured_client(
        repository=rejected_repository
    )
    with rejected_api:
        rejected = rejected_api.post(
            "/api/v1/analysis-runs",
            headers=headers(),
            files={"file": ("request.hwpx", HWPX, "application/vnd.hancom.hwpx")},
        )
    assert rejected.status_code == 503
    assert not rejected_storage.objects
    assert len(rejected_storage.deleted) == 1
    assert next(iter(rejected_repository.records.values()))[1].status == "failed"

    uncertain_repository = FakeRepository()
    uncertain_repository.finalize_fail_with = AnalysisRunFinalizationUncertain(
        "commit acknowledgement lost"
    )
    uncertain_api, uncertain_repository, uncertain_storage = configured_client(
        repository=uncertain_repository
    )
    with uncertain_api:
        uncertain = uncertain_api.post(
            "/api/v1/analysis-runs",
            headers=headers(OTHER_USER_ID),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
    assert uncertain.status_code == 503
    assert len(uncertain_storage.objects) == 1
    assert not uncertain_storage.deleted
    assert next(iter(uncertain_repository.records.values()))[1].status == "uploading"
    assert uncertain_repository.events == [
        "repository.reserve",
        "storage.put",
        "repository.finalize",
    ]


def test_new_reservation_retries_stale_cleanup_before_current_upload() -> None:
    repository = FakeRepository()
    repository.cleanup_candidates = (
        UploadCleanupObject(
            analysis_run_id="44444444-4444-4444-4444-444444444444",
            bucket="request-temp",
            object_key="stale/source/deadbeef.hwpx",
        ),
    )
    api, repository, storage = configured_client(repository=repository)
    with api:
        response = api.post(
            "/api/v1/analysis-runs",
            headers=headers(THIRD_USER_ID),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
    assert response.status_code == 202
    assert storage.deleted == [("request-temp", "stale/source/deadbeef.hwpx")]
    assert repository.events[:4] == [
        "repository.reserve",
        "storage.delete",
        "repository.complete_cleanup",
        "storage.put",
    ]


def test_upload_requires_trusted_origin_and_matching_magic() -> None:
    api, _repository, storage = configured_client()
    with api:
        missing_origin = api.post(
            "/api/v1/analysis-runs",
            headers={
                "X-PreReview-Dev-User": USER_ID,
                "Idempotency-Key": str(uuid4()),
            },
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
        assert missing_origin.status_code == 403

        bad_magic = api.post(
            "/api/v1/analysis-runs",
            headers=headers(),
            files={"file": ("request.hwpx", b"not-a-zip", "application/vnd.hancom.hwpx")},
        )
        assert bad_magic.status_code == 415
        assert not storage.objects


def test_upload_requires_uuid_idempotency_key() -> None:
    api, _repository, storage = configured_client()
    with api:
        missing = api.post(
            "/api/v1/analysis-runs",
            headers={"Origin": ORIGIN, "X-PreReview-Dev-User": USER_ID},
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
        malformed = api.post(
            "/api/v1/analysis-runs",
            headers={
                "Origin": ORIGIN,
                "X-PreReview-Dev-User": USER_ID,
                "Idempotency-Key": "not-a-uuid",
            },
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
    assert missing.status_code == 422
    assert malformed.status_code == 422
    assert not storage.objects


def test_browser_preflight_allows_idempotency_header_for_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PREREVIEW_AUTH_ALLOWED_ORIGINS", ORIGIN)
    api, _repository, _storage = configured_client()
    with api:
        response = api.options(
            "/api/v1/analysis-runs",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,idempotency-key",
            },
        )
    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in allowed
    assert response.headers["access-control-allow-credentials"] == "true"
