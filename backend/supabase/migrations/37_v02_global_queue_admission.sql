-- ============================================================================
-- Migration 37: atomic global API queue admission / backpressure
--
-- Analysis upload reservations and chat create/retry all acquire the same
-- transaction advisory lock immediately before they make a *new* unit of
-- worker work visible.  Exact idempotency replays deliberately return before
-- the lock/capacity check, so a lost 202 remains safely replayable while the
-- queue is full.
-- ============================================================================

BEGIN;

-- Both branches of the admission count are hot under one global advisory
-- lock. Analysis already has status indexes from the base schema; keep chat's
-- terminal history out of the generating-message scan as well.
CREATE INDEX IF NOT EXISTS ix_result_conversation_message_generating_assistant
    ON result.conversation_message (message_pk)
 WHERE role = 'assistant' AND status = 'generating';

-- The API sets this custom GUC with set_config(..., true) in the same
-- transaction as its trusted mutation RPC.  A bounded fallback keeps direct
-- service-role maintenance calls fail-closed if they omit the setting.
CREATE OR REPLACE FUNCTION workspace.global_queue_limit_v2()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_config TEXT;
    v_limit INTEGER;
BEGIN
    v_config := NULLIF(
        btrim(current_setting('prereview.global_queue_max', TRUE)),
        ''
    );
    IF v_config IS NULL THEN
        RETURN 25;
    END IF;
    IF v_config !~ '^[0-9]{1,5}$' THEN
        RAISE EXCEPTION 'INVALID_GLOBAL_QUEUE_LIMIT' USING ERRCODE = '22023';
    END IF;
    v_limit := v_config::INTEGER;
    IF v_limit NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION 'INVALID_GLOBAL_QUEUE_LIMIT' USING ERRCODE = '22023';
    END IF;
    RETURN v_limit;
END;
$$;

-- This is the only global admission gate.  The transaction lock is held
-- through the caller's INSERT/UPDATE and COMMIT, so two accepted concurrent
-- calls cannot both observe the final free slot.  Expired upload/queue rows
-- and retained-but-expired chat cases do not consume a slot; their workers or
-- owner-scoped lifecycle calls reconcile them terminally on normal polling.
-- An exact replay of an expired upload reacquires this same lock before it
-- renews its TTL, so it cannot turn a non-counted abandoned reservation into
-- an extra admitted job.
CREATE OR REPLACE FUNCTION workspace.admit_global_queue_work_v2()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_limit INTEGER;
    v_active_work BIGINT;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('prereview/global-queue-admission/v2', 0)
    );
    v_limit := workspace.global_queue_limit_v2();

    SELECT
        (
            SELECT count(*)
              FROM workspace.analysis_run AS run
             WHERE (run.status IN ('uploading', 'queued') AND run.expires_at > v_now)
                OR run.status = 'running'
        ) + (
            SELECT count(*)
              FROM workspace.conversation_message_dispatch AS dispatch
              JOIN result.conversation_message AS assistant
                ON assistant.message_pk = dispatch.assistant_message_pk
              JOIN result.analysis_case AS analysis_case
                ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
             WHERE assistant.role = 'assistant'
               AND assistant.status = 'generating'
               AND analysis_case.retention_expires_at > v_now
        )
      INTO v_active_work;

    IF v_active_work >= v_limit THEN
        RAISE EXCEPTION 'GLOBAL_QUEUE_CAPACITY_EXCEEDED' USING ERRCODE = '53000';
    END IF;
END;
$$;

