import asyncio
import hashlib
import io
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings
from app.core.security import hash_password
from app.infrastructure.local_object_storage import LocalObjectStorage
from app.infrastructure.reportlab_pdf_renderer import ReportLabPdfRenderer
from app.schemas.cpl import CPL_FIELDS, CplItem, CplOccurrence, CplResult, CplStatus
from app.schemas.report import REPORT_SCHEMA_VERSION, ReportEvidence
import app.services.reporting as reporting
from app.services.reporting import (
    ReportNotReadyError,
    compose_report,
    finalize_report,
)
from main import create_app


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"


class FakePdfRenderer:
    async def render(self, _report) -> bytes:
        return b"%PDF-1.4\n% fake report for persistence contract\n%%EOF\n"


class InvalidPdfRenderer:
    async def render(self, _report) -> bytes:
        return b"not-a-pdf"


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if value is None:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL integration")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    value = create_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def users(engine: Engine) -> Iterator[Callable[[str], int]]:
    ids: list[int] = []

    def create(login_id: str) -> int:
        with engine.begin() as connection:
            user_id = connection.scalar(
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
        assert user_id is not None
        ids.append(user_id)
        return user_id

    yield create
    with engine.begin() as connection:
        for user_id in ids:
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


def settings(database_url: str, storage_root: Path) -> Settings:
    return Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=storage_root,
    )


def cpl_result() -> CplResult:
    return CplResult(
        ruleset_version="cpl-alpha-v0.2",
        prompt_version="cpl-semantic-v0.2",
        model_profile="gpt-4o-mini",
        items=[
            CplItem(
                field_code=field,
                status=CplStatus.MISSING,
                reason_code="FIELD_NOT_FOUND",
                explanation="원문에서 항목을 확인하지 못했습니다.",
            )
            for field in CPL_FIELDS
        ],
    )


