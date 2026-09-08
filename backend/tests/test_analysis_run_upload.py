"""Slice 7: Edge 업로드 진입점(RUN-01/RUN-03)이 기존 분석 큐에 닿는지 본다.

인메모리 흉내로는 확인할 수 없는 것이 네 가지다.

1. ``workspace.analysis_run.status`` CHECK 이 ``uploading`` 을 받는다는 것 —
   migration 103 이 없으면 RUN-01 자체가 CHECK 위반으로 죽는다.
2. 완료 요청이 실제로 ``queued`` 행을 만들고, ``worker.jobs.claim_next`` 가
   **그 행을** 집는다는 것. 두 번째 큐를 만들지 않았다는 증거다.
3. 같은 완료 요청을 두 번 보내도 케이스·큐 행이 하나뿐이라는 것. 멱등성은
   ``UPDATE ... WHERE status='uploading'`` 의 rowcount 에 달려 있어 진짜
   트랜잭션이 아니면 확인되지 않는다.
4. 실패한 완료 요청이 케이스·원본·큐 어느 것도 남기지 않는다는 것.

공용 ``sims_test`` 는 건드리지 않는다. 팀원 스키마를 거기 올리면 보류 중인
tests/test_worker_queue.py 가 skip 에서 실패로 바뀐다 (persistence·dispatcher
테스트가 같은 이유로 자기 DB 를 만든다).

**실제 Supabase 는 없다.** RUN-02(브라우저 → Storage)는 우리 코드가 아니므로,
테스트는 워커가 읽는 것과 **같은 ``ObjectStorage`` 포트**로 바이트를 넣는다.
Supabase Storage 의 RLS·서명 URL·버킷 정책은 여기서 검증되지 않는다.
"""

from __future__ import annotations

import io
import os
import pathlib
import zipfile
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.security import hash_password
from app.core.upload_limits import MAX_UPLOAD_BYTES
from app.db.identity_bridge import teammate_schema_installed
from app.services.analysis_run_upload import REQUEST_TEMP_BUCKET
from worker.jobs import claim_next
from main import create_app


_DATABASE = "sims_edge_upload_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"
PASSWORD = "correct-horse"
JWT_SECRET = "test-secret-that-is-at-least-32-bytes"


def hwpx_bytes(mimetype: bytes = b"application/hwp+zip") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        archive.writestr("Contents/section0.xml", "<section />")
    return output.getvalue()


HWPX = hwpx_bytes()


# ------------------------------------------------------------------ DB 픽스처


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for edge upload tests")
    return value


