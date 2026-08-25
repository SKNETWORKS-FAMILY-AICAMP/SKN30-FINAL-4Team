import os

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from main import create_app


UNREACHABLE_DATABASE_URL = (
    "postgresql+psycopg://postgres:test@127.0.0.1:1/sims"
)


def test_live_works_while_database_is_unreachable() -> None:
    with TestClient(create_app(UNREACHABLE_DATABASE_URL)) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_is_unavailable_without_leaking_database_details() -> None:
    with TestClient(create_app(UNREACHABLE_DATABASE_URL)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "postgres" not in response.text
    assert "127.0.0.1" not in response.text


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        "sqlite:///test.db",
        "postgresql://postgres:test@127.0.0.1:5432/sims",
        "postgresql+asyncpg://postgres:test@127.0.0.1:5432/sims",
    ],
)
def test_invalid_database_configuration_is_rejected(database_url: str) -> None:
    with pytest.raises(ValidationError):
        create_app(database_url)


@pytest.mark.parametrize("connect_timeout_seconds", [0, -1])
def test_invalid_database_connect_timeout_is_rejected(
    connect_timeout_seconds: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url=UNREACHABLE_DATABASE_URL,
            database_connect_timeout_seconds=connect_timeout_seconds,
        )


def test_unknown_route_uses_fastapi_default_error() -> None:
    with TestClient(create_app(UNREACHABLE_DATABASE_URL)) as client:
        response = client.get("/not-found")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_ready_connects_to_real_postgresql() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL integration")

    with TestClient(create_app(database_url)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
