from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "bootstrap_local_auth_user.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_local_auth_user", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

EMAIL = "frontend-developer@example.invalid"
PASSWORD = "local-only-secret-A1!"
ANON_KEY = "anon-local-secret"
SERVICE_KEY = "service-role-local-secret"
TOKEN = "provider-token-that-must-not-be-printed"


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _local_files(
    tmp_path: Path,
    *,
    marker: bool = True,
) -> tuple[Path, Path]:
    supabase_dir = tmp_path / "supabase-dev"
    supabase_dir.mkdir()
    _write_private(
        supabase_dir / ".env",
        "ANON_KEY='anon-local-secret'\n"
        "SERVICE_ROLE_KEY='service-role-local-secret'\n"
        "API_GW_HTTP_PORT='8123'\n",
    )
    if marker:
        (supabase_dir / MODULE.INSTALLER_MARKER).write_text(
            f"ref={MODULE.PINNED_SUPABASE_REF}\n"
            f"commit={MODULE.PINNED_SUPABASE_COMMIT}\n",
            encoding="utf-8",
        )
    credentials = tmp_path / "pre-review-dev-auth.env"
    _write_private(
        credentials,
        "PREREVIEW_DEV_AUTH_EMAIL='frontend-developer@example.invalid'\n"
        "PREREVIEW_DEV_AUTH_PASSWORD='local-only-secret-A1!'\n"
        "PREREVIEW_DEV_AUTH_ROLE='user'\n",
    )
    return supabase_dir, credentials


def _development_environment() -> dict[str, str]:
    return {
        "PREREVIEW_ENVIRONMENT": "development",
        "PREREVIEW_DEV_AUTH_BOOTSTRAP_ENABLED": "true",
    }


def _settings() -> object:
    return MODULE.BootstrapSettings(
        base_url="http://127.0.0.1:8123",
        anon_key=ANON_KEY,
        service_role_key=SERVICE_KEY,
        email=EMAIL,
        password=PASSWORD,
    )


def _login_payload(
    *, role: str = "user", auth_role: str = "authenticated"
) -> dict[str, object]:
    return {
        "access_token": TOKEN,
        "refresh_token": f"refresh-{TOKEN}",
        "user": {
            "id": "5f32212c-98e2-42b0-a230-04fbf4cb887e",
            "email": EMAIL,
            "role": auth_role,
            "app_metadata": {"role": role},
        },
    }


def test_non_sensitive_prod_guards_run_before_secret_files_are_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    opened = False

    def unexpected_read(*args: object, **kwargs: object) -> dict[str, str]:
        nonlocal opened
        opened = True
        raise AssertionError("secret file must not be read")

    monkeypatch.setattr(MODULE, "_read_private_env", unexpected_read)
    with pytest.raises(MODULE.BootstrapError, match="must be development"):
        MODULE.load_settings(
            supabase_dir=tmp_path / "missing",
            credentials_path=tmp_path / "missing.env",
            allow_unmanaged_local=False,
            environ={
                "PREREVIEW_ENVIRONMENT": "production",
                "PREREVIEW_DEV_AUTH_BOOTSTRAP_ENABLED": "true",
            },
        )
    assert opened is False

    with pytest.raises(MODULE.BootstrapError, match="must be true"):
        MODULE.load_settings(
            supabase_dir=tmp_path / "missing",
            credentials_path=tmp_path / "missing.env",
            allow_unmanaged_local=False,
            environ={"PREREVIEW_ENVIRONMENT": "development"},
        )
    assert opened is False


def test_marker_is_required_but_explicit_legacy_override_keeps_other_guards(
    tmp_path: Path,
) -> None:
    supabase_dir, credentials = _local_files(tmp_path, marker=False)
    with pytest.raises(MODULE.BootstrapError, match="installer marker is missing"):
        MODULE.load_settings(
            supabase_dir=supabase_dir,
            credentials_path=credentials,
            allow_unmanaged_local=False,
            environ=_development_environment(),
        )

    loaded = MODULE.load_settings(
        supabase_dir=supabase_dir,
        credentials_path=credentials,
        allow_unmanaged_local=True,
        environ=_development_environment(),
    )
    assert loaded.base_url == "http://127.0.0.1:8123"


