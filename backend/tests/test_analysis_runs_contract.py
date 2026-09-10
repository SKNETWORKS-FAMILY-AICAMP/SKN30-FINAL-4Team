"""Contract tests for upload -> private storage -> durable queue -> polling."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    AnalysisRunRecord,
    SourceObject,
)
from app.services.analysis_runs import AnalysisRunService
from main import create_app


ORIGIN = "http://frontend.test"
USER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
HWP = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1request"
HWPX = b"PK\x03\x04request"


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.deleted: list[tuple[str, str]] = []

    async def put(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> None:
        self.objects[(bucket, object_key)] = (content, content_type)

    async def delete(self, *, bucket: str, object_key: str) -> None:
        self.deleted.append((bucket, object_key))
        self.objects.pop((bucket, object_key), None)


class FakeRepository:
    def __init__(self) -> None:
        self.records: dict[str, tuple[str, AnalysisRunRecord, SourceObject]] = {}
        self.fail_with: Exception | None = None

    async def create_queued(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
    ) -> AnalysisRunRecord:
        if self.fail_with:
            raise self.fail_with
        now = datetime.now(timezone.utc)
        record = AnalysisRunRecord(
            analysis_run_id=analysis_run_id,
            status="queued",
            analysis_case_id=None,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        self.records[analysis_run_id] = (owner_id, record, source)
        return record

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
    app = create_app()
    app.state.analysis_run_service = AnalysisRunService(repo, objects)
    app.state.auth_allowed_origins = frozenset({ORIGIN})
    app.state.upload_max_bytes = 50 * 1024 * 1024
    return TestClient(app), repo, objects


def headers(user_id: str = USER_ID) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-PreReview-Dev-User": user_id}


def test_hwp_and_hwpx_create_durable_pollable_runs() -> None:
    api, repository, storage = configured_client()
    with api:
        for filename, payload, mime in (
            ("요청서.hwp", HWP, "application/x-hwp"),
            ("요청서.hwpx", HWPX, "application/vnd.hancom.hwpx"),
        ):
            response = api.post(
                "/api/v1/analysis-runs",
                headers=headers(),
                files={"file": (filename, payload, mime)},
            )
            assert response.status_code == 202
            body = response.json()
            assert body["status"] == "queued"
            run_id = body["analysis_run_id"]

            owner, _record, source = repository.records[run_id]
            assert owner == USER_ID
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
                headers=headers(),
            )
            assert polled.status_code == 200
            assert polled.json() == {
                "analysis_run_id": run_id,
                "status": "queued",
                "analysis_case_id": None,
                "error_code": None,
                "error_message": None,
            }


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


def test_db_failure_compensates_uploaded_object() -> None:
    repository = FakeRepository()
    repository.fail_with = ActiveAnalysisRunExists("already active")
    api, _repository, storage = configured_client(repository=repository)
    with api:
        response = api.post(
            "/api/v1/analysis-runs",
            headers=headers(),
            files={"file": ("request.hwp", HWP, "application/x-hwp")},
        )
        assert response.status_code == 409
        assert not storage.objects
        assert len(storage.deleted) == 1


def test_upload_requires_trusted_origin_and_matching_magic() -> None:
    api, _repository, storage = configured_client()
    with api:
        missing_origin = api.post(
            "/api/v1/analysis-runs",
            headers={"X-PreReview-Dev-User": USER_ID},
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
