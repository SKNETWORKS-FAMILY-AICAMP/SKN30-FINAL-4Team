"""Small checks for the current FastAPI surface and retired scaffold."""

from fastapi.testclient import TestClient
import pytest

import main as main_module
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
    monkeypatch.setenv("PREREVIEW_CURSOR_SIGNING_SECRET", "test-cursor-secret")
    with TestClient(create_app()) as api:
        response = api.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "build_id": None}


def test_ready_rejects_an_empty_cursor_signing_secret(monkeypatch) -> None:
    monkeypatch.setenv("PREREVIEW_OFFLINE_MODE", "false")
    monkeypatch.setenv("SUPABASE_URL", "http://supabase.example.test")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "test-anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://test:test@database.example.test:5432/test",
    )
    monkeypatch.setenv(
        "PREREVIEW_AUTH_ALLOWED_ORIGINS", "https://frontend.example.test"
    )
    monkeypatch.setenv("PREREVIEW_CURSOR_SIGNING_SECRET", "   ")

    with TestClient(create_app()) as api:
        assert api.get("/health/ready").status_code == 503


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


def test_host_development_does_not_accept_runtime_build_identity(monkeypatch) -> None:
    monkeypatch.setenv("PREREVIEW_BUILD_ID", "a" * 64)

    assert create_app().state.build_id is None
    assert main_module._health_headers(None) == {}


def test_baked_build_identity_cannot_be_overridden_at_runtime(
    tmp_path, monkeypatch
) -> None:
    baked_identity = "b" * 64
    baked_file = tmp_path / ".prereview-build-id"
    baked_file.write_text(f"{baked_identity}\n", encoding="ascii")
    monkeypatch.setattr(main_module, "_BUILD_ID_FILE", baked_file)
    monkeypatch.setenv("PREREVIEW_BUILD_ID", "a" * 64)

    assert create_app().state.build_id == baked_identity


def test_invalid_baked_build_identity_fails_without_env_fallback(
    tmp_path, monkeypatch
) -> None:
    baked_file = tmp_path / ".prereview-build-id"
    baked_file.write_text("not-a-sha\n", encoding="ascii")
    monkeypatch.setattr(main_module, "_BUILD_ID_FILE", baked_file)
    monkeypatch.setenv("PREREVIEW_BUILD_ID", "a" * 64)

    with pytest.raises(RuntimeError, match="Baked API build identity"):
        create_app()


@pytest.mark.parametrize("value", ["0", "33", "not-an-integer"])
def test_upload_concurrency_rejects_unsafe_configuration(monkeypatch, value) -> None:
    monkeypatch.setenv("PREREVIEW_UPLOAD_CONCURRENCY", value)
    with pytest.raises(RuntimeError, match="PREREVIEW_UPLOAD_CONCURRENCY"):
        create_app()


@pytest.mark.parametrize("value", ["0", "9", "not-an-integer"])
def test_report_download_concurrency_rejects_unsafe_configuration(
    monkeypatch, value
) -> None:
    monkeypatch.setenv("PREREVIEW_REPORT_DOWNLOAD_CONCURRENCY", value)
    with pytest.raises(RuntimeError, match="PREREVIEW_REPORT_DOWNLOAD_CONCURRENCY"):
        create_app()


@pytest.mark.parametrize("value", ["0", "26214401", "not-an-integer"])
def test_report_size_rejects_unsafe_configuration(monkeypatch, value) -> None:
    monkeypatch.setenv("PREREVIEW_REPORT_MAX_BYTES", value)
    with pytest.raises(RuntimeError, match="PREREVIEW_REPORT_MAX_BYTES"):
        create_app()


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
