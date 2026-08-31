import os
import ssl
import smtplib
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.api.v1.password_reset import (
    PasswordResetConfirmRequest,
    PasswordResetRequest,
)
from app.core.config import Settings
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.services.password_reset import (
    InvalidResetTokenError,
    RESET_TOKEN_TTL_SECONDS,
    RESET_TOKEN_AUDIENCE,
    RESET_TOKEN_PURPOSE,
    ResetRateLimiter,
    confirm_password_reset,
    create_password_reset_token,
    decode_password_reset_token,
)
from main import create_app


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
OLD_PASSWORD = "old-password-without-digit"
NEW_PASSWORD = "New-password-1!"


@dataclass
class FakeMailSender:
    sent: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        if self.fail:
            raise OSError("smtp unavailable")
        self.sent.append((to_email, reset_url))


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if value is None:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL integration")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    value = create_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def create_user(engine: Engine) -> Iterator[Callable[..., int]]:
    user_ids: list[int] = []

    def create(
        login_id: str,
        password: str = OLD_PASSWORD,
        *,
        email: str | None = None,
        is_active: bool = True,
    ) -> int:
        with engine.begin() as connection:
            user_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.app_user (login_id, email, password_hash, is_active)
                    VALUES (:login_id, :email, :password_hash, :is_active)
                    RETURNING id
                    """
                ),
                {
                    "login_id": login_id,
                    "email": email or f"{login_id}@example.com",
                    "password_hash": hash_password(password),
                    "is_active": is_active,
                },
            )
        assert user_id is not None
        user_ids.append(user_id)
        return user_id

    yield create

    with engine.begin() as connection:
        for user_id in user_ids:
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


@pytest.fixture
def mail_sender() -> FakeMailSender:
    return FakeMailSender()


@pytest.fixture
def client(database_url: str, mail_sender: FakeMailSender) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        password_reset_url="http://localhost:3000/reset-password",
    )
    with TestClient(create_app(settings, mail_sender=mail_sender)) as value:
        yield value


def _row(engine: Engine, user_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    """
                    SELECT id, email::text AS email, password_hash, password_changed_at
                    FROM sims.app_user WHERE id = :user_id
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .one()
        )


def test_new_password_schema_requires_ascii_letter_digit_and_punctuation() -> None:
    PasswordResetConfirmRequest(token="token", new_password=NEW_PASSWORD)
    for password in ("short1!", "abcdefgh!", "abcdefg1", "가나다라마바사1!"):
        with pytest.raises(ValueError):
            PasswordResetConfirmRequest(token="token", new_password=password)


def test_request_has_generic_response_and_sends_only_to_active_registered_email(
    client: TestClient,
    create_user: Callable[..., int],
    mail_sender: FakeMailSender,
) -> None:
    create_user("reset-request-user", email="Reset.User@example.com")

    registered = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "reset.user@example.com"},
    )
    unknown = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "unknown@example.com"},
    )

    assert registered.status_code == unknown.status_code == 200
    assert registered.json() == unknown.json()
    assert registered.json()["code"] == "SUCCESS"
    assert registered.json()["data"] is None
    assert registered.json()["errors"] == []
    assert len(mail_sender.sent) == 1
    to_email, reset_url = mail_sender.sent[0]
    assert to_email == "Reset.User@example.com"
    assert reset_url.startswith("http://localhost:3000/reset-password#token=")


