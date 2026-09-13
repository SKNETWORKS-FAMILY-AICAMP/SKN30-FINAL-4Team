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


def _cookie(set_cookie: list[str], name: str) -> str:
    return next(item for item in set_cookie if item.startswith(f"{name}="))


def test_sign_in_sets_http_only_cookies_and_me_uses_cookie_only() -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/v1/token":
            assert request.url.params["grant_type"] == "password"
            assert json.loads(request.content) == {"email": "user@example.com", "password": "correct-password"}
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "user": {
                        "id": "user-1",
                        "email": "user@example.com",
                        "user_metadata": {"display_name": "홍길동"},
                    },
                },
            )
        assert request.url.path == "/auth/v1/user"
        assert request.headers["authorization"] == "Bearer access-secret"
        assert request.headers["apikey"] == "test-anon-key"
        return httpx.Response(
            200,
            json={
                "id": "user-1",
                "email": "user@example.com",
                "user_metadata": {"display_name": "홍길동"},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(httpx.MockTransport(provider))), base_url="http://testserver") as api:
            sign_in = await api.post("/api/v1/auth/sign-in", headers={"Origin": ORIGIN}, json={"email": "User@Example.com", "password": "correct-password"})
            assert sign_in.status_code == 200
            assert sign_in.json() == {"user": {"id": "user-1", "email": "user@example.com", "display_name": "홍길동"}}
            assert "secret" not in sign_in.text
            assert sign_in.headers["cache-control"] == "private, no-store"
            assert "Cookie" in sign_in.headers["vary"]
            set_cookie = sign_in.headers.get_list("set-cookie")
            assert len(set_cookie) == 2
            access = _cookie(set_cookie, "pre_review_access")
            refresh = _cookie(set_cookie, "pre_review_refresh")
            assert "HttpOnly" in access and "SameSite=lax" in access and "Path=/" in access
            # The refresh cookie is scoped to the auth prefix only, so
            # browsers never attach it to business API calls.
            assert "HttpOnly" in refresh and "SameSite=lax" in refresh
            assert "Path=/api/v1/auth" in refresh
            assert not any("Secure" in item for item in set_cookie)

            me = await api.get("/api/v1/auth/me", headers={"Authorization": "Bearer ignored-by-server"})
            assert me.status_code == 200
            assert me.json() == {"user": {"id": "user-1", "email": "user@example.com", "display_name": "홍길동"}}

    asyncio.run(run())
    assert len(requests) == 2


def test_display_name_falls_back_to_email_local_part_when_missing_or_invalid() -> None:
    cases = [
        None,
        "",
        "   ",
        123,
        "x" * 101,
        "bad\x00name",
    ]

    async def run() -> None:
        for metadata_value in cases:
            metadata = {} if metadata_value is None else {"display_name": metadata_value}

            def provider(request: httpx.Request, metadata: dict = metadata) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={"id": "user-1", "email": "someone@example.com", "user_metadata": metadata},
                )

            app = auth_app(httpx.MockTransport(provider))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
                api.cookies.set("pre_review_access", "access-secret", domain="testserver.local", path="/")
                me = await api.get("/api/v1/auth/me")
                assert me.status_code == 200
                assert me.json()["user"]["display_name"] == "someone"

    asyncio.run(run())


def test_display_name_is_nfc_normalized_and_trimmed() -> None:
    # "가" decomposed (NFD, U+1100 U+1161) must normalize to the same
    # precomposed form (NFC, U+AC00) so equal-looking names compare equal.
    decomposed = "가 "  # "가 " (with trailing space) in NFD

    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "user-1",
                "email": "someone@example.com",
                "user_metadata": {"display_name": decomposed},
            },
        )

    async def run() -> None:
        app = auth_app(httpx.MockTransport(provider))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
            api.cookies.set("pre_review_access", "access-secret", domain="testserver.local", path="/")
            me = await api.get("/api/v1/auth/me")
            assert me.status_code == 200
            assert me.json()["user"]["display_name"] == "가"

    asyncio.run(run())


