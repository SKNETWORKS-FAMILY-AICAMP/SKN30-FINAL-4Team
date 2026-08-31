"""Stateless, database-backed email password reset flow."""

import hashlib
import hmac
import logging
import math
import re
import secrets
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import jwt
from sqlalchemy import Engine, text

from app.core.password_policy import validate_new_password
from app.core.security import hash_password, verify_password
from app.ports.mail_sender import MailSender
from app.services.auth import PasswordUnchangedError


logger = logging.getLogger(__name__)

RESET_TOKEN_TTL_SECONDS = 10 * 60
RESET_TOKEN_AUDIENCE = "password-reset"
RESET_TOKEN_PURPOSE = "password-reset"
MAX_RESET_TOKEN_LENGTH = 4096
_RESET_KEY_CONTEXT = b"SIMS password reset signing key v1"
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z")
_EMAIL_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class InvalidEmailError(ValueError):
    pass


class InvalidResetTokenError(ValueError):
    pass


def normalize_email(email: str) -> str:
    """Validate a bounded plain address and return its case-folded form."""

    if not isinstance(email, str):
        raise InvalidEmailError("Invalid email address")
    value = email
    if (
        not 3 <= len(value) <= 254
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in value
        )
        or value.count("@") != 1
    ):
        raise InvalidEmailError("Invalid email address")

    local, domain = value.split("@")
    if (
        not local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or _EMAIL_LOCAL.fullmatch(local) is None
        or len(domain) > 253
    ):
        raise InvalidEmailError("Invalid email address")
    labels = domain.split(".")
    if not domain or any(_EMAIL_DOMAIN_LABEL.fullmatch(label) is None for label in labels):
        raise InvalidEmailError("Invalid email address")
    return value.casefold()


def _reset_signing_key(jwt_secret: str) -> bytes:
    if not isinstance(jwt_secret, str) or not jwt_secret:
        raise ValueError("JWT secret must not be empty")
    return hmac.new(
        jwt_secret.encode("utf-8"),
        _RESET_KEY_CONTEXT,
        hashlib.sha256,
    ).digest()


def _password_fingerprint(
    jwt_secret: str,
    password_changed_at: datetime,
    email: str,
) -> str:
    if password_changed_at.tzinfo is None:
        changed_at = password_changed_at.replace(tzinfo=timezone.utc)
    else:
        changed_at = password_changed_at.astimezone(timezone.utc)
    value = f"{changed_at.isoformat(timespec='microseconds')}\x00{email}".encode(
        "utf-8"
    )
    return hmac.new(_reset_signing_key(jwt_secret), value, hashlib.sha256).hexdigest()


def create_password_reset_token(
    user_id: int,
    email: str,
    password_changed_at: datetime,
    jwt_secret: str,
    *,
    now: float | None = None,
) -> str:
    issued_at = time.time() if now is None else now
    if not math.isfinite(issued_at):
        raise ValueError("Token timestamp must be finite")
    return jwt.encode(
        {
            "uid": str(user_id),
            "aud": RESET_TOKEN_AUDIENCE,
            "purpose": RESET_TOKEN_PURPOSE,
            "iat": issued_at,
            "exp": issued_at + RESET_TOKEN_TTL_SECONDS,
            "nonce": secrets.token_urlsafe(32),
            "fp": _password_fingerprint(jwt_secret, password_changed_at, email),
        },
        _reset_signing_key(jwt_secret),
        algorithm="HS256",
    )


