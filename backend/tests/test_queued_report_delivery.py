"""큐가 만든 PDF 를 다운로드 진입점이 실제로 꺼내는지 본다.

두 쪽은 각각 잘 덮여 있다. 디스패처 테스트는 ``result.report_artifact`` 행이
남는 데까지 보고, 다운로드 테스트는 손으로 심은 artifact 를 꺼내는 데까지 본다.
**그 사이의 이음매는 아무도 보지 않았다.**

이음매에서 어긋날 수 있는 것들이 실제로 있다.

  - 워커가 쓰는 버킷·키와 다운로드가 읽는 버킷·키가 같은가
  - 워커가 넣은 ``expires_at`` 이 ``can_download`` 조건을 통과하는가
  - 워커가 ``report_status`` 를 실제로 ``ready`` 로 올리는가
  - 저장소에 실제 바이트가 있고 그것이 내려오는가

한쪽만 고쳐도 조용히 깨지는 자리라 여기서 한 번에 고정한다.

LLM 은 타지 않는다. 분석 콜백이 저장된 결과 계약을 그대로 돌려주고, PDF 는
실제 ReportLab 렌더러가 그린다.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.security import hash_password
from app.db.identity_bridge import ensure_identity
from app.infrastructure.reportlab_pdf_renderer import ReportLabPdfRenderer
from app.schemas.cpl import CplFieldCode
from worker.contracts.cpl_result import CplItem, CplResult
from worker.dispatcher import enqueue, run_once
from worker.persistence import AnalysisResults
from main import create_app


_DATABASE = "sims_queued_delivery_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"
JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"

CREATE_URL = "/functions/v1/edge-report-create-download-url"


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for queued delivery tests")

    url = make_url(value)
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
def client(
    database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path_factory.mktemp("queued-delivery"),
    )
    with TestClient(create_app(settings)) as value:
        yield value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    value = create_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


def _case(engine: Engine) -> tuple[int, int]:
    """업로드가 남기는 행 모양 그대로 검사 건 하나를 만든다."""
    with engine.begin() as connection:
        user_id = connection.scalar(
            text(
                "INSERT INTO sims.app_user (login_id, email, password_hash)"
                " VALUES ('owner', 'owner@example.com', :hash) RETURNING id"
            ),
            {"hash": hash_password(PASSWORD)},
        )
        ensure_identity(connection, user_id)
        case_id = connection.scalar(
            text(
                "INSERT INTO sims.inspection_case (owner_user_id)"
                " VALUES (:user_id) RETURNING id"
            ),
            {"user_id": user_id},
        )
        file_asset_id = connection.scalar(
            text(
                "INSERT INTO sims.file_asset ("
                "  asset_scope, owner_user_id, inspection_case_id, storage_key,"
                "  original_filename, detected_mime_type, extension, size_bytes,"
                "  sha256_hex)"
                " VALUES ('USER', :user_id, :case_id, :key, '요청서.hwpx',"
                "         'application/hwp+zip', 'hwpx', 10, :sha)"
                " RETURNING id"
            ),
            {
                "user_id": user_id,
                "case_id": case_id,
                "key": f"request-temp/{case_id}/source.hwpx",
                "sha": "ab" * 32,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sims.uploaded_document ("
                "  inspection_case_id, file_asset_id, declared_format)"
                " VALUES (:case_id, :file_asset_id, 'HWPX')"
            ),
            {"case_id": case_id, "file_asset_id": file_asset_id},
        )
    return case_id, user_id


def _results() -> AnalysisResults:
    """가장 작은 정상 결과. LLM 을 타지 않는다."""
    return AnalysisResults(
        cpl=CplResult(
            items=[
                CplItem(
                    field_code=field_code,
                    representative_status="needs_confirmation",
                    status_reason=None,
                )
                for field_code in CplFieldCode
            ],
            profile_id="test-profile",
            common_ir_document_id=None,
            common_ir_source_sha256=None,
            candidate_pack_id=None,
            pipeline_version="test",
            structured_schema_version="test",
            model_id="test",
            prompt_version="test",
        ),
        program_name="디지털 전환 지원",
        original_filename="요청서.hwpx",
    )


def _bearer(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_a_pdf_written_by_the_queue_can_be_downloaded_through_the_edge_entrypoint(
    client: TestClient, engine: Engine
) -> None:
    case_id, _ = _case(engine)
    run_id = enqueue(engine, case_id)

    async def analyse(claimed_case_id: int) -> AnalysisResults:
        assert claimed_case_id == case_id
        return _results()

    # 워커가 쓰는 저장소는 앱이 읽는 저장소와 **같은 것**이어야 한다.
    assert (
        asyncio.run(
            run_once(
                engine,
                analyse,
                worker_id="delivery",
                require_analysis_results=True,
                object_storage=client.app.state.object_storage,
                pdf_renderer=ReportLabPdfRenderer(),
            )
        )
        == run_id
    )

    with engine.connect() as connection:
        case = (
            connection.execute(
                text(
                    "SELECT analysis_case_pk, report_status"
                    "  FROM result.analysis_case"
                    " WHERE source_analysis_run_id = :run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )
    assert case["report_status"] == "ready"
    analysis_case_id = case["analysis_case_pk"]

    auth = _bearer(client)
    issued = client.post(
        CREATE_URL, headers=auth, json={"analysis_case_id": str(analysis_case_id)}
    )
    assert issued.status_code == 200, issued.text

    downloaded = client.get(issued.json()["signed_url"])

    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"] == "application/pdf"
    # 워커가 실제로 그린 바이트가 그대로 내려온다.
    assert downloaded.content.startswith(b"%PDF-")
    with engine.connect() as connection:
        size = connection.scalar(
            text(
                "SELECT size_bytes FROM result.report_artifact"
                " WHERE analysis_case_pk = :pk"
            ),
            {"pk": analysis_case_id},
        )
    assert len(downloaded.content) == size


def test_the_result_rpc_and_the_download_entrypoint_agree(
    client: TestClient, engine: Engine
) -> None:
    """화면은 ``can_download`` 를 보고 버튼을 켠 뒤 발급으로 온다.

    두 조건이 어긋나면 켜진 버튼이 오류를 받는다. 큐가 만든 진짜 artifact 로
    확인한다 — 손으로 심은 행이 아니다.
    """
    with engine.connect() as connection:
        analysis_case_id: UUID = connection.scalar(
            text(
                "SELECT analysis_case_pk FROM result.analysis_case"
                " ORDER BY created_at DESC LIMIT 1"
            )
        )
        user_uuid = connection.scalar(
            text(
                "SELECT user_id FROM result.analysis_case"
                " WHERE analysis_case_pk = :pk"
            ),
            {"pk": analysis_case_id},
        )

    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('request.jwt.claim.sub', :sub, true)"),
            {"sub": str(user_uuid)},
        )
        can_download = connection.scalar(
            text(
                "SELECT (api.rpc_get_analysis_result(:pk) -> 'report'"
                " ->> 'can_download')::boolean"
            ),
            {"pk": analysis_case_id},
        )

    issued = client.post(
        CREATE_URL,
        headers=_bearer(client),
        json={"analysis_case_id": str(analysis_case_id)},
    )

    assert can_download is True
    assert issued.status_code == 200
