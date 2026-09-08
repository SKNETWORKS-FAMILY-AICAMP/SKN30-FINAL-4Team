"""서명 URL 로 보고서 PDF 를 내려받는 두 걸음을 고정한다.

화면은 발급받은 URL 로 ``window.location.href`` 이동을 한다. 그 이동에는
``Authorization`` 헤더가 붙지 않으므로 **URL 자체가 자격증명**이다. 그래서
이 파일이 확인하는 것은 대부분 그 자격증명의 경계다.

  - 소유권은 발급 시점에 본다 (남의 케이스로는 URL 이 나오지 않는다)
  - 발급된 URL 은 짧게 살고, 만료되면 못 쓴다
  - 접근 토큰과 다운로드 토큰이 서로 통용되지 않는다
  - 다운로드 가능 조건이 ``rpc_get_analysis_result`` 의 ``can_download`` 와 같다

마지막 항목이 특히 중요하다. 화면은 ``can_download`` 를 보고 버튼을 켠 뒤
여기로 온다. 두 조건이 어긋나면 켜진 버튼이 오류를 받는다.
"""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import time
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from jwt import InvalidTokenError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.security import (
    create_report_download_token,
    decode_access_token,
    decode_report_download_token,
)
from app.db.identity_bridge import ensure_identity
from app.ports.object_storage import storage_key
from main import create_app


_DATABASE = "sims_report_download_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"
JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"
REPORT_BUCKET = "report"
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"

CREATE_URL = "/functions/v1/edge-report-create-download-url"


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for report download tests")
    return value