-- Keep the v0.2 function signature stable for service-role callers, but put
-- global admission after exact replay and the owner-specific lifecycle
-- checks.  The global transaction lock remains held while this function adds
-- the uploading reservation, which deliberately reserves a slot before the
-- private object upload begins.
CREATE OR REPLACE FUNCTION workspace.reserve_analysis_upload_v2(
    p_user_id UUID,
    p_idempotency_key UUID,
    p_original_filename TEXT,
    p_declared_mime_type TEXT,
    p_declared_size_bytes BIGINT,
    p_source_bucket TEXT,
    p_source_object_key TEXT,
    p_source_content_sha256 TEXT,
    p_upload_ttl_seconds INTEGER DEFAULT 900
)
RETURNS TABLE (
    analysis_run_id UUID,
    status TEXT,
    replayed BOOLEAN,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    cleanup_objects JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
DECLARE
    v_existing_run workspace.analysis_run%ROWTYPE;
    v_existing_dispatch workspace.analysis_run_dispatch%ROWTYPE;
    v_new_run_id UUID := gen_random_uuid();
    v_now TIMESTAMPTZ := clock_timestamp();
    v_cleanup_objects JSONB := '[]'::jsonb;
BEGIN
    IF p_user_id IS NULL OR p_idempotency_key IS NULL
       OR btrim(COALESCE(p_original_filename, '')) = ''
       OR p_declared_size_bytes IS NULL OR p_declared_size_bytes < 0
       OR btrim(COALESCE(p_source_bucket, '')) = ''
       OR btrim(COALESCE(p_source_object_key, '')) = ''
       OR COALESCE(p_source_content_sha256, '') !~* '^[0-9a-f]{64}$'
       OR p_upload_ttl_seconds IS NULL
       OR p_upload_ttl_seconds NOT BETWEEN 60 AND 3600 THEN
        RAISE EXCEPTION 'INVALID_UPLOAD_RESERVATION' USING ERRCODE = '22023';
    END IF;

    PERFORM workspace.lock_analysis_lifecycle_user_v2(p_user_id);

    SELECT run.*
      INTO v_existing_run
      FROM workspace.analysis_run AS run
     WHERE run.user_id = p_user_id
       AND run.idempotency_key = p_idempotency_key
     FOR UPDATE;

    IF FOUND THEN
        SELECT dispatch.*
          INTO v_existing_dispatch
          FROM workspace.analysis_run_dispatch AS dispatch
         WHERE dispatch.analysis_run_pk = v_existing_run.analysis_run_pk
         FOR UPDATE;

        IF v_existing_dispatch.analysis_run_pk IS NULL
           OR ROW(
               v_existing_run.original_filename,
               v_existing_run.declared_mime_type,
               v_existing_run.declared_size_bytes,
               v_existing_dispatch.source_bucket,
               v_existing_dispatch.source_object_key,
               lower(v_existing_dispatch.source_content_sha256)
           ) IS DISTINCT FROM ROW(
               p_original_filename,
               p_declared_mime_type,
               p_declared_size_bytes,
               p_source_bucket,
               p_source_object_key,
               lower(p_source_content_sha256)
           ) THEN
            RAISE EXCEPTION 'IDEMPOTENCY_KEY_CONFLICT' USING ERRCODE = '23505';
        END IF;

        IF v_existing_run.status = 'uploading' THEN
            -- The owner lock may have waited behind another lifecycle call;
            -- decide replay liveness at the locked row, not at function
            -- entry, or a just-expired reservation could bypass admission.
            v_now := clock_timestamp();
            -- A live reservation already occupies a counted slot and is an
            -- exact replay even when the cap is full.  An expired one was
            -- deliberately excluded from the count above, so atomically
            -- reacquire a slot before making it live again.
            IF v_existing_run.expires_at <= v_now THEN
                PERFORM workspace.admit_global_queue_work_v2();
            END IF;
            UPDATE workspace.analysis_run
               SET expires_at = v_now + make_interval(secs => p_upload_ttl_seconds)
             WHERE analysis_run_pk = v_existing_run.analysis_run_pk
            RETURNING * INTO v_existing_run;
        END IF;

        IF v_existing_run.status = 'cleanup_pending' THEN
            v_cleanup_objects := jsonb_build_array(jsonb_build_object(
                'analysis_run_pk', v_existing_run.analysis_run_pk,
                'source_bucket', v_existing_dispatch.source_bucket,
                'source_object_key', v_existing_dispatch.source_object_key
            ));
        END IF;

        RETURN QUERY SELECT
            v_existing_run.analysis_run_pk,
            v_existing_run.status,
            TRUE,
            v_existing_run.error_code,
            v_existing_run.error_message,
            v_existing_run.created_at,
            v_existing_run.updated_at,
            v_cleanup_objects;
        RETURN;
    END IF;

    SELECT COALESCE(
               jsonb_agg(jsonb_build_object(
                   'analysis_run_pk', cleanup.analysis_run_id,
                   'source_bucket', cleanup.source_bucket,
                   'source_object_key', cleanup.source_object_key
               )),
               '[]'::jsonb
           )
      INTO v_cleanup_objects
      FROM workspace.reconcile_stale_analysis_runs_for_user_v2(p_user_id) AS cleanup;

    IF EXISTS (
        SELECT 1
          FROM workspace.analysis_run AS run
         WHERE run.user_id = p_user_id
           AND run.status IN ('uploading', 'queued', 'running')
         FOR KEY SHARE
    ) THEN
        RAISE EXCEPTION 'ANALYSIS_RUN_ACTIVE' USING ERRCODE = '23505';
    END IF;

    UPDATE result.analysis_session AS session
       SET status = 'expired', updated_at = v_now
      FROM result.analysis_case AS analysis_case
     WHERE session.analysis_case_pk = analysis_case.analysis_case_pk
       AND session.user_id = p_user_id
       AND analysis_case.user_id = p_user_id
       AND session.status = 'active'
       AND session.expires_at <= v_now;

    IF EXISTS (
        SELECT 1
          FROM result.analysis_session AS session
         WHERE session.user_id = p_user_id
           AND session.status = 'active'
           AND session.expires_at > v_now
         FOR KEY SHARE
    ) THEN
        RAISE EXCEPTION 'ACTIVE_RESULT_SESSION' USING ERRCODE = '23505';
    END IF;

    PERFORM workspace.admit_global_queue_work_v2();

    INSERT INTO workspace.analysis_run (
        analysis_run_pk, user_id, idempotency_key, status, original_filename,
        declared_mime_type, declared_size_bytes, expires_at
    ) VALUES (
        v_new_run_id, p_user_id, p_idempotency_key, 'uploading',
        p_original_filename, p_declared_mime_type, p_declared_size_bytes,
        v_now + make_interval(secs => p_upload_ttl_seconds)
    )
    RETURNING * INTO v_existing_run;

    INSERT INTO workspace.analysis_run_dispatch (
        analysis_run_pk, source_bucket, source_object_key, source_content_sha256
    ) VALUES (
        v_new_run_id, p_source_bucket, p_source_object_key, p_source_content_sha256
    );

    RETURN QUERY SELECT
        v_existing_run.analysis_run_pk,
        v_existing_run.status,
        FALSE,
        v_existing_run.error_code,
        v_existing_run.error_message,
        v_existing_run.created_at,
        v_existing_run.updated_at,
        v_cleanup_objects;
END;
$$;

-- The session row continues to serialize with close.  A replay returns before
-- capacity, while a new user/assistant pair acquires the global lock before
-- either row or dispatch is inserted.
CREATE OR REPLACE FUNCTION workspace.prepare_conversation_messages_v2(
    p_user_id UUID,
    p_analysis_case_id UUID,
    p_content TEXT,
    p_idempotency_key UUID
)
RETURNS TABLE (
    user_message_id UUID,
    assistant_message_id UUID,
    analysis_session_id UUID,
    replayed BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
DECLARE
    v_session result.analysis_session%ROWTYPE;
    v_existing_user_message_id UUID;
    v_existing_content_sha256 TEXT;
    v_assistant_id UUID;
    v_content TEXT := btrim(COALESCE(p_content, ''));
    v_content_sha256 TEXT;
    v_sequence_no INTEGER;
BEGIN
    IF p_user_id IS NULL OR p_analysis_case_id IS NULL OR p_idempotency_key IS NULL
       OR v_content = '' OR char_length(v_content) > 4000 THEN
        RAISE EXCEPTION 'INVALID_CHAT_CREATE_REQUEST' USING ERRCODE = '22023';
    END IF;
    v_content_sha256 := encode(extensions.digest(v_content, 'sha256'), 'hex');

    SELECT session.*
      INTO v_session
      FROM result.analysis_session AS session
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = session.analysis_case_pk
     WHERE session.analysis_case_pk = p_analysis_case_id
       AND session.user_id = p_user_id
       AND analysis_case.user_id = p_user_id
       AND analysis_case.retention_expires_at > clock_timestamp()
     FOR UPDATE OF session, analysis_case;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'CONVERSATION_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;

    SELECT message.message_pk,
           message.content_sha256,
           assistant.message_pk
      INTO v_existing_user_message_id, v_existing_content_sha256, v_assistant_id
      FROM result.conversation_message AS message
      JOIN result.conversation_message AS assistant
        ON assistant.reply_to_message_pk = message.message_pk
       AND assistant.role = 'assistant'
     WHERE message.analysis_session_pk = v_session.analysis_session_pk
       AND message.role = 'user'
       AND message.idempotency_key = p_idempotency_key
     FOR UPDATE OF message, assistant;
    IF FOUND THEN
        IF v_existing_content_sha256 IS DISTINCT FROM v_content_sha256 THEN
            RAISE EXCEPTION 'IDEMPOTENCY_KEY_CONFLICT' USING ERRCODE = '23505';
        END IF;
        RETURN QUERY SELECT v_existing_user_message_id, v_assistant_id,
                             v_session.analysis_session_pk, TRUE;
        RETURN;
    END IF;

    IF v_session.status <> 'active' OR v_session.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_EXPIRED' USING ERRCODE = 'P0002';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM result.conversation_message AS assistant
         WHERE assistant.analysis_session_pk = v_session.analysis_session_pk
           AND assistant.role = 'assistant'
           AND assistant.status = 'generating'
         FOR KEY SHARE
    ) THEN
        RAISE EXCEPTION 'CHAT_CONFLICT' USING ERRCODE = '23505';
    END IF;

    PERFORM workspace.admit_global_queue_work_v2();
    PERFORM pg_advisory_xact_lock(hashtextextended(v_session.analysis_session_pk::text, 0));
    SELECT COALESCE(MAX(sequence_no), 0)
      INTO v_sequence_no
      FROM result.conversation_message
     WHERE analysis_session_pk = v_session.analysis_session_pk;

    user_message_id := gen_random_uuid();
    assistant_message_id := gen_random_uuid();
    INSERT INTO result.conversation_message (
        message_pk, analysis_session_pk, role, sequence_no, content, status,
        idempotency_key, content_sha256
    ) VALUES (
        user_message_id, v_session.analysis_session_pk, 'user', v_sequence_no + 1,
        v_content, 'completed', p_idempotency_key, v_content_sha256
    );
    INSERT INTO result.conversation_message (
        message_pk, analysis_session_pk, role, sequence_no, content, status,
        reply_to_message_pk
    ) VALUES (
        assistant_message_id, v_session.analysis_session_pk, 'assistant', v_sequence_no + 2,
        '', 'generating', user_message_id
    );
    INSERT INTO workspace.conversation_message_dispatch (
        assistant_message_pk, analysis_case_pk, analysis_session_pk, user_message_pk
    ) VALUES (
        assistant_message_id, p_analysis_case_id, v_session.analysis_session_pk, user_message_id
    );
    analysis_session_id := v_session.analysis_session_pk;
    replayed := FALSE;
    RETURN NEXT;
END;
$$;

-- Manual retry reuses the existing assistant message.  The idempotency ledger
-- is consulted before the global gate; only a fresh failed->generating retry
-- must reserve a slot.
CREATE OR REPLACE FUNCTION workspace.retry_conversation_message_v2(
    p_user_id UUID,
    p_analysis_case_id UUID,
    p_assistant_message_id UUID,
    p_idempotency_key UUID
)
RETURNS TABLE (
    assistant_message_id UUID,
    user_message_id UUID,
    analysis_case_id UUID,
    analysis_session_id UUID,
    retry_count INTEGER,
    replayed BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
DECLARE
    v_session result.analysis_session%ROWTYPE;
    v_message result.conversation_message%ROWTYPE;
    v_dispatch workspace.conversation_message_dispatch%ROWTYPE;
    v_existing_assistant_id UUID;
BEGIN
    IF p_user_id IS NULL OR p_analysis_case_id IS NULL
       OR p_assistant_message_id IS NULL OR p_idempotency_key IS NULL THEN
        RAISE EXCEPTION 'INVALID_CHAT_RETRY_REQUEST' USING ERRCODE = '22023';
    END IF;

    SELECT session.*
      INTO v_session
      FROM result.analysis_session AS session
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = session.analysis_case_pk
     WHERE session.analysis_case_pk = p_analysis_case_id
       AND session.user_id = p_user_id
       AND analysis_case.user_id = p_user_id
       AND analysis_case.retention_expires_at > clock_timestamp()
     FOR UPDATE OF session, analysis_case;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'CONVERSATION_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;

    SELECT assistant_message_pk
      INTO v_existing_assistant_id
      FROM workspace.conversation_retry_idempotency_v2
     WHERE analysis_session_pk = v_session.analysis_session_pk
       AND idempotency_key = p_idempotency_key
     FOR UPDATE;
    IF FOUND THEN
        IF v_existing_assistant_id <> p_assistant_message_id THEN
            RAISE EXCEPTION 'IDEMPOTENCY_KEY_CONFLICT' USING ERRCODE = '23505';
        END IF;
        SELECT * INTO v_message
          FROM result.conversation_message
         WHERE message_pk = v_existing_assistant_id
           AND analysis_session_pk = v_session.analysis_session_pk
           AND role = 'assistant';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'CONVERSATION_NOT_FOUND' USING ERRCODE = 'P0002';
        END IF;
        RETURN QUERY SELECT
            v_message.message_pk, v_message.reply_to_message_pk,
            p_analysis_case_id, v_session.analysis_session_pk,
            v_message.auto_retry_count + v_message.manual_retry_count, TRUE;
        RETURN;
    END IF;

    IF v_session.status <> 'active' OR v_session.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_EXPIRED' USING ERRCODE = 'P0002';
    END IF;

    SELECT message.*
      INTO v_message
      FROM result.conversation_message AS message
     WHERE message.message_pk = p_assistant_message_id
       AND message.analysis_session_pk = v_session.analysis_session_pk
       AND message.role = 'assistant'
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'CONVERSATION_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;

    -- The legacy retry routine validates status, cooldown, lease, and retry
    -- budget.  Reject capacity only after those domain checks have succeeded
    -- in this wrapper, before the new idempotency row or dispatch is written.
    IF v_message.status <> 'failed' THEN
        RAISE EXCEPTION 'CHAT_MESSAGE_NOT_RETRYABLE' USING ERRCODE = '22023';
    END IF;
    IF v_message.manual_retry_count >= 2 THEN
        RAISE EXCEPTION 'CHAT_RETRY_EXHAUSTED' USING ERRCODE = '22023';
    END IF;
    IF v_message.last_retry_at IS NOT NULL
       AND v_message.last_retry_at > clock_timestamp() - interval '5 seconds' THEN
        RAISE EXCEPTION 'CHAT_RETRY_COOLDOWN' USING ERRCODE = '55P03';
    END IF;

    SELECT dispatch.*
      INTO v_dispatch
      FROM workspace.conversation_message_dispatch AS dispatch
     WHERE dispatch.assistant_message_pk = v_message.message_pk
     FOR UPDATE;
    IF v_dispatch.processing_run_pk IS NOT NULL
       AND v_dispatch.lease_expires_at > clock_timestamp() THEN
        RAISE EXCEPTION 'CHAT_MESSAGE_BUSY' USING ERRCODE = '55P03';
    END IF;

    PERFORM workspace.admit_global_queue_work_v2();

    INSERT INTO workspace.conversation_retry_idempotency_v2 (
        analysis_session_pk, idempotency_key, assistant_message_pk
    ) VALUES (v_session.analysis_session_pk, p_idempotency_key, p_assistant_message_id);

    RETURN QUERY
    SELECT retry.assistant_message_id, retry.user_message_id,
           retry.analysis_case_id, retry.analysis_session_id,
           retry.retry_count, FALSE
      FROM workspace.retry_conversation_message(
          p_user_id, p_assistant_message_id, p_analysis_case_id
      ) AS retry;
END;
$$;

REVOKE ALL ON FUNCTION workspace.global_queue_limit_v2()
FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION workspace.admit_global_queue_work_v2()
FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION workspace.reserve_analysis_upload_v2(UUID, UUID, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, INTEGER)
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.prepare_conversation_messages_v2(UUID, UUID, TEXT, UUID)
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.retry_conversation_message_v2(UUID, UUID, UUID, UUID)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION workspace.reserve_analysis_upload_v2(UUID, UUID, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, INTEGER),
                         workspace.prepare_conversation_messages_v2(UUID, UUID, TEXT, UUID),
                         workspace.retry_conversation_message_v2(UUID, UUID, UUID, UUID)
TO service_role;

COMMIT;
