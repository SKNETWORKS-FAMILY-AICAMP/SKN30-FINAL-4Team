import asyncio
import hashlib
import io
import os
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings
from app.core.security import hash_password
from app.parsers import hwp_parser
from app.parsers.hwp_parser import DocumentParsingError, RhwpDocumentParser
from app.ports.document_parser import FileSource
from app.schemas.parsed_document import DocumentBlock, ParsedDocument
from app.services.document_parsing import start_analysis
from main import create_app


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"


def hwpx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "mimetype",
            b"application/hwp+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("Contents/section0.xml", "<section />")
    return output.getvalue()


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
def create_user(engine: Engine) -> Iterator[Callable[[str], int]]:
    user_ids: list[int] = []

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
        user_ids.append(user_id)
        return user_id

    yield create

    with engine.begin() as connection:
        for user_id in user_ids:
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


def bearer_for(client: TestClient, login_id: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"login_id": login_id, "password": PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def wait_for_internal_status(
    engine: Engine,
    case_id: int,
    expected_status: str,
) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            current_status = connection.scalar(
                text("SELECT status FROM sims.inspection_case WHERE id = :case_id"),
                {"case_id": case_id},
            )
        if current_status == expected_status:
            return
        time.sleep(0.01)
    pytest.fail(f"Analysis did not reach {expected_status} status")


class FakeParser:
    name = "fake-parser"
    version = "1.0"

    def __init__(self, *, fail: bool = False, partial: bool = False) -> None:
        self.fail = fail
        self.partial = partial

    def supports(self, mime_type: str, extension: str) -> bool:
        return mime_type == "application/hwp+zip" and extension == "hwpx"

    async def parse(self, source: FileSource) -> ParsedDocument:
        assert source.content.read(2) == b"PK"
        if self.fail:
            raise DocumentParsingError("sensitive provider details")
        return ParsedDocument(
            parser_name=self.name,
            parser_version=self.version,
            text="요청서 본문",
            blocks=[
                DocumentBlock(
                    block_id="body:0",
                    block_type="paragraph",
                    text="요청서 본문",
                    section_path=["section:0"],
                    source_locator={"section_index": 0, "paragraph_index": 0},
                )
            ],
            warnings=["one block skipped"] if self.partial else [],
            partial=self.partial,
        )


class CancelledDispatcher:
    async def enqueue_analysis(self, _case_id: int) -> str:
        raise asyncio.CancelledError


def test_rhwp_parser_maps_paragraph_table_and_merged_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hwp_parser, "_preflight_source", lambda *_args: None)
    provenance = SimpleNamespace(
        section_idx=0,
        para_idx=3,
        char_start=None,
        char_end=None,
        page_range=None,
    )
    paragraph_class = type("ParagraphBlock", (), {})
    paragraph = paragraph_class()
    paragraph.text = "첫 문단"
    paragraph.prov = provenance
    child = SimpleNamespace(text="병합 셀")
    cell = SimpleNamespace(
        row=0,
        col=0,
        row_span=1,
        col_span=2,
        grid_index=0,
        role="header",
        blocks=[child],
    )
    table_class = type("TableBlock", (), {})
    table = table_class()
    table.text = "병합 셀"
    table.rows = 1
    table.cols = 2
    table.cells = [cell]
    table.prov = provenance
    picture_class = type("PictureBlock", (), {})
    picture = picture_class()
    picture.prov = provenance
    fake_document = SimpleNamespace(
        extract_text=lambda: "첫 문단\n병합 셀",
        to_ir=lambda: SimpleNamespace(body=[paragraph, table, picture]),
    )
    monkeypatch.setattr(
        hwp_parser.rhwp.Document,
        "from_bytes",
        lambda *_args, **_kwargs: fake_document,
    )

    parser = RhwpDocumentParser()
    result = asyncio.run(
        parser.parse(
            FileSource(
                content=io.BytesIO(b"hwp"),
                filename="request.hwp",
                mime_type="application/x-hwp",
                extension="hwp",
            )
        )
    )

    assert [block.block_type for block in result.blocks] == ["paragraph", "table"]
    assert result.blocks[1].source_locator["cells"] == [
        {
            "row": 0,
            "col": 0,
            "row_span": 1,
            "col_span": 2,
            "grid_index": 0,
            "role": "header",
            "text": "병합 셀",
        }
    ]
    assert result.blocks[0].page_no is None
    assert result.partial is True
    assert result.warnings == ["Unsupported PictureBlock skipped at body:2"]