@pytest.fixture(scope="module")
def edge_database_url(database_url: str) -> Iterator[str]:
    """sims 와 팀원 스키마를 한 DB 에 올린 이 파일 전용 DB."""
    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)'
            )
            connection.exec_driver_sql(f'CREATE DATABASE "{_DATABASE}"')
    finally:
        admin.dispose()

    target = url.set(database=_DATABASE)
    engine = create_engine(target)
    with engine.connect() as connection:
        # 팀원 파일의 `format('... %I', t)` 때문에 드라이버 커넥션에 원문을 넘긴다.
        raw = connection.connection.driver_connection
        raw.execute(_SCHEMA.read_text(encoding="utf-8"))
        for path in sorted((_MIGRATIONS / "supabase").glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
    with engine.connect() as connection:
        assert teammate_schema_installed(connection)
    engine.dispose()
    yield target.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def engine(edge_database_url: str) -> Iterator[Engine]:
    value = create_engine(edge_database_url)
    try:
        yield value
    finally:
        value.dispose()


class _RecordingDispatcher:
    """실행기를 대신한다. 이 Slice 가 확인할 것은 등록이지 분석이 아니다.

    진짜 ``QueueJobDispatcher`` 를 두면 LLM 없는 분석이 곧바로 돌아 run 을
    ``failed`` 로 바꿔 버려, 등록 결과를 관찰할 창이 사라진다. 워커가 그 행을
    집을 수 있다는 것은 ``claim_next`` 로 직접 확인한다.
    """

    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    def dispatch(self, run_id: UUID) -> None:
        self.dispatched.append(run_id)


@pytest.fixture(scope="module")
def client(
    edge_database_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[TestClient]:
    settings = Settings(
        database_url=edge_database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path_factory.mktemp("edge-storage"),
    )
    with TestClient(create_app(settings)) as value:
        value.app.state.job_dispatcher = _RecordingDispatcher()
        yield value


@pytest.fixture(scope="module")
def users(engine: Engine) -> dict[str, int]:
    ids: dict[str, int] = {}
    with engine.begin() as connection:
        for login_id in ("owner", "stranger"):
            ids[login_id] = connection.scalar(
                text(
                    """
                    INSERT INTO sims.app_user (login_id, email, password_hash)
                    VALUES (:login_id, :email, :password_hash)
                    RETURNING id
                    """
                ),
                {
                    "login_id": login_id,
                    "email": f"{login_id}@example.com",
                    "password_hash": hash_password(PASSWORD),
                },
            )
    return ids


@pytest.fixture(scope="module")
def owner_auth(client: TestClient, users: dict[str, int]) -> dict[str, str]:
    return _bearer(client, "owner")


@pytest.fixture(scope="module")
def stranger_auth(client: TestClient, users: dict[str, int]) -> dict[str, str]:
    return _bearer(client, "stranger")


def _bearer(client: TestClient, login_id: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": f"{login_id}@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ------------------------------------------------------------------- 도우미


CREATE = "/functions/v1/edge-analysis-run-create"
COMPLETE = "/functions/v1/edge-analysis-run-complete-upload"


def _create_run(
    client: TestClient,
    auth: dict[str, str],
    *,
    filename: str = "요청서.hwpx",
    size: int = len(HWPX),
    mime: str = "application/octet-stream",
) -> Any:
    return client.post(
        CREATE,
        headers=auth,
        json={
            "original_filename": filename,
            "declared_mime_type": mime,
            "declared_size_bytes": size,
        },
    )


def _put_source(client: TestClient, object_key: str, payload: bytes) -> None:
    """RUN-02 를 대신한다. 브라우저가 Supabase Storage 로 직접 올리는 자리다."""
    storage = client.app.state.object_storage
    client.portal.call(  # type: ignore[union-attr]
        storage.put, f"{REQUEST_TEMP_BUCKET}/{object_key}", io.BytesIO(payload)
    )


def _run_status(engine: Engine, run_id: str) -> str | None:
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT status FROM workspace.analysis_run"
                " WHERE analysis_run_pk = :run_id"
            ),
            {"run_id": run_id},
        )


def _linked_cases(engine: Engine, run_id: str) -> int:
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_case"
                " WHERE analysis_run_id = :run_id"
            ),
            {"run_id": run_id},
        )


# -------------------------------------------------------------------- RUN-01


def test_create_returns_run_scoped_object_key(
    client: TestClient, owner_auth: dict[str, str], engine: Engine, users: dict[str, int]
) -> None:
    response = _create_run(client, owner_auth)

    assert response.status_code == 200, response.text
    body = response.json()
    run_id = UUID(body["analysis_run_id"])
    assert body["bucket"] == REQUEST_TEMP_BUCKET

    with engine.connect() as connection:
        user_uuid = connection.scalar(
            text("SELECT external_uuid FROM sims.app_user WHERE id = :id"),
            {"id": users["owner"]},
        )
    # 경로는 사용자와 run 에 귀속된다. 둘 다 응답 밖에서 구할 수 없는 값이다.
    assert body["object_key"] == f"request-source/{user_uuid}/{run_id}/source.hwpx"
    assert _run_status(engine, str(run_id)) == "uploading"


def test_create_requires_authentication(client: TestClient) -> None:
    response = client.post(
        CREATE,
        json={
            "original_filename": "요청서.hwpx",
            "declared_mime_type": "application/octet-stream",
            "declared_size_bytes": 100,
        },
    )
    assert response.status_code == 401


