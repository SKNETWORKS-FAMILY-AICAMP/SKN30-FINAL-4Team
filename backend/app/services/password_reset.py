"""임시 비밀번호를 만들어 메일로 보내는 비밀번호 재설정."""

import logging
import re
import secrets
import time
from collections import OrderedDict, deque

from sqlalchemy import Engine, text

from app.core.password_policy import validate_new_password
from app.core.security import hash_password
from app.ports.mail_sender import MailSender


logger = logging.getLogger(__name__)

TEMPORARY_PASSWORD_LENGTH = 12
# 메일에 적힌 값을 사람이 그대로 옮겨 적는다. 0/O, 1/l/I 처럼 헷갈리는 글자는 뺀다.
_TEMPORARY_LETTERS = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
_TEMPORARY_DIGITS = "23456789"
_TEMPORARY_SYMBOLS = "!@#$%^&*"
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z")
_EMAIL_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class InvalidEmailError(ValueError):
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


def generate_temporary_password() -> str:
    """비밀번호 정책을 반드시 만족하는 임시 비밀번호를 만든다."""

    pools = (_TEMPORARY_LETTERS, _TEMPORARY_DIGITS, _TEMPORARY_SYMBOLS)
    # 종류별로 한 글자씩 먼저 넣어야 정책 검사를 확정적으로 통과한다.
    characters = [secrets.choice(pool) for pool in pools]
    everything = "".join(pools)
    characters += [
        secrets.choice(everything)
        for _ in range(TEMPORARY_PASSWORD_LENGTH - len(characters))
    ]
    secrets.SystemRandom().shuffle(characters)
    return validate_new_password("".join(characters))


def issue_temporary_password(
    engine: Engine,
    mail_sender: MailSender,
    email: str,
) -> None:
    """Look up, mail, then replace in a background task; never expose failures."""

    try:
        normalized_email = normalize_email(email)
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT id, email::text AS email
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

        temporary_password = generate_temporary_password()
        # 발송이 실패한 뒤에 비밀번호를 바꾸면 아무도 모르는 값으로 잠긴다.
        # 보내고 나서 바꾸면 최악이라도 기존 비밀번호가 그대로 살아 있다.
        mail_sender.send_temporary_password(stored_email, temporary_password)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE sims.app_user
                    SET password_hash = :password_hash
                    WHERE id = :user_id AND is_active = true
                    """
                ),
                {
                    "password_hash": hash_password(temporary_password),
                    "user_id": row["id"],
                },
            )
    except Exception as error:
        logger.warning("Temporary password task failed: %s", type(error).__name__)


class ResetRateLimiter:
    """Bounded per-process limiter: 15 분에 IP 당·이메일 당 5 회."""

    REQUEST_LIMIT = 5
    WINDOW_SECONDS = 15 * 60
    RETRY_AFTER_SECONDS = WINDOW_SECONDS
    MAX_KEYS = 4096

    def __init__(self) -> None:
        self._request_attempts: OrderedDict[str, deque[float]] = OrderedDict()
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
