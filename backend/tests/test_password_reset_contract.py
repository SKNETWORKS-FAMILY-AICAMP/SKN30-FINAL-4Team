import pytest

from app.services.password_reset import ResetRateLimiter
from main import create_app
from app.core.config import Settings


@pytest.mark.parametrize("capacity", [1, 3])
def test_request_capacity_never_accepts_without_both_counters(capacity):
    limiter = ResetRateLimiter()
    limiter.MAX_KEYS = capacity
    if capacity == 3:
        assert limiter.allow_request("first", "first@example.com")
    assert not limiter.allow_request("new", "new@example.com")
    if capacity == 3:
        # A rejected request must not consume the remaining counter slot.
        assert limiter.allow_request("first", "other@example.com")


def test_full_limiter_keeps_live_counters_and_releases_expired_ones(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.services.password_reset.time.monotonic", lambda: now[0])
    limiter = ResetRateLimiter()
    limiter.MAX_KEYS = 1
    assert all(limiter.allow_confirm("first") for _ in range(10))
    assert not limiter.allow_confirm("first")
    assert not limiter.allow_confirm("new")
    assert not limiter.allow_confirm("first")
    now[0] += 900
    assert limiter.allow_confirm("new")


def test_openapi_reset_validation_matches_actual_envelope():
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://test:test@localhost/test", jwt_secret="x" * 32)
    schema = create_app(settings).openapi()
    for action in ("request", "confirm"):
        operation = schema["paths"][f"/api/v1/auth/password-reset/{action}"]["post"]
        # 실패 응답은 화면 문구 하나만 담는다.
        schema_ref = operation["responses"]["422"]["content"]["application/json"]["schema"]
        assert schema_ref["$ref"].endswith("/PasswordResetResponse")
        assert schema["components"]["schemas"]["PasswordResetResponse"]["required"] == ["message"]
        assert not operation.get("security")
    operations = sum(method in {"get", "post", "put", "delete", "patch"} for path in schema["paths"].values() for method in path)
    assert operations == 16