def seed_reporting_case(
    engine: Engine,
    owner_user_id: int,
) -> tuple[int, int, int]:
    marker = uuid.uuid4().hex
    with engine.begin() as connection:
        case_id = connection.scalar(
            text(
                """
                INSERT INTO sims.inspection_case (owner_user_id, status)
                VALUES (:owner_user_id, 'RETRIEVING') RETURNING id
                """
            ),
            {"owner_user_id": owner_user_id},
        )
        file_id = connection.scalar(
            text(
                """
                INSERT INTO sims.file_asset (
                    asset_scope, owner_user_id, inspection_case_id,
                    storage_key, original_filename, extension
                ) VALUES (
                    'USER', :owner_user_id, :case_id,
                    :storage_key, 'request.hwpx', 'hwpx'
                ) RETURNING id
                """
            ),
            {
                "owner_user_id": owner_user_id,
                "case_id": case_id,
                "storage_key": f"tests/report/{marker}.hwpx",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO sims.uploaded_document (
                    inspection_case_id, file_asset_id, declared_format
                ) VALUES (:case_id, :file_id, 'HWPX')
                """
            ),
            {"case_id": case_id, "file_id": file_id},
        )
        parse_id = connection.scalar(
            text(
                """
                INSERT INTO sims.document_parse_run (
                    file_asset_id, attempt_no, parser_name, parser_version,
                    status, finished_at
                ) VALUES (:file_id, 1, 'test', '1', 'SUCCESS', now())
                RETURNING id
                """
            ),
            {"file_id": file_id},
        )
        form_id = connection.scalar(
            text(
                "INSERT INTO sims.form_schema (schema_name, version_no) "
                "VALUES (:name, 1) RETURNING id"
            ),
            {"name": f"report-{marker}"},
        )
        extraction_id = connection.scalar(
            text(
                """
                INSERT INTO sims.request_extraction (
                    inspection_case_id, form_schema_id, parse_run_id,
                    status, extractor_name, extractor_version
                ) VALUES (:case_id, :form_id, :parse_id, 'SUCCESS', 'test', '1')
                RETURNING id
                """
            ),
            {"case_id": case_id, "form_id": form_id, "parse_id": parse_id},
        )
        missing_id = connection.scalar(
            text(
                """
                INSERT INTO sims.missing_check_run (
                    inspection_case_id, request_extraction_id,
                    ruleset_version, status
                ) VALUES (:case_id, :extraction_id, 'test', 'SUCCESS')
                RETURNING id
                """
            ),
            {"case_id": case_id, "extraction_id": extraction_id},
        )
        model_id = connection.scalar(
            text(
                """
                INSERT INTO sims.embedding_model (
                    provider, model_name, model_version, dimension
                ) VALUES ('test', :name, '1', 2) RETURNING id
                """
            ),
            {"name": marker},
        )
        profile_id = connection.scalar(
            text(
                """
                INSERT INTO sims.embedding_profile (
                    embedding_model_id, profile_name, version_no, profile_kind
                ) VALUES (:model_id, :name, 1, 'SUMMARY') RETURNING id
                """
            ),
            {"model_id": model_id, "name": f"report-{marker}"},
        )
        embedding_id = connection.scalar(
            text(
                """
                INSERT INTO sims.inspection_embedding (
                    inspection_case_id, embedding_profile_id, input_text,
                    input_sha256_hex, embedding
                ) VALUES (:case_id, :profile_id, 'test', :digest, '[0,0]')
                RETURNING id
                """
            ),
            {
                "case_id": case_id,
                "profile_id": profile_id,
                "digest": hashlib.sha256(b"test").hexdigest(),
            },
        )
        retrieval_id = connection.scalar(
            text(
                """
                INSERT INTO sims.retrieval_run (
                    inspection_case_id, inspection_embedding_id, status,
                    top_k_used, corpus_snapshot_at
                ) VALUES (:case_id, :embedding_id, 'SUCCESS', 5, now())
                RETURNING id
                """
            ),
            {"case_id": case_id, "embedding_id": embedding_id},
        )
    assert case_id and missing_id and retrieval_id
    return case_id, missing_id, retrieval_id


def bearer(client: TestClient, login_id: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": f"{login_id}@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_composer_keeps_v01_reserved_fields_and_dynamic_url_outside_snapshot(
    database_url: str,
    tmp_path: Path,
) -> None:
    report = compose_report(
        settings(database_url, tmp_path),
        case_id=1,
        title="request.hwpx",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        cpl_result=cpl_result(),
        fit_result=None,
        sim_results=[],
        expected_candidate_count=0,
    )

    snapshot = report.model_dump(mode="json")
    assert snapshot["schema_version"] == REPORT_SCHEMA_VERSION
    assert snapshot["ben_references"] == []
    assert snapshot["differences"] == []
    assert "report_download_url" not in snapshot
    assert snapshot["structural_consistency"]["module_status"] == "UNAVAILABLE"
    assert len(snapshot["self_check"]["items"]) == 13


def test_result_screen_projection_hides_internal_evidence_metadata(
    database_url: str,
    tmp_path: Path,
) -> None:
    result = cpl_result()
    result.items[0] = CplItem(
        field_code=CPL_FIELDS[0],
        status=CplStatus.NEEDS_CONFIRMATION,
        reason_code="REQUEST_TYPE_AMBIGUOUS",
        occurrences=[
            CplOccurrence(
                raw_text="□ 내역사업 신설",
                normalized_value={
                    "mark": "□",
                    "selected": False,
                    "request_reason": "SUBPROGRAM_NEW",
                },
                block_id="body:4",
                extraction_method="RULE",
            ),
            CplOccurrence(
                raw_text="□ 내내역사업 신설",
                normalized_value={
                    "mark": "□",
                    "selected": False,
                    "request_reason": "SUBSUBPROGRAM_NEW",
                },
                block_id="body:4",
                extraction_method="RULE",
            ),
            CplOccurrence(
                raw_text="□ 사업내용 변경",
                normalized_value={
                    "mark": "□",
                    "selected": False,
                    "request_reason": "CONTENT_CHANGE",
                },
                block_id="body:4",
                extraction_method="RULE",
            ),
        ],
    )
    result.items[1] = CplItem(
        field_code=CPL_FIELDS[1],
        status=CplStatus.PARSE_FAILED,
    )
    report = compose_report(
        settings(database_url, tmp_path),
        case_id=1,
        title="request.hwpx",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        cpl_result=result,
        fit_result=None,
        sim_results=[],
        expected_candidate_count=0,
    )

    detail = reporting._report_response(report).model_dump(
        mode="json",
        exclude_none=True,
    )
    response = detail["report"]

    assert set(detail) == {"case", "report"}
    assert set(detail["case"]) == {"case_id", "title", "completed_at"}
    assert "schema_version" not in response
    assert "warnings" not in response
    assert "PURPOSE_GOAL" not in {
        item["field_code"] for item in response["self_check"]["items"]
    }
    assert all("summary" not in issue for issue in response["review_issues"])
    item = response["self_check"]["items"][0]
    assert item == {
        "field_code": "REQUEST_TYPE",
        "status": "NEEDS_CONFIRMATION",
        "display": {
            "type": "checkbox_group",
            "summary": "선택된 요청 유형이 없습니다.",
            "options": [
                {"code": "SUBPROGRAM_NEW", "label": "내역사업 신설", "selected": False},
                {
                    "code": "SUBSUBPROGRAM_NEW",
                    "label": "내내역사업 신설",
                    "selected": False,
                },
                {"code": "CONTENT_CHANGE", "label": "사업내용 변경", "selected": False},
            ],
        },
        "evidence": [
            {
                "excerpt": "□ 내역사업 신설\n\n□ 내내역사업 신설\n\n□ 사업내용 변경"
            }
        ],
    }


def test_cpl_screen_evidence_keeps_full_source_not_its_fragments() -> None:
    evidence = [
        ReportEvidence.model_construct(excerpt="업력 3년 이상 10년 이내"),
        ReportEvidence.model_construct(excerpt="최근 3개년 연평균 매출액 10억원 이상 300억원 이하"),
        ReportEvidence.model_construct(
            excerpt=(
                "- 업력: 업력 3년 이상 10년 이내\n"
                "- 매출액: 최근 3개년 연평균 매출액 10억원 이상 300억원 이하"
            )
        ),
    ]

    display_evidence = reporting._display_evidence(evidence)

    assert [item.excerpt for item in display_evidence] == [
        "- 업력: 업력 3년 이상 10년 이내\n"
        "- 매출액: 최근 3개년 연평균 매출액 10억원 이상 300억원 이하"
    ]


def test_reportlab_renderer_produces_pdf_bytes(
    database_url: str,
    tmp_path: Path,
) -> None:
    report = compose_report(
        settings(database_url, tmp_path),
        case_id=1,
        title="한글 요청서.hwpx",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        cpl_result=cpl_result(),
        fit_result=None,
        sim_results=[],
        expected_candidate_count=0,
    )
    content = asyncio.run(ReportLabPdfRenderer().render(report))
    assert content.startswith(b"%PDF-")
    assert len(content) > 1_000


def test_finalize_persists_immutable_snapshot_pdf_and_owner_scoped_apis(
    database_url: str,
    engine: Engine,
    users: Callable[[str], int],
    tmp_path: Path,
) -> None:
    marker = uuid.uuid4().hex[:8]
    owner_login = f"report-owner-{marker}"
    other_login = f"report-other-{marker}"
    owner_id = users(owner_login)
    users(other_login)
    case_id, missing_id, retrieval_id = seed_reporting_case(engine, owner_id)
    runtime = settings(database_url, tmp_path)
    storage = LocalObjectStorage(tmp_path)

    report = asyncio.run(
        finalize_report(
            engine,
            storage,
            FakePdfRenderer(),
            runtime,
            case_id=case_id,
            missing_check_run_id=missing_id,
            retrieval_run_id=retrieval_id,
            cpl_result=cpl_result(),
            fit_result=None,
            sim_results=[],
            expected_candidate_count=0,
        )
    )
    assert report.schema_version == REPORT_SCHEMA_VERSION

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT c.status, c.completed_at, c.result_frozen_at,
                       r.report_json, f.storage_key, f.size_bytes,
                       a.template_version
                FROM sims.inspection_case c
                JOIN sims.inspection_report r ON r.inspection_case_id = c.id
                JOIN sims.output_artifact a ON a.inspection_report_id = r.id
                JOIN sims.file_asset f ON f.id = a.file_asset_id
                WHERE c.id = :case_id
                """
            ),
            {"case_id": case_id},
        ).mappings().one()
    assert row["status"] == "COMPLETED"
    assert row["completed_at"] == row["result_frozen_at"]
    assert "report_download_url" not in row["report_json"]
    assert row["template_version"] == "alpha-pdf-v0.1"
    assert (tmp_path / row["storage_key"]).stat().st_size == row["size_bytes"]

    with TestClient(create_app(runtime, pdf_renderer=FakePdfRenderer())) as client:
        owner_headers = bearer(client, owner_login)
        other_headers = bearer(client, other_login)
        result = client.get(f"/api/v1/cases/{case_id}", headers=owner_headers)
        assert result.status_code == 200
        # 보고서와 대화를 한 번에 준다. PDF 링크는 경로가 고정이라 담지 않는다.
        assert set(result.json()) == {"case", "report", "chat"}
        assert result.json()["case"]["case_id"] == case_id
        assert result.json()["chat"]["messages"] == []
        download = client.get(
            f"/api/v1/cases/{case_id}/report.pdf",
            headers=owner_headers,
        )
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/pdf"
        assert download.content.startswith(b"%PDF-")
        assert client.get(
            f"/api/v1/cases/{case_id}", headers=other_headers
        ).status_code == 404
        assert client.get(
            f"/api/v1/cases/{case_id}/report.pdf", headers=other_headers
        ).status_code == 404


