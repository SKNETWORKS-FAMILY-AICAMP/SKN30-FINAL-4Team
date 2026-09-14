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
ADMISSION = (MIGRATIONS / "37_v02_global_queue_admission.sql").read_text(
    encoding="utf-8"
)
MODEL1_RUNTIME_REFRESH = (MIGRATIONS / "38_model1_runtime_manifest_refresh.sql").read_text(
    encoding="utf-8"
)
FINALIZATION = (MIGRATIONS / "39_v02_atomic_upload_finalization.sql").read_text(
    encoding="utf-8"
)
MODEL1_RUNTIME_REFRESH_V4 = (
    MIGRATIONS / "41_model1_runtime_manifest_refresh_v4.sql"
).read_text(encoding="utf-8")


def test_v02_migrations_are_forward_only_after_32() -> None:
    names = {path.name for path in MIGRATIONS.glob("*.sql")}
    assert {
        "33_v02_lifecycle_and_fastapi_boundary.sql",
        "34_v02_public_result_projection_and_evidence.sql",
        "35_v02_partial_axis_retrieval.sql",
        "36_v02_conversation_idempotency_and_claim.sql",
        "37_v02_global_queue_admission.sql",
        "38_model1_runtime_manifest_refresh.sql",
        "39_v02_atomic_upload_finalization.sql",
        "40_v02_embedding_execution_provenance.sql",
        "41_model1_runtime_manifest_refresh_v4.sql",
    } <= names


def test_model1_runtime_refresh_registers_a_new_inactive_immutable_identity() -> None:
    assert "2903d0e90e71cd121af3185eeab3fefe3e1407175d14476e8d60f611b6861a60" in MODEL1_RUNTIME_REFRESH
    assert "pre-review-existing-model1-runtime-v3" in MODEL1_RUNTIME_REFRESH
    assert "FALSE" in MODEL1_RUNTIME_REFRESH
    assert "ON CONFLICT DO NOTHING" in MODEL1_RUNTIME_REFRESH
    assert "UPDATE retrieval.classification_configuration" not in MODEL1_RUNTIME_REFRESH
    assert "existing_profile_classification" not in MODEL1_RUNTIME_REFRESH


def test_model1_runtime_v4_refresh_preserves_existing_classifications() -> None:
    assert "85aee02364390b97385987ed6acb64406ca83651128585cb0a53cefb28dc9597" in MODEL1_RUNTIME_REFRESH_V4
    assert "pre-review-existing-model1-runtime-v4" in MODEL1_RUNTIME_REFRESH_V4
    assert "FALSE" in MODEL1_RUNTIME_REFRESH_V4
    assert "ON CONFLICT DO NOTHING" in MODEL1_RUNTIME_REFRESH_V4
    assert "UPDATE retrieval.classification_configuration" not in MODEL1_RUNTIME_REFRESH_V4
    assert "existing_profile_classification" not in MODEL1_RUNTIME_REFRESH_V4


def test_global_queue_admission_is_atomic_and_replays_precede_capacity() -> None:
    assert "CREATE OR REPLACE FUNCTION workspace.global_queue_limit_v2" in ADMISSION
    assert "CREATE OR REPLACE FUNCTION workspace.admit_global_queue_work_v2" in ADMISSION
    assert "pg_advisory_xact_lock" in ADMISSION
    assert "GLOBAL_QUEUE_CAPACITY_EXCEEDED" in ADMISSION
    assert "ERRCODE = '53000'" in ADMISSION
    # One gate counts both durable analysis reservations and chat dispatches;
    # it holds the transaction lock until the caller inserts its new work.
    assert "workspace.analysis_run AS run" in ADMISSION
    assert "workspace.conversation_message_dispatch AS dispatch" in ADMISSION
    assert "assistant.status = 'generating'" in ADMISSION
    assert "run.status IN ('uploading', 'queued') AND run.expires_at > v_now" in ADMISSION

    reserve = ADMISSION[ADMISSION.index(
        "CREATE OR REPLACE FUNCTION workspace.reserve_analysis_upload_v2"
    ) :]
    expired_replay = reserve.index("IF v_existing_run.expires_at <= v_now THEN")
    renewed_upload = reserve.index("SET expires_at = v_now + make_interval")
    assert expired_replay < renewed_upload
    assert reserve.index("PERFORM workspace.admit_global_queue_work_v2()", expired_replay) < renewed_upload

    prepare = ADMISSION[ADMISSION.index(
        "CREATE OR REPLACE FUNCTION workspace.prepare_conversation_messages_v2"
    ) :]
    assert prepare.index("v_session.analysis_session_pk, TRUE") < prepare.index(
        "PERFORM workspace.admit_global_queue_work_v2()"
    )

    retry = ADMISSION[ADMISSION.index(
        "CREATE OR REPLACE FUNCTION workspace.retry_conversation_message_v2"
    ) :]
    assert retry.index("v_message.auto_retry_count + v_message.manual_retry_count, TRUE") < retry.index(
        "PERFORM workspace.admit_global_queue_work_v2()"
    )
    assert retry.index("PERFORM workspace.admit_global_queue_work_v2()") < retry.index(
        "INSERT INTO workspace.conversation_retry_idempotency_v2"
    )
    assert "TO service_role" in ADMISSION