def test_sign_up_requires_display_name_and_sends_it_as_data() -> None:
    seen: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/auth/v1/signup"
        assert json.loads(request.content) == {
            "email": "user@example.com",
            "password": "correct-password",
            "data": {"display_name": "홍길동"},
        }
        return httpx.Response(200, json={"email_confirmation_required": True})

    async def run() -> None:
        app = auth_app(httpx.MockTransport(provider))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
            missing = await api.post(
                "/api/v1/auth/sign-up",
                headers={"Origin": ORIGIN},
                json={"email": "user@example.com", "password": "correct-password"},
            )
            assert missing.status_code == 422

            created = await api.post(
                "/api/v1/auth/sign-up",
                headers={"Origin": ORIGIN},
                json={"email": "user@example.com", "password": "correct-password", "display_name": "홍길동"},
            )
            assert created.status_code == 201

    asyncio.run(run())
    assert len(seen) == 1


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
            api.cookies.set("pre_review_refresh", "old-refresh", domain="testserver.local", path="/api/v1/auth")
            refreshed = await api.post("/api/v1/auth/refresh", headers={"Origin": ORIGIN})
            assert refreshed.status_code == 204
            assert refreshed.content == b""
            assert api.cookies.get("pre_review_access") == "new-access"
            assert api.cookies.get("pre_review_refresh", path="/api/v1/auth") == "new-refresh"

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
            api.cookies.set("pre_review_refresh", "refresh-secret", domain="testserver.local", path="/api/v1/auth")
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
                domain="testserver.local", path="/api/v1/auth",
            )
            refreshed = await api.post(
                "/api/v1/auth/refresh", headers={"Origin": ORIGIN}
            )
            assert refreshed.status_code == 401
            assert refreshed.json()["code"] == "UNAUTHORIZED"
            assert len(refreshed.headers.get_list("set-cookie")) == 2

            api.cookies.set(
                "pre_review_access", "access-secret",
                domain="testserver.local", path="/",
            )
            api.cookies.set(
                "pre_review_refresh", "refresh-secret",
                domain="testserver.local", path="/api/v1/auth",
            )
            signed_out = await api.post(
                "/api/v1/auth/sign-out", headers={"Origin": ORIGIN}
            )
            assert signed_out.status_code == 503
            assert len(signed_out.headers.get_list("set-cookie")) == 2

    asyncio.run(run())


def test_refresh_preserves_cookies_on_rate_limit_transport_and_malformed_payload() -> None:
    """Only an invalid/expired credential may clear the refresh cookie.

    Rate limits, upstream 5xx/transport failures, and malformed 200 payloads
    are transient; clearing cookies on them would force a still-valid user to
    sign in again for no reason (v0.2 spec section 3.2).
    """

    outcomes = iter(
        [
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, content=b"not json"),
        ]
    )
    expected_status = iter([429, 503, 502])

    def provider(request: httpx.Request) -> httpx.Response:
        return next(outcomes)

    async def run() -> None:
        app = auth_app(httpx.MockTransport(provider))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
            for expected in expected_status:
                api.cookies.set(
                    "pre_review_refresh", "still-valid-refresh",
                    domain="testserver.local", path="/api/v1/auth",
                )
                response = await api.post("/api/v1/auth/refresh", headers={"Origin": ORIGIN})
                assert response.status_code == expected
                assert "set-cookie" not in response.headers
                # httpx's cookie jar only clears entries an actual
                # Set-Cookie deleted; the refresh cookie must still be there.
                assert api.cookies.get("pre_review_refresh", path="/api/v1/auth") == "still-valid-refresh"

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

    schemas = schema["components"]["schemas"]
    assert set(schemas["AuthUserResponse"]["required"]) == {"id", "email", "display_name"}

    credentials = schemas["CredentialsRequest"]
    sign_up = schemas["SignUpRequest"]
    update_password = schemas["UpdatePasswordRequest"]
    assert "display_name" not in credentials["properties"]
    assert sign_up["properties"]["display_name"]["maxLength"] == 100
    assert "display_name" in sign_up["required"]
    for request_model in (credentials, sign_up, update_password):
        password = request_model["properties"]["password"]
        assert password["format"] == "password"
        assert password["writeOnly"] is True

    # Cookie-mutating 204 operations deliberately have no success body.
    for path in ("/api/v1/auth/refresh", "/api/v1/auth/sign-out", "/api/v1/auth/update-password"):
        response = paths[path]["post"]["responses"]["204"]
        assert "content" not in response
