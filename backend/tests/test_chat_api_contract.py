"""Contract tests for the cookie-authenticated asynchronous chat boundary."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.conversations import router
from app.ports.conversations import ConversationMessageRecord


OWNER_ID = "11111111-1111-1111-1111-111111111111"
CASE_ID = "22222222-2222-2222-2222-222222222222"
SESSION_ID = "33333333-3333-3333-3333-333333333333"
USER_MESSAGE_ID = "44444444-4444-4444-4444-444444444444"
ASSISTANT_MESSAGE_ID = "55555555-5555-5555-5555-555555555555"
ORIGIN = "http://frontend.test"
NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class FakeConversationRepository:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.retried: list[tuple[str, str, str]] = []

    async def create_message(self, *, owner_id: str, analysis_case_id: str, content: str):
        self.created.append((owner_id, analysis_case_id, content))
        return type(
            "Turn",
            (),
            {
                "user_message_id": USER_MESSAGE_ID,
                "assistant_message_id": ASSISTANT_MESSAGE_ID,
                "analysis_session_id": SESSION_ID,
                "status": "generating",
                "retry_count": 0,
            },
        )()

    async def list_messages(
        self,
        *,
        owner_id: str,
        analysis_case_id: str,
        limit: int = 50,
        updated_since: datetime | None = None,
    ) -> list[ConversationMessageRecord]:
        assert owner_id == OWNER_ID
        assert analysis_case_id == CASE_ID
        assert limit == 2
        self.updated_since = updated_since
        return [
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

    async def retry_message(
        self, *, owner_id: str, analysis_case_id: str, assistant_message_id: str
    ):
        self.retried.append((owner_id, analysis_case_id, assistant_message_id))
        return type(
            "Turn",
            (),
            {
                "user_message_id": USER_MESSAGE_ID,
                "assistant_message_id": ASSISTANT_MESSAGE_ID,
                "analysis_session_id": SESSION_ID,
                "status": "generating",
                "retry_count": 1,
            },
        )()


def _client() -> tuple[TestClient, FakeConversationRepository]:
    app = FastAPI()
    app.state.offline_mode = True
    app.state.auth_allowed_origins = frozenset({ORIGIN})
    repository = FakeConversationRepository()
    app.state.conversation_repository = repository
    app.include_router(router, prefix="/api/v1")
    return TestClient(app), repository


def _headers() -> dict[str, str]:
    return {"X-PreReview-Dev-User": OWNER_ID, "Origin": ORIGIN}


def test_post_creates_user_and_generating_assistant_ids() -> None:
    client, repository = _client()
    response = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages",
        headers=_headers(),
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
    assert repository.created == [(OWNER_ID, CASE_ID, "지원 대상이 무엇인가요?")]


def test_get_returns_ordered_messages_and_null_for_generating_content() -> None:
    client, _repository = _client()
    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages?limit=2",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert [item["sequence_no"] for item in response.json()] == [1, 2]
    assert response.json()[1]["content"] is None


def test_updated_since_reaches_the_repository_as_an_aware_datetime() -> None:
    """폴링 델타 커서가 그대로 전달돼야 한다.

    이게 끊기면 폴링이 매번 대화 전체를 다시 받는다 — 조용히 느려질 뿐
    기능은 멀쩡해 보이므로 테스트로 고정한다.
    """

    client, repository = _client()
    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages"
        "?limit=2&updated_since=2026-09-10T04:05:06Z",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert repository.updated_since == datetime(
        2026, 9, 10, 4, 5, 6, tzinfo=timezone.utc
    )


def test_updated_since_is_optional_and_defaults_to_the_whole_conversation() -> None:
    client, repository = _client()
    response = client.get(
        f"/api/v1/analysis-cases/{CASE_ID}/messages?limit=2", headers=_headers()
    )
    assert response.status_code == 200
    assert repository.updated_since is None


def test_list_sql_filters_on_updated_at_not_sequence_no() -> None:
    """generating -> completed 는 sequence_no 를 안 바꾼다.

    sequence_no 커서로 좁히면 답변 완료를 영영 못 받는다. 커서 컬럼이
    updated_at 인 것이 이 델타의 전제다.
    """

    from app.infrastructure.postgres_conversations import _LIST_SQL

    assert "m.updated_at >= COALESCE(%s::timestamptz" in _LIST_SQL


def test_retry_is_case_fenced_and_returns_accepted_generating_state() -> None:
    client, repository = _client()
    response = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages/{ASSISTANT_MESSAGE_ID}/retry",
        headers=_headers(),
    )
    assert response.status_code == 202
    assert response.json()["status"] == "generating"
    assert repository.retried == [(OWNER_ID, CASE_ID, ASSISTANT_MESSAGE_ID)]


def test_state_changing_chat_requests_require_trusted_origin() -> None:
    client, _repository = _client()
    response = client.post(
        f"/api/v1/analysis-cases/{CASE_ID}/messages",
        headers={"X-PreReview-Dev-User": OWNER_ID},
        json={"content": "질문"},
    )
    assert response.status_code == 403

