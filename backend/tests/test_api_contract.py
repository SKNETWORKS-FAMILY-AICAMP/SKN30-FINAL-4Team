"""Small checks for the current FastAPI surface and retired scaffold."""

from fastapi.testclient import TestClient

from main import create_app


def client() -> TestClient:
    return TestClient(create_app())


def test_ready_is_not_ready_for_noop_services() -> None:
    # Starlette 1.x requires TestClient lifespan ownership to be explicit.
    with client() as api:
        response = api.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "NOT_READY"


def test_ready_requires_a_browser_origin_in_online_mode(monkeypatch) -> None:
    monkeypatch.setenv("PREREVIEW_OFFLINE_MODE", "false")
    monkeypatch.setenv("SUPABASE_URL", "http://supabase.example.test")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "test-anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://test:test@database.example.test:5432/test",
    )
    monkeypatch.delenv("PREREVIEW_AUTH_ALLOWED_ORIGINS", raising=False)

    with TestClient(create_app()) as api:
        assert api.get("/health/ready").status_code == 503

    monkeypatch.setenv(
        "PREREVIEW_AUTH_ALLOWED_ORIGINS",
        "https://frontend.example.test",
    )
    with TestClient(create_app()) as api:
        response = api.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}


def test_retired_in_memory_request_routes_are_not_mounted() -> None:
    with TestClient(create_app()) as api:
        assert api.get("/api/v1/requests").status_code == 404
        assert api.get("/api/v1/cases").status_code == 404
        assert api.get("/api/v1/admin/existing/ingestions").status_code == 404