def test_rhwp_parser_supports_only_validated_hwp_and_hwpx_contract() -> None:
    parser = RhwpDocumentParser()

    assert parser.supports("application/x-hwp", ".HWP")
    assert parser.supports("application/hwp+zip", "hwpx")
    assert not parser.supports("application/pdf", "pdf")
    with pytest.raises(DocumentParsingError):
        asyncio.run(
            parser.parse(
                FileSource(
                    content=io.BytesIO(b"pdf"),
                    filename="request.pdf",
                    mime_type="application/pdf",
                    extension="pdf",
                )
            )
        )


def test_hwpx_preflight_rejects_unsafe_or_excessively_compressed_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = RhwpDocumentParser()
    monkeypatch.setattr(hwp_parser, "MAX_HWPX_COMPRESSION_RATIO", 2)

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.xml", "safe")
    compressed = io.BytesIO()
    with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Contents/section0.xml", "x" * 10_000)

    for content in (unsafe.getvalue(), compressed.getvalue()):
        with pytest.raises(DocumentParsingError):
            asyncio.run(
                parser.parse(
                    FileSource(
                        content=io.BytesIO(content),
                        filename="request.hwpx",
                        mime_type="application/hwp+zip",
                        extension="hwpx",
                    )
                )
            )


@pytest.mark.parametrize("partial", [False, True])
def test_analysis_persists_parse_snapshot_and_moves_to_checking(
    database_url: str,
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
    partial: bool,
) -> None:
    login_id = f"parse-success-{partial}"
    user_id = create_user(login_id)
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path,
    )
    with TestClient(
        create_app(settings, FakeParser(partial=partial), None)
    ) as client:
        headers = bearer_for(client, login_id)
        upload = client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
        )
        case_id = upload.json()["case_id"]

        started = client.post(f"/api/v1/cases/{case_id}/analyze", headers=headers)
        assert started.status_code == 202
        assert started.json()["case_id"] == case_id
        assert started.json()["status"] == "PARSING"
        assert started.json()["job_id"]
        wait_for_internal_status(engine, case_id, "CHECKING")
        public_status = client.get(
            f"/api/v1/cases/{case_id}/status", headers=headers
        )
        assert public_status.json()["status"] == "분석 중"

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT r.attempt_no, r.parser_name, r.parser_version, r.status,
                       r.used_ocr, r.extracted_text, r.structured_content,
                       r.text_sha256_hex, r.started_at, r.finished_at,
                       c.owner_user_id
                FROM sims.document_parse_run r
                JOIN sims.uploaded_document d ON d.file_asset_id = r.file_asset_id
                JOIN sims.inspection_case c ON c.id = d.inspection_case_id
                WHERE c.id = :case_id
                """
            ),
            {"case_id": case_id},
        ).mappings().one()

    assert row["owner_user_id"] == user_id
    assert row["attempt_no"] == 1
    assert row["parser_name"] == "fake-parser"
    assert row["parser_version"] == "1.0"
    assert row["status"] == ("PARTIAL_SUCCESS" if partial else "SUCCESS")
    assert row["used_ocr"] is False
    assert row["extracted_text"] == "요청서 본문"
    assert row["text_sha256_hex"] == hashlib.sha256(
        "요청서 본문".encode("utf-8")
    ).hexdigest()
    assert row["structured_content"]["partial"] is partial
    assert row["structured_content"]["blocks"][0]["block_id"] == "body:0"
    assert row["started_at"] is not None
    assert row["finished_at"] is not None


def test_parser_failure_is_safe_and_terminal(
    database_url: str,
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
) -> None:
    login_id = "parse-failure"
    create_user(login_id)
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path,
    )
    with TestClient(create_app(settings, FakeParser(fail=True), None)) as client:
        headers = bearer_for(client, login_id)
        upload = client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
        )
        case_id = upload.json()["case_id"]
        assert client.post(
            f"/api/v1/cases/{case_id}/analyze", headers=headers
        ).status_code == 202
        wait_for_internal_status(engine, case_id, "FAILED")
        result = client.get(
            f"/api/v1/cases/{case_id}/status", headers=headers
        ).json()

    assert result == {
        "case_id": case_id,
        "status": "분석 실패",
        "failure_code": "DOCUMENT_PARSE_FAILED",
        "failure_message": "The uploaded document could not be parsed",
    }
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT r.status, r.error_code, r.error_message, r.finished_at
                FROM sims.document_parse_run r
                JOIN sims.uploaded_document d ON d.file_asset_id = r.file_asset_id
                WHERE d.inspection_case_id = :case_id
                """
            ),
            {"case_id": case_id},
        ).mappings().one()
        cpl_count = connection.scalar(
            text(
                "SELECT count(*) FROM sims.request_extraction "
                "WHERE inspection_case_id = :case_id"
            ),
            {"case_id": case_id},
        )
    assert row["status"] == "FAILED"
    assert row["error_code"] == "DOCUMENT_PARSE_FAILED"
    assert row["error_message"] == "The uploaded document could not be parsed"
    assert "sensitive" not in row["error_message"]
    assert row["finished_at"] is not None
    assert cpl_count == 0


