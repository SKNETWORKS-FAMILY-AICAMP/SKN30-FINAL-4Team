import asyncio
import hashlib
import io
import os
import tempfile
import threading
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from app.api.request_body_limit import RequestBodyLimitMiddleware
from app.core.config import Settings
from app.core.security import hash_password
from app.infrastructure.local_object_storage import LocalObjectStorage
from app.ports.object_storage import StoredObject
from app.services import case_upload
from app.services.case_upload import (
    InvalidUploadError,
    UnsupportedDocumentError,
    UploadTooLargeError,
    create_case_from_upload,
    validate_upload,
)
from main import app, create_app


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"


def hwpx_bytes(mimetype: bytes = b"application/hwp+zip") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        archive.writestr("Contents/section0.xml", "<section />")
    return output.getvalue()


def test_upload_openapi_describes_file_items_as_binary() -> None:
    schema = app.openapi()
    request_schema = schema["paths"]["/api/v1/cases"]["post"]["requestBody"][
        "content"
    ]["multipart/form-data"]["schema"]
    file_schema = schema["components"]["schemas"][
        request_schema["$ref"].rsplit("/", 1)[-1]
    ]["properties"]["file"]

    assert file_schema["type"] == "array"
    assert file_schema["items"] == {"type": "string", "format": "binary"}


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


@pytest.fixture(scope="module")
def storage_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("object-storage")


@pytest.fixture(scope="module")
def client(database_url: str, storage_root: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=storage_root,
    )
    with TestClient(create_app(settings)) as value:
        yield value


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


def test_hwpx_validation_uses_container_mimetype_not_client_mime() -> None:
    content = hwpx_bytes()
    result = validate_upload("request.HWPX", io.BytesIO(content))

    assert result.declared_format == "HWPX"
    assert result.extension == "hwpx"
    assert result.detected_mime_type == "application/hwp+zip"
    assert result.size_bytes == len(content)
    assert result.sha256_hex == hashlib.sha256(content).hexdigest()


def test_hwp_validation_checks_internal_file_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompoundFile:
        def __init__(self, _content: io.BytesIO) -> None:
            pass

        def __enter__(self) -> "FakeCompoundFile":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def exists(self, name: str) -> bool:
            return name == "FileHeader"

        def openstream(self, _name: str) -> io.BytesIO:
            return io.BytesIO(b"HWP Document File".ljust(32, b"\x00"))

    monkeypatch.setattr(case_upload.olefile, "OleFileIO", FakeCompoundFile)
    content = case_upload.HWP_COMPOUND_FILE_SIGNATURE + b"payload"

    result = validate_upload("request.hwp", io.BytesIO(content))

    assert result.declared_format == "HWP"
    assert result.detected_mime_type == "application/x-hwp"


def test_hwp_validation_rejects_wrong_internal_file_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompoundFile:
        def __init__(self, _content: io.BytesIO) -> None:
            pass

        def __enter__(self) -> "FakeCompoundFile":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def exists(self, _name: str) -> bool:
            return True

        def openstream(self, _name: str) -> io.BytesIO:
            return io.BytesIO(b"HWP Document File plus garbage!!!")

    monkeypatch.setattr(case_upload.olefile, "OleFileIO", FakeCompoundFile)
    content = case_upload.HWP_COMPOUND_FILE_SIGNATURE + b"payload"

    with pytest.raises(UnsupportedDocumentError):
        validate_upload("request.hwp", io.BytesIO(content))


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("request.pdf", b"%PDF-1.7"),
        ("request.hwpx", b"not-a-zip"),
        ("request.hwpx", hwpx_bytes(b"application/zip")),
        ("request.hwp", hwpx_bytes()),
    ],
)
def test_unsupported_or_disguised_documents_are_rejected(
    filename: str,
    content: bytes,
) -> None:
    with pytest.raises(UnsupportedDocumentError):
        validate_upload(filename, io.BytesIO(content))


def test_empty_upload_and_invalid_filename_are_rejected() -> None:
    with pytest.raises(InvalidUploadError):
        validate_upload("empty.hwp", io.BytesIO())
    with pytest.raises(InvalidUploadError):
        validate_upload("bad\x00name.hwp", io.BytesIO(b"content"))


