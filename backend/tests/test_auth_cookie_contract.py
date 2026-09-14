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
    app.state.auth_password_reset_callback_url = (
        "https://frontend.example.test/api/v1/auth/password-recovery/callback"
    )
    app.state.auth_password_reset_redirect_to = (
        "https://frontend.example.test/password-reset/update"
    )
    return app


def _cookie(set_cookie: list[str], name: str, *, path: str) -> str:
    return next(
        item
        for item in set_cookie
        if item.startswith(f"{name}=")
        and f"Path={path}" in {part.strip() for part in item.split(";")[1:]}
    )


def _assert_session_deletions(set_cookie: list[str]) -> None:
    assert len(set_cookie) == 3
    deleted = (
        _cookie(set_cookie, "pre_review_access", path="/"),
        _cookie(set_cookie, "pre_review_refresh", path="/api/v1/auth"),
        _cookie(set_cookie, "pre_review_refresh", path="/"),
    )
    assert all("Max-Age=0" in item for item in deleted)
    assert all("HttpOnly" in item and "SameSite=lax" in item for item in deleted)
    assert not any("Secure" in item for item in deleted)


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
            # A browser upgrading from v0.1 can still have this root-scoped
            # credential. Every new v0.2 session must retire it.
            api.cookies.set(
                "pre_review_refresh", "legacy-refresh",
                domain="testserver.local", path="/",
            )
            sign_in = await api.post("/api/v1/auth/sign-in", headers={"Origin": ORIGIN}, json={"email": "User@Example.com", "password": "correct-password"})
            assert sign_in.status_code == 200
            assert sign_in.json() == {"user": {"id": "user-1", "email": "user@example.com", "display_name": "홍길동"}}
            assert "secret" not in sign_in.text
            assert sign_in.headers["cache-control"] == "private, no-store"
            assert "Cookie" in sign_in.headers["vary"]
            set_cookie = sign_in.headers.get_list("set-cookie")
            assert len(set_cookie) == 3
            access = _cookie(set_cookie, "pre_review_access", path="/")
            refresh = _cookie(
                set_cookie, "pre_review_refresh", path="/api/v1/auth"
            )
            legacy_refresh = _cookie(
                set_cookie, "pre_review_refresh", path="/"
            )
            assert "HttpOnly" in access and "SameSite=lax" in access and "Path=/" in access
            # The refresh cookie is scoped to the auth prefix only, so
            # browsers never attach it to business API calls.
            assert "HttpOnly" in refresh and "SameSite=lax" in refresh
            assert "Path=/api/v1/auth" in refresh
            assert "Max-Age=0" in legacy_refresh
            assert not any("Secure" in item for item in set_cookie)
            assert api.cookies.get(
                "pre_review_refresh", path="/api/v1/auth"
            ) == "refresh-secret"
            assert api.cookies.get("pre_review_refresh", path="/") is None

            me = await api.get("/api/v1/auth/me", headers={"Authorization": "Bearer ignored-by-server"})
            assert me.status_code == 200
            assert me.json() == {"user": {"id": "user-1", "email": "user@example.com", "display_name": "홍길동"}}

    asyncio.run(run())
    assert len(requests) == 2