def test_reset_token_is_domain_separated_expiring_and_fingerprint_bound(
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    user_id = create_user("reset-token-user", email="token@example.com")
    user = _row(engine, user_id)
    token = create_password_reset_token(
        user_id,
        str(user["email"]),
        user["password_changed_at"],
        JWT_SECRET,
    )
    claims = decode_password_reset_token(token, JWT_SECRET)
    assert claims["uid"] == str(user_id)
    assert claims["aud"] == RESET_TOKEN_AUDIENCE
    assert claims["purpose"] == RESET_TOKEN_PURPOSE
    assert "sub" not in claims
    assert claims["exp"] - claims["iat"] == RESET_TOKEN_TTL_SECONDS

    with pytest.raises(InvalidResetTokenError):
        decode_password_reset_token(token + "x", JWT_SECRET)
    with pytest.raises(InvalidResetTokenError):
        decode_password_reset_token(
            jwt.encode(
                {"sub": str(user_id), "iat": time.time(), "exp": time.time() + 60},
                JWT_SECRET,
                algorithm="HS256",
            ),
            JWT_SECRET,
        )
    with pytest.raises(InvalidResetTokenError):
        decode_password_reset_token(
            create_password_reset_token(
                user_id,
                str(user["email"]),
                user["password_changed_at"],
                JWT_SECRET,
                now=time.time() - RESET_TOKEN_TTL_SECONDS - 1,
            ),
            JWT_SECRET,
        )

    access_token = create_access_token(user_id, JWT_SECRET, 600)
    with pytest.raises(InvalidResetTokenError):
        decode_password_reset_token(access_token, JWT_SECRET)


def test_confirm_updates_hash_history_and_invalidates_old_access_and_reset_tokens(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
    mail_sender: FakeMailSender,
) -> None:
    user_id = create_user("reset-confirm-user", email="confirm@example.com")
    access_response = client.post(
        "/api/v1/auth/login",
        json={"login_id": "reset-confirm-user", "password": OLD_PASSWORD},
    )
    assert access_response.status_code == 200
    access_token = access_response.json()["access_token"]
    request_response = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "confirm@example.com"},
    )
    assert request_response.status_code == 200
    reset_token = mail_sender.sent[-1][1].split("#token=", 1)[1]

    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": NEW_PASSWORD},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["data"] is None
    assert client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    ).status_code == 401
    assert client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {reset_token}"},
    ).status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        json={"login_id": "reset-confirm-user", "password": OLD_PASSWORD},
    ).status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        json={"login_id": "reset-confirm-user", "password": NEW_PASSWORD},
    ).status_code == 200

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT password_hash,
                       (SELECT count(*) FROM sims.password_change_history h
                        WHERE h.user_id = sims.app_user.id) AS history_count
                FROM sims.app_user WHERE id = :user_id
                """
            ),
            {"user_id": user_id},
        ).mappings().one()
    assert verify_password(NEW_PASSWORD, row["password_hash"])
    assert row["history_count"] == 1
    reused = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "Another-password-2!"},
    )
    assert reused.status_code == 400
    assert reused.json()["code"] == "BAD_REQUEST"


def test_reset_token_rejects_email_change_and_inactive_user(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
    mail_sender: FakeMailSender,
) -> None:
    user_id = create_user("email-change-reset", email="before@example.com")
    user = _row(engine, user_id)
    token = create_password_reset_token(
        user_id,
        str(user["email"]),
        user["password_changed_at"],
        JWT_SECRET,
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE sims.app_user SET email = :email WHERE id = :user_id"),
            {"email": "after@example.com", "user_id": user_id},
        )
    changed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 400

    active_id = create_user("deactivated-reset", email="deactivate@example.com")
    active_user = _row(engine, active_id)
    active_token = create_password_reset_token(
        active_id,
        str(active_user["email"]),
        active_user["password_changed_at"],
        JWT_SECRET,
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE sims.app_user SET is_active = false WHERE id = :user_id"),
            {"user_id": active_id},
        )
    deactivated = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": active_token, "new_password": NEW_PASSWORD},
    )
    assert deactivated.status_code == 400

    create_user("inactive-reset", email="inactive@example.com", is_active=False)
    response = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "inactive@example.com"},
    )
    assert response.status_code == 200
    assert not any(address == "inactive@example.com" for address, _ in mail_sender.sent)


def test_concurrent_reset_with_one_token_has_one_winner(
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    user_id = create_user("concurrent-reset", email="concurrent@example.com")
    user = _row(engine, user_id)
    token = create_password_reset_token(
        user_id,
        str(user["email"]),
        user["password_changed_at"],
        JWT_SECRET,
    )

    def reset(password: str) -> str:
        try:
            confirm_password_reset(engine, token, password, JWT_SECRET)
        except Exception as error:
            return type(error).__name__
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(reset, ("Concurrent-one-1!", "Concurrent-two-2!"))
        )
    assert sorted(results) == ["InvalidResetTokenError", "success"]


def test_request_and_confirm_rate_limits_return_retry_after(
    client: TestClient,
    create_user: Callable[..., int],
) -> None:
    create_user("rate-limit-user", email="rate@example.com")
    responses = [
        client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": f"rate-{index}@example.com"},
        )
        for index in range(6)
    ]
    assert responses[-1].status_code == 429
    assert responses[-1].headers["Retry-After"] == "900"

    responses = [
        client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": "bad", "new_password": NEW_PASSWORD},
        )
        for _ in range(11)
    ]
    assert responses[-1].status_code == 429
    assert responses[-1].headers["Retry-After"] == "900"


def test_rate_limiter_applies_request_limit_to_each_email_across_ips() -> None:
    limiter = ResetRateLimiter()
    assert all(
        limiter.allow_request(f"198.51.100.{index}", "same@example.com")
        for index in range(5)
    )
    assert not limiter.allow_request("198.51.100.99", "same@example.com")


def test_mail_failures_are_hidden_and_missing_configuration_is_503(
    database_url: str,
    create_user: Callable[..., int],
) -> None:
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        password_reset_url="http://localhost:3000/reset-password",
    )
    sender = FakeMailSender(fail=True)
    create_user("smtp-failure-user", email="smtp-failure@example.com")
    with TestClient(create_app(settings, mail_sender=sender)) as client:
        response = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "smtp-failure@example.com"},
        )
        assert response.status_code == 200
        assert "smtp-failure@example.com" not in response.text

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "missing@example.com"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"


def test_same_password_is_rejected_without_history(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    user_id = create_user("same-reset", password=NEW_PASSWORD, email="same@example.com")
    user = _row(engine, user_id)
    token = create_password_reset_token(
        user_id,
        str(user["email"]),
        user["password_changed_at"],
        JWT_SECRET,
    )
    response = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 400
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM sims.password_change_history WHERE user_id = :user_id"),
            {"user_id": user_id},
        ) == 0


def test_password_reset_validation_does_not_echo_secret(client: TestClient) -> None:
    secret = "sensitive-reset-token-" + ("x" * 4096)
    response = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": secret, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_smtp_sender_uses_verified_ssl_and_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, **kwargs: object) -> None:
            seen.update(host=host, port=port, **kwargs)

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def login(self, username: str, password: str) -> None:
            seen["login"] = (username, password)

        def send_message(self, message: object) -> None:
            seen["message"] = message

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    from app.infrastructure.smtp_mail_sender import SmtpMailSender

    SmtpMailSender(
        "smtp.example.com", 465, "mailer", "secret", "from@example.com"
    ).send_password_reset("to@example.com", "https://front.example/reset#token=abc")
    assert seen["host"] == "smtp.example.com"
    assert seen["port"] == 465
    context = seen["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    message = seen["message"]
    assert message["From"] == "from@example.com"
    assert message["To"] == "to@example.com"
    assert "#token=abc" in message.get_content()
