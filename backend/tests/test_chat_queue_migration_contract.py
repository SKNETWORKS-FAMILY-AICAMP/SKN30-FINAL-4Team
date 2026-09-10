"""Static contract checks for the private PostgreSQL chat queue migration."""

from pathlib import Path


MIGRATION = Path(__file__).parents[1] / "supabase" / "migrations" / "26_chat_worker_queue.sql"


def test_chat_queue_is_private_and_has_one_dispatch_row_per_assistant() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS workspace.conversation_message_dispatch" in sql
    assert "assistant_message_pk UUID PRIMARY KEY" in sql
    assert "REFERENCES result.conversation_message(message_pk)" in sql
    assert "ALTER TABLE workspace.conversation_message_dispatch ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON TABLE workspace.conversation_message_dispatch" in sql
    assert "FROM PUBLIC, anon, authenticated" in sql


def test_http_commands_enqueue_and_retry_durably() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION workspace.prepare_conversation_messages" in sql
    assert "INSERT INTO workspace.conversation_message_dispatch" in sql
    assert "CREATE OR REPLACE FUNCTION workspace.retry_conversation_message" in sql
    assert "manual_retry_count = manual_retry_count + 1" in sql
    assert "CHAT_RETRY_EXHAUSTED" in sql
    assert "last_retry_at > now() - interval '5 seconds'" in sql
    assert "CHAT_MESSAGE_BUSY" in sql
    assert "char_length(btrim(p_content)) > 4000" in sql


def test_claim_payload_is_owner_checked_and_completion_is_fenced() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION workspace.claim_next_conversation_message" in sql
    for field in (
        "assistant_message_id UUID",
        "analysis_case_id UUID",
        "analysis_session_id UUID",
        "user_message_id UUID",
        "question TEXT",
        "conversation JSONB",
        "result_payload JSONB",
        "processing_run_pk UUID",
    ):
        assert field in sql
    assert "c.retention_expires_at > v_now" in sql
    assert "m.sequence_no < v_user_sequence_no" in sql
    assert "m.status = 'completed'" in sql
    assert "ORDER BY m.sequence_no DESC" in sql
    assert "LIMIT 20" in sql
    assert "jsonb_build_object('role', prior.role, 'content', prior.content)" in sql
    assert "CREATE OR REPLACE FUNCTION workspace.heartbeat_conversation_message" in sql
    assert "CREATE OR REPLACE FUNCTION workspace.complete_conversation_message" in sql
    assert "d.processing_run_pk = p_processing_run_pk" in sql
    assert "p_evidence_ids UUID[]" in sql
    assert "result_payload = NULL" in sql
    assert "CREATE OR REPLACE FUNCTION workspace.fail_conversation_message" in sql


def test_every_chat_rpc_is_service_role_only() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for function_name in (
        "prepare_conversation_messages",
        "retry_conversation_message",
        "claim_next_conversation_message",
        "heartbeat_conversation_message",
        "complete_conversation_message",
        "fail_conversation_message",
    ):
        assert f"workspace.{function_name}" in sql
    grants = sql[sql.index("REVOKE ALL ON FUNCTION workspace.prepare_conversation_messages"):]
    assert "TO service_role" in grants
    assert "TO authenticated" not in grants
