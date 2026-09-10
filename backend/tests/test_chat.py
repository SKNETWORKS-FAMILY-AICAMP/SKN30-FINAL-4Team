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
from app.schemas.chat import ChatAnswer, ChatReference
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
        self.response = response or ChatAnswer(
            answer="분석 결과에 표시된 내용입니다.",
            references=[],
            suggested_revision=None,
        )
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


def public_id(engine: Engine, case_id: int) -> str:
    """외부 API 가 쓰는 UUID. 내부 PK 는 URL 에 나오지 않는다."""
    with engine.connect() as connection:
        value = connection.scalar(
            text(
                "SELECT analysis_case_id FROM sims.inspection_case WHERE id = :case_id"
            ),
            {"case_id": case_id},
        )
    assert value is not None
    return str(value)


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
    # 말풍선에는 모델명·토큰 수를 담지 않는다. 근거와 수정 제안은 말풍선에
    # 붙는다 — POST 응답과 GET 이력이 같은 모양이어야 한다.
    assert set(turn.model_dump()) == {"user_message", "assistant_message"}
    assert set(turn.user_message.model_dump()) == {
        "id",
        "role",
        "content",
        "references",
        "suggested_revision",
    }
    # 사용자 질문 행에는 근거도 제안도 붙지 않는다.
    assert turn.user_message.references == []
    assert turn.user_message.suggested_revision is None
    # 챗봇 Context 는 Agent·Model 결과 전부를 한 묶음으로 받는다.
    context = llm.calls[0]
    assert set(context["report"]) == {
        "cpl",
        "fit",
        "retrieval",
        "sim",
        "model1",
        "model2",
        "model3",
        "summary",
    }
    assert context["analysis_id"] == str(case_id)

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
    llm = FakeChatLLM(
        ChatAnswer(
            answer="FIT-1 관계에서 확인이 필요합니다.",
            references=[
                ChatReference(agent="FIT", item="FIT-1", evidence_id=None),
            ],
            suggested_revision="지원대상을 다음과 같이 구체화하는 방안을 검토할 수 있습니다.",
        )
    )
    settings = runtime(database_url, tmp_path)
    case_uuid = public_id(engine, case_id)

    with TestClient(create_app(settings, llm_client=llm)) as client:
        owner_headers = bearer(client, owner_login)
        other_headers = bearer(client, other_login)
        assert client.get(
            f"/api/v1/cases/{case_uuid}/messages",
            headers=owner_headers,
        ).status_code == 200
        answer = client.post(
            f"/api/v1/cases/{case_uuid}/messages",
            headers=owner_headers,
            json={"content": "이 결과를 설명해줘"},
        )
        assert answer.status_code == 200
        body = answer.json()
        # 응답에는 말풍선 두 개만 담는다.
        assert set(body) == {"user_message", "assistant_message"}
        assert body["user_message"]["role"] == "USER"
        assert body["assistant_message"]["role"] == "ASSISTANT"

        # 새로고침해도 같은 것이 보여야 한다 — POST 응답에만 근거가 실리고
        # 이력에는 없으면, 같은 대화가 화면에서 다르게 그려진다.
        reloaded = client.get(
            f"/api/v1/cases/{case_uuid}/messages",
            headers=owner_headers,
        ).json()["messages"]
        assert [item["role"] for item in reloaded] == ["USER", "ASSISTANT"]
        assert reloaded[1] == body["assistant_message"]
        assert reloaded[1]["references"] == [
            {"agent": "FIT", "item": "FIT-1", "evidence_id": None}
        ]
        assert reloaded[1]["suggested_revision"].startswith("지원대상을")
        assert reloaded[0]["references"] == []
        assert reloaded[0]["suggested_revision"] is None

        # 내부 PK 는 외부 API 의 식별자가 아니다. UUID 가 아닌 값은 형식 오류다.
        # 이 앱은 모든 검증 오류를 400 으로 통일한다(main.validation_error_handler)
        # — FastAPI 기본값인 422 가 아니다.
        assert client.get(
            f"/api/v1/cases/{case_id}/messages",
            headers=owner_headers,
        ).status_code == 400

        # 남의 건은 존재 여부도 알리지 않는다.
        assert client.get(
            f"/api/v1/cases/{case_uuid}/messages",
            headers=other_headers,
        ).status_code == 404
        assert client.post(
            f"/api/v1/cases/{case_uuid}/messages",
            headers=other_headers,
            json={"content": "남의 결과"},
        ).status_code == 404
        # 없는 UUID 도 같은 404.
        assert client.get(
            f"/api/v1/cases/{uuid.uuid4()}/messages",
            headers=owner_headers,
        ).status_code == 404

        with engine.begin() as connection:
            pending_uuid = connection.scalar(
                text(
                    "INSERT INTO sims.inspection_case (owner_user_id, status) "
                    "VALUES (:owner_id, 'PARSING') RETURNING analysis_case_id"
                ),
                {"owner_id": other_id},
            )
        assert client.get(
            f"/api/v1/cases/{pending_uuid}/messages",
            headers=other_headers,
        ).status_code == 409
        assert client.post(
            f"/api/v1/cases/{pending_uuid}/messages",
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
        ChatAnswer(
            answer="근거가 있다고 답합니다.",
            references=[
                ChatReference(agent="CPL", item="PURPOSE_GOAL", evidence_id="invented:1")
            ],
            suggested_revision=None,
        )
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
