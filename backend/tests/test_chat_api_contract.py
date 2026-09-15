"""Contract tests for the cookie-authenticated asynchronous chat boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.cursor import encode_cursor
from app.ports.conversations import (
    ConversationIdempotencyKeyConflict,
    ConversationMessagePage,
    ConversationMessageRecord,
    ConversationNotFound,
    ConversationRetryExhausted,
)
from main import create_app


OWNER_ID = "11111111-1111-1111-1111-111111111111"
CASE_ID = "22222222-2222-2222-2222-222222222222"
SESSION_ID = "33333333-3333-3333-3333-333333333333"
USER_MESSAGE_ID = "44444444-4444-4444-4444-444444444444"
ASSISTANT_MESSAGE_ID = "55555555-5555-5555-5555-555555555555"
OTHER_OWNER_ID = "66666666-6666-6666-6666-666666666666"
ORIGIN = "http://frontend.test"
NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)
CURSOR_SECRET = "test-cursor-signing-secret"


def _turn(**overrides: object) -> object:
    fields = {
        "user_message_id": USER_MESSAGE_ID,
        "assistant_message_id": ASSISTANT_MESSAGE_ID,
        "analysis_session_id": SESSION_ID,
        "status": "generating",
        "retry_count": 0,
    }
    fields.update(overrides)
    return type("Turn", (), fields)()


class FakeConversationRepository:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str]] = []
        self.retried: list[tuple[str, str, str, str]] = []
        self.used_idempotency_keys: dict[str, tuple[str, str]] = {}
        self.retry_count = 1
        self.list_cursor: tuple[int, str] | None = None
        self.messages: dict[str, ConversationMessageRecord] = {
            ASSISTANT_MESSAGE_ID: ConversationMessageRecord(
                message_id=ASSISTANT_MESSAGE_ID,
                analysis_case_id=CASE_ID,
                role="assistant",
                sequence_no=2,
                content=None,
                status="generating",
                reply_to_message_id=USER_MESSAGE_ID,
                retry_count=0,
                error_code=None,
                error_message=None,
                created_at=NOW,
                updated_at=NOW,
            )
        }

    async def create_message(
        self, *, owner_id: str, analysis_case_id: str, content: str, idempotency_key: str
    ):
        if idempotency_key in self.used_idempotency_keys:
            used_content, used_case = self.used_idempotency_keys[idempotency_key]
            if used_content != content or used_case != analysis_case_id:
                raise ConversationIdempotencyKeyConflict("reused with different input")
            return _turn()
        self.used_idempotency_keys[idempotency_key] = (content, analysis_case_id)
        self.created.append((owner_id, analysis_case_id, content, idempotency_key))
        return _turn()

    async def list_messages(
        self,
        *,
        owner_id: str,
        analysis_case_id: str,
        limit: int = 50,
        cursor: tuple[int, str] | None = None,
    ) -> ConversationMessagePage:
        assert owner_id == OWNER_ID
        assert analysis_case_id == CASE_ID
        assert limit == 2
        self.list_cursor = cursor
        items = [
            ConversationMessageRecord(
                message_id=USER_MESSAGE_ID,
                analysis_case_id=CASE_ID,
                role="user",
                sequence_no=1,
                content="지원 대상이 무엇인가요?",
                status="completed",
                reply_to_message_id=None,
                retry_count=0,
                error_code=None,
                error_message=None,
                created_at=NOW,
                updated_at=NOW,
            ),
            ConversationMessageRecord(
                message_id=ASSISTANT_MESSAGE_ID,
                analysis_case_id=CASE_ID,
                role="assistant",
                sequence_no=2,
                content=None,
                status="generating",
                reply_to_message_id=USER_MESSAGE_ID,
                retry_count=0,
                error_code=None,
                error_message=None,
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
        return ConversationMessagePage(items=items, next_cursor=None)

    async def get_message(
        self, *, owner_id: str, analysis_case_id: str, message_id: str
    ) -> ConversationMessageRecord:
        if owner_id != OWNER_ID or analysis_case_id != CASE_ID:
            raise ConversationNotFound("not visible")
        record = self.messages.get(message_id)
        if record is None:
            raise ConversationNotFound("not found")
        return record

    async def retry_message(
        self,
        *,
        owner_id: str,
        analysis_case_id: str,
        assistant_message_id: str,
        idempotency_key: str,
    ):
        if idempotency_key in self.used_idempotency_keys:
            used_content, used_target = self.used_idempotency_keys[idempotency_key]
            if used_target != assistant_message_id:
                raise ConversationIdempotencyKeyConflict("reused with different input")
            return _turn(retry_count=self.retry_count)
        self.used_idempotency_keys[idempotency_key] = ("__retry__", assistant_message_id)
        self.retried.append((owner_id, analysis_case_id, assistant_message_id, idempotency_key))
        return _turn(retry_count=self.retry_count)


def _client() -> tuple[TestClient, FakeConversationRepository]:
    app = create_app()
    app.state.offline_mode = True
    app.state.auth_allowed_origins = frozenset({ORIGIN})
    app.state.cursor_signing_secret = CURSOR_SECRET
    repository = FakeConversationRepository()
    app.state.conversation_repository = repository
    return TestClient(app), repository


def _headers(idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"X-PreReview-Dev-User": OWNER_ID, "Origin": ORIGIN}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def test_post_creates_user_and_generating_assistant_ids() -> None:
    client, repository = _client()
    key = str(uuid4())
    response = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages",
        headers=_headers(key),
        json={"content": "지원 대상이 무엇인가요?"},
    )
    assert response.status_code == 202
    assert response.json() == {
        "user_message_id": USER_MESSAGE_ID,
        "assistant_message_id": ASSISTANT_MESSAGE_ID,
        "analysis_session_id": SESSION_ID,
        "status": "generating",
        "retry_count": 0,
    }
    assert repository.created == [(OWNER_ID, CASE_ID, "지원 대상이 무엇인가요?", key)]


def test_post_requires_idempotency_key_header() -> None:
    client, _repository = _client()
    response = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages",
        headers=_headers(),
        json={"content": "질문"},
    )
    assert response.status_code == 422


def test_same_key_same_body_replays_without_a_second_create() -> None:
    client, repository = _client()
    key = str(uuid4())
    body = {"content": "지원 대상이 무엇인가요?"}
    first = client.post(f"/api/v1/analysis-cases/{CASE_ID}/messages", headers=_headers(key), json=body)
    second = client.post(f"/api/v1/analysis-cases/{CASE_ID}/messages", headers=_headers(key), json=body)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    assert len(repository.created) == 1


def test_same_key_different_body_is_idempotency_key_conflict() -> None:
    client, _repository = _client()
    key = str(uuid4())
    first = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages",
        headers=_headers(key),
        json={"content": "지원 대상이 무엇인가요?"},
    )
    second = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages",
        headers=_headers(key),
        json={"content": "다른 질문입니다"},
    )
    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_get_single_message_polls_exact_status() -> None:
    client, _repository = _client()
    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages/{ASSISTANT_MESSAGE_ID}",
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["message_id"] == ASSISTANT_MESSAGE_ID
    assert body["status"] == "generating"
    assert body["content"] is None


def test_get_single_message_is_404_for_a_foreign_or_unknown_id() -> None:
    client, _repository = _client()
    missing = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages/{uuid4()}",
        headers=_headers(),
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


def test_get_returns_envelope_in_chronological_order() -> None:
    client, _repository = _client()
    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages?limit=2",
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["sequence_no"] for item in body["items"]] == [1, 2]
    assert body["items"][1]["content"] is None
    assert body["next_cursor"] is None


def test_message_history_requires_cursor_secret_even_for_one_page() -> None:
    client, _repository = _client()
    client.app.state.cursor_signing_secret = ""  # type: ignore[attr-defined]

    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages",
        headers=_headers(),
    )

    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"


def test_list_history_cursor_round_trips_and_updated_since_is_gone() -> None:
    client, repository = _client()
    cursor = encode_cursor(
        secret=CURSOR_SECRET,
        endpoint="analysis-case-messages",
        scope=f"{OWNER_ID}:{CASE_ID}",
        version=1,
        fields={"sequence_no": 1, "message_id": USER_MESSAGE_ID},
    )
    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages?limit=2&cursor={cursor}",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert repository.list_cursor == (1, USER_MESSAGE_ID)

    # updated_since is removed from the new public contract; a caller that
    # still sends it must not silently be honored as a filter.
    rejected = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages?limit=2&updated_since=2026-09-10T04:05:06Z",
        headers=_headers(),
    )
    assert rejected.status_code == 200  # unknown query params are just ignored, not filtered on
    assert rejected.json()["items"][0]["sequence_no"] == 1


def test_message_cursor_from_a_different_case_is_rejected() -> None:
    client, _repository = _client()
    other_case = str(uuid4())
    cursor = encode_cursor(
        secret=CURSOR_SECRET,
        endpoint="analysis-case-messages",
        scope=f"{OWNER_ID}:{other_case}",
        version=1,
        fields={"sequence_no": 1, "message_id": USER_MESSAGE_ID},
    )
    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages?cursor={cursor}",
        headers=_headers(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_message_cursor_rejects_signed_but_wrongly_typed_fields() -> None:
    client, repository = _client()
    cursor = encode_cursor(
        secret=CURSOR_SECRET,
        endpoint="analysis-case-messages",
        scope=f"{OWNER_ID}:{CASE_ID}",
        version=1,
        fields={"sequence_no": "1", "message_id": USER_MESSAGE_ID},
    )

    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages?cursor={cursor}",
        headers=_headers(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert repository.list_cursor is None


def test_retry_requires_idempotency_key_and_is_case_fenced() -> None:
    client, repository = _client()
    key = str(uuid4())
    missing_key = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages/{ASSISTANT_MESSAGE_ID}/retry",
        headers=_headers(),
    )
    assert missing_key.status_code == 422

    response = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages/{ASSISTANT_MESSAGE_ID}/retry",
        headers=_headers(key),
    )
    assert response.status_code == 202
    assert response.json()["status"] == "generating"
    assert repository.retried == [(OWNER_ID, CASE_ID, ASSISTANT_MESSAGE_ID, key)]


def test_retry_total_of_three_and_its_idempotency_replay_are_accepted() -> None:
    """One automatic plus two manual retries is a valid public total.

    This guards the response-model boundary: a valid DB idempotency replay at
    total=3 must remain a 202 response instead of Pydantic causing a 500.
    """

    client, repository = _client()
    repository.retry_count = 3
    key = str(uuid4())
    path = f"/api/v1/analysis-cases/{CASE_ID}/messages/{ASSISTANT_MESSAGE_ID}/retry"

    first = client.post(path, headers=_headers(key))
    replay = client.post(path, headers=_headers(key))

    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json() == replay.json()
    assert replay.json()["retry_count"] == 3
    assert repository.retried == [(OWNER_ID, CASE_ID, ASSISTANT_MESSAGE_ID, key)]


def test_openapi_turn_retry_count_maximum_is_combined_total_of_three() -> None:
    schema = create_app().openapi()
    for response_name in ("ConversationTurnResponse", "ConversationMessageResponse"):
        retry_count = schema["components"]["schemas"][response_name]["properties"][
            "retry_count"
        ]
        assert retry_count["minimum"] == 0
        assert retry_count["maximum"] == 3


def test_retry_exhausted_is_chat_retry_exhausted_conflict() -> None:
    client, repository = _client()

    async def exhausted(*, owner_id, analysis_case_id, assistant_message_id, idempotency_key):
        raise ConversationRetryExhausted("no retries left")

    repository.retry_message = exhausted  # type: ignore[method-assign]
    response = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages/{ASSISTANT_MESSAGE_ID}/retry",
        headers=_headers(str(uuid4())),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "CHAT_RETRY_EXHAUSTED"


def test_state_changing_chat_requests_require_trusted_origin() -> None:
    client, _repository = _client()
    response = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages",
        headers={"X-PreReview-Dev-User": OWNER_ID, "Idempotency-Key": str(uuid4())},
        json={"content": "질문"},
    )
    assert response.status_code == 403
