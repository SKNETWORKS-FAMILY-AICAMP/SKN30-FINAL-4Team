"""Offline contract tests for the Supabase Auth cookie proxy."""

from __future__ import annotations

import asyncio
import json

import httpx

from main import create_app


ORIGIN = "https://frontend.example.test"


def auth_app(handler: httpx.MockTransport) -> object:
    app = create_app()
    app.state.offline_mode = False
    app.state.supabase_url = "https://supabase.example.test"
    app.state.supabase_anon_key = "test-anon-key"
    app.state.supabase_auth_transport = handler
    app.state.auth_allowed_origins = frozenset({ORIGIN})
    app.state.auth_cookie_secure = False
    app.state.auth_cookie_samesite = "lax"
    return app


def test_sign_in_sets_http_only_cookies_and_me_uses_cookie_only() -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/v1/token":
            assert request.url.params["grant_type"] == "password"
            assert json.loads(request.content) == {"email": "user@example.com", "password": "correct-password"}
            return httpx.Response(200, json={"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 3600, "user": {"id": "user-1", "email": "user@example.com"}})
        assert request.url.path == "/auth/v1/user"
        assert request.headers["authorization"] == "Bearer access-secret"
        assert request.headers["apikey"] == "test-anon-key"
        return httpx.Response(200, json={"id": "user-1", "email": "user@example.com"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(httpx.MockTransport(provider))), base_url="http://testserver") as api:
            sign_in = await api.post("/api/v1/auth/sign-in", headers={"Origin": ORIGIN}, json={"email": "User@Example.com", "password": "correct-password"})
            assert sign_in.status_code == 200
            assert sign_in.json() == {"user": {"id": "user-1", "email": "user@example.com"}}
            assert "secret" not in sign_in.text
            set_cookie = sign_in.headers.get_list("set-cookie")
            assert len(set_cookie) == 2
            assert all("HttpOnly" in item and "SameSite=lax" in item and "Path=/" in item for item in set_cookie)
            assert not any("Secure" in item for item in set_cookie)

            me = await api.get("/api/v1/auth/me", headers={"Authorization": "Bearer ignored-by-server"})
            assert me.status_code == 200
            assert me.json() == {"user": {"id": "user-1", "email": "user@example.com"}}

    asyncio.run(run())
    assert len(requests) == 2


def test_state_changes_fail_closed_without_a_trusted_origin() -> None:
    calls = 0

    def provider(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(httpx.MockTransport(provider))), base_url="http://testserver") as api:
            missing = await api.post("/api/v1/auth/sign-in", json={"email": "user@example.com", "password": "correct-password"})
            foreign = await api.post("/api/v1/auth/password-reset", headers={"Origin": "https://attacker.example"}, json={"email": "user@example.com"})
            assert missing.status_code == 403
            assert foreign.status_code == 403

    asyncio.run(run())
    assert calls == 0


def test_refresh_rotates_cookies_and_password_reset_is_non_enumerating() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/v1/token":
            assert request.url.params["grant_type"] == "refresh_token"
            assert json.loads(request.content) == {"refresh_token": "old-refresh"}
            return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})
        assert request.url.path == "/auth/v1/recover"
        assert request.url.params.get("redirect_to") is None
        return httpx.Response(400, json={"message": "user not found"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(httpx.MockTransport(provider))), base_url="http://testserver") as api:
            api.cookies.set("pre_review_refresh", "old-refresh", domain="testserver.local", path="/")
            refreshed = await api.post("/api/v1/auth/refresh", headers={"Origin": ORIGIN})
            assert refreshed.status_code == 204
            assert refreshed.content == b""
            assert api.cookies.get("pre_review_access") == "new-access"
            assert api.cookies.get("pre_review_refresh") == "new-refresh"

            reset = await api.post("/api/v1/auth/password-reset", headers={"Origin": ORIGIN}, json={"email": "nobody@example.com"})
            assert reset.status_code == 200
            assert reset.json() == {"message": "If an account exists, password reset instructions have been sent."}

    asyncio.run(run())