def test_legacy_refresh_expiration_matches_secure_domain_cookie_configuration() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/v1/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "user": {
                        "id": "user-1",
                        "email": "user@example.com",
                    },
                },
            )
        assert request.url.path == "/auth/v1/logout"
        return httpx.Response(204)

    async def run() -> None:
        app = auth_app(httpx.MockTransport(provider))
        app.state.auth_cookie_secure = True
        app.state.auth_cookie_samesite = "none"
        app.state.auth_cookie_domain = "example.test"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://api.example.test",
        ) as api:
            signed_in = await api.post(
                "/api/v1/auth/sign-in",
                headers={"Origin": ORIGIN},
                json={
                    "email": "user@example.com",
                    "password": "correct-password",
                },
            )
            assert signed_in.status_code == 200
            set_cookie = signed_in.headers.get_list("set-cookie")
            assert len(set_cookie) == 3
            scoped = _cookie(
                set_cookie, "pre_review_refresh", path="/api/v1/auth"
            )
            legacy = _cookie(set_cookie, "pre_review_refresh", path="/")
            for item in (scoped, legacy):
                assert "Domain=example.test" in item
                assert "HttpOnly" in item
                assert "SameSite=none" in item
                assert "Secure" in item
            assert "Max-Age=0" in legacy

            signed_out = await api.post(
                "/api/v1/auth/sign-out", headers={"Origin": ORIGIN}
            )
            assert signed_out.status_code == 204
            deleted = signed_out.headers.get_list("set-cookie")
            assert len(deleted) == 3
            for item in deleted:
                assert "Domain=example.test" in item
                assert "HttpOnly" in item
                assert "SameSite=none" in item
                assert "Secure" in item
                assert "Max-Age=0" in item

    asyncio.run(run())


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
    refresh_tokens: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/v1/token":
            assert request.url.params["grant_type"] == "refresh_token"
            refresh_tokens.append(json.loads(request.content)["refresh_token"])
            ordinal = len(refresh_tokens)
            return httpx.Response(200, json={"access_token": f"new-access-{ordinal}", "refresh_token": f"new-refresh-{ordinal}", "expires_in": 3600})
        assert request.url.path == "/auth/v1/recover"
        assert request.url.params["redirect_to"] == (
            "https://frontend.example.test/api/v1/auth/password-recovery/callback"
        )
        assert json.loads(request.content) == {"email": "nobody@example.com"}
        return httpx.Response(400, json={"message": "user not found"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(httpx.MockTransport(provider))), base_url="http://testserver") as api:
            # HTTPX emits the longer path first; Starlette collapses duplicate
            # names to the last value. This recreates the deployed v0.1/v0.2
            # collision instead of hand-writing an artificial Cookie header.
            api.cookies.set("pre_review_refresh", "scoped-current", domain="testserver.local", path="/api/v1/auth")
            api.cookies.set("pre_review_refresh", "root-legacy", domain="testserver.local", path="/")
            request = api.build_request(
                "POST", "/api/v1/auth/refresh", headers={"Origin": ORIGIN}
            )
            assert request.headers["cookie"].count("pre_review_refresh=") == 2
            refreshed = await api.send(request)
            assert refreshed.status_code == 204
            assert refreshed.content == b""
            refreshed_headers = refreshed.headers.get_list("set-cookie")
            assert len(refreshed_headers) == 3
            assert "Max-Age=0" in _cookie(
                refreshed_headers, "pre_review_refresh", path="/"
            )
            assert api.cookies.get("pre_review_access") == "new-access-1"
            assert api.cookies.get("pre_review_refresh", path="/api/v1/auth") == "new-refresh-1"
            assert api.cookies.get("pre_review_refresh", path="/") is None

            refreshed_again = await api.post(
                "/api/v1/auth/refresh", headers={"Origin": ORIGIN}
            )
            assert refreshed_again.status_code == 204
            assert len(refreshed_again.headers.get_list("set-cookie")) == 3
            # The stale root credential cannot override the rotated scoped one
            # on the request after the migration response.
            assert refresh_tokens[-1] == "new-refresh-1"

            reset = await api.post("/api/v1/auth/password-reset", headers={"Origin": ORIGIN}, json={"email": "nobody@example.com"})
            assert reset.status_code == 200
            assert reset.json() == {"message": "If an account exists, password reset instructions have been sent."}

    asyncio.run(run())
    assert refresh_tokens == ["root-legacy", "new-refresh-1"]


def test_password_recovery_callback_works_in_a_fresh_browser() -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/v1/recover":
            assert request.url.params["redirect_to"] == (
                "https://frontend.example.test/api/v1/auth/password-recovery/callback"
            )
            assert json.loads(request.content) == {"email": "user@example.com"}
            return httpx.Response(200, json={})

        assert request.url.path == "/auth/v1/verify"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "token_hash": "recovery-token-hash",
            "type": "recovery",
        }
        return httpx.Response(
            200,
            json={
                "access_token": "recovery-access",
                "refresh_token": "recovery-refresh",
                "expires_in": 3600,
            },
        )

    async def run() -> None:
        app = auth_app(httpx.MockTransport(provider))
        # The reset request and email click deliberately use separate cookie
        # jars. The email token itself is sufficient proof, so another browser
        # or device can complete recovery.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://request-browser",
        ) as requester:
            reset = await requester.post(
                "/api/v1/auth/password-reset",
                headers={"Origin": ORIGIN},
                json={"email": "user@example.com"},
            )
            assert reset.status_code == 200
            assert "set-cookie" not in reset.headers

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://email-browser",
            follow_redirects=False,
        ) as email_browser:
            callback = await email_browser.get(
                "/api/v1/auth/password-recovery/callback",
                params={"token_hash": "recovery-token-hash"},
            )
            assert callback.status_code == 303
            assert callback.headers["location"] == (
                "https://frontend.example.test/password-reset/update"
            )
            assert callback.headers["cache-control"] == "private, no-store"
            assert callback.headers["referrer-policy"] == "no-referrer"
            assert email_browser.cookies.get("pre_review_access") == "recovery-access"
            assert email_browser.cookies.get(
                "pre_review_refresh", path="/api/v1/auth"
            ) == "recovery-refresh"

    asyncio.run(run())
    assert [request.url.path for request in requests] == [
        "/auth/v1/recover",
        "/auth/v1/verify",
    ]


