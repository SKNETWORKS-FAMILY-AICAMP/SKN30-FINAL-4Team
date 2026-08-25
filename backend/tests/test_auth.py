import os
import time
from collections.abc import Callable, Iterator

import jwt
import pytest
from fastapi.testclient import TestClient
from jwt import (
    ExpiredSignatureError,
    InvalidAlgorithmError,
    InvalidSignatureError,
    MissingRequiredClaimError,
)
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, text

from app.api.v1.auth import ChangePasswordRequest
from app.core.config import Settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from main import create_app


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
OLD_PASSWORD = "correct-horse"
NEW_PASSWORD = "new-correct-horse"


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


@pytest.fixture(scope="module")
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, jwt_secret=JWT_SECRET)
    with TestClient(create_app(settings)) as value:
        yield value


@pytest.fixture
def create_user(engine: Engine) -> Iterator[Callable[..., int]]:
    user_ids: list[int] = []

    def create(
        login_id: str,
        password: str = OLD_PASSWORD,
        *,
        is_active: bool = True,
        password_hash: str | None = None,
    ) -> int:
        with engine.begin() as connection:
            user_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.app_user (
                        login_id, email, password_hash, is_active
                    )
                    VALUES (
                        :login_id, :email, :password_hash, :is_active
                    )
                    RETURNING id
                    """
                ),
                {
                    "login_id": login_id,
                    "email": f"{login_id}@example.com",
                    "password_hash": password_hash or hash_password(password),
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


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, login_id: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"login_id": login_id, "password": password},
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    return response.json()["access_token"]


def test_password_hash_is_argon2id_salted_and_verifiable() -> None:
    first_hash = hash_password(OLD_PASSWORD)
    second_hash = hash_password(OLD_PASSWORD)

    assert first_hash.startswith("$argon2id$")
    assert first_hash != second_hash
    assert verify_password(OLD_PASSWORD, first_hash)
    assert not verify_password("wrong-password", first_hash)
    assert not verify_password(OLD_PASSWORD, "broken-hash")


def test_access_token_contract_and_validation() -> None:
    token = create_access_token(7, JWT_SECRET, 1800)
    claims = decode_access_token(token, JWT_SECRET)

    assert claims["sub"] == "7"
    assert isinstance(claims["iat"], float)
    assert claims["exp"] - claims["iat"] == 1800
    assert set(claims) == {"sub", "iat", "exp"}

    with pytest.raises(InvalidSignatureError):
        decode_access_token(token, "different-secret-that-is-32-bytes")

    expired = jwt.encode(
        {"sub": "7", "iat": time.time() - 2, "exp": time.time() - 1},
        JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(ExpiredSignatureError):
        decode_access_token(expired, JWT_SECRET)

    wrong_algorithm = jwt.encode(
        {"sub": "7", "iat": time.time(), "exp": time.time() + 60},
        "s" * 64,
        algorithm="HS384",
    )
    with pytest.raises(InvalidAlgorithmError):
        decode_access_token(wrong_algorithm, JWT_SECRET)

    missing_expiration = jwt.encode(
        {"sub": "7", "iat": time.time()},
        JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(MissingRequiredClaimError):
        decode_access_token(missing_expiration, JWT_SECRET)


@pytest.mark.parametrize(
    ("secret", "expire_minutes"),
    [("short", 30), (JWT_SECRET, 0)],
)
def test_invalid_jwt_settings_are_rejected(
    secret: str,
    expire_minutes: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://postgres:test@127.0.0.1:1/sims",
            jwt_secret=secret,
            jwt_access_token_expire_minutes=expire_minutes,
        )


def test_invalid_jwt_secret_is_hidden_from_settings_error() -> None:
    exposed_secret = "must-not-appear"
    with pytest.raises(ValidationError) as error:
        Settings(
            database_url="postgresql+psycopg://postgres:test@127.0.0.1:1/sims",
            jwt_secret=exposed_secret,
        )

    assert exposed_secret not in str(error.value)


@pytest.mark.parametrize(
    ("length", "valid"),
    [(11, False), (12, True), (128, True), (129, False)],
)
def test_new_password_length_contract(length: int, valid: bool) -> None:
    if valid:
        ChangePasswordRequest(current_password="current", new_password="x" * length)
    else:
        with pytest.raises(ValidationError):
            ChangePasswordRequest(
                current_password="current",
                new_password="x" * length,
            )


def test_login_is_case_insensitive_and_updates_last_login(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    user_id = create_user("Demo-Analyst")
    token = login(client, "demo-analyst", OLD_PASSWORD)

    assert decode_access_token(token, JWT_SECRET)["sub"] == str(user_id)
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT last_login_at FROM sims.app_user WHERE id = :user_id"),
            {"user_id": user_id},
        ) is not None


def test_login_failures_are_indistinguishable_and_do_not_update_last_login(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    active_id = create_user("wrong-password-user")
    inactive_id = create_user("inactive-user", is_active=False)
    requests = [
        {"login_id": "wrong-password-user", "password": "wrong-password"},
        {"login_id": "missing-user", "password": "wrong-password"},
        {"login_id": "inactive-user", "password": OLD_PASSWORD},
        {"login_id": "' OR 1=1 --", "password": "wrong-password"},
    ]

    responses = [client.post("/api/v1/auth/login", json=body) for body in requests]
    assert {response.status_code for response in responses} == {401}
    assert {response.text for response in responses} == {
        '{"detail":"Could not validate credentials"}'
    }
    with engine.connect() as connection:
        last_logins = connection.execute(
            text(
                """
                SELECT last_login_at
                FROM sims.app_user
                WHERE id IN (:active_id, :inactive_id)
                """
            ),
            {"active_id": active_id, "inactive_id": inactive_id},
        ).scalars()
        assert all(value is None for value in last_logins)


def test_invalid_login_password_is_not_echoed(client: TestClient) -> None:
    exposed_password = "must-not-appear-" + ("x" * 128)
    response = client.post(
        "/api/v1/auth/login",
        json={"login_id": "any-user", "password": exposed_password},
    )

    assert response.status_code == 422
    assert exposed_password not in response.text


def test_me_rejects_bad_tokens_and_returns_minimal_user(
    client: TestClient,
    create_user: Callable[..., int],
) -> None:
    user_id = create_user("me-user")
    token = login(client, "me-user", OLD_PASSWORD)

    response = client.get("/api/v1/auth/me", headers=bearer(token))
    assert response.status_code == 200
    assert response.json() == {"id": user_id, "login_id": "me-user"}

    missing = client.get("/api/v1/auth/me")
    wrong_scheme = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Basic {token}"},
    )
    tampered = client.get("/api/v1/auth/me", headers=bearer(token + "x"))
    for failure in (missing, wrong_scheme, tampered):
        assert failure.status_code == 401
        assert failure.headers["www-authenticate"] == "Bearer"


def test_token_timestamp_boundary_uses_fractional_seconds(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    user_id = create_user("time-boundary-user")
    with engine.connect() as connection:
        changed_at = connection.scalar(
            text(
                "SELECT password_changed_at FROM sims.app_user WHERE id = :user_id"
            ),
            {"user_id": user_id},
        )
    assert changed_at is not None
    timestamp = changed_at.timestamp()

    exact = jwt.encode(
        {"sub": str(user_id), "iat": timestamp, "exp": time.time() + 60},
        JWT_SECRET,
        algorithm="HS256",
    )
    older = jwt.encode(
        {"sub": str(user_id), "iat": timestamp - 0.001, "exp": time.time() + 60},
        JWT_SECRET,
        algorithm="HS256",
    )

    assert client.get("/api/v1/auth/me", headers=bearer(exact)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=bearer(older)).status_code == 401


def test_existing_token_is_rejected_after_user_is_deactivated(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    user_id = create_user("deactivated-token-user")
    token = login(client, "deactivated-token-user", OLD_PASSWORD)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE sims.app_user SET is_active = false WHERE id = :user_id"),
            {"user_id": user_id},
        )

    assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401


def test_password_change_is_atomic_and_invalidates_old_token(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    user_id = create_user("password-change-user")
    old_token = login(client, "password-change-user", OLD_PASSWORD)

    response = client.post(
        "/api/v1/auth/change-password",
        headers=bearer(old_token),
        json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 204
    assert response.content == b""
    assert client.get(
        "/api/v1/auth/me",
        headers=bearer(old_token),
    ).status_code == 401

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT u.password_hash,
                       (SELECT count(*) FROM sims.password_change_history h
                        WHERE h.user_id = u.id) AS history_count
                FROM sims.app_user u
                WHERE u.id = :user_id
                """
            ),
            {"user_id": user_id},
        ).mappings().one()
    assert verify_password(NEW_PASSWORD, row["password_hash"])
    assert row["history_count"] == 1

    failed_old_login = client.post(
        "/api/v1/auth/login",
        json={"login_id": "password-change-user", "password": OLD_PASSWORD},
    )
    assert failed_old_login.status_code == 401
    new_token = login(client, "password-change-user", NEW_PASSWORD)
    assert client.get(
        "/api/v1/auth/me",
        headers=bearer(new_token),
    ).status_code == 200