def test_changed_stored_source_is_rejected_before_parser(
    database_url: str,
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
) -> None:
    login_id = "parse-tampered-source"
    create_user(login_id)
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path,
    )
    with TestClient(create_app(settings, FakeParser(), None)) as client:
        headers = bearer_for(client, login_id)
        upload = client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
        )
        case_id = upload.json()["case_id"]
        with engine.connect() as connection:
            storage_key = connection.scalar(
                text(
                    "SELECT storage_key FROM sims.file_asset "
                    "WHERE inspection_case_id = :case_id"
                ),
                {"case_id": case_id},
            )
        assert storage_key is not None
        (tmp_path / storage_key).write_bytes(b"changed after upload")

        assert client.post(
            f"/api/v1/cases/{case_id}/analyze", headers=headers
        ).status_code == 202
        wait_for_internal_status(engine, case_id, "FAILED")

    with engine.connect() as connection:
        parse_status = connection.scalar(
            text(
                """
                SELECT r.status
                FROM sims.document_parse_run r
                JOIN sims.uploaded_document d ON d.file_asset_id = r.file_asset_id
                WHERE d.inspection_case_id = :case_id
                """
            ),
            {"case_id": case_id},
        )
    assert parse_status == "FAILED"


def test_cancelled_enqueue_does_not_leave_case_parsing(
    database_url: str,
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
) -> None:
    login_id = "parse-cancelled-enqueue"
    user_id = create_user(login_id)
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path,
    )
    with TestClient(create_app(settings, FakeParser(), None)) as client:
        headers = bearer_for(client, login_id)
        upload = client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
        )
        case_id = upload.json()["case_id"]

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                start_analysis(
                    engine,
                    CancelledDispatcher(),
                    FakeParser(),
                    user_id,
                    case_id,
                )
            )

    with engine.connect() as connection:
        statuses = connection.execute(
            text(
                """
                SELECT c.status AS case_status, r.status AS parse_status
                FROM sims.inspection_case c
                JOIN sims.uploaded_document d ON d.inspection_case_id = c.id
                JOIN sims.document_parse_run r ON r.file_asset_id = d.file_asset_id
                WHERE c.id = :case_id
                """
            ),
            {"case_id": case_id},
        ).mappings().one()
    assert statuses == {"case_status": "FAILED", "parse_status": "FAILED"}


def test_analysis_is_owner_scoped_and_cannot_start_twice(
    database_url: str,
    tmp_path: Path,
    create_user: Callable[[str], int],
) -> None:
    owner = "parse-owner"
    stranger = "parse-stranger"
    create_user(owner)
    create_user(stranger)
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path,
    )
    with TestClient(create_app(settings, FakeParser(), None)) as client:
        owner_headers = bearer_for(client, owner)
        stranger_headers = bearer_for(client, stranger)
        upload = client.post(
            "/api/v1/cases",
            headers=owner_headers,
            files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
        )
        case_id = upload.json()["case_id"]

        assert client.post(
            f"/api/v1/cases/{case_id}/analyze", headers=stranger_headers
        ).status_code == 404
        assert client.get(
            f"/api/v1/cases/{case_id}/status", headers=stranger_headers
        ).status_code == 404
        assert client.post(
            f"/api/v1/cases/{case_id}/analyze", headers=owner_headers
        ).status_code == 202
        assert client.post(
            f"/api/v1/cases/{case_id}/analyze", headers=owner_headers
        ).status_code == 409
