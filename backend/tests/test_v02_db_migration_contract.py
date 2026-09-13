"""Static contracts for the forward-only backend v0.2 database migrations.

These checks deliberately focus on the SQL boundary shared by FastAPI and the
workers.  They are runnable without a local Supabase compose installation;
the self-hosted runtime suite remains the integration gate for PostgreSQL.
"""

from pathlib import Path


MIGRATIONS = Path(__file__).parents[1] / "supabase" / "migrations"
LIFECYCLE = (MIGRATIONS / "33_v02_lifecycle_and_fastapi_boundary.sql").read_text(
    encoding="utf-8"
)
RESULT = (MIGRATIONS / "34_v02_public_result_projection_and_evidence.sql").read_text(
    encoding="utf-8"
)
RETRIEVAL = (MIGRATIONS / "35_v02_partial_axis_retrieval.sql").read_text(
    encoding="utf-8"
)
CHAT = (MIGRATIONS / "36_v02_conversation_idempotency_and_claim.sql").read_text(
    encoding="utf-8"
)


def test_v02_migrations_are_forward_only_after_32() -> None:
    names = {path.name for path in MIGRATIONS.glob("*.sql")}
    assert {
        "33_v02_lifecycle_and_fastapi_boundary.sql",
        "34_v02_public_result_projection_and_evidence.sql",
        "35_v02_partial_axis_retrieval.sql",
        "36_v02_conversation_idempotency_and_claim.sql",
    } <= names


def test_lifecycle_uses_one_owner_lock_and_exact_session_close() -> None:
    assert "uq_result_analysis_session_one_active_owner" in LIFECYCLE
    assert "migration_reconciled" in LIFECYCLE
    assert "workspace.lock_analysis_lifecycle_user_v2" in LIFECYCLE
    assert "CREATE OR REPLACE FUNCTION workspace.reserve_analysis_upload_v2" in LIFECYCLE
    reserve = LIFECYCLE[LIFECYCLE.index("CREATE OR REPLACE FUNCTION workspace.reserve_analysis_upload_v2") :]
    assert reserve.index("lock_analysis_lifecycle_user_v2") < reserve.index(
        "FROM workspace.analysis_run AS run"
    )
    assert "IDEMPOTENCY_KEY_CONFLICT" in reserve
    assert reserve.index("IDEMPOTENCY_KEY_CONFLICT") < reserve.index("ANALYSIS_RUN_ACTIVE")
    assert reserve.index("ANALYSIS_RUN_ACTIVE") < reserve.index("ACTIVE_RESULT_SESSION")
    assert "CREATE OR REPLACE FUNCTION api.rpc_close_analysis_session_v2" in LIFECYCLE
    assert "session.analysis_session_pk = p_analysis_session_id" in LIFECYCLE
    assert "ANALYSIS_SESSION_NOT_FOUND" in LIFECYCLE


def test_lifecycle_stale_current_history_and_fastapi_boundary_are_explicit() -> None:
    assert "reconcile_stale_analysis_runs_for_user_v2" in LIFECYCLE
    assert "UPLOAD_RESERVATION_EXPIRED" in LIFECYCLE
    assert "ANALYSIS_QUEUE_EXPIRED" in LIFECYCLE
    assert "CREATE OR REPLACE FUNCTION api.rpc_get_analysis_current_v2" in LIFECYCLE
    assert "'state', 'processing'" in LIFECYCLE
    assert "'state', 'ready'" in LIFECYCLE
    assert "'state', 'idle'" in LIFECYCLE
    assert "CREATE OR REPLACE FUNCTION api.rpc_get_analysis_history_v2" in LIFECYCLE
    assert "LIMIT 6" in LIFECYCLE
    assert "next_cursor AS" in LIFECYCLE
    assert "SELECT visible.analysis_completed_at, visible.analysis_case_pk" in LIFECYCLE
    assert "REVOKE SELECT ON ALL TABLES IN SCHEMA kb FROM authenticated" in LIFECYCLE
    assert "REVOKE SELECT ON app.user_profile FROM authenticated" in LIFECYCLE
    assert "DROP POLICY IF EXISTS request_temp_insert_reserved_source" in LIFECYCLE


def test_v2_writer_aligns_worker_payload_and_keeps_public_raw_separate() -> None:
    for token in (
        "public_detail JSONB",
        "public_metadata JSONB",
        "public_axes JSONB",
        "source_identity TEXT",
        "CREATE OR REPLACE FUNCTION workspace.persist_analysis_result_core_v2",
        "'contract_version','program_name','sim','axes','candidates','evidences'",
        "'source_identity'",
        "'analysis_result/v0.2'",
        "workspace.persist_analysis_result_core(",
        "usage_scope = 'CONVERSATION'",
        "DANGLING_OR_CROSS_CONTEXT_PUBLIC_EVIDENCE",
        "DUPLICATE_PUBLIC_EVIDENCE_ID",
        "LEGACY_PUBLIC_DETAIL_UNAVAILABLE",
    ):
        assert token in RESULT
    assert "jsonb_typeof(v_evidence->'source_identity') <> 'string'" in RESULT
    assert "WHEN p_result ? 'ml' THEN p_result" in RESULT
    assert "'LEFT' AND v_evidence->>'side' = 'REQUEST'" in RESULT
    assert "'RIGHT' AND v_evidence->>'side' = 'REQUEST'" in RESULT