def test_upload_finalization_reuses_global_lock_and_cleans_expired_capacity() -> None:
    assert "CREATE OR REPLACE FUNCTION workspace.finalize_analysis_upload_v2" in FINALIZATION
    assert "hashtextextended('prereview/global-queue-admission/v2', 0)" in FINALIZATION
    assert "PERFORM workspace.admit_global_queue_work_v2()" in FINALIZATION
    assert "v_run.expires_at <= v_now" in FINALIZATION
    assert "EXCEPTION WHEN SQLSTATE '53000'" in FINALIZATION
    assert "status = 'cleanup_pending'" in FINALIZATION
    assert "UPLOAD_RESERVATION_EXPIRED" in FINALIZATION
    assert "'expired_capacity'" in FINALIZATION
    assert "workspace.source_artifact" in FINALIZATION
    assert "UPLOAD_FINALIZATION_SOURCE_MISMATCH" in FINALIZATION
    assert "TO service_role" in FINALIZATION


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
    assert "sole run-ID allocator" in LIFECYCLE
    assert "returned analysis_run_id through" in LIFECYCLE


def test_lifecycle_stale_current_history_and_fastapi_boundary_are_explicit() -> None:
    assert "reconcile_stale_analysis_runs_for_user_v2" in LIFECYCLE
    assert "UPLOAD_RESERVATION_EXPIRED" in LIFECYCLE
    assert "ANALYSIS_QUEUE_EXPIRED" in LIFECYCLE
    claim = LIFECYCLE[LIFECYCLE.index(
        "CREATE OR REPLACE FUNCTION workspace.claim_next_analysis_run"
    ) :]
    assert "ar.status = 'queued' AND ar.expires_at > v_now" in claim
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


def test_embedding_execution_provenance_is_fenced_and_records_zero_axis_null() -> None:
    provenance = (MIGRATIONS / "40_v02_embedding_execution_provenance.sql").read_text(
        encoding="utf-8"
    )
    for token in (
        "record_analysis_embedding_provenance_v2",
        "processing.status = 'running'",
        "dispatch.lease_expires_at > clock_timestamp()",
        "'embedding_configuration'",
        "'selected', false",
        "'configuration_id', NULL",
        "FOR UPDATE OF run, dispatch",
        "v_existing_provenance ->> 'configuration_id' = p_embedding_config_pk::text",
        "processing.run_metadata -> 'embedding_configuration' IS NULL",
    ):
        assert token in provenance


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
    detail = RESULT[RESULT.index("CREATE OR REPLACE FUNCTION api.rpc_get_sim_candidate_detail_v2") :]
    assert "'axis_type', 'SIM'" in detail


def test_sim_status_reason_pairs_accept_only_worker_terminal_semantics() -> None:
    # Both the worker payload gate and the persisted analysis_case trigger use
    # the same safe pairs: completed may be normal/null or KB_EMPTY; skipped
    # is exclusively the absence of retrieval input.
    assert RESULT.count("'KB_EMPTY'") >= 2
    assert RESULT.count("'RETRIEVAL_INPUT_MISSING'") >= 2
    assert "NEW.sim_reason_code IS DISTINCT FROM 'RETRIEVAL_INPUT_MISSING'" in RESULT
    assert "p_result#>>'{sim,reason_code}' IS DISTINCT FROM 'RETRIEVAL_INPUT_MISSING'" in RESULT
    assert "NEW.sim_reason_code <> 'KB_EMPTY'" in RESULT
    assert "p_result#>>'{sim,reason_code}' <> 'KB_EMPTY'" in RESULT
    assert "WHEN analysis_case.sim_status IS NULL" in RESULT
    assert "ELSE analysis_case.sim_reason_code" in RESULT


def test_fit_public_side_accepts_nullable_summary_for_unperformed_comparison() -> None:
    assert "p_detail#>'{left,value_summary}', 'fit.left.value_summary'" in RESULT
    assert "p_detail#>'{right,value_summary}', 'fit.right.value_summary'" in RESULT
    assert "jsonb_typeof(p_detail#>'{left,value_summary}') <> 'string'" not in RESULT
    assert "jsonb_typeof(p_detail#>'{right,value_summary}') <> 'string'" not in RESULT
    assert "FIT_STATUS_COMPARISON_MISMATCH" in RESULT


def test_chat_v2_projects_safe_error_message_and_validates_retry_target_first() -> None:
    assert CHAT.count("'error_message', message.error_message") >= 1
    assert "'error_message', chronological.error_message" in CHAT
    retry = CHAT[CHAT.index("CREATE OR REPLACE FUNCTION workspace.retry_conversation_message_v2") :]
    assert retry.index("message.message_pk = p_assistant_message_id") < retry.index(
        "INSERT INTO workspace.conversation_retry_idempotency_v2"
    )
    heartbeat = CHAT[CHAT.index("CREATE OR REPLACE FUNCTION workspace.heartbeat_conversation_message_v2") :]
    assert "FROM result.conversation_message AS message," in heartbeat
    assert "analysis_case.analysis_case_pk = dispatch.analysis_case_pk" in heartbeat


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
        "worker poll did not reconcile expired queued analysis",
        "global admission accepted retry above capacity",
        "analysis replay was rejected at global capacity",
        "expired upload replay over-admitted global capacity",
        "expired upload replay did not atomically reacquire capacity",
    ):
        assert token in runtime
    assert '"$SCRIPT_DIR/tests/v02_runtime.sql"' in runner
