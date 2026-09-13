\set ON_ERROR_STOP on

-- Runtime contract for migrations 33-36.  It is independent from the older
-- queue suite so it can run against an empty Existing-KB corpus.  Every row is
-- rolled back after the assertions.
BEGIN;

DO $$
DECLARE
    v_user_id UUID := gen_random_uuid();
    v_case_id UUID := gen_random_uuid();
    v_session_id UUID := gen_random_uuid();
    v_first RECORD;
    v_replay RECORD;
    v_claim RECORD;
    v_dispatch_payload JSONB;
    v_history JSONB;
    v_next_history JSONB;
    v_cursor_completed_at TIMESTAMPTZ;
    v_cursor_case_id UUID;
    v_history_case_ids UUID[] := '{}'::uuid[];
    v_extra_case_id UUID;
    v_index INTEGER;
BEGIN
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_user_id, 'v02-runtime@example.invalid', clock_timestamp(), clock_timestamp());

    INSERT INTO result.analysis_case (
        analysis_case_pk, source_analysis_run_id, user_id, case_status,
        analysis_completed_at, retention_expires_at, program_name, original_filename
    ) VALUES (
        v_case_id, gen_random_uuid(), v_user_id, 'ready', clock_timestamp(),
        clock_timestamp() + interval '90 days', 'v0.2 runtime', 'runtime.hwpx'
    );
    INSERT INTO result.analysis_session (
        analysis_session_pk, analysis_case_pk, user_id, status, expires_at
    ) VALUES (
        v_session_id, v_case_id, v_user_id, 'active', clock_timestamp() + interval '30 minutes'
    );

    SELECT * INTO STRICT v_first
      FROM workspace.prepare_conversation_messages_v2(
          v_user_id, v_case_id, '동일한 질문', gen_random_uuid()
      );
    -- Pass the same UUID from the generated first user row into an exact
    -- replay; derive it from the persisted row to avoid client-side state.
    SELECT * INTO STRICT v_replay
      FROM workspace.prepare_conversation_messages_v2(
          v_user_id,
          v_case_id,
          '동일한 질문',
          (SELECT idempotency_key FROM result.conversation_message
            WHERE message_pk = v_first.user_message_id)
      );
    IF v_first.user_message_id <> v_replay.user_message_id
       OR v_first.assistant_message_id <> v_replay.assistant_message_id
       OR v_first.replayed OR NOT v_replay.replayed THEN
        RAISE EXCEPTION 'v2 prepare replay did not return the original turn';
    END IF;
    IF (SELECT count(*) FROM result.conversation_message
         WHERE analysis_session_pk = v_session_id) <> 2
       OR (SELECT count(*) FROM workspace.conversation_message_dispatch
            WHERE assistant_message_pk = v_first.assistant_message_id) <> 1 THEN
        RAISE EXCEPTION 'v2 prepare replay created duplicate message or dispatch';
    END IF;

    BEGIN
        PERFORM workspace.prepare_conversation_messages_v2(
            v_user_id, v_case_id, '새로운 동시 질문', gen_random_uuid()
        );
        RAISE EXCEPTION 'v2 prepare accepted a second generating assistant turn';
    EXCEPTION WHEN unique_violation THEN
        NULL;
    END;

    -- A committed create remains claimable after a later close.  The v2 claim
    -- must not inspect session.active/session expiry.
    UPDATE result.analysis_session
       SET status = 'closed', closed_at = clock_timestamp(), close_reason = 'new_analysis'
     WHERE analysis_session_pk = v_session_id;
    SELECT * INTO STRICT v_claim
      FROM workspace.claim_next_conversation_message_v2('v02-runtime-worker', 120);
    IF v_claim.assistant_message_id <> v_first.assistant_message_id
       OR v_claim.processing_run_pk IS NULL THEN
        RAISE EXCEPTION 'post-close v2 claim was not accepted';
    END IF;
    IF NOT workspace.fail_conversation_message_v2(
        v_claim.assistant_message_id, v_claim.processing_run_pk,
        'CHAT_RUNTIME_FAILURE', '안전한 실패 문구', 'internal test detail'
    ) THEN
        RAISE EXCEPTION 'v2 chat failure was fenced unexpectedly';
    END IF;
    SELECT result_payload INTO v_dispatch_payload
      FROM workspace.conversation_message_dispatch
     WHERE assistant_message_pk = v_claim.assistant_message_id;
    IF v_dispatch_payload IS NOT NULL THEN
        RAISE EXCEPTION 'v2 failed chat dispatch retained result_payload';
    END IF;

    -- Six closed historical cases make the page boundary observable.  The
    -- first cursor must use item five, allowing item six onto page two.
    FOR v_index IN 1..6 LOOP
        v_extra_case_id := gen_random_uuid();
        v_history_case_ids := array_append(v_history_case_ids, v_extra_case_id);
        INSERT INTO result.analysis_case (
            analysis_case_pk, source_analysis_run_id, user_id, case_status,
            analysis_completed_at, retention_expires_at
        ) VALUES (
            v_extra_case_id, gen_random_uuid(), v_user_id, 'ready',
            clock_timestamp() - make_interval(secs => (10 - v_index)),
            clock_timestamp() + interval '90 days'
        );
        INSERT INTO result.analysis_session (
            analysis_session_pk, analysis_case_pk, user_id, status, expires_at,
            closed_at, close_reason
        ) VALUES (
            gen_random_uuid(), v_extra_case_id, v_user_id, 'closed',
            clock_timestamp() + interval '30 minutes', clock_timestamp(), 'runtime'
        );
    END LOOP;
    v_history := api.rpc_get_analysis_history_v2(v_user_id);
    IF jsonb_array_length(v_history->'items') <> 5
       OR v_history->'next_completed_at' = 'null'::jsonb
       OR v_history->'next_analysis_case_id' = 'null'::jsonb THEN
        RAISE EXCEPTION 'v2 history did not return a five-row cursor page';
    END IF;
    v_cursor_completed_at := (v_history->>'next_completed_at')::timestamptz;
    v_cursor_case_id := (v_history->>'next_analysis_case_id')::uuid;
    v_next_history := api.rpc_get_analysis_history_v2(
        v_user_id, (v_history->>'snapshot_at')::timestamptz,
        v_cursor_completed_at, v_cursor_case_id
    );
    IF NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(v_next_history->'items') AS item(value)
         WHERE (item.value->>'analysis_case_id')::uuid = v_history_case_ids[2]
    ) THEN
        RAISE EXCEPTION 'v2 history cursor skipped the limit-plus-one boundary row';
    END IF;
END;
$$;

ROLLBACK;