def test_upload_limit_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        case_upload,
        "_detect_document_format",
        lambda _content: ("HWP", "application/x-hwp"),
    )
    with tempfile.TemporaryFile() as content:
        content.seek(case_upload.MAX_UPLOAD_BYTES - 1)
        content.write(b"x")
        assert validate_upload("limit.hwp", content).size_bytes == (
            case_upload.MAX_UPLOAD_BYTES
        )

        content.seek(case_upload.MAX_UPLOAD_BYTES)
        content.write(b"x")
        with pytest.raises(UploadTooLargeError):
            validate_upload("too-large.hwp", content)


def test_local_storage_rejects_escape_and_never_overwrites(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    for key in ("../escape", "..\\escape", "", "C:\\outside"):
        with pytest.raises(ValueError):
            asyncio.run(storage.put(key, io.BytesIO(b"bad")))

    stored = asyncio.run(storage.put("users/1/file", io.BytesIO(b"first")))
    assert stored == StoredObject(key="users/1/file", size_bytes=5)
    assert (tmp_path / "users/1/file").read_bytes() == b"first"
    with pytest.raises(FileExistsError):
        asyncio.run(storage.put("users/1/file", io.BytesIO(b"second")))
    assert (tmp_path / "users/1/file").read_bytes() == b"first"
    assert not list(tmp_path.rglob(".upload-*"))


def test_request_body_limit_counts_streamed_chunks() -> None:
    sent: list[dict[str, Any]] = []
    incoming = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]
    )

    async def receive() -> dict[str, Any]:
        return next(incoming)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def consume_body(
        _scope: dict[str, Any],
        body_receive: Callable[[], Any],
        _send: Callable[[dict[str, Any]], Any],
    ) -> None:
        while (await body_receive()).get("more_body"):
            pass

    middleware = RequestBodyLimitMiddleware(consume_body, max_bytes=6)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/cases",
        "headers": [],
    }
    asyncio.run(middleware(scope, receive, send))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_authenticated_upload_creates_owned_rows_and_unique_objects(
    client: TestClient,
    engine: Engine,
    storage_root: Path,
    create_user: Callable[[str], int],
) -> None:
    login_id = "upload-owner"
    user_id = create_user(login_id)
    headers = bearer_for(client, login_id)
    content = hwpx_bytes()

    responses = [
        client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("../../request.HWPX", content, "text/plain")},
        )
        for _ in range(2)
    ]

    assert [response.status_code for response in responses] == [200, 200]
    assert all(response.json()["status"] == "UPLOADED" for response in responses)
    case_ids = [response.json()["case_id"] for response in responses]
    assert len(set(case_ids)) == 2

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.id AS case_id,
                       c.owner_user_id,
                       c.status,
                       f.inspection_case_id AS file_case_id,
                       f.storage_key,
                       f.original_filename,
                       f.detected_mime_type,
                       f.extension,
                       f.size_bytes,
                       f.sha256_hex,
                       d.inspection_case_id AS document_case_id,
                       d.declared_format
                FROM sims.inspection_case c
                JOIN sims.file_asset f ON f.inspection_case_id = c.id
                JOIN sims.uploaded_document d ON d.file_asset_id = f.id
                WHERE c.id = ANY(:case_ids)
                ORDER BY c.id
                """
            ),
            {"case_ids": case_ids},
        ).mappings().all()

    assert len(rows) == 2
    assert len({row["storage_key"] for row in rows}) == 2
    for row in rows:
        assert row["owner_user_id"] == user_id
        assert row["status"] == "UPLOADED"
        assert row["case_id"] == row["file_case_id"] == row["document_case_id"]
        assert row["original_filename"] == "request.HWPX"
        assert row["detected_mime_type"] == "application/hwp+zip"
        assert row["extension"] == "hwpx"
        assert row["size_bytes"] == len(content)
        assert row["sha256_hex"] == hashlib.sha256(content).hexdigest()
        assert row["declared_format"] == "HWPX"
        assert (storage_root / row["storage_key"]).read_bytes() == content


def test_rejected_uploads_have_no_database_or_storage_side_effects(
    client: TestClient,
    engine: Engine,
    storage_root: Path,
    create_user: Callable[[str], int],
) -> None:
    login_id = "rejected-upload"
    user_id = create_user(login_id)
    headers = bearer_for(client, login_id)
    before_files = set(storage_root.rglob("*"))

    responses = [
        client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("empty.hwp", b"", "application/octet-stream")},
        ),
        client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("request.pdf", b"%PDF-1.7", "application/pdf")},
        ),
        client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("fake.hwpx", b"PK-not-zip", "application/zip")},
        ),
    ]

    assert [response.status_code for response in responses] == [400, 415, 415]
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_case WHERE owner_user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 0
    assert set(storage_root.rglob("*")) == before_files


def test_upload_requires_exactly_one_file(
    client: TestClient,
    engine: Engine,
    create_user: Callable[[str], int],
) -> None:
    login_id = "multiple-upload"
    user_id = create_user(login_id)
    headers = bearer_for(client, login_id)
    content = hwpx_bytes()

    missing = client.post("/api/v1/cases", headers=headers)
    multiple = client.post(
        "/api/v1/cases",
        headers=headers,
        files=[
            ("file", ("first.hwpx", content, "application/hwp+zip")),
            ("file", ("second.hwpx", content, "application/hwp+zip")),
        ],
    )

    assert missing.status_code == 422
    assert multiple.status_code == 400
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_case WHERE owner_user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 0


def test_api_maps_file_limit_to_413(
    client: TestClient,
    engine: Engine,
    create_user: Callable[[str], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_id = "too-large-upload"
    user_id = create_user(login_id)
    headers = bearer_for(client, login_id)
    monkeypatch.setattr(case_upload, "MAX_UPLOAD_BYTES", 8)

    response = client.post(
        "/api/v1/cases",
        headers=headers,
        files={"file": ("large.hwp", b"123456789", "application/octet-stream")},
    )

    assert response.status_code == 413
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_case WHERE owner_user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 0


def test_upload_requires_authentication(
    client: TestClient,
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        before = connection.scalar(text("SELECT count(*) FROM sims.inspection_case"))
    response = client.post(
        "/api/v1/cases",
        files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
    )

    assert response.status_code == 401
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM sims.inspection_case")) == before


class WrongSizeStorage(LocalObjectStorage):
    async def put(self, key: str, content: io.BytesIO) -> StoredObject:
        stored = await super().put(key, content)
        return StoredObject(key=stored.key, size_bytes=stored.size_bytes + 1)


def test_post_storage_failure_rolls_back_database_and_compensates_file(
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
) -> None:
    user_id = create_user("compensated-upload")
    storage = WrongSizeStorage(tmp_path)

    with pytest.raises(RuntimeError, match="Stored file size"):
        asyncio.run(
            create_case_from_upload(
                engine,
                storage,
                user_id,
                "request.hwpx",
                io.BytesIO(hwpx_bytes()),
            )
        )

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_case WHERE owner_user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 0
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_database_write_failure_rolls_back_and_compensates_file(
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = create_user("database-failure-upload")
    storage = LocalObjectStorage(tmp_path)
    content = hwpx_bytes()
    valid = validate_upload("request.hwpx", io.BytesIO(content))
    invalid = replace(valid, declared_format="DOCX")
    monkeypatch.setattr(case_upload, "validate_upload", lambda *_args: invalid)

    with pytest.raises(IntegrityError):
        asyncio.run(
            create_case_from_upload(
                engine,
                storage,
                user_id,
                "request.hwpx",
                io.BytesIO(content),
            )
        )

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_case WHERE owner_user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 0
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


class SlowStorage(LocalObjectStorage):
    def __init__(
        self,
        root: Path,
        started: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(root)
        self._started = started
        self._release = release

    def _put(self, key: str, content: io.BytesIO) -> StoredObject:
        self._started.set()
        if not self._release.wait(timeout=5):
            raise TimeoutError("Test did not release storage writer")
        return super()._put(key, content)


def test_cancelled_upload_waits_for_writer_then_compensates(
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
) -> None:
    user_id = create_user("cancelled-upload")
    started = threading.Event()
    release = threading.Event()
    storage = SlowStorage(tmp_path, started, release)

    async def cancel_during_write() -> None:
        task = asyncio.create_task(
            create_case_from_upload(
                engine,
                storage,
                user_id,
                "request.hwpx",
                io.BytesIO(hwpx_bytes()),
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_write())

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.inspection_case WHERE owner_user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 0
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