def test_retrieval_failure_has_no_report(engine: Engine, users) -> None:
    user_id = users(f"report-failed-{uuid.uuid4().hex[:8]}")
    with engine.begin() as connection:
        case_id = connection.scalar(
            text(
                """
                INSERT INTO sims.inspection_case (
                    owner_user_id, status, failure_code, failure_message
                ) VALUES (
                    :owner_user_id, 'FAILED', 'RETRIEVAL_UNAVAILABLE', 'failed'
                ) RETURNING id
                """
            ),
            {"owner_user_id": user_id},
        )
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_report "
                "WHERE inspection_case_id = :case_id"
            ),
            {"case_id": case_id},
        ) == 0


def test_pdf_failure_marks_case_failed_without_freezing_report(
    database_url: str,
    engine: Engine,
    users: Callable[[str], int],
    tmp_path: Path,
) -> None:
    user_id = users(f"report-pdf-failed-{uuid.uuid4().hex[:8]}")
    case_id, missing_id, retrieval_id = seed_reporting_case(engine, user_id)

    with pytest.raises(ValueError, match="invalid content"):
        asyncio.run(
            finalize_report(
                engine,
                LocalObjectStorage(tmp_path),
                InvalidPdfRenderer(),
                settings(database_url, tmp_path),
                case_id=case_id,
                missing_check_run_id=missing_id,
                retrieval_run_id=retrieval_id,
                cpl_result=cpl_result(),
                fit_result=None,
                sim_results=[],
                expected_candidate_count=0,
            )
        )

    with engine.connect() as connection:
        case = connection.execute(
            text(
                "SELECT status, failure_code, completed_at, result_frozen_at "
                "FROM sims.inspection_case WHERE id = :case_id"
            ),
            {"case_id": case_id},
        ).mappings().one()
        report_count = connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_report "
                "WHERE inspection_case_id = :case_id"
            ),
            {"case_id": case_id},
        )
    assert case["status"] == "FAILED"
    assert case["failure_code"] == "REPORT_GENERATION_FAILED"
    assert case["completed_at"] is None
    assert case["result_frozen_at"] is None
    assert report_count == 0