def test_create_rejects_unsupported_extension(
    client: TestClient, owner_auth: dict[str, str]
) -> None:
    response = _create_run(client, owner_auth, filename="요청서.pdf")
    assert response.status_code == 415


def test_create_rejects_oversized_declaration(
    client: TestClient, owner_auth: dict[str, str]
) -> None:
    response = _create_run(client, owner_auth, size=MAX_UPLOAD_BYTES + 1)
    assert response.status_code == 413


def test_create_rejects_empty_declaration(
    client: TestClient, owner_auth: dict[str, str]
) -> None:
    assert _create_run(client, owner_auth, size=0).status_code == 400


def test_create_ignores_client_supplied_bucket_and_key(
    client: TestClient, owner_auth: dict[str, str], engine: Engine, users: dict[str, int]
) -> None:
    """버킷·키를 끼워 넣어도 서버가 정한 경로가 나온다."""
    response = client.post(
        CREATE,
        headers=owner_auth,
        json={
            "original_filename": "요청서.hwpx",
            "declared_mime_type": "application/octet-stream",
            "declared_size_bytes": len(HWPX),
            "bucket": "public",
            "object_key": "../../etc/passwd",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bucket"] == REQUEST_TEMP_BUCKET
    assert body["object_key"].startswith("request-source/")
    assert ".." not in body["object_key"]


# -------------------------------------------------------------------- RUN-03


def test_complete_queues_exactly_one_job_the_worker_can_claim(
    client: TestClient, owner_auth: dict[str, str], engine: Engine
) -> None:
    created = _create_run(client, owner_auth).json()
    _put_source(client, created["object_key"], HWPX)

    response = client.post(
        COMPLETE,
        headers=owner_auth,
        json={"analysis_run_id": created["analysis_run_id"]},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "analysis_run_id": created["analysis_run_id"],
        "status": "queued",
    }
    assert _run_status(engine, created["analysis_run_id"]) == "queued"
    assert _linked_cases(engine, created["analysis_run_id"]) == 1
    assert client.app.state.job_dispatcher.dispatched == [
        UUID(created["analysis_run_id"])
    ]

    # 기존 워커가 이 행을 집는다. 두 번째 큐를 만들지 않았다는 증거다.
    with engine.begin() as connection:
        claim = claim_next(connection, worker_id="test-worker", lease_seconds=30)
    assert claim is not None
    assert str(claim.analysis_run_pk) == created["analysis_run_id"]

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace.analysis_run SET status = 'cancelled',"
                " claim_token = NULL, lease_expires_at = NULL"
                " WHERE analysis_run_pk = :run_id"
            ),
            {"run_id": created["analysis_run_id"]},
        )


def test_complete_is_idempotent(
    client: TestClient, owner_auth: dict[str, str], engine: Engine
) -> None:
    created = _create_run(client, owner_auth).json()
    _put_source(client, created["object_key"], HWPX)
    body = {"analysis_run_id": created["analysis_run_id"]}

    first = client.post(COMPLETE, headers=owner_auth, json=body)
    second = client.post(COMPLETE, headers=owner_auth, json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    # 두 번 불러도 케이스도 큐 행도 하나다.
    assert _linked_cases(engine, created["analysis_run_id"]) == 1
    with engine.connect() as connection:
        queued = connection.scalar(
            text(
                "SELECT count(*) FROM workspace.analysis_run"
                " WHERE analysis_run_pk = :run_id AND status = 'queued'"
            ),
            {"run_id": created["analysis_run_id"]},
        )
    assert queued == 1

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace.analysis_run SET status = 'cancelled'"
                " WHERE analysis_run_pk = :run_id"
            ),
            {"run_id": created["analysis_run_id"]},
        )


def test_complete_requires_authentication(
    client: TestClient, owner_auth: dict[str, str]
) -> None:
    created = _create_run(client, owner_auth).json()
    response = client.post(
        COMPLETE, json={"analysis_run_id": created["analysis_run_id"]}
    )
    assert response.status_code == 401