@pytest.fixture(scope="module")
def report_database_url(database_url: str) -> Iterator[str]:
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
        raw = connection.connection.driver_connection
        raw.execute(_SCHEMA.read_text(encoding="utf-8"))
        for path in sorted((_MIGRATIONS / "supabase").glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
    engine.dispose()
    yield target.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def engine(report_database_url: str) -> Iterator[Engine]:
    value = create_engine(report_database_url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture(scope="module")
def client(
    report_database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[TestClient]:
    settings = Settings(
        database_url=report_database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path_factory.mktemp("report-storage"),
    )
    with TestClient(create_app(settings)) as value:
        yield value


@pytest.fixture(scope="module")
def users(engine: Engine) -> dict[str, int]:
    ids: dict[str, int] = {}
    with engine.begin() as connection:
        for login_id in ("owner", "stranger"):
            ids[login_id] = connection.scalar(
                text(
                    "INSERT INTO sims.app_user (login_id, email, password_hash)"
                    " VALUES (:login_id, :email, :hash) RETURNING id"
                ),
                {
                    "login_id": login_id,
                    "email": f"{login_id}@example.com",
                    "hash": _password_hash(),
                },
            )
    return ids


def _password_hash() -> str:
    from app.core.security import hash_password

    return hash_password(PASSWORD)


def _bearer(client: TestClient, login_id: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": f"{login_id}@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="module")
def owner_auth(client: TestClient, users: dict[str, int]) -> dict[str, str]:
    return _bearer(client, "owner")


@pytest.fixture(scope="module")
def stranger_auth(client: TestClient, users: dict[str, int]) -> dict[str, str]:
    return _bearer(client, "stranger")


def _make_case(
    client: TestClient,
    engine: Engine,
    owner_user_id: int,
    *,
    report_status: str = "ready",
    with_artifact: bool = True,
    artifact_ttl_hours: int = 24,
    original_filename: str = "요청서.hwpx",
) -> UUID:
    """결과 케이스 하나와 (선택적으로) 보고서 아티팩트를 심는다.

    PDF 생성은 아직 큐 경로에 연결되지 않았다. 이 Slice 가 고정하는 것은
    "준비된 보고서를 어떻게 내보내는가" 이므로, 준비된 상태를 직접 만든다.
    """
    with engine.begin() as connection:
        user_uuid = ensure_identity(connection, owner_user_id)
        run_id = connection.scalar(
            text(
                "INSERT INTO workspace.analysis_run (user_id, status)"
                " VALUES (:user_id, 'succeeded') RETURNING analysis_run_pk"
            ),
            {"user_id": user_uuid},
        )
        case_pk = connection.scalar(
            text(
                "INSERT INTO result.analysis_case ("
                "  source_analysis_run_id, user_id, case_status,"
                "  analysis_completed_at, original_filename, report_status)"
                " VALUES (:run_id, :user_id, 'ready', now(), :filename, :status)"
                " RETURNING analysis_case_pk"
            ),
            {
                "run_id": run_id,
                "user_id": user_uuid,
                "filename": original_filename,
                "status": report_status,
            },
        )
        if with_artifact:
            object_key = f"{case_pk}/report.pdf"
            connection.execute(
                text(
                    "INSERT INTO result.report_artifact ("
                    "  analysis_case_pk, report_type, storage_bucket,"
                    "  storage_object_key, content_sha256, mime_type,"
                    "  size_bytes, expires_at)"
                    " VALUES (:case_pk, 'pdf', :bucket, :key, :sha,"
                    "         'application/pdf', :size,"
                    "         now() + make_interval(hours => :ttl))"
                ),
                {
                    "case_pk": case_pk,
                    "bucket": REPORT_BUCKET,
                    "key": object_key,
                    "sha": hashlib.sha256(PDF).hexdigest(),
                    "size": len(PDF),
                    "ttl": artifact_ttl_hours,
                },
            )
            client.portal.call(  # type: ignore[union-attr]
                client.app.state.object_storage.put,
                storage_key(REPORT_BUCKET, object_key),
                io.BytesIO(PDF),
            )
    return case_pk


def _rpc_can_download(engine: Engine, case_pk: UUID, owner_user_id: int) -> bool | None:
    """RPC 가 보는 can_download. 화면이 버튼을 켜는 근거와 같은 값이다.

    ``rpc_get_analysis_result`` 는 ``auth.uid()`` 로 소유자를 확인하므로,
    PostgREST 가 세우는 클레임을 여기서 직접 세운다.
    """
    with engine.begin() as connection:
        user_uuid = ensure_identity(connection, owner_user_id)
        connection.execute(
            text("SELECT set_config('request.jwt.claim.sub', :sub, true)"),
            {"sub": str(user_uuid)},
        )
        return connection.scalar(
            text(
                "SELECT (api.rpc_get_analysis_result(:case_pk) -> 'report'"
                " ->> 'can_download')::boolean"
            ),
            {"case_pk": case_pk},
        )


# ------------------------------------------------------------------- 발급


def test_owner_gets_a_short_lived_url_and_can_download_the_pdf(
    client: TestClient, engine: Engine, users: dict[str, int], owner_auth
) -> None:
    case_pk = _make_case(client, engine, users["owner"])

    issued = client.post(
        CREATE_URL, headers=owner_auth, json={"analysis_case_id": str(case_pk)}
    )

    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["expires_in_seconds"] == 60
    assert body["signed_url"].startswith("http")

    # 헤더 없이, URL 만으로 받아진다. 화면이 하는 일과 같다.
    downloaded = client.get(body["signed_url"])
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.content == PDF
    # 원본 파일명을 pdf 로 바꿔 쓴다.
    assert "%EC%9A%94%EC%B2%AD%EC%84%9C.pdf" in downloaded.headers[
        "content-disposition"
    ]


def test_issuing_requires_authentication(client: TestClient) -> None:
    response = client.post(CREATE_URL, json={"analysis_case_id": str(uuid4())})
    assert response.status_code == 401


def test_another_user_cannot_get_a_url_for_someone_elses_case(
    client: TestClient, engine: Engine, users: dict[str, int], stranger_auth
) -> None:
    case_pk = _make_case(client, engine, users["owner"])

    response = client.post(
        CREATE_URL, headers=stranger_auth, json={"analysis_case_id": str(case_pk)}
    )

    # 남의 케이스는 없는 케이스와 구분되지 않는다.
    assert response.status_code == 404


def test_unknown_case_is_not_found(client: TestClient, owner_auth) -> None:
    response = client.post(
        CREATE_URL, headers=owner_auth, json={"analysis_case_id": str(uuid4())}
    )
    assert response.status_code == 404


def test_report_still_generating_is_a_conflict_not_a_url(
    client: TestClient, engine: Engine, users: dict[str, int], owner_auth
) -> None:
    case_pk = _make_case(
        client, engine, users["owner"], report_status="generating", with_artifact=False
    )

    response = client.post(
        CREATE_URL, headers=owner_auth, json={"analysis_case_id": str(case_pk)}
    )

    assert response.status_code == 409


def test_expired_artifact_matches_can_download_being_false(
    client: TestClient, engine: Engine, users: dict[str, int], owner_auth
) -> None:
    """보관 기간이 지난 보고서는 RPC 도 can_download=false 로 본다."""
    case_pk = _make_case(client, engine, users["owner"], artifact_ttl_hours=-1)

    can_download = _rpc_can_download(engine, case_pk, users["owner"])
    response = client.post(
        CREATE_URL, headers=owner_auth, json={"analysis_case_id": str(case_pk)}
    )

    assert can_download is False
    assert response.status_code == 409


def test_ready_report_matches_can_download_being_true(
    client: TestClient, engine: Engine, users: dict[str, int], owner_auth
) -> None:
    case_pk = _make_case(client, engine, users["owner"])

    can_download = _rpc_can_download(engine, case_pk, users["owner"])
    response = client.post(
        CREATE_URL, headers=owner_auth, json={"analysis_case_id": str(case_pk)}
    )

    assert can_download is True
    assert response.status_code == 200


# ------------------------------------------------------------------ 토큰


def test_expired_token_is_rejected(
    client: TestClient, engine: Engine, users: dict[str, int]
) -> None:
    case_pk = _make_case(client, engine, users["owner"])
    token = create_report_download_token(str(case_pk), JWT_SECRET, 1)
    time.sleep(1.2)

    response = client.get("/api/v1/reports/download", params={"token": token})

    assert response.status_code == 404


def test_token_signed_with_another_secret_is_rejected(
    client: TestClient, engine: Engine, users: dict[str, int]
) -> None:
    case_pk = _make_case(client, engine, users["owner"])
    forged = create_report_download_token(str(case_pk), "y" * 32, 60)

    response = client.get("/api/v1/reports/download", params={"token": forged})

    assert response.status_code == 404


def test_access_token_cannot_be_used_as_a_download_token(
    client: TestClient, engine: Engine, users: dict[str, int], owner_auth
) -> None:
    """두 토큰은 클레임 집합이 달라 서로 통용되지 않는다.

    경계를 **함수 수준에서** 고정한다. 엔드포인트만 보면 지금은 sub 가
    "1" 이라 UUID 파싱에서 걸려 통과하는데, Supabase Auth 로 옮기면 sub 가
    UUID 가 되어 그 우연이 사라진다. purpose 검사가 진짜 문이어야 한다.
    """
    _make_case(client, engine, users["owner"])
    access_token = owner_auth["Authorization"].removeprefix("Bearer ")

    with pytest.raises(InvalidTokenError):
        decode_report_download_token(access_token, JWT_SECRET)

    response = client.get("/api/v1/reports/download", params={"token": access_token})
    assert response.status_code == 404


def test_download_token_cannot_be_used_as_an_access_token(
    client: TestClient, engine: Engine, users: dict[str, int]
) -> None:
    case_pk = _make_case(client, engine, users["owner"])
    download_token = create_report_download_token(str(case_pk), JWT_SECRET, 60)

    # 접근 토큰 해독기는 sub/pwd 를 요구하므로 이 토큰을 받지 않는다.
    with pytest.raises(InvalidTokenError):
        decode_access_token(download_token, JWT_SECRET)

    response = client.post(
        CREATE_URL,
        headers={"Authorization": f"Bearer {download_token}"},
        json={"analysis_case_id": str(case_pk)},
    )
    assert response.status_code == 401


def test_token_carries_only_the_case_it_was_issued_for(
    client: TestClient, engine: Engine, users: dict[str, int], owner_auth
) -> None:
    """한 케이스로 받은 URL 로 다른 케이스를 받을 수 없다."""
    mine = _make_case(client, engine, users["owner"])
    other = _make_case(client, engine, users["stranger"])

    issued = client.post(
        CREATE_URL, headers=owner_auth, json={"analysis_case_id": str(mine)}
    ).json()
    token = issued["signed_url"].rsplit("token=", 1)[-1]

    assert decode_report_download_token(token, JWT_SECRET) == str(mine)
    assert str(other) != decode_report_download_token(token, JWT_SECRET)


def test_missing_stored_file_is_not_a_500_and_leaks_no_path(
    client: TestClient, engine: Engine, users: dict[str, int], owner_auth
) -> None:
    case_pk = _make_case(client, engine, users["owner"], with_artifact=True)
    # 아티팩트 행은 있는데 바이트가 사라진 상태를 만든다.
    client.portal.call(  # type: ignore[union-attr]
        client.app.state.object_storage.delete,
        storage_key(REPORT_BUCKET, f"{case_pk}/report.pdf"),
    )
    issued = client.post(
        CREATE_URL, headers=owner_auth, json={"analysis_case_id": str(case_pk)}
    ).json()

    response = client.get(issued["signed_url"])

    assert response.status_code == 503
    for leak in ("Traceback", "FileNotFoundError", REPORT_BUCKET, str(case_pk)):
        assert leak not in response.text