def test_password_reset_preserves_provider_rate_limit() -> None:
    def provider(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "rate limited"})

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=auth_app(httpx.MockTransport(provider))
            ),
            base_url="http://testserver",
        ) as api:
            response = await api.post(
                "/api/v1/auth/password-reset",
                headers={"Origin": ORIGIN},
                json={"email": "nobody@example.com"},
            )
            assert response.status_code == 429
            assert response.json()["code"] == "RATE_LIMITED"

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
            api.cookies.set("pre_review_refresh", "legacy-refresh", domain="testserver.local", path="/")
            updated = await api.post("/api/v1/auth/update-password", headers={"Origin": ORIGIN}, json={"password": "new-correct-password"})
            assert updated.status_code == 204
            assert "new-correct-password" not in updated.text

            signed_out = await api.post("/api/v1/auth/sign-out", headers={"Origin": ORIGIN})
            assert signed_out.status_code == 204
            deleted = signed_out.headers.get_list("set-cookie")
            _assert_session_deletions(deleted)
            assert api.cookies.get("pre_review_access", path="/") is None
            assert api.cookies.get("pre_review_refresh", path="/api/v1/auth") is None
            assert api.cookies.get("pre_review_refresh", path="/") is None

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
            _assert_session_deletions(refreshed.headers.get_list("set-cookie"))

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
            _assert_session_deletions(signed_out.headers.get_list("set-cookie"))

    asyncio.run(run())


def test_sign_out_transport_failure_still_clears_local_cookies() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider unavailable", request=request)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=auth_app(httpx.MockTransport(provider))
            ),
            base_url="http://testserver",
        ) as api:
            api.cookies.set(
                "pre_review_access", "access-secret",
                domain="testserver.local", path="/",
            )
            api.cookies.set(
                "pre_review_refresh", "refresh-secret",
                domain="testserver.local", path="/api/v1/auth",
            )
            response = await api.post(
                "/api/v1/auth/sign-out", headers={"Origin": ORIGIN}
            )

            assert response.status_code == 503
            assert response.json()["code"] == "SERVICE_UNAVAILABLE"
            deleted = response.headers.get_list("set-cookie")
            _assert_session_deletions(deleted)

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


def test_invalid_access_cookie_is_cleared_on_business_and_me_routes() -> None:
    def provider(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "expired"})

    async def run() -> None:
        for path in (
            "/api/v1/auth/me",
            "/api/v1/analysis-sessions/active",
        ):
            app = auth_app(httpx.MockTransport(provider))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as api:
                api.cookies.set(
                    "pre_review_access", "expired-access",
                    domain="testserver.local", path="/",
                )
                api.cookies.set(
                    "pre_review_refresh", "expired-refresh",
                    domain="testserver.local", path="/api/v1/auth",
                )
                response = await api.get(path)
                assert response.status_code == 401
                assert response.json()["code"] == "UNAUTHORIZED"
                deleted = response.headers.get_list("set-cookie")
                _assert_session_deletions(deleted)

    asyncio.run(run())


def test_business_auth_preserves_cookie_and_classifies_transient_provider_failures() -> None:
    outcomes = (
        (429, {"message": "slow down"}, 429),
        (503, {"message": "unavailable"}, 503),
        (418, {"message": "unexpected"}, 502),
        (200, None, 502),
    )

    async def run() -> None:
        for provider_status, payload, expected_status in outcomes:
            def provider(
                _: httpx.Request,
                provider_status: int = provider_status,
                payload: object = payload,
            ) -> httpx.Response:
                if payload is None:
                    return httpx.Response(provider_status, content=b"not-json")
                return httpx.Response(provider_status, json=payload)

            app = auth_app(httpx.MockTransport(provider))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as api:
                api.cookies.set(
                    "pre_review_access", "still-valid-access",
                    domain="testserver.local", path="/",
                )
                response = await api.get("/api/v1/analysis-sessions/active")
                assert response.status_code == expected_status
                assert "set-cookie" not in response.headers
                assert api.cookies.get("pre_review_access") == "still-valid-access"

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
    assert "/api/v1/auth/password-recovery/exchange" not in paths
    callback = paths["/api/v1/auth/password-recovery/callback"]["get"]
    assert callback["parameters"][0]["name"] == "token_hash"
    assert callback["parameters"][0]["in"] == "query"
    assert "303" in callback["responses"]
    assert "PreReviewRecoveryVerifierCookie" not in schema["components"]["securitySchemes"]

    schemas = schema["components"]["schemas"]
    assert set(schemas["AuthUserResponse"]["required"]) == {"id", "email", "display_name"}

    credentials = schemas["CredentialsRequest"]
    sign_up = schemas["SignUpRequest"]
    update_password = schemas["UpdatePasswordRequest"]
    assert "display_name" not in credentials["properties"]
    assert sign_up["properties"]["display_name"]["maxLength"] == 100
    assert "display_name" in sign_up["required"]
    assert sign_up["properties"]["password"]["minLength"] == 8
    assert update_password["properties"]["password"]["minLength"] == 8
    for request_model in (credentials, sign_up, update_password):
        password = request_model["properties"]["password"]
        assert password["format"] == "password"
        assert password["writeOnly"] is True

    # Cookie-mutating 204 operations deliberately have no success body.
    for path in ("/api/v1/auth/refresh", "/api/v1/auth/sign-out", "/api/v1/auth/update-password"):
        response = paths[path]["post"]["responses"]["204"]
        assert "content" not in response
