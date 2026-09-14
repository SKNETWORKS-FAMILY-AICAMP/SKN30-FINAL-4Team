"""Optional live-PostgreSQL smoke test for the v0.2 FastAPI adapters.

The default unit suite skips this module.  Point
``PREREVIEW_TEST_DATABASE_URL`` at a disposable, fully migrated Supabase
PostgreSQL database to verify the Python calls against the real function
signatures.  Never point it at production.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest

from app.infrastructure.postgres_analysis_runs import PostgresAnalysisRunRepository
from app.infrastructure.postgres_conversations import PostgresConversationRepository
from app.infrastructure.postgres_results import PostgresResultRepository
from app.ports.analysis_runs import SourceObject
from app.ports.conversations import ConversationNotFound
from app.ports.results import ResultNotFound


DATABASE_URL = os.getenv("PREREVIEW_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="PREREVIEW_TEST_DATABASE_URL must target a disposable migrated database",
)


def test_v02_fastapi_adapters_match_live_postgres_contract() -> None:
    owner_id = str(uuid4())
    idempotency_key = str(uuid4())
    missing_id = str(uuid4())
    digest = "a" * 64
    source = SourceObject(
        bucket="request-temp",
        object_key=f"{uuid4()}/source/{digest}.hwpx",
        content_sha256=digest,
        filename="runtime-smoke.hwpx",
        mime_type="application/vnd.hancom.hwpx",
        declared_mime_type="application/vnd.hancom.hwpx",
        size_bytes=1234,
    )

    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            "INSERT INTO auth.users (id, email, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (
                owner_id,
                f"adapter-{owner_id}@example.invalid",
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            ),
        )

    async def exercise() -> None:
        runs = PostgresAnalysisRunRepository(DATABASE_URL)
        results = PostgresResultRepository(DATABASE_URL)
        conversations = PostgresConversationRepository(DATABASE_URL)

        assert await results.get_current(owner_id=owner_id) == {
            "state": "idle",
            "run": None,
            "session": None,
        }
        assert await results.get_active_session(owner_id=owner_id) is None
        history = await results.list_analysis_history_page(
            owner_id=owner_id,
            snapshot_at=None,
            after=None,
            limit=5,
        )
        assert history.rows == []
        assert history.next_after is None

        with pytest.raises(ResultNotFound):
            await results.get_analysis_case(
                owner_id=owner_id,
                analysis_case_id=missing_id,
            )
        with pytest.raises(ResultNotFound):
            await results.get_sim_candidate(
                owner_id=owner_id,
                sim_candidate_id=missing_id,
            )
        with pytest.raises(ResultNotFound):
            await results.close_session(
                owner_id=owner_id,
                analysis_session_id=missing_id,
            )
        with pytest.raises(ConversationNotFound):
            await conversations.create_message(
                owner_id=owner_id,
                analysis_case_id=missing_id,
                content="없는 분석에 대한 질문",
                idempotency_key=str(uuid4()),
            )
        with pytest.raises(ConversationNotFound):
            await conversations.get_message(
                owner_id=owner_id,
                analysis_case_id=missing_id,
                message_id=missing_id,
            )
        with pytest.raises(ConversationNotFound):
            await conversations.list_messages(
                owner_id=owner_id,
                analysis_case_id=missing_id,
            )

        first = await runs.reserve_uploading(
            idempotency_key=idempotency_key,
            owner_id=owner_id,
            source=source,
        )
        assert first.record.status == "uploading"
        assert first.replayed is False

        # The exact idempotent retry must be observed before stale cleanup.
        # Expire the reservation deliberately; the same key+source resumes it
        # and receives a fresh upload window instead of cleanup_pending.
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(
                "UPDATE workspace.analysis_run "
                "SET expires_at = clock_timestamp() - interval '1 second' "
                "WHERE analysis_run_pk = %s",
                (first.record.analysis_run_id,),
            )

        replay = await runs.reserve_uploading(
            idempotency_key=idempotency_key,
            owner_id=owner_id,
            source=source,
        )
        assert replay.record.analysis_run_id == first.record.analysis_run_id
        assert replay.replayed is True
        assert replay.record.status == "uploading"
        assert replay.cleanup_objects == ()
        with psycopg.connect(DATABASE_URL) as connection:
            renewed = connection.execute(
                "SELECT expires_at > clock_timestamp() "
                "FROM workspace.analysis_run WHERE analysis_run_pk = %s",
                (first.record.analysis_run_id,),
            ).fetchone()
        assert renewed == (True,)

        queued = await runs.finalize_queued(
            analysis_run_id=first.record.analysis_run_id,
            owner_id=owner_id,
            source=source,
        )
        assert queued.status == "queued"
        current = await results.get_current(owner_id=owner_id)
        assert current["state"] == "processing"
        assert current["run"]["analysis_run_id"] == first.record.analysis_run_id

    try:
        asyncio.run(exercise())
    finally:
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM workspace.analysis_run WHERE user_id = %s",
                (owner_id,),
            )
            connection.execute("DELETE FROM auth.users WHERE id = %s", (owner_id,))
