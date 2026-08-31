from collections.abc import Iterator

import pytest
from httpx import Response
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.upload_limits import MAX_MULTIPART_BODY_BYTES
from main import create_app


DATABASE_URL = "postgresql+psycopg://test:test@127.0.0.1:1/sims"
JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
DEFAULT_ORIGIN = "http://localhost:3000"
LOOPBACK_ORIGIN = "http://127.0.0.1:3000"
# Vite 기본 포트도 기본 허용 목록에 있다.
VITE_ORIGIN = "http://localhost:5173"
VITE_LOOPBACK_ORIGIN = "http://127.0.0.1:5173"
CUSTOM_ORIGIN = "https://frontend.example.test"


def settings_for(
    monkeypatch: pytest.MonkeyPatch,
    **overrides: object,
) -> Settings:
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    values: dict[str, object] = {
        "bizinfo_api_key": None,
        "openai_api_key": None,
    }
    values.update(overrides)
    return Settings(
        _env_file=None,
        database_url=DATABASE_URL,
        jwt_secret=JWT_SECRET,
        **values,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with TestClient(create_app(settings_for(monkeypatch))) as value:
        yield value


def assert_allowed_origin(response: Response, origin: str = DEFAULT_ORIGIN) -> None:
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["vary"] == "Origin"


def test_settings_default_and_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_for(monkeypatch)
    assert settings.cors_allowed_origins == [
        DEFAULT_ORIGIN,
        LOOPBACK_ORIGIN,
        VITE_ORIGIN,
        VITE_LOOPBACK_ORIGIN,
    ]

    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["https://env.example.test"]')
    overridden = Settings(
        _env_file=None,
        database_url=DATABASE_URL,
        jwt_secret=JWT_SECRET,
    )
    assert overridden.cors_allowed_origins == ["https://env.example.test"]


def test_empty_origin_setting_disables_cors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_for(monkeypatch, cors_allowed_origins=[])
    with TestClient(create_app(settings)) as value:
        response = value.get("/health/live", headers={"Origin": DEFAULT_ORIGIN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "",
        "ftp://localhost:3000",
        "http://localhost:3000/",
        "https://user:password@example.test",
        "https://example.test/path",
        "https://example.test?query=1",
        "https://example.test#fragment",
        "https://example.test\x00",
    ],
)
def test_invalid_origin_setting_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    with pytest.raises(ValidationError):
        settings_for(monkeypatch, cors_allowed_origins=[origin])


@pytest.mark.parametrize("origin", [DEFAULT_ORIGIN, LOOPBACK_ORIGIN])
def test_allowed_origin_get_response_exposes_content_disposition(
    client: TestClient,
    origin: str,
) -> None:
    response = client.get("/health/live", headers={"Origin": origin})

    assert response.status_code == 200
    assert_allowed_origin(response, origin)
    assert response.headers["access-control-expose-headers"] == "Content-Disposition, Retry-After"
    assert "access-control-allow-credentials" not in response.headers


def test_custom_origin_replaces_default_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_for(monkeypatch, cors_allowed_origins=[CUSTOM_ORIGIN])
    with TestClient(create_app(settings)) as value:
        custom = value.get("/health/live", headers={"Origin": CUSTOM_ORIGIN})
        default = value.get("/health/live", headers={"Origin": DEFAULT_ORIGIN})

    assert_allowed_origin(custom, CUSTOM_ORIGIN)
    assert "access-control-allow-origin" not in default.headers


@pytest.mark.parametrize("path", ["/api/v1/auth/login", "/api/v1/cases"])
@pytest.mark.parametrize("origin", [DEFAULT_ORIGIN, LOOPBACK_ORIGIN])
def test_allowed_preflight_for_login_and_upload(
    client: TestClient,
    path: str,
    origin: str,
) -> None:
    response = client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )

    assert response.status_code == 200
    assert_allowed_origin(response, origin)
    assert response.headers["access-control-allow-methods"] == "GET, POST"
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "content-type" in allowed_headers
    assert "access-control-allow-credentials" not in response.headers


def test_allowed_origin_bearer_request_gets_existing_401(client: TestClient) -> None:
    response = client.get(
        "/api/v1/cases",
        headers={"Origin": DEFAULT_ORIGIN, "Authorization": "Bearer invalid"},
    )

    assert response.status_code == 401
    assert_allowed_origin(response)


def test_allowed_origin_gets_cors_on_existing_404(client: TestClient) -> None:
    response = client.get("/not-found", headers={"Origin": DEFAULT_ORIGIN})

    assert response.status_code == 404
    assert_allowed_origin(response)


def test_allowed_origin_gets_cors_on_existing_422(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": DEFAULT_ORIGIN},
        json={},
    )

    assert response.status_code == 422
    assert_allowed_origin(response)


def test_allowed_origin_gets_cors_on_request_body_limit_413(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/cases",
        headers={
            "Origin": DEFAULT_ORIGIN,
            "Content-Length": str(MAX_MULTIPART_BODY_BYTES + 1),
        },
        content=b"",
    )

    assert response.status_code == 413
    assert_allowed_origin(response)


def test_disallowed_origin_is_not_reflected_and_request_still_runs(
    client: TestClient,
) -> None:
    response = client.get("/health/live", headers={"Origin": CUSTOM_ORIGIN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_disallowed_preflight_is_rejected(client: TestClient) -> None:
    response = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": CUSTOM_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_disallowed_method_preflight_is_rejected(client: TestClient) -> None:
    response = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": DEFAULT_ORIGIN,
            "Access-Control-Request-Method": "PUT",
        },
    )

    assert response.status_code == 400


def test_disallowed_header_preflight_is_rejected(client: TestClient) -> None:
    response = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": DEFAULT_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-not-allowed",
        },
    )

    assert response.status_code == 400


def test_originless_request_keeps_existing_behavior(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
