from __future__ import annotations

import pytest

from worker.chat.handler import ResultGroundedChatHandler
from worker.chat_main import (
    ChatWorkerConfigurationError,
    ChatWorkerSettings,
    build_chat_worker,
)
from worker.postgres_chat_repository import PostgresChatJobRepository


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql://worker:never-print@db.internal:5432/postgres",
        "OPENAI_API_KEY": "never-print-openai",
        "OPENAI_LLM_MODEL": "configured-llm",
        "OPENAI_EMBEDDING_MODEL": "configured-embedding",
    }


def test_chat_worker_settings_have_queue_defaults_and_legacy_dsn_support() -> None:
    settings = ChatWorkerSettings.from_env(
        {
            "SUPABASE_DB_URL": "postgresql://worker@db/postgres",
        }
    )
    assert settings.database_url == "postgresql://worker@db/postgres"
    assert settings.heartbeat_seconds == 30.0
    assert settings.lease_seconds == 120
    assert settings.idle_poll_seconds == 1.0


@pytest.mark.parametrize(
    "overrides, message",
    [
        (
            {
                "DATABASE_URL": "postgresql://worker@db/postgres",
                "PREREVIEW_CHAT_WORKER_HEARTBEAT_SECONDS": "120",
                "PREREVIEW_CHAT_WORKER_LEASE_SECONDS": "120",
            },
            "shorter",
        ),
        (
            {
                "DATABASE_URL": "postgresql://worker@db/postgres",
                "PREREVIEW_CHAT_WORKER_LEASE_SECONDS": "29",
            },
            "between 30 and 3600",
        ),
    ],
)
def test_chat_worker_settings_fail_closed_on_unsafe_queue_values(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(ChatWorkerConfigurationError, match=message):
        ChatWorkerSettings.from_env(overrides)


def test_build_chat_worker_uses_dedicated_chat_repository_and_llm_profile() -> None:
    composition = build_chat_worker(_environment())

    assert isinstance(composition.repository, PostgresChatJobRepository)
    assert isinstance(composition.handler, ResultGroundedChatHandler)
    assert composition.settings.lease_seconds == 120
    rendered = repr(composition.repository) + repr(composition.handler)
    assert "never-print" not in rendered

