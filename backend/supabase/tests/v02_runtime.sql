\set ON_ERROR_STOP on

-- Runtime contract for migrations 33-40.  It is independent from the older
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
    v_expired_case_id UUID := gen_random_uuid();
    v_expired_reservation RECORD;
    v_expired_claim RECORD;
    v_admission_owner UUID := gen_random_uuid();
    v_abandoned_owner UUID := gen_random_uuid();
    v_cleanup_owner UUID := gen_random_uuid();
    v_admission_run RECORD;
    v_admission_replay RECORD;
    v_abandoned_run RECORD;
    v_expired_upload_replay RECORD;
    v_live_upload_replay RECORD;
    v_finalization_run RECORD;
    v_finalization_outcome RECORD;
    v_finalization_readmit RECORD;
    v_finalization_readmit_outcome RECORD;
    v_cleanup_run RECORD;
    v_cleanup_next RECORD;
    v_cleanup_retry RECORD;
    v_current JSONB;
    v_retry RECORD;
    v_retry_replay RECORD;
    v_retry_key UUID := gen_random_uuid();
    v_index INTEGER;
BEGIN
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_user_id, 'v02-runtime@example.invalid', clock_timestamp(), clock_timestamp());
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_admission_owner, 'v02-admission@example.invalid', clock_timestamp(), clock_timestamp());
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_abandoned_owner, 'v02-abandoned@example.invalid', clock_timestamp(), clock_timestamp());
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_cleanup_owner, 'v02-cleanup@example.invalid', clock_timestamp(), clock_timestamp());

    -- Current-state polling has no Storage client, so it must hide but not
    -- claim an expired upload cleanup. The next mutating upload returns that
    -- exact key, and a failed delete remains retryable on a later upload.
    SELECT * INTO STRICT v_cleanup_run
      FROM workspace.reserve_analysis_upload_v2(
          v_cleanup_owner, gen_random_uuid(), 'cleanup-old.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/cleanup-old/source.hwpx', repeat('c', 64), 900
      );
    UPDATE workspace.analysis_run
       SET expires_at = clock_timestamp() - interval '1 second'
     WHERE analysis_run_pk = v_cleanup_run.analysis_run_id;
    v_current := api.rpc_get_analysis_current_v2(v_cleanup_owner);
    IF v_current->>'state' <> 'idle'
       OR NOT EXISTS (
           SELECT 1 FROM workspace.analysis_run
            WHERE analysis_run_pk = v_cleanup_run.analysis_run_id
              AND status = 'uploading'
       ) THEN
        RAISE EXCEPTION 'current-state read claimed an upload cleanup object';
    END IF;

    SELECT * INTO STRICT v_cleanup_next
      FROM workspace.reserve_analysis_upload_v2(
          v_cleanup_owner, gen_random_uuid(), 'cleanup-next.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/cleanup-next/source.hwpx', repeat('d', 64), 900
      );
    IF NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(v_cleanup_next.cleanup_objects) AS item(value)
         WHERE (item.value->>'analysis_run_pk')::uuid = v_cleanup_run.analysis_run_id
           AND item.value->>'source_object_key' =
               'request-source/v02-runtime/cleanup-old/source.hwpx'
    ) THEN
        RAISE EXCEPTION 'next upload omitted expired upload cleanup key';
    END IF;
    UPDATE workspace.analysis_run SET status = 'failed'
     WHERE analysis_run_pk = v_cleanup_next.analysis_run_id;

    SELECT * INTO STRICT v_cleanup_retry
      FROM workspace.reserve_analysis_upload_v2(
          v_cleanup_owner, gen_random_uuid(), 'cleanup-retry.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/cleanup-retry/source.hwpx', repeat('f', 64), 900
      );
    IF NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(v_cleanup_retry.cleanup_objects) AS item(value)
         WHERE (item.value->>'analysis_run_pk')::uuid = v_cleanup_run.analysis_run_id
    ) THEN
        RAISE EXCEPTION 'cleanup_pending object was not returned for delete retry';
    END IF;
    UPDATE workspace.analysis_run SET status = 'failed'
     WHERE analysis_run_pk IN (
         v_cleanup_run.analysis_run_id, v_cleanup_retry.analysis_run_id
     );

    -- Polling itself owns queue-TTL reconciliation.  No subsequent browser
    -- upload/current request is needed to terminalise this expired job.
    SELECT * INTO STRICT v_expired_reservation
      FROM workspace.reserve_analysis_upload_v2(
          v_user_id, gen_random_uuid(), 'expired.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/expired/source.hwpx', repeat('a', 64), 900
      );
    INSERT INTO workspace.source_artifact (
        analysis_run_pk, artifact_type, storage_bucket, storage_object_key,
        content_sha256, mime_type, size_bytes, schema_version
    ) VALUES (
        v_expired_reservation.analysis_run_id, 'source', 'request-temp',
        'request-source/v02-runtime/expired/source.hwpx', repeat('a', 64),
        'application/octet-stream', 10, 'source/v1'
    );
    UPDATE workspace.analysis_run
       SET status = 'queued', expires_at = clock_timestamp() - interval '1 second'
     WHERE analysis_run_pk = v_expired_reservation.analysis_run_id;

    SELECT * INTO v_expired_claim
      FROM workspace.claim_next_analysis_run('v02-runtime-worker', 120);
    IF FOUND THEN
        RAISE EXCEPTION 'v2 worker claim accepted an expired queued analysis';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM workspace.analysis_run
         WHERE analysis_run_pk = v_expired_reservation.analysis_run_id
           AND status = 'failed'
           AND error_code = 'ANALYSIS_QUEUE_EXPIRED'
    ) THEN
        RAISE EXCEPTION 'v2 worker poll did not reconcile expired queued analysis';
    END IF;

    -- An old signed pagination snapshot must not keep an already-expired case
    -- visible.  Cursor stability never overrides the current retention cutoff.
    INSERT INTO result.analysis_case (
        analysis_case_pk, source_analysis_run_id, user_id, case_status,
        analysis_completed_at, retention_expires_at, program_name
    ) VALUES (
        v_expired_case_id, gen_random_uuid(), v_user_id, 'ready',
        clock_timestamp() - interval '2 hours',
        clock_timestamp() - interval '30 minutes', 'expired runtime case'
    );
    v_history := api.rpc_get_analysis_history_v2(
        v_user_id, clock_timestamp() - interval '1 hour', NULL, NULL
    );
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(v_history->'items') AS item(value)
         WHERE (item.value->>'analysis_case_id')::uuid = v_expired_case_id
    ) THEN
        RAISE EXCEPTION 'v2 history cursor bypassed current retention cutoff';
    END IF;

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

    -- Unperformed FIT axes deliberately carry nullable summaries and no
    -- evidence.  This is the normal INSUFFICIENT/NOT_APPLICABLE worker shape,
    -- not a malformed public payload.
    PERFORM result.assert_public_detail_shape_v2(
        'FIT',
        'INSUFFICIENT',
        jsonb_build_object(
            'comparison_performed', FALSE,
            'reason_code', 'INPUT_EVIDENCE_MISSING',
            'reason', '비교에 필요한 근거가 부족합니다.',
            'left', jsonb_build_object(
                'value_summary', NULL,
                'evidence_ids', '[]'::jsonb
            ),
            'right', jsonb_build_object(
                'value_summary', NULL,
                'evidence_ids', '[]'::jsonb
            ),
            'evidence_ids', '[]'::jsonb
        )
    );
    BEGIN
        PERFORM result.assert_public_detail_shape_v2(
            'FIT',
            'FIT',
            jsonb_build_object(
                'comparison_performed', FALSE,
                'reason_code', NULL,
                'reason', '잘못된 상태 조합',
                'left', jsonb_build_object(
                    'value_summary', NULL,
                    'evidence_ids', '[]'::jsonb
                ),
                'right', jsonb_build_object(
                    'value_summary', NULL,
                    'evidence_ids', '[]'::jsonb
                ),
                'evidence_ids', '[]'::jsonb
            )
        );
        RAISE EXCEPTION 'FIT accepted a verdict without performing comparison';
    EXCEPTION WHEN SQLSTATE '23514' THEN
        NULL;
    END;

    -- Worker terminal SIM states: an optional but empty KB is a completed
    -- retrieval; absent request retrieval input is the only skipped state.
    UPDATE result.analysis_case
       SET sim_status = 'completed', sim_reason_code = 'KB_EMPTY',
           sim_summary = '검색 가능한 기존 공고가 없습니다.'
     WHERE analysis_case_pk = v_case_id;
    UPDATE result.analysis_case
       SET sim_status = 'skipped', sim_reason_code = 'RETRIEVAL_INPUT_MISSING',
           sim_summary = '비교에 필요한 요청 축이 없습니다.'
     WHERE analysis_case_pk = v_case_id;
    BEGIN
        UPDATE result.analysis_case
           SET sim_status = 'completed', sim_reason_code = 'RETRIEVAL_INPUT_MISSING'
         WHERE analysis_case_pk = v_case_id;
        RAISE EXCEPTION 'completed SIM accepted RETRIEVAL_INPUT_MISSING';
    EXCEPTION WHEN SQLSTATE '22023' THEN
        NULL;
    END;
    BEGIN
        UPDATE result.analysis_case
           SET sim_status = 'skipped', sim_reason_code = 'KB_EMPTY'
         WHERE analysis_case_pk = v_case_id;
        RAISE EXCEPTION 'skipped SIM accepted KB_EMPTY';
    EXCEPTION WHEN SQLSTATE '22023' THEN
        NULL;
    END;
    IF position('''axis_type'', ''SIM''' IN pg_get_functiondef(
        'api.rpc_get_sim_candidate_detail_v2(uuid, uuid)'::regprocedure
    )) = 0 THEN
        RAISE EXCEPTION 'v2 SIM candidate detail omits evidence axis_type';
    END IF;

    -- An abandoned expired upload is excluded from capacity.  Its exact replay
    -- must reacquire a slot atomically, while a still-live exact replay stays
    -- replayable at capacity because it already owns the counted slot.
    PERFORM set_config('prereview.global_queue_max', '1', TRUE);
    SELECT * INTO STRICT v_abandoned_run
      FROM workspace.reserve_analysis_upload_v2(
          v_abandoned_owner, gen_random_uuid(), 'abandoned.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/abandoned/source.hwpx', repeat('e', 64), 900
      );
    UPDATE workspace.analysis_run
       SET expires_at = clock_timestamp() - interval '1 second'
     WHERE analysis_run_pk = v_abandoned_run.analysis_run_id;

    -- This fresh reservation proves the expired abandoned row did not hold
    -- the only slot.
    SELECT * INTO STRICT v_admission_run
      FROM workspace.reserve_analysis_upload_v2(
          v_admission_owner, gen_random_uuid(), 'admission.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/admission/source.hwpx', repeat('b', 64), 900
      );

    -- An expired exact replay now needs capacity, and cannot over-admit above
    -- the still-live reservation.
    BEGIN
        PERFORM workspace.reserve_analysis_upload_v2(
            v_abandoned_owner,
            (SELECT idempotency_key FROM workspace.analysis_run
              WHERE analysis_run_pk = v_abandoned_run.analysis_run_id),
            'abandoned.hwpx', 'application/octet-stream', 10, 'request-temp',
            'request-source/v02-runtime/abandoned/source.hwpx', repeat('e', 64), 900
        );
        RAISE EXCEPTION 'v2 expired upload replay over-admitted global capacity';
    EXCEPTION WHEN SQLSTATE '53000' THEN
        NULL;
    END;

    -- A live exact replay remains accepted at the same full capacity.
    SELECT * INTO STRICT v_admission_replay
      FROM workspace.reserve_analysis_upload_v2(
          v_admission_owner,
          (SELECT idempotency_key FROM workspace.analysis_run
            WHERE analysis_run_pk = v_admission_run.analysis_run_id),
          'admission.hwpx', 'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/admission/source.hwpx', repeat('b', 64), 900
      );
    IF v_admission_run.analysis_run_id <> v_admission_replay.analysis_run_id
       OR v_admission_run.replayed OR NOT v_admission_replay.replayed THEN
        RAISE EXCEPTION 'v2 analysis replay was rejected at global capacity';
    END IF;
    UPDATE workspace.analysis_run
       SET status = 'failed', completed_at = clock_timestamp(),
           error_code = 'RUNTIME_ADMISSION_RELEASE'
     WHERE analysis_run_pk = v_admission_run.analysis_run_id;

    -- Once the live slot is released, the expired row can be resumed and then
    -- behaves as a normal live exact replay at its newly acquired capacity.
    SELECT * INTO STRICT v_expired_upload_replay
      FROM workspace.reserve_analysis_upload_v2(
          v_abandoned_owner,
          (SELECT idempotency_key FROM workspace.analysis_run
            WHERE analysis_run_pk = v_abandoned_run.analysis_run_id),
          'abandoned.hwpx', 'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/abandoned/source.hwpx', repeat('e', 64), 900
      );
    SELECT * INTO STRICT v_live_upload_replay
      FROM workspace.reserve_analysis_upload_v2(
          v_abandoned_owner,
          (SELECT idempotency_key FROM workspace.analysis_run
            WHERE analysis_run_pk = v_abandoned_run.analysis_run_id),
          'abandoned.hwpx', 'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/abandoned/source.hwpx', repeat('e', 64), 900
      );
    IF v_expired_upload_replay.analysis_run_id <> v_abandoned_run.analysis_run_id
       OR NOT v_expired_upload_replay.replayed
       OR v_live_upload_replay.analysis_run_id <> v_abandoned_run.analysis_run_id
       OR NOT v_live_upload_replay.replayed
       OR NOT EXISTS (
           SELECT 1 FROM workspace.analysis_run
            WHERE analysis_run_pk = v_abandoned_run.analysis_run_id
              AND status = 'uploading'
              AND expires_at > clock_timestamp()
       ) THEN
        RAISE EXCEPTION 'v2 expired upload replay did not atomically reacquire capacity';
    END IF;
    UPDATE workspace.analysis_run
       SET status = 'failed', completed_at = clock_timestamp(),
           error_code = 'RUNTIME_ADMISSION_RELEASE'
     WHERE analysis_run_pk = v_abandoned_run.analysis_run_id;

    -- Finalisation acquires the same admission lock.  A private object may
    -- have been written after its reservation expired; at full capacity the
    -- RPC must not over-admit it or leave that object orphaned.  It instead
    -- returns a typed cleanup key after atomically fencing the exact row.
    SELECT * INTO STRICT v_finalization_run
      FROM workspace.reserve_analysis_upload_v2(
          v_abandoned_owner, gen_random_uuid(), 'finalize-expired.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/finalize-expired/source.hwpx',
          repeat('9', 64), 900
      );
    UPDATE workspace.analysis_run
       SET expires_at = clock_timestamp() - interval '1 second'
     WHERE analysis_run_pk = v_finalization_run.analysis_run_id;
    SELECT * INTO STRICT v_admission_run
      FROM workspace.reserve_analysis_upload_v2(
          v_admission_owner, gen_random_uuid(), 'finalize-blocker.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/finalize-blocker/source.hwpx',
          repeat('8', 64), 900
      );
    SELECT * INTO STRICT v_finalization_outcome
      FROM workspace.finalize_analysis_upload_v2(
          v_finalization_run.analysis_run_id, v_abandoned_owner,
          'finalize-expired.hwpx', 'application/octet-stream', 10,
          'request-temp', 'request-source/v02-runtime/finalize-expired/source.hwpx',
          repeat('9', 64), 'application/octet-stream', 10, 3600
      );
    IF v_finalization_outcome.status <> 'cleanup_pending'
       OR v_finalization_outcome.outcome <> 'expired_capacity'
       OR v_finalization_outcome.error_code <> 'UPLOAD_RESERVATION_EXPIRED'
       OR NOT EXISTS (
           SELECT 1
             FROM jsonb_array_elements(v_finalization_outcome.cleanup_objects) AS item(value)
            WHERE (item.value->>'analysis_run_pk')::uuid = v_finalization_run.analysis_run_id
              AND item.value->>'source_object_key' =
                  'request-source/v02-runtime/finalize-expired/source.hwpx'
       )
       OR EXISTS (
           SELECT 1 FROM workspace.source_artifact
            WHERE analysis_run_pk = v_finalization_run.analysis_run_id
       ) THEN
        RAISE EXCEPTION 'v2 expired finalisation exceeded capacity or orphaned cleanup';
    END IF;
    UPDATE workspace.analysis_run
       SET status = 'failed', completed_at = clock_timestamp(),
           error_code = 'RUNTIME_ADMISSION_RELEASE'
     WHERE analysis_run_pk = v_admission_run.analysis_run_id;

    -- With capacity released, the same expired-upload path re-admits before
    -- registering its source and exposing exactly one queued job.
    SELECT * INTO STRICT v_finalization_readmit
      FROM workspace.reserve_analysis_upload_v2(
          v_cleanup_owner, gen_random_uuid(), 'finalize-readmit.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/finalize-readmit/source.hwpx',
          repeat('7', 64), 900
      );
    UPDATE workspace.analysis_run
       SET expires_at = clock_timestamp() - interval '1 second'
     WHERE analysis_run_pk = v_finalization_readmit.analysis_run_id;
    SELECT * INTO STRICT v_finalization_readmit_outcome
      FROM workspace.finalize_analysis_upload_v2(
          v_finalization_readmit.analysis_run_id, v_cleanup_owner,
          'finalize-readmit.hwpx', 'application/octet-stream', 10,
          'request-temp', 'request-source/v02-runtime/finalize-readmit/source.hwpx',
          repeat('7', 64), 'application/octet-stream', 10, 3600
      );
    IF v_finalization_readmit_outcome.status <> 'queued'
       OR v_finalization_readmit_outcome.outcome <> 'queued'
       OR NOT EXISTS (
           SELECT 1 FROM workspace.source_artifact
            WHERE analysis_run_pk = v_finalization_readmit.analysis_run_id
              AND artifact_type = 'source'
       ) THEN
        RAISE EXCEPTION 'v2 expired finalisation did not atomically re-admit';
    END IF;
    UPDATE workspace.analysis_run
       SET status = 'failed', completed_at = clock_timestamp(),
           error_code = 'RUNTIME_ADMISSION_RELEASE'
     WHERE analysis_run_pk = v_finalization_readmit.analysis_run_id;

    -- The global gate is shared across analysis and chat.  A fresh chat is
    -- refused while this later analysis reservation occupies the only slot.
    SELECT * INTO STRICT v_admission_run
      FROM workspace.reserve_analysis_upload_v2(
          v_admission_owner, gen_random_uuid(), 'chat-capacity.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/chat-capacity/source.hwpx', repeat('f', 64), 900
      );
    BEGIN
        PERFORM workspace.prepare_conversation_messages_v2(
            v_user_id, v_case_id, 'global capacity blocked chat', gen_random_uuid()
        );
        RAISE EXCEPTION 'v2 global admission accepted chat above analysis capacity';
    EXCEPTION WHEN SQLSTATE '53000' THEN
        NULL;
    END;
    UPDATE workspace.analysis_run
       SET status = 'failed', completed_at = clock_timestamp(),
           error_code = 'RUNTIME_ADMISSION_RELEASE'
     WHERE analysis_run_pk = v_admission_run.analysis_run_id;

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
        PERFORM workspace.reserve_analysis_upload_v2(
            v_admission_owner, gen_random_uuid(), 'blocked.hwpx',
            'application/octet-stream', 10, 'request-temp',
            'request-source/v02-runtime/blocked/source.hwpx', repeat('c', 64), 900
        );
        RAISE EXCEPTION 'v2 global admission accepted analysis above chat capacity';
    EXCEPTION WHEN SQLSTATE '53000' THEN
        NULL;
    END;

    BEGIN
        PERFORM workspace.prepare_conversation_messages_v2(
            v_user_id, v_case_id, '새로운 동시 질문', gen_random_uuid()
        );
        RAISE EXCEPTION 'v2 prepare accepted a second generating assistant turn';
    EXCEPTION WHEN unique_violation THEN
        NULL;
    END;

    -- A retry is also fresh work.  A capacity refusal does not write its retry
    -- ledger, and the same key becomes an exact replay after later admission.
    UPDATE result.conversation_message
       SET status = 'failed', error_code = 'RUNTIME_RETRY'
     WHERE message_pk = v_first.assistant_message_id;
    SELECT * INTO STRICT v_admission_run
      FROM workspace.reserve_analysis_upload_v2(
          v_admission_owner, gen_random_uuid(), 'retry-capacity.hwpx',
          'application/octet-stream', 10, 'request-temp',
          'request-source/v02-runtime/retry-capacity/source.hwpx', repeat('d', 64), 900
      );
    BEGIN
        PERFORM workspace.retry_conversation_message_v2(
            v_user_id, v_case_id, v_first.assistant_message_id, v_retry_key
        );
        RAISE EXCEPTION 'v2 global admission accepted retry above capacity';
    EXCEPTION WHEN SQLSTATE '53000' THEN
        NULL;
    END;
    UPDATE workspace.analysis_run
       SET status = 'failed', completed_at = clock_timestamp(),
           error_code = 'RUNTIME_ADMISSION_RELEASE'
     WHERE analysis_run_pk = v_admission_run.analysis_run_id;
    SELECT * INTO STRICT v_retry
      FROM workspace.retry_conversation_message_v2(
          v_user_id, v_case_id, v_first.assistant_message_id, v_retry_key
      );
    SELECT * INTO STRICT v_retry_replay
      FROM workspace.retry_conversation_message_v2(
          v_user_id, v_case_id, v_first.assistant_message_id, v_retry_key
      );
    IF v_retry.assistant_message_id <> v_retry_replay.assistant_message_id
       OR v_retry.replayed OR NOT v_retry_replay.replayed THEN
        RAISE EXCEPTION 'v2 retry replay was rejected at global capacity';
    END IF;

    BEGIN
        PERFORM workspace.retry_conversation_message_v2(
            v_user_id, v_case_id, gen_random_uuid(), gen_random_uuid()
        );
        RAISE EXCEPTION 'v2 retry accepted an unknown assistant message';
    EXCEPTION WHEN SQLSTATE 'P0002' THEN
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
    IF NOT workspace.heartbeat_conversation_message_v2(
        v_claim.assistant_message_id, v_claim.processing_run_pk, 120
    ) THEN
        RAISE EXCEPTION 'v2 chat heartbeat rejected a live claim';
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