@pytest.mark.parametrize("unsafe_kind", ("mode", "symlink"))
def test_both_secret_inputs_must_be_private_regular_files(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    supabase_dir, credentials = _local_files(tmp_path)
    if unsafe_kind == "mode":
        (supabase_dir / ".env").chmod(0o644)
    else:
        target = tmp_path / "real-credentials.env"
        credentials.rename(target)
        credentials.symlink_to(target)

    with pytest.raises(MODULE.BootstrapError):
        MODULE.load_settings(
            supabase_dir=supabase_dir,
            credentials_path=credentials,
            allow_unmanaged_local=False,
            environ=_development_environment(),
        )


def test_account_and_gateway_guards_are_fail_closed(tmp_path: Path) -> None:
    supabase_dir, credentials = _local_files(tmp_path)
    _write_private(
        credentials,
        "PREREVIEW_DEV_AUTH_EMAIL='real-user@example.com'\n"
        "PREREVIEW_DEV_AUTH_PASSWORD='local-only-secret-A1!'\n",
    )
    with pytest.raises(MODULE.BootstrapError, match="@example.invalid"):
        MODULE.load_settings(
            supabase_dir=supabase_dir,
            credentials_path=credentials,
            allow_unmanaged_local=False,
            environ=_development_environment(),
        )

    assert MODULE._loopback_base_url("8000") == "http://127.0.0.1:8000"
    with pytest.raises(MODULE.BootstrapError, match="port is invalid"):
        MODULE._loopback_base_url("https://production.example.com")


@pytest.mark.parametrize(
    ("password", "accepted"),
    (
        ("a" * 72, True),
        ("a" * 73, False),
        ("가" * 23 + "abc", True),  # 69 + 3 UTF-8 bytes
        ("가" * 23 + "abcd", False),  # 69 + 4 UTF-8 bytes
    ),
)
def test_password_utf8_byte_boundary(password: str, accepted: bool) -> None:
    values = {
        "PREREVIEW_DEV_AUTH_EMAIL": EMAIL,
        "PREREVIEW_DEV_AUTH_PASSWORD": password,
        "PREREVIEW_DEV_AUTH_ROLE": "user",
    }
    if accepted:
        assert MODULE._validate_account(values) == (EMAIL, password)
    else:
        with pytest.raises(MODULE.BootstrapError, match="password length"):
            MODULE._validate_account(values)


def test_existing_confirmed_ordinary_user_is_reused_without_admin_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_login_payload())

    outcome = asyncio.run(
        MODULE.bootstrap_user(_settings(), transport=httpx.MockTransport(handler))
    )

    assert outcome == "reused"
    assert [request.url.path for request in requests] == ["/auth/v1/token"]
    assert requests[0].headers["apikey"] == ANON_KEY
    assert "authorization" not in requests[0].headers