@pytest.mark.parametrize(
    ("current_password", "new_password"),
    [("wrong-password", NEW_PASSWORD), (OLD_PASSWORD, OLD_PASSWORD)],
)
def test_failed_password_change_leaves_no_history(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
    current_password: str,
    new_password: str,
) -> None:
    user_id = create_user(f"failed-change-{current_password}")
    token = login(client, f"failed-change-{current_password}", OLD_PASSWORD)

    response = client.post(
        "/api/v1/auth/change-password",
        headers=bearer(token),
        json={"current_password": current_password, "new_password": new_password},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Password change failed"}
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.password_change_history WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 0


@pytest.mark.parametrize("field", ["current_password", "new_password"])
def test_invalid_change_password_input_is_not_echoed(
    client: TestClient,
    create_user: Callable[..., int],
    field: str,
) -> None:
    create_user(f"validation-{field}")
    token = login(client, f"validation-{field}", OLD_PASSWORD)
    exposed_password = "must-not-appear-" + ("x" * 128)
    body = {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD}
    body[field] = exposed_password

    response = client.post(
        "/api/v1/auth/change-password",
        headers=bearer(token),
        json=body,
    )
    assert response.status_code == 422
    assert exposed_password not in response.text


def test_unicode_password_can_be_compared_and_changed(
    client: TestClient,
    engine: Engine,
    create_user: Callable[..., int],
) -> None:
    current_password = "가" * 12
    new_password = "나" * 12
    user_id = create_user("unicode-password-user", current_password)
    token = login(client, "unicode-password-user", current_password)

    unchanged = client.post(
        "/api/v1/auth/change-password",
        headers=bearer(token),
        json={
            "current_password": current_password,
            "new_password": current_password,
        },
    )
    assert unchanged.status_code == 400

    changed = client.post(
        "/api/v1/auth/change-password",
        headers=bearer(token),
        json={"current_password": current_password, "new_password": new_password},
    )
    assert changed.status_code == 204
    assert login(client, "unicode-password-user", new_password)
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM sims.password_change_history WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        ) == 1


def test_logout_is_stateless_client_token_disposal(
    client: TestClient,
    create_user: Callable[..., int],
) -> None:
    create_user("logout-user")
    token = login(client, "logout-user", OLD_PASSWORD)

    response = client.post("/api/v1/auth/logout", headers=bearer(token))
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 200


def test_malformed_stored_hash_is_a_generic_login_failure(
    client: TestClient,
    create_user: Callable[..., int],
) -> None:
    create_user("broken-hash-user", password_hash="broken-hash")

    response = client.post(
        "/api/v1/auth/login",
        json={"login_id": "broken-hash-user", "password": OLD_PASSWORD},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Could not validate credentials"}