def decode_password_reset_token(token: str, jwt_secret: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > MAX_RESET_TOKEN_LENGTH:
        raise InvalidResetTokenError("Invalid password reset token")
    try:
        claims = jwt.decode(
            token,
            _reset_signing_key(jwt_secret),
            algorithms=["HS256"],
            audience=RESET_TOKEN_AUDIENCE,
            options={
                "require": [
                    "uid",
                    "aud",
                    "purpose",
                    "iat",
                    "exp",
                    "nonce",
                    "fp",
                ]
            },
        )
    except (jwt.InvalidTokenError, TypeError, ValueError) as error:
        raise InvalidResetTokenError("Invalid password reset token") from error

    if "sub" in claims:
        raise InvalidResetTokenError("Invalid password reset token")
    uid = claims.get("uid")
    nonce = claims.get("nonce")
    fingerprint = claims.get("fp")
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if (
        not isinstance(uid, str)
        or not uid.isdigit()
        or uid == "0"
        or isinstance(issued_at, bool)
        or not isinstance(issued_at, (int, float))
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(float(issued_at))
        or not math.isfinite(float(expires_at))
        or expires_at <= issued_at
        or expires_at - issued_at > RESET_TOKEN_TTL_SECONDS
        or not isinstance(claims.get("aud"), str)
        or claims["aud"] != RESET_TOKEN_AUDIENCE
        or claims.get("purpose") != RESET_TOKEN_PURPOSE
        or not isinstance(nonce, str)
        or not 16 <= len(nonce) <= 256
        or not isinstance(fingerprint, str)
        or len(fingerprint) != hashlib.sha256().digest_size * 2
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise InvalidResetTokenError("Invalid password reset token")
    return claims


def build_password_reset_url(base_url: str, token: str) -> str:
    if not base_url or "?" in base_url or "#" in base_url:
        raise ValueError("Password reset URL must not contain query or fragment")
    return f"{base_url}#token={quote(token, safe='')}"


def issue_password_reset_email(
    engine: Engine,
    mail_sender: MailSender,
    password_reset_url: str,
    jwt_secret: str,
    email: str,
) -> None:
    """Look up and send in a background task; never expose task failures."""

    try:
        normalized_email = normalize_email(email)
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT id, email::text AS email, password_changed_at
                        FROM sims.app_user
                        WHERE email = :email AND is_active = true
                        """
                    ),
                    {"email": normalized_email},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return
        stored_email = str(row["email"])
        # Existing DB rows predate this endpoint; do not turn unsafe legacy data
        # into an SMTP header.
        normalize_email(stored_email)
        token = create_password_reset_token(
            row["id"],
            stored_email,
            row["password_changed_at"],
            jwt_secret,
        )
        mail_sender.send_password_reset(
            stored_email,
            build_password_reset_url(password_reset_url, token),
        )
    except Exception as error:
        logger.warning("Password reset email task failed: %s", type(error).__name__)


def confirm_password_reset(
    engine: Engine,
    token: str,
    new_password: str,
    jwt_secret: str,
) -> None:
    validate_new_password(new_password)
    claims = decode_password_reset_token(token, jwt_secret)
    user_id = int(claims["uid"])

    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT id, email::text AS email, password_hash, password_changed_at
                    FROM sims.app_user
                    WHERE id = :user_id AND is_active = true
                    FOR UPDATE
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise InvalidResetTokenError("Invalid password reset token")

        expires_at = float(claims["exp"])
        if time.time() >= expires_at:
            raise InvalidResetTokenError("Invalid password reset token")
        expected_fingerprint = _password_fingerprint(
            jwt_secret,
            row["password_changed_at"],
            str(row["email"]),
        )
        if not hmac.compare_digest(claims["fp"], expected_fingerprint):
            raise InvalidResetTokenError("Invalid password reset token")
        if verify_password(new_password, row["password_hash"]):
            raise PasswordUnchangedError

        connection.execute(
            text(
                """
                UPDATE sims.app_user
                SET password_hash = :password_hash
                WHERE id = :user_id
                """
            ),
            {"password_hash": hash_password(new_password), "user_id": user_id},
        )


class ResetRateLimiter:
    """Bounded per-process limiter: request 5/IP+email per 15m, confirm 10/IP."""

    REQUEST_LIMIT = 5
    CONFIRM_LIMIT = 10
    WINDOW_SECONDS = 15 * 60
    RETRY_AFTER_SECONDS = WINDOW_SECONDS
    MAX_KEYS = 4096

    def __init__(self) -> None:
        self._request_attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self._confirm_attempts: OrderedDict[str, deque[float]] = OrderedDict()
        import threading

        self._lock = threading.RLock()

    @staticmethod
    def _prune(attempts: deque[float], now: float) -> None:
        cutoff = now - ResetRateLimiter.WINDOW_SECONDS
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    @staticmethod
    def _prune_store(
        store: OrderedDict[str, deque[float]],
        now: float,
    ) -> None:
        for key, attempts in list(store.items()):
            ResetRateLimiter._prune(attempts, now)
            if not attempts:
                del store[key]

    def _bounded_put(
        self,
        store: OrderedDict[str, deque[float]],
        key: str,
        now: float,
    ) -> bool:
        attempts = store.get(key)
        if attempts is None:
            if len(store) >= self.MAX_KEYS:
                return False
            attempts = deque()
            store[key] = attempts
        store.move_to_end(key)
        attempts.append(now)
        return True

    def allow_request(self, client_ip: str, email: str) -> bool:
        normalized_email = normalize_email(email)
        now = time.monotonic()
        keys = (f"ip:{client_ip}", f"email:{normalized_email}")
        with self._lock:
            self._prune_store(self._request_attempts, now)
            attempts_by_key = []
            for key in keys:
                attempts = self._request_attempts.get(key)
                if attempts is None:
                    attempts = deque()
                self._prune(attempts, now)
                attempts_by_key.append((key, attempts))
                if len(attempts) >= self.REQUEST_LIMIT:
                    return False
            new_keys = sum(key not in self._request_attempts for key in keys)
            if len(self._request_attempts) + new_keys > self.MAX_KEYS:
                return False
            for key, _ in attempts_by_key:
                self._bounded_put(self._request_attempts, key, now)
        return True

    def allow_confirm(self, client_ip: str) -> bool:
        key = f"ip:{client_ip}"
        now = time.monotonic()
        with self._lock:
            self._prune_store(self._confirm_attempts, now)
            attempts = self._confirm_attempts.get(key)
            if attempts is None:
                attempts = deque()
            self._prune(attempts, now)
            if len(attempts) >= self.CONFIRM_LIMIT:
                return False
            return self._bounded_put(self._confirm_attempts, key, now)