def test_auth_client_ignores_environment_proxy_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    client_options: list[dict[str, object]] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_login_payload())

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        client_options.append(dict(kwargs))
        return real_async_client(*args, **kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setattr(MODULE.httpx, "AsyncClient", client_factory)

    outcome = asyncio.run(
        MODULE.bootstrap_user(_settings(), transport=httpx.MockTransport(handler))
    )

    assert outcome == "reused"
    assert len(client_options) == 1
    assert client_options[0]["trust_env"] is False
    assert [request.url.host for request in requests] == ["127.0.0.1"]


def test_malformed_success_without_session_tokens_fails_without_admin_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = _login_payload()
        payload["access_token"] = ""
        return httpx.Response(200, json=payload)

    with pytest.raises(MODULE.BootstrapError, match="HTTP 200"):
        asyncio.run(
            MODULE.bootstrap_user(_settings(), transport=httpx.MockTransport(handler))
        )

    assert [request.url.path for request in requests] == ["/auth/v1/token"]


@pytest.mark.parametrize("auth_role", (None, "service_role", "supabase_admin"))
def test_malformed_success_without_authenticated_user_role_fails_closed(
    auth_role: str | None,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = _login_payload(auth_role=auth_role or "authenticated")
        user = payload["user"]
        assert isinstance(user, dict)
        if auth_role is None:
            user.pop("role")
        return httpx.Response(200, json=payload)

    with pytest.raises(MODULE.BootstrapError, match="HTTP 200"):
        asyncio.run(
            MODULE.bootstrap_user(_settings(), transport=httpx.MockTransport(handler))
        )

    assert [request.url.path for request in requests] == ["/auth/v1/token"]


def test_missing_user_is_created_confirmed_then_verified() -> None:
    requests: list[httpx.Request] = []
    login_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_attempts
        requests.append(request)
        if request.url.path == "/auth/v1/token":
            login_attempts += 1
            if login_attempts == 1:
                return httpx.Response(400, json={"message": "invalid credentials"})
            return httpx.Response(200, json=_login_payload())
        assert request.url.path == "/auth/v1/admin/users"
        assert request.headers["apikey"] == SERVICE_KEY
        assert request.headers["authorization"] == f"Bearer {SERVICE_KEY}"
        body = json.loads(request.content)
        assert body == {
            "email": EMAIL,
            "password": PASSWORD,
            "email_confirm": True,
            "app_metadata": {"role": "user"},
        }
        return httpx.Response(201, json={"id": "not-printed"})

    outcome = asyncio.run(
        MODULE.bootstrap_user(_settings(), transport=httpx.MockTransport(handler))
    )

    assert outcome == "created"
    assert [request.url.path for request in requests] == [
        "/auth/v1/token",
        "/auth/v1/admin/users",
        "/auth/v1/token",
    ]


def test_concurrent_create_conflict_is_resolved_by_one_login_retry() -> None:
    login_attempts = 0
    admin_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_attempts, admin_calls
        if request.url.path == "/auth/v1/token":
            login_attempts += 1
            if login_attempts == 1:
                return httpx.Response(400, json={"message": "not found"})
            return httpx.Response(200, json=_login_payload())
        admin_calls += 1
        return httpx.Response(422, json={"message": "already exists"})

    outcome = asyncio.run(
        MODULE.bootstrap_user(_settings(), transport=httpx.MockTransport(handler))
    )

    assert outcome == "reused"
    assert login_attempts == 2
    assert admin_calls == 1


def test_wrong_existing_password_fails_without_mutation_or_secret_leakage() -> None:
    requests: list[httpx.Request] = []
    provider_body_secret = "provider-response-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/v1/admin/users":
            return httpx.Response(
                307,
                headers={"location": "http://attacker.invalid/collect"},
                text=f"{provider_body_secret} {EMAIL} {SERVICE_KEY}",
            )
        return httpx.Response(
            400,
            text=f"{provider_body_secret} {EMAIL} {PASSWORD} {TOKEN}",
        )

    with pytest.raises(MODULE.BootstrapError) as captured:
        asyncio.run(
            MODULE.bootstrap_user(_settings(), transport=httpx.MockTransport(handler))
        )

    message = str(captured.value)
    for secret in (
        EMAIL,
        PASSWORD,
        ANON_KEY,
        SERVICE_KEY,
        TOKEN,
        provider_body_secret,
    ):
        assert secret not in message
    assert [request.url.host for request in requests] == [
        "127.0.0.1",
        "127.0.0.1",
        "127.0.0.1",
    ]
    assert [request.method for request in requests] == ["POST", "POST", "POST"]


def test_main_prints_only_safe_status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(MODULE, "run", lambda argv: "created")
    assert MODULE.main([]) == 0
    output = capsys.readouterr()
    assert "created" in output.out
    for secret in (EMAIL, PASSWORD, ANON_KEY, SERVICE_KEY, TOKEN):
        assert secret not in output.out + output.err