def test_v2_projection_and_candidate_detail_are_service_only() -> None:
    for function_name in (
        "api.public_analysis_projection_v2(UUID, UUID)",
        "api.rpc_get_analysis_result_v2(UUID, UUID)",
        "api.rpc_get_sim_candidate_detail_v2(UUID, UUID)",
    ):
        assert function_name in RESULT
    grants = RESULT[RESULT.rindex("REVOKE ALL ON FUNCTION result.jsonb_has_exact_keys_v2") :]
    assert "TO service_role" in grants
    assert "GRANT EXECUTE ON FUNCTION api.rpc_get_analysis_result_v2(UUID, UUID)\nTO authenticated" not in grants


def test_partial_axis_retrieval_requires_active_config_and_full_candidate_coverage() -> None:
    assert "CREATE OR REPLACE FUNCTION retrieval.match_existing_profiles_partial_axes" in RETRIEVAL
    assert "ACTIVE_EMBEDDING_CONFIGURATION_REQUIRED" in RETRIEVAL
    assert "p_request_vectors = '{}'::jsonb" in RETRIEVAL
    assert "complete_existing_coverage AS" in RETRIEVAL
    assert "HAVING count(DISTINCT embedding.scope) = 3" in RETRIEVAL
    assert "JOIN complete_existing_coverage AS coverage" in RETRIEVAL
    assert "HAVING count(*) = v_axis_count" in RETRIEVAL
    assert "portal_metadata JSONB" in RETRIEVAL
    assert "source_profile_id TEXT" in RETRIEVAL
    assert "notice_id TEXT" in RETRIEVAL


def test_chat_v2_has_idempotency_poll_history_and_post_close_claim_semantics() -> None:
    for token in (
        "idempotency_key UUID",
        "content_sha256 TEXT",
        "conversation_retry_idempotency_v2",
        "CREATE OR REPLACE FUNCTION workspace.prepare_conversation_messages_v2",
        "CREATE OR REPLACE FUNCTION workspace.retry_conversation_message_v2",
        "IDEMPOTENCY_KEY_CONFLICT",
        "CREATE OR REPLACE FUNCTION api.rpc_get_conversation_message_v2",
        "CREATE OR REPLACE FUNCTION api.rpc_get_conversation_history_v2",
        "CREATE OR REPLACE FUNCTION workspace.claim_next_conversation_message_v2",
        "CHAT_RETENTION_EXPIRED",
        "api.public_analysis_projection_v2",
        "CHAT_EVIDENCE_NOT_IN_PUBLIC_ALLOW_LIST",
        "CHAT_CONFLICT",
        "SET result_payload = NULL",
    ):
        assert token in CHAT
    claim = CHAT[CHAT.index("CREATE OR REPLACE FUNCTION workspace.claim_next_conversation_message_v2") :]
    # The v2 candidate query intentionally has no session active/expiry gate.
    candidate_query = claim[claim.index("SELECT dispatch.*") : claim.index("IF NOT FOUND THEN\n        RETURN;")]
    assert "session.status = 'active'" not in candidate_query
    assert "session.expires_at" not in candidate_query
    assert "next_cursor AS" in CHAT
    assert "SELECT visible.sequence_no, visible.message_pk" in CHAT


def test_page_cursor_is_the_last_returned_row_not_the_limit_plus_one_probe() -> None:
    # The SQL functions fetch N+1 descending rows, display N rows in ascending
    # order, then apply a strict '<' predicate on the cursor.  The correct
    # cursor is therefore the oldest displayed (last returned) key; using the
    # probe key would skip it permanently on the next page.
    rows = [(9, "i"), (8, "h"), (7, "g"), (6, "f"), (5, "e"), (4, "d")]
    limit = 5
    visible = rows[:limit]
    probe = rows[limit]
    cursor = visible[-1]
    next_page = [row for row in rows if row < cursor][:limit]
    assert probe == next_page[0]
    assert cursor != probe


def test_runtime_script_exercises_prepare_replay_close_claim_and_page_boundary() -> None:
    runtime = (MIGRATIONS.parent / "tests" / "v02_runtime.sql").read_text(encoding="utf-8")
    runner = (MIGRATIONS.parent / "run_worker_queue_validation.sh").read_text(encoding="utf-8")
    for token in (
        "prepare_conversation_messages_v2",
        "v2 prepare replay did not return the original turn",
        "second generating assistant turn",
        "claim_next_conversation_message_v2",
        "post-close v2 claim was not accepted",
        "fail_conversation_message_v2",
        "retained result_payload",
        "history cursor skipped the limit-plus-one boundary row",
    ):
        assert token in runtime
    assert '"$SCRIPT_DIR/tests/v02_runtime.sql"' in runner
