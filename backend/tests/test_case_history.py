import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings
from app.core.security import hash_password
from main import create_app


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"


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
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, jwt_secret=JWT_SECRET)
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

    # app_user 삭제는 v2.1 DDL의 ON DELETE CASCADE로 case·file_asset까지 정리한다.
    with engine.begin() as connection:
        for user_id in user_ids:
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


def bearer_for(client: TestClient, login_id: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": f"{login_id}@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_case(
    engine: Engine,
    owner_user_id: int,
    *,
    status: str,
    created_at: datetime,
    filename: str | None,
) -> int:
    """Insert one inspection_case, optionally with its uploaded document."""
    completed = status == "COMPLETED"
    with engine.begin() as connection:
        case_id = connection.scalar(
            text(
                """
                INSERT INTO sims.inspection_case (
                    owner_user_id, status, created_at,
                    completed_at, result_frozen_at
                )
                VALUES (
                    :owner_user_id, :status, :created_at,
                    :completed_at, :completed_at
                )
                RETURNING id
                """
            ),
            {
                "owner_user_id": owner_user_id,
                "status": status,
                "created_at": created_at,
                "completed_at": created_at if completed else None,
            },
        )
        assert case_id is not None
        if filename is None:
            return case_id

        file_asset_id = connection.scalar(
            text(
                """
                INSERT INTO sims.file_asset (
                    asset_scope, owner_user_id, inspection_case_id,
                    storage_key, original_filename, extension
                )
                VALUES (
                    'USER', :owner_user_id, :case_id,
                    :storage_key, :original_filename, 'hwpx'
                )
                RETURNING id
                """
            ),
            {
                "owner_user_id": owner_user_id,
                "case_id": case_id,
                # storage_key 재사용은 DDL 트리거가 막으므로 실행마다 새 값을 쓴다.
                "storage_key": f"tests/history/{uuid.uuid4().hex}.hwpx",
                "original_filename": filename,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO sims.uploaded_document (
                    inspection_case_id, file_asset_id, declared_format
                )
                VALUES (:case_id, :file_asset_id, 'HWPX')
                """
            ),
            {"case_id": case_id, "file_asset_id": file_asset_id},
        )
    return case_id


def history(
    client: TestClient,
    headers: dict[str, str],
    **params: object,
) -> dict:
    response = client.get("/api/v1/cases", headers=headers, params=params)
    assert response.status_code == 200
    return response.json()


def items(client: TestClient, headers: dict[str, str], **params: object) -> list[dict]:
    return history(client, headers, **params)["items"]


def test_history_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/cases").status_code == 401
    assert (
        client.get(
            "/api/v1/cases",
            headers={"Authorization": "Bearer not-a-token"},
        ).status_code
        == 401
    )


def test_history_is_empty_for_a_user_without_analyses(
    client: TestClient,
    create_user: Callable[[str], int],
) -> None:
    login_id = f"history-empty-{uuid.uuid4().hex[:8]}"
    create_user(login_id)
    assert items(client, bearer_for(client, login_id)) == []


def test_history_is_owner_scoped(
    client: TestClient,
    engine: Engine,
    create_user: Callable[[str], int],
) -> None:
    marker = uuid.uuid4().hex[:8]
    owner_login = f"history-owner-{marker}"
    other_login = f"history-other-{marker}"
    owner_id = create_user(owner_login)
    other_id = create_user(other_login)
    now = datetime.now(timezone.utc)

    mine = make_case(
        engine, owner_id, status="COMPLETED", created_at=now, filename="mine.hwpx"
    )
    theirs = make_case(
        engine, other_id, status="COMPLETED", created_at=now, filename="theirs.hwpx"
    )

    owner_cases = items(client, bearer_for(client, owner_login))
    assert [case["case_id"] for case in owner_cases] == [mine]

    other_cases = items(client, bearer_for(client, other_login))
    assert [case["case_id"] for case in other_cases] == [theirs]


def test_history_is_newest_first_and_uses_filename_as_title(
    client: TestClient,
    engine: Engine,
    create_user: Callable[[str], int],
) -> None:
    login_id = f"history-order-{uuid.uuid4().hex[:8]}"
    user_id = create_user(login_id)
    now = datetime.now(timezone.utc)

    oldest = make_case(
        engine,
        user_id,
        status="COMPLETED",
        created_at=now - timedelta(days=2),
        filename="oldest.hwpx",
    )
    middle = make_case(
        engine,
        user_id,
        status="COMPLETED",
        created_at=now - timedelta(days=1),
        filename="middle.hwpx",
    )
    newest = make_case(
        engine,
        user_id,
        status="COMPLETED",
        created_at=now,
        filename="newest.hwpx",
    )

    cases = items(client, bearer_for(client, login_id))
    assert [case["case_id"] for case in cases] == [newest, middle, oldest]
    assert [case["title"] for case in cases] == [
        "newest.hwpx",
        "middle.hwpx",
        "oldest.hwpx",
    ]


def test_history_without_uploaded_document_has_no_title(
    client: TestClient,
    engine: Engine,
    create_user: Callable[[str], int],
) -> None:
    login_id = f"history-untitled-{uuid.uuid4().hex[:8]}"
    user_id = create_user(login_id)
    make_case(
        engine,
        user_id,
        status="COMPLETED",
        created_at=datetime.now(timezone.utc),
        filename=None,
    )

    cases = items(client, bearer_for(client, login_id))
    assert len(cases) == 1
    assert cases[0]["title"] is None


@pytest.mark.parametrize(
    "internal_status",
    ["UPLOADED", "PARSING", "CHECKING", "RETRIEVING", "REPORTING", "FAILED"],
)
def test_history_holds_only_completed_cases(
    client: TestClient,
    engine: Engine,
    create_user: Callable[[str], int],
    internal_status: str,
) -> None:
    """진행 중과 실패는 이력에 넣지 않는다.

    진행 중인 분석은 업로드한 브라우저가 case_id 로 복구하고, 실패는 업로드
    화면에서만 알린다. 그래서 이력 항목에는 상태 필드가 없다.
    """
    login_id = f"history-status-{uuid.uuid4().hex[:8]}"
    user_id = create_user(login_id)
    make_case(
        engine,
        user_id,
        status=internal_status,
        created_at=datetime.now(timezone.utc),
        filename="case.hwpx",
    )

    page = history(client, bearer_for(client, login_id))
    assert page["items"] == []
    assert page["next_cursor"] is None


def test_history_pages_by_cursor_without_gaps_or_repeats(
    client: TestClient,
    engine: Engine,
    create_user: Callable[[str], int],
) -> None:
    login_id = f"history-page-{uuid.uuid4().hex[:8]}"
    user_id = create_user(login_id)
    now = datetime.now(timezone.utc)
    expected = [
        make_case(
            engine,
            user_id,
            status="COMPLETED",
            created_at=now - timedelta(minutes=index),
            filename=f"case-{index}.hwpx",
        )
        for index in range(12)
    ]

    headers = bearer_for(client, login_id)
    collected: list[int] = []
    cursor: str | None = None
    for _ in range(3):
        page = history(client, headers, limit=5, **({"cursor": cursor} if cursor else {}))
        collected.extend(case["case_id"] for case in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert collected == expected
    assert cursor is None


def test_history_rejects_a_forged_cursor(
    client: TestClient,
    create_user: Callable[[str], int],
) -> None:
    login_id = f"history-cursor-{uuid.uuid4().hex[:8]}"
    create_user(login_id)
    response = client.get(
        "/api/v1/cases",
        headers=bearer_for(client, login_id),
        params={"cursor": "not-a-real-cursor"},
    )
    assert response.status_code == 400