def test_sign_out_deletes_cookie_pair_and_update_password_never_echoes_secret() -> None:
    seen: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/auth/v1/user" and request.method == "PUT":
            assert request.headers["authorization"] == "Bearer access-secret"
            assert json.loads(request.content) == {"password": "new-correct-password"}
            return httpx.Response(200, json={"id": "user-1"})
        assert request.url.path == "/auth/v1/logout"
        assert request.headers["authorization"] == "Bearer access-secret"
        return httpx.Response(204)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(httpx.MockTransport(provider))), base_url="http://testserver") as api:
            api.cookies.set("pre_review_access", "access-secret", domain="testserver.local", path="/")
            api.cookies.set("pre_review_refresh", "refresh-secret", domain="testserver.local", path="/")
            updated = await api.post("/api/v1/auth/update-password", headers={"Origin": ORIGIN}, json={"password": "new-correct-password"})
            assert updated.status_code == 204
            assert "new-correct-password" not in updated.text

            signed_out = await api.post("/api/v1/auth/sign-out", headers={"Origin": ORIGIN})
            assert signed_out.status_code == 204
            deleted = signed_out.headers.get_list("set-cookie")
            assert len(deleted) == 2
            assert all("Max-Age=0" in item for item in deleted)

    asyncio.run(run())
    assert len(seen) == 2


def test_invalid_refresh_and_remote_logout_failure_clear_local_cookies() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/v1/token":
            return httpx.Response(401, json={"message": "expired"})
        assert request.url.path == "/auth/v1/logout"
        return httpx.Response(503, json={"message": "unavailable"})

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=auth_app(httpx.MockTransport(provider))
            ),
            base_url="http://testserver",
        ) as api:
            api.cookies.set(
                "pre_review_refresh", "expired-refresh",
                domain="testserver.local", path="/",
            )
            refreshed = await api.post(
                "/api/v1/auth/refresh", headers={"Origin": ORIGIN}
            )
            assert refreshed.status_code == 401
            assert len(refreshed.headers.get_list("set-cookie")) == 2

            api.cookies.set(
                "pre_review_access", "access-secret",
                domain="testserver.local", path="/",
            )
            api.cookies.set(
                "pre_review_refresh", "refresh-secret",
                domain="testserver.local", path="/",
            )
            signed_out = await api.post(
                "/api/v1/auth/sign-out", headers={"Origin": ORIGIN}
            )
            assert signed_out.status_code == 503
            assert len(signed_out.headers.get_list("set-cookie")) == 2

    asyncio.run(run())


def test_openapi_exposes_typed_auth_success_models_and_write_only_passwords() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    def response_ref(path: str, status_code: str) -> str:
        return paths[path]["post" if path != "/api/v1/auth/me" else "get"][
            "responses"
        ][status_code]["content"]["application/json"]["schema"]["$ref"]

    assert response_ref("/api/v1/auth/sign-in", "200").endswith(
        "/AuthUserEnvelope"
    )
    assert response_ref("/api/v1/auth/sign-up", "201").endswith(
        "/SignUpResponse"
    )
    assert response_ref("/api/v1/auth/password-reset", "200").endswith(
        "/PasswordResetResponse"
    )
    assert response_ref("/api/v1/auth/me", "200").endswith("/AuthUserEnvelope")

    credentials = schema["components"]["schemas"]["CredentialsRequest"]
    update_password = schema["components"]["schemas"]["UpdatePasswordRequest"]
    for request_model in (credentials, update_password):
        password = request_model["properties"]["password"]
        assert password["format"] == "password"
        assert password["writeOnly"] is True

    # Cookie-mutating 204 operations deliberately have no success body.
    for path in ("/api/v1/auth/refresh", "/api/v1/auth/sign-out", "/api/v1/auth/update-password"):
        response = paths[path]["post"]["responses"]["204"]
        assert "content" not in response
