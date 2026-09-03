import asyncio
import json
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings
from app.core.security import hash_password
from app.infrastructure.local_object_storage import LocalObjectStorage
from app.schemas.chat import ChatAnswer
from app.services.chat import (
    ChatGenerationError,
    answer_chat,
    get_chat_history,
)
from app.services.reporting import finalize_report
from main import create_app
from tests.test_report import (
    FakePdfRenderer,
    cpl_result,
    seed_reporting_case,
)


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"


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
def users(
    engine: Engine,
    global_seed_cleanup: dict[str, list[int]],
) -> Iterator[Callable[[str], int]]:
    ids: list[int] = []

    def create(login_id: str) -> int:
        with engine.begin() as connection:
            user_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.app_user (login_id, email, password_hash)
                    VALUES (:login_id, :email, :password_hash)
                    RETURNING id
                    """
                ),
                {
                    "login_id": login_id,
                    "email": f"{login_id}@example.com",
                    "password_hash": hash_password(PASSWORD),
                },
            )
        assert user_id is not None
        ids.append(user_id)
        return user_id

    yield create
    with engine.begin() as connection:
        for user_id in ids:
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


class FakeChatLLM:
    def __init__(self, response: ChatAnswer | None = None) -> None:
        self.response = response or ChatAnswer(answer="분석 결과에 표시된 내용입니다.", evidence_refs=[])
        self.calls: list[dict] = []

    async def generate_structured(self, **kwargs):
        assert kwargs["task_name"] == "result_grounded_chat"
        assert kwargs["response_schema"] is ChatAnswer
        self.calls.append(json.loads(kwargs["messages"][1].content))
        return self.response


def runtime(database_url: str, storage_root: Path) -> Settings:
    return Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=storage_root,
    )


def ready_case(
    engine: Engine,
    database_url: str,
    owner_user_id: int,
    storage_root: Path,
    global_seed_cleanup: dict[str, list[int]],
) -> int:
    case_id, missing_id, retrieval_id = seed_reporting_case(
        engine, owner_user_id, cleanup=global_seed_cleanup
    )
    asyncio.run(
        finalize_report(
            engine,
            LocalObjectStorage(storage_root),
            FakePdfRenderer(),
            runtime(database_url, storage_root),
            case_id=case_id,
            missing_check_run_id=missing_id,
            retrieval_run_id=retrieval_id,
            cpl_result=cpl_result(),
            fit_result=None,
            sim_results=[],
            expected_candidate_count=0,
        )
    )
    return case_id


def bearer(client: TestClient, login_id: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": f"{login_id}@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_chat_history_and_answer_are_stored_in_sequence(
    database_url: str,
    engine: Engine,
    users: Callable[[str], int],
    global_seed_cleanup: dict[str, list[int]],
    tmp_path: Path,
) -> None:
    owner_id = users(f"chat-service-{uuid.uuid4().hex[:8]}")
    case_id = ready_case(
        engine, database_url, owner_id, tmp_path, global_seed_cleanup
    )
    llm = FakeChatLLM()
    settings = runtime(database_url, tmp_path)

    before = get_chat_history(engine, owner_id, case_id)
    assert before.messages == []
    assert before.next_cursor is None

    turn = asyncio.run(
        answer_chat(
            engine,
            llm,
            settings,
            owner_user_id=owner_id,
            case_id=case_id,
            question="왜 검토가 필요한가요?",
        )
    )
    assert turn.user_message.role == "USER"
    assert turn.assistant_message.role == "ASSISTANT"
    # 모델명·토큰 수·근거 식별자는 내부 정보라 응답에 담지 않는다.
    assert set(turn.model_dump()) == {"user_message", "assistant_message"}
    assert set(turn.user_message.model_dump()) == {
        "id",
        "role",
        "content",
    }
    assert llm.calls[0]["report_json"]["schema_version"] == "alpha-report-v0.1"

    history = get_chat_history(engine, owner_id, case_id)
    assert [item.role for item in history.messages] == ["USER", "ASSISTANT"]
    assert history.next_cursor is None


def test_chat_api_is_owner_scoped_and_rejects_unready_case(
    database_url: str,
    engine: Engine,
    users: Callable[[str], int],
    global_seed_cleanup: dict[str, list[int]],
    tmp_path: Path,
) -> None:
    marker = uuid.uuid4().hex[:8]
    owner_login = f"chat-owner-{marker}"
    other_login = f"chat-other-{marker}"
    owner_id = users(owner_login)
    other_id = users(other_login)
    case_id = ready_case(
        engine, database_url, owner_id, tmp_path, global_seed_cleanup
    )
    llm = FakeChatLLM()
    settings = runtime(database_url, tmp_path)

    with TestClient(create_app(settings, llm_client=llm)) as client:
        owner_headers = bearer(client, owner_login)
        other_headers = bearer(client, other_login)
        assert client.get(
            f"/api/v1/cases/{case_id}/messages",
            headers=owner_headers,
        ).status_code == 200
        answer = client.post(
            f"/api/v1/cases/{case_id}/messages",
            headers=owner_headers,
            json={"content": "이 결과를 설명해줘"},
        )
        assert answer.status_code == 200
        body = answer.json()
        # 응답에는 말풍선 두 개만 담는다.
        assert set(body) == {"user_message", "assistant_message"}
        assert body["user_message"]["role"] == "USER"
        assert body["assistant_message"]["role"] == "ASSISTANT"
        assert client.get(
            f"/api/v1/cases/{case_id}/messages",
            headers=other_headers,
        ).status_code == 404
        assert client.post(
            f"/api/v1/cases/{case_id}/messages",
            headers=other_headers,
            json={"content": "남의 결과"},
        ).status_code == 404

        with engine.begin() as connection:
            pending_id = connection.scalar(
                text(
                    "INSERT INTO sims.inspection_case (owner_user_id, status) "
                    "VALUES (:owner_id, 'PARSING') RETURNING id"
                ),
                {"owner_id": other_id},
            )
        assert client.get(
            f"/api/v1/cases/{pending_id}/messages",
            headers=other_headers,
        ).status_code == 409
        assert client.post(
            f"/api/v1/cases/{pending_id}/messages",
            headers=other_headers,
            json={"content": "아직 안 끝났나요?"},
        ).status_code == 409


def test_chat_provider_failures_do_not_persist_messages(
    database_url: str,
    engine: Engine,
    users: Callable[[str], int],
    global_seed_cleanup: dict[str, list[int]],
    tmp_path: Path,
) -> None:
    owner_id = users(f"chat-error-{uuid.uuid4().hex[:8]}")
    case_id = ready_case(
        engine, database_url, owner_id, tmp_path, global_seed_cleanup
    )
    settings = runtime(database_url, tmp_path)

    with pytest.raises(ChatGenerationError) as error:
        asyncio.run(
            answer_chat(
                engine,
                None,
                settings,
                owner_user_id=owner_id,
                case_id=case_id,
                question="답변해줘",
            )
        )
    assert error.value.code == "LLM_UNAVAILABLE"
    history = get_chat_history(engine, owner_id, case_id)
    assert history.messages == []


def test_chat_unknown_evidence_reference_is_rejected(
    database_url: str,
    engine: Engine,
    users: Callable[[str], int],
    global_seed_cleanup: dict[str, list[int]],
    tmp_path: Path,
) -> None:
    owner_id = users(f"chat-grounding-{uuid.uuid4().hex[:8]}")
    case_id = ready_case(
        engine, database_url, owner_id, tmp_path, global_seed_cleanup
    )
    llm = FakeChatLLM(
        ChatAnswer(answer="근거가 있다고 답합니다.", evidence_refs=["invented:1"])
    )

    with pytest.raises(ChatGenerationError) as error:
        asyncio.run(
            answer_chat(
                engine,
                llm,
                runtime(database_url, tmp_path),
                owner_user_id=owner_id,
                case_id=case_id,
                question="근거를 보여줘",
            )
        )
    assert error.value.code == "LLM_INVALID_RESPONSE"
    assert get_chat_history(engine, owner_id, case_id).messages == []