def test_complete_rejects_another_users_run(
    client: TestClient,
    owner_auth: dict[str, str],
    stranger_auth: dict[str, str],
    engine: Engine,
) -> None:
    created = _create_run(client, owner_auth).json()
    _put_source(client, created["object_key"], HWPX)

    response = client.post(
        COMPLETE,
        headers=stranger_auth,
        json={"analysis_run_id": created["analysis_run_id"]},
    )

    # 남의 run 은 '없는 run' 과 구분되지 않는다.
    assert response.status_code == 404
    assert _run_status(engine, created["analysis_run_id"]) == "uploading"
    assert _linked_cases(engine, created["analysis_run_id"]) == 0


def test_complete_rejects_unknown_run(
    client: TestClient, owner_auth: dict[str, str]
) -> None:
    response = client.post(
        COMPLETE, headers=owner_auth, json={"analysis_run_id": str(uuid4())}
    )
    assert response.status_code == 404


def test_complete_rejects_missing_storage_object(
    client: TestClient, owner_auth: dict[str, str], engine: Engine
) -> None:
    created = _create_run(client, owner_auth).json()

    response = client.post(
        COMPLETE,
        headers=owner_auth,
        json={"analysis_run_id": created["analysis_run_id"]},
    )

    assert response.status_code == 409
    assert _run_status(engine, created["analysis_run_id"]) == "uploading"
    assert _linked_cases(engine, created["analysis_run_id"]) == 0


def test_complete_rejects_content_that_is_not_hwpx(
    client: TestClient, owner_auth: dict[str, str], engine: Engine
) -> None:
    """확장자를 믿지 않는다. 실제 바이트를 매직바이트로 다시 본다."""
    payload = b"%PDF-1.7 not an hwpx at all"
    created = _create_run(client, owner_auth, size=len(payload)).json()
    _put_source(client, created["object_key"], payload)

    response = client.post(
        COMPLETE,
        headers=owner_auth,
        json={"analysis_run_id": created["analysis_run_id"]},
    )

    assert response.status_code == 415
    assert _run_status(engine, created["analysis_run_id"]) == "uploading"
    assert _linked_cases(engine, created["analysis_run_id"]) == 0


def test_complete_rejects_size_mismatch(
    client: TestClient, owner_auth: dict[str, str], engine: Engine
) -> None:
    """신고한 크기와 실제로 올라온 크기가 다르면 큐에 넣지 않는다."""
    created = _create_run(client, owner_auth, size=len(HWPX) + 1).json()
    _put_source(client, created["object_key"], HWPX)

    response = client.post(
        COMPLETE,
        headers=owner_auth,
        json={"analysis_run_id": created["analysis_run_id"]},
    )

    assert response.status_code == 400
    assert _run_status(engine, created["analysis_run_id"]) == "uploading"
    assert _linked_cases(engine, created["analysis_run_id"]) == 0


def test_complete_rejects_terminal_run(
    client: TestClient, owner_auth: dict[str, str], engine: Engine
) -> None:
    created = _create_run(client, owner_auth).json()
    _put_source(client, created["object_key"], HWPX)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace.analysis_run SET status = 'cancelled'"
                " WHERE analysis_run_pk = :run_id"
            ),
            {"run_id": created["analysis_run_id"]},
        )

    response = client.post(
        COMPLETE,
        headers=owner_auth,
        json={"analysis_run_id": created["analysis_run_id"]},
    )

    assert response.status_code == 409
    assert _linked_cases(engine, created["analysis_run_id"]) == 0


def test_failed_complete_leaks_no_internal_detail(
    client: TestClient, owner_auth: dict[str, str]
) -> None:
    """사용자 응답에 예외 타입·경로·자격증명이 새지 않는다."""
    created = _create_run(client, owner_auth).json()

    response = client.post(
        COMPLETE,
        headers=owner_auth,
        json={"analysis_run_id": created["analysis_run_id"]},
    )

    assert response.status_code == 409
    text_body = response.text
    for leak in (
        "Traceback",
        "FileNotFoundError",
        "postgresql",
        "password",
        REQUEST_TEMP_BUCKET,
        "request-source",
    ):
        assert leak not in text_body
