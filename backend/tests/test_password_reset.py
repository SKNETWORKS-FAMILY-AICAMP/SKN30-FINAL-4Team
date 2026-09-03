import os
import ssl
import smtplib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings
from app.core.password_policy import validate_new_password
from app.core.security import hash_password, verify_password
from app.services.password_reset import (
    ResetRateLimiter,
    generate_temporary_password,
)
from main import create_app


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
OLD_PASSWORD = "old-password-without-digit"


@dataclass
class FakeMailSender:
    sent: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    def send_temporary_password(self, to_email: str, temporary_password: str) -> None:
        if self.fail:
            raise OSError("smtp unavailable")
        self.sent.append((to_email, temporary_password))


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
    settings = Settings(database_url=database_url, jwt_secret=JWT_SECRET)
    with TestClient(create_app(settings, mail_sender=mail_sender)) as value:
        yield value


def _password_hash(engine: Engine, user_id: int) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(
                text("SELECT password_hash FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )
        )


def test_generated_temporary_password_always_satisfies_the_policy() -> None:
    """정책을 만족할 때까지 다시 뽑는 방식이 아니라 확정적으로 만족해야 한다."""

    passwords = {generate_temporary_password() for _ in range(200)}
    for password in passwords:
        validate_new_password(password)
    # 매번 같은 값이 나오면 임시 비밀번호의 의미가 없다.
    assert len(passwords) > 190


def test_request_has_generic_response_and_mails_only_active_registered_email(
    client: TestClient,
    create_user: Callable[..., int],
    mail_sender: FakeMailSender,
) -> None:
    create_user("reset-request-user", email="Reset.User@example.com")
    create_user("reset-inactive-user", email="inactive@example.com", is_active=False)

    responses = [
        client.post("/api/v1/auth/password-reset/request", json={"email": email})
        for email in (
            "reset.user@example.com",
            "unknown@example.com",
            "inactive@example.com",
        )
    ]

    assert {response.status_code for response in responses} == {200}
    # 가입 여부·활성 여부를 응답으로 구분할 수 없어야 한다. 본문은 문구 하나뿐이다.
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert set(responses[0].json()) == {"message"}
    assert len(mail_sender.sent) == 1
    to_email, temporary_password = mail_sender.sent[0]
    assert to_email == "Reset.User@example.com"
    validate_new_password(temporary_password)


def test_mailed_temporary_password_replaces_the_old_one_and_its_sessions(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
    mail_sender: FakeMailSender,
) -> None:
    user_id = create_user("temp-login-user", email="temp-login@example.com")
    signed_in = client.post(
        "/api/v1/auth/login",
        json={"email": "temp-login@example.com", "password": OLD_PASSWORD},
    )
    assert signed_in.status_code == 200
    access_token = signed_in.json()["access_token"]

    client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "temp-login@example.com"},
    )
    _, temporary_password = mail_sender.sent[0]

    with_temporary = client.post(
        "/api/v1/auth/login",
        json={"email": "temp-login@example.com", "password": temporary_password},
    )
    with_old = client.post(
        "/api/v1/auth/login",
        json={"email": "temp-login@example.com", "password": OLD_PASSWORD},
    )

    assert with_temporary.status_code == 200
    assert with_temporary.json()["access_token"]
    assert with_old.status_code == 401
    # 발급 전에 로그인해 둔 세션도 끊긴다.
    assert client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    ).status_code == 401
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.password_change_history"
                " WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 1


def test_failed_delivery_leaves_the_old_password_working(
    database_url: str,
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    """보내지 못했으면 바꾸지도 않아야 한다.

    먼저 바꾸고 나서 발송이 실패하면 아무도 모르는 값으로 계정이 잠긴다.
    """

    user_id = create_user("mail-failure-user", email="mail-failure@example.com")
    before = _password_hash(engine, user_id)

    settings = Settings(database_url=database_url, jwt_secret=JWT_SECRET)
    with TestClient(
        create_app(settings, mail_sender=FakeMailSender(fail=True))
    ) as client:
        response = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "mail-failure@example.com"},
        )
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "mail-failure@example.com", "password": OLD_PASSWORD},
        )

    assert response.status_code == 200
    assert login.status_code == 200
    assert _password_hash(engine, user_id) == before
    assert verify_password(OLD_PASSWORD, before)


def test_mail_problems_are_hidden_from_the_screen(
    database_url: str,
    create_user: Callable[..., int],
) -> None:
    """메일이 나가지 않아도 화면에는 드러내지 않는다.

    가입 여부를 숨기는 것과 같은 이유다. 발송 실패든 설정 누락이든 응답이
    같아야 이메일을 넣어보며 무언가를 알아낼 수 없다. 운영자는 서버 로그로
    확인한다.
    """

    settings = Settings(database_url=database_url, jwt_secret=JWT_SECRET)
    create_user("smtp-failure-user", email="smtp-failure@example.com")

    # 발송이 실패하는 경우
    with TestClient(
        create_app(settings, mail_sender=FakeMailSender(fail=True))
    ) as client:
        failed = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "smtp-failure@example.com"},
        )

    # 메일 기능이 아예 설정돼 있지 않은 경우
    unconfigured_settings = Settings(
        _env_file=None,
        database_url=database_url,
        jwt_secret=JWT_SECRET,
    )
    with TestClient(create_app(unconfigured_settings)) as client:
        unconfigured = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "smtp-failure@example.com"},
        )

    assert failed.status_code == unconfigured.status_code == 200
    assert failed.json() == unconfigured.json()
    assert "smtp-failure@example.com" not in failed.text


def test_request_rate_limit_returns_retry_after(
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


def test_rate_limiter_applies_request_limit_to_each_email_across_ips() -> None:
    limiter = ResetRateLimiter()
    assert all(
        limiter.allow_request(f"198.51.100.{index}", "same@example.com")
        for index in range(5)
    )
    assert not limiter.allow_request("198.51.100.99", "same@example.com")


def test_request_validation_does_not_echo_the_address(client: TestClient) -> None:
    address = "not-an-email-" + ("x" * 300)
    response = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": address},
    )
    assert response.status_code == 422
    assert address not in response.text


def test_smtp_sender_uses_verified_ssl_and_carries_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    ).send_temporary_password("to@example.com", "Ab3!xY7qWz2#")
    assert seen["host"] == "smtp.example.com"
    assert seen["port"] == 465
    context = seen["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    message = seen["message"]
    assert message["From"] == "from@example.com"
    assert message["To"] == "to@example.com"
    content = message.get_content()
    assert "Ab3!xY7qWz2#" in content
    # 이 메일이 나간 뒤에는 기존 비밀번호를 못 쓴다. 무시하라고 안내하면 안 된다.
    assert "무시" not in content
