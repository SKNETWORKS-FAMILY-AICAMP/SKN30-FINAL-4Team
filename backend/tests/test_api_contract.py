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


def test_offline_mode_defaults_to_false_when_unset(monkeypatch) -> None:
    """An unset PREREVIEW_OFFLINE_MODE must fail toward the safer boundary.

    Regression guard for the v0.2 fix: the previous default of "true" meant a
    misconfigured production deployment would silently accept the
    X-PreReview-Dev-User bypass header instead of requiring the Supabase
    Auth cookie.
    """

    monkeypatch.delenv("PREREVIEW_OFFLINE_MODE", raising=False)
    app = create_app()
    assert app.state.offline_mode is False


def test_cursor_signing_secret_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("PREREVIEW_CURSOR_SIGNING_SECRET", "local-test-secret")
    app = create_app()
    assert app.state.cursor_signing_secret == "local-test-secret"

    monkeypatch.delenv("PREREVIEW_CURSOR_SIGNING_SECRET", raising=False)
    assert create_app().state.cursor_signing_secret == ""


def test_dev_headers_are_rejected_unless_offline_mode_is_explicitly_enabled() -> None:
    app = create_app()
    app.state.offline_mode = False
    with TestClient(app) as api:
        response = api.get(
            "/api/v1/analysis-sessions/active",
            headers={"X-PreReview-Dev-User": "11111111-1111-1111-1111-111111111111"},
        )
        # The dev header is ignored (not honored as identity) once offline
        # mode is off; the cookie boundary applies and there is no cookie.
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"