def test_composer_failure_marks_case_failed(
    database_url: str,
    engine: Engine,
    users: Callable[[str], int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = users(f"report-compose-failed-{uuid.uuid4().hex[:8]}")
    case_id, missing_id, retrieval_id = seed_reporting_case(engine, user_id)

    def fail_composition(*_args, **_kwargs):
        raise ValueError("invalid report source data")

    monkeypatch.setattr(reporting, "compose_report", fail_composition)
    with pytest.raises(ValueError, match="invalid report source data"):
        asyncio.run(
            finalize_report(
                engine,
                LocalObjectStorage(tmp_path),
                FakePdfRenderer(),
                settings(database_url, tmp_path),
                case_id=case_id,
                missing_check_run_id=missing_id,
                retrieval_run_id=retrieval_id,
                cpl_result=cpl_result(),
                fit_result=None,
                sim_results=[],
                expected_candidate_count=0,
            )
        )

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, failure_code FROM sims.inspection_case "
                "WHERE id = :case_id"
            ),
            {"case_id": case_id},
        ).mappings().one()
    assert row["status"] == "FAILED"
    assert row["failure_code"] == "REPORT_GENERATION_FAILED"


def test_non_successful_cpl_run_cannot_be_frozen_into_report(
    database_url: str,
    engine: Engine,
    users: Callable[[str], int],
    tmp_path: Path,
) -> None:
    user_id = users(f"report-cpl-failed-{uuid.uuid4().hex[:8]}")
    case_id, missing_id, retrieval_id = seed_reporting_case(engine, user_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sims.missing_check_run SET status = 'FAILED' "
                "WHERE id = :missing_id"
            ),
            {"missing_id": missing_id},
        )

    with pytest.raises(ReportNotReadyError, match="source runs are inconsistent"):
        asyncio.run(
            finalize_report(
                engine,
                LocalObjectStorage(tmp_path),
                FakePdfRenderer(),
                settings(database_url, tmp_path),
                case_id=case_id,
                missing_check_run_id=missing_id,
                retrieval_run_id=retrieval_id,
                cpl_result=cpl_result(),
                fit_result=None,
                sim_results=[],
                expected_candidate_count=0,
            )
        )

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, failure_code FROM sims.inspection_case "
                "WHERE id = :case_id"
            ),
            {"case_id": case_id},
        ).mappings().one()
    assert row["status"] == "FAILED"
    assert row["failure_code"] == "REPORT_GENERATION_FAILED"
