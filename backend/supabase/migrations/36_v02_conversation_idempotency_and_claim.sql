-- ============================================================================
-- Migration 36: v0.2 conversation idempotency, paging, and claim semantics
-- ============================================================================

BEGIN;

ALTER TABLE result.conversation_message
    ADD COLUMN IF NOT EXISTS idempotency_key UUID NULL,
    ADD COLUMN IF NOT EXISTS content_sha256 TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'result.conversation_message'::regclass
           AND conname = 'ck_result_conversation_message_content_sha256_v2'
    ) THEN
        ALTER TABLE result.conversation_message
            ADD CONSTRAINT ck_result_conversation_message_content_sha256_v2
            CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9A-Fa-f]{64}$');
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_result_conversation_user_idempotency_v2
    ON result.conversation_message (analysis_session_pk, idempotency_key)
 WHERE role = 'user' AND idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS workspace.conversation_retry_idempotency_v2 (
    analysis_session_pk UUID NOT NULL
        REFERENCES result.analysis_session(analysis_session_pk) ON DELETE CASCADE,
    idempotency_key UUID NOT NULL,
    assistant_message_pk UUID NOT NULL
        REFERENCES result.conversation_message(message_pk) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (analysis_session_pk, idempotency_key)
);

ALTER TABLE workspace.conversation_retry_idempotency_v2 ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE workspace.conversation_retry_idempotency_v2
FROM PUBLIC, anon, authenticated;

CREATE INDEX IF NOT EXISTS ix_workspace_conversation_retry_assistant_v2
    ON workspace.conversation_retry_idempotency_v2 (assistant_message_pk);

-- Create is serialized with close by its session row lock.  The exact
-- idempotency lookup is made before the active/expiry check so a lost 202 can
-- be replayed without creating a second assistant dispatch.
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

    -- At most one generating assistant turn per session.  This comes after an
    -- exact replay so a lost 202 stays replayable while a fresh HTTP burst
    -- cannot create unbounded OpenAI work or reorder its grounding context.
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

-- A retry gets its own idempotency ledger.  The same retry key returns the
-- same assistant message even after its response moved from generating to a
-- terminal state; reuse for another assistant is a conflict.
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

    -- Resolve and lock the requested assistant before writing the retry
    -- idempotency ledger.  Otherwise a guessed/nonexistent UUID fails through
    -- the ledger FK as 23503 and the API incorrectly reports a DB outage
    -- instead of the owner-scoped 404 contract.
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

    -- The legacy retry function has the mature retry budget/fence handling.
    -- This insert is in the same transaction, so any rejection rolls it back.
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

CREATE OR REPLACE FUNCTION api.rpc_get_conversation_message_v2(
    p_user_id UUID,
    p_analysis_case_id UUID,
    p_message_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, api, result
AS $$
DECLARE
    v_payload JSONB;
BEGIN
    SELECT jsonb_build_object(
        'message_id', message.message_pk,
        'analysis_case_id', analysis_case.analysis_case_pk,
        'role', message.role,
        'sequence_no', message.sequence_no,
        'content', CASE WHEN message.status = 'generating' THEN NULL ELSE message.content END,
        'status', message.status,
        'reply_to_message_id', message.reply_to_message_pk,
        'retry_count', message.auto_retry_count + message.manual_retry_count,
        'error_code', message.error_code,
        'error_message', message.error_message,
        'created_at', message.created_at,
        'updated_at', message.updated_at
    ) INTO v_payload
      FROM result.conversation_message AS message
      JOIN result.analysis_session AS session
        ON session.analysis_session_pk = message.analysis_session_pk
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = session.analysis_case_pk
     WHERE message.message_pk = p_message_id
       AND message.role = 'assistant'
       AND analysis_case.analysis_case_pk = p_analysis_case_id
       AND analysis_case.user_id = p_user_id
       AND analysis_case.retention_expires_at > clock_timestamp();
    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'CONVERSATION_MESSAGE_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;
    RETURN v_payload;
END;
$$;

-- The cursor is parsed/HMAC-bound by FastAPI.  The SQL function accepts the
-- position only and returns older rows in chronological display order.
CREATE OR REPLACE FUNCTION api.rpc_get_conversation_history_v2(
    p_user_id UUID,
    p_analysis_case_id UUID,
    p_before_sequence_no INTEGER DEFAULT NULL,
    p_before_message_id UUID DEFAULT NULL,
    p_limit INTEGER DEFAULT 50
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, api, result
AS $$
DECLARE
    v_items JSONB;
    v_next_sequence INTEGER;
    v_next_message UUID;
BEGIN
    IF p_user_id IS NULL OR p_analysis_case_id IS NULL
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
       OR (p_before_sequence_no IS NULL) <> (p_before_message_id IS NULL)
       OR (p_before_sequence_no IS NOT NULL AND p_before_sequence_no < 1) THEN
        RAISE EXCEPTION 'INVALID_CONVERSATION_CURSOR' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM result.analysis_case AS analysis_case
         WHERE analysis_case.analysis_case_pk = p_analysis_case_id
           AND analysis_case.user_id = p_user_id
           AND analysis_case.retention_expires_at > clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'CONVERSATION_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;

    WITH page AS MATERIALIZED (
        SELECT message.message_pk, message.sequence_no, message.role,
               message.content, message.status, message.reply_to_message_pk,
               message.auto_retry_count + message.manual_retry_count AS retry_count,
               message.error_code, message.error_message,
               message.created_at, message.updated_at
          FROM result.conversation_message AS message
          JOIN result.analysis_session AS session
            ON session.analysis_session_pk = message.analysis_session_pk
         WHERE session.analysis_case_pk = p_analysis_case_id
           AND (
               p_before_sequence_no IS NULL
               OR (message.sequence_no, message.message_pk)
                    < (p_before_sequence_no, p_before_message_id)
           )
         ORDER BY message.sequence_no DESC, message.message_pk DESC
         LIMIT p_limit + 1
    ), visible AS (
        SELECT * FROM page
         ORDER BY sequence_no DESC, message_pk DESC
         LIMIT p_limit
    ), chronological AS (
        SELECT * FROM visible ORDER BY sequence_no, message_pk
    ), successor AS (
        SELECT sequence_no, message_pk
          FROM page
         ORDER BY sequence_no DESC, message_pk DESC
         OFFSET p_limit LIMIT 1
    ), next_cursor AS (
        -- As in analysis history, use the oldest returned item as the strict
        -- '<' cursor only when the limit+1 probe found an older successor.
        SELECT visible.sequence_no, visible.message_pk
          FROM visible
         WHERE EXISTS (SELECT 1 FROM successor)
         ORDER BY visible.sequence_no, visible.message_pk
         LIMIT 1
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'message_id', chronological.message_pk,
               'analysis_case_id', p_analysis_case_id,
               'role', chronological.role,
               'sequence_no', chronological.sequence_no,
               'content', CASE WHEN chronological.status = 'generating' THEN NULL ELSE chronological.content END,
               'status', chronological.status,
               'reply_to_message_id', chronological.reply_to_message_pk,
               'retry_count', chronological.retry_count,
               'error_code', chronological.error_code,
               'error_message', chronological.error_message,
               'created_at', chronological.created_at,
               'updated_at', chronological.updated_at
           ) ORDER BY chronological.sequence_no, chronological.message_pk), '[]'::jsonb),
           (SELECT sequence_no FROM next_cursor),
           (SELECT message_pk FROM next_cursor)
      INTO v_items, v_next_sequence, v_next_message
      FROM chronological;
    RETURN jsonb_build_object(
        'items', v_items,
        'next_sequence_no', v_next_sequence,
        'next_message_id', v_next_message
    );
END;
$$;

-- v2 claim differs from migration 27 in one important way: after a create
-- committed, close/expiry no longer cancels its generating dispatch.  Claim,
-- heartbeat, complete and fail use message state, case retention and fencing
-- only.  Retention expiry itself terminally fails unfinished work.
CREATE OR REPLACE FUNCTION workspace.claim_next_conversation_message_v2(
    p_worker_id TEXT,
    p_lease_seconds INTEGER DEFAULT 120
)
RETURNS TABLE (
    assistant_message_id UUID,
    analysis_case_id UUID,
    analysis_session_id UUID,
    user_message_id UUID,
    owner_id UUID,
    question TEXT,
    conversation JSONB,
    result_payload JSONB,
    processing_run_pk UUID,
    attempt_count INTEGER,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_interval_seconds INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, api, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_dispatch workspace.conversation_message_dispatch%ROWTYPE;
    v_owner_id UUID;
    v_question TEXT;
    v_user_sequence INTEGER;
    v_processing_run UUID;
    v_conversation JSONB;
BEGIN
    IF btrim(COALESCE(p_worker_id, '')) = ''
       OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'INVALID_CHAT_WORKER_CLAIM' USING ERRCODE = '22023';
    END IF;

    -- Expired retention is a terminal outcome regardless of session state.
    UPDATE ops.processing_run AS processing
       SET status = 'failed', finished_at = v_now,
           error_code = 'CHAT_RETENTION_EXPIRED',
           error_message = 'Conversation retention expired before completion.'
      FROM workspace.conversation_message_dispatch AS dispatch
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
      JOIN result.conversation_message AS message
        ON message.message_pk = dispatch.assistant_message_pk
     WHERE processing.processing_run_pk = dispatch.processing_run_pk
       AND analysis_case.retention_expires_at <= v_now
       AND message.status = 'generating'
       AND processing.status IN ('queued','running');
    UPDATE result.conversation_message AS message
       SET status = 'failed', error_code = 'CHAT_RETENTION_EXPIRED',
           error_message = '대화 보관 기간이 만료되었습니다.'
      FROM workspace.conversation_message_dispatch AS dispatch
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
     WHERE message.message_pk = dispatch.assistant_message_pk
       AND message.status = 'generating'
       AND analysis_case.retention_expires_at <= v_now;
    UPDATE workspace.conversation_message_dispatch AS dispatch
       SET processing_run_pk = NULL, claimed_by = NULL, claimed_at = NULL,
           heartbeat_at = NULL, lease_expires_at = NULL,
           last_error_code = 'CHAT_RETENTION_EXPIRED',
           last_error_message = 'Conversation retention expired before completion.'
      FROM result.conversation_message AS message
      JOIN result.analysis_session AS session
        ON session.analysis_session_pk = message.analysis_session_pk
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = session.analysis_case_pk
     WHERE message.message_pk = dispatch.assistant_message_pk
       AND message.status = 'failed'
       AND message.error_code = 'CHAT_RETENTION_EXPIRED'
       AND analysis_case.analysis_case_pk = dispatch.analysis_case_pk
       AND analysis_case.retention_expires_at <= v_now
       AND dispatch.last_error_code IS DISTINCT FROM 'CHAT_RETENTION_EXPIRED';

    -- Second expired lease is terminal; first expired lease can be reclaimed.
    UPDATE ops.processing_run AS processing
       SET status = 'failed', finished_at = v_now,
           error_code = 'WORKER_LEASE_EXPIRED',
           error_message = 'Chat worker lease expired after two attempts.'
      FROM workspace.conversation_message_dispatch AS dispatch
      JOIN result.conversation_message AS message
        ON message.message_pk = dispatch.assistant_message_pk
     WHERE processing.processing_run_pk = dispatch.processing_run_pk
       AND dispatch.attempt_count >= 2
       AND dispatch.lease_expires_at <= v_now
       AND message.status = 'generating'
       AND processing.status IN ('queued','running');
    UPDATE result.conversation_message AS message
       SET status = 'failed', error_code = 'WORKER_MAX_ATTEMPTS_EXCEEDED',
           error_message = 'AI 답변 생성 시간이 초과되었습니다.'
      FROM workspace.conversation_message_dispatch AS dispatch
     WHERE message.message_pk = dispatch.assistant_message_pk
       AND dispatch.attempt_count >= 2
       AND dispatch.lease_expires_at <= v_now
       AND message.status = 'generating';
    UPDATE workspace.conversation_message_dispatch AS dispatch
       SET processing_run_pk = NULL, claimed_by = NULL, claimed_at = NULL,
           heartbeat_at = NULL, lease_expires_at = NULL,
           last_error_code = 'WORKER_MAX_ATTEMPTS_EXCEEDED',
           last_error_message = 'Chat worker lease expired after two attempts.'
      FROM result.conversation_message AS message
     WHERE message.message_pk = dispatch.assistant_message_pk
       AND message.status = 'failed'
       AND dispatch.attempt_count >= 2
       AND dispatch.lease_expires_at <= v_now;

    SELECT dispatch.*
      INTO v_dispatch
      FROM workspace.conversation_message_dispatch AS dispatch
      JOIN result.conversation_message AS assistant
        ON assistant.message_pk = dispatch.assistant_message_pk
      JOIN result.conversation_message AS user_message
        ON user_message.message_pk = dispatch.user_message_pk
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
     WHERE assistant.status = 'generating'
       AND dispatch.attempt_count < 2
       AND analysis_case.retention_expires_at > v_now
       AND (dispatch.processing_run_pk IS NULL OR dispatch.lease_expires_at <= v_now)
     ORDER BY dispatch.created_at, dispatch.assistant_message_pk
     LIMIT 1
     FOR UPDATE OF dispatch, assistant, user_message, analysis_case SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT analysis_case.user_id, user_message.content, user_message.sequence_no
      INTO v_owner_id, v_question, v_user_sequence
      FROM result.conversation_message AS user_message
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = v_dispatch.analysis_case_pk
     WHERE user_message.message_pk = v_dispatch.user_message_pk;

    IF v_dispatch.processing_run_pk IS NOT NULL THEN
        UPDATE ops.processing_run AS processing
           SET status = 'failed', finished_at = v_now,
               error_code = 'WORKER_LEASE_EXPIRED',
               error_message = 'Chat worker lease expired before completion.'
         WHERE processing.processing_run_pk = v_dispatch.processing_run_pk
           AND processing.status IN ('queued','running');
    END IF;

    SELECT COALESCE(jsonb_agg(jsonb_build_object('role', prior.role, 'content', prior.content)
                     ORDER BY prior.sequence_no), '[]'::jsonb)
      INTO v_conversation
      FROM (
          SELECT message.role, message.content, message.sequence_no
            FROM result.conversation_message AS message
           WHERE message.analysis_session_pk = v_dispatch.analysis_session_pk
             AND message.sequence_no < v_user_sequence
             AND message.status = 'completed'
             AND message.role IN ('user','assistant')
             AND btrim(message.content) <> ''
           ORDER BY message.sequence_no DESC
           LIMIT 20
      ) AS prior;

    INSERT INTO ops.processing_run AS processing (
        source_analysis_run_id, run_type, status, started_at, run_metadata
    ) VALUES (
        NULL, 'chat', 'running', v_now,
        jsonb_build_object('assistant_message_id', v_dispatch.assistant_message_pk,
                           'analysis_case_id', v_dispatch.analysis_case_pk,
                           'worker_id', btrim(p_worker_id),
                           'attempt_no', v_dispatch.attempt_count + 1,
                           'lease_seconds', p_lease_seconds)
    ) RETURNING processing.processing_run_pk INTO v_processing_run;

    UPDATE workspace.conversation_message_dispatch
       SET attempt_count = v_dispatch.attempt_count + 1,
           processing_run_pk = v_processing_run, claimed_by = btrim(p_worker_id),
           claimed_at = v_now, heartbeat_at = v_now,
           lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
           result_payload = api.public_analysis_projection_v2(v_dispatch.analysis_case_pk, v_owner_id),
           last_error_code = NULL, last_error_message = NULL
     WHERE assistant_message_pk = v_dispatch.assistant_message_pk;

    RETURN QUERY SELECT
        v_dispatch.assistant_message_pk, v_dispatch.analysis_case_pk,
        v_dispatch.analysis_session_pk, v_dispatch.user_message_pk, v_owner_id,
        v_question, v_conversation,
        api.public_analysis_projection_v2(v_dispatch.analysis_case_pk, v_owner_id),
        v_processing_run, v_dispatch.attempt_count + 1,
        v_now + make_interval(secs => p_lease_seconds), 30;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.heartbeat_conversation_message_v2(
    p_assistant_message_id UUID,
    p_processing_run_pk UUID,
    p_lease_seconds INTEGER DEFAULT 120
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_live BOOLEAN := FALSE;
BEGIN
    IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'LEASE_SECONDS_OUT_OF_RANGE' USING ERRCODE = '22023';
    END IF;
    UPDATE workspace.conversation_message_dispatch AS dispatch
       SET heartbeat_at = v_now,
           lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
      FROM result.conversation_message AS message,
           result.analysis_case AS analysis_case
     WHERE dispatch.assistant_message_pk = p_assistant_message_id
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND message.message_pk = dispatch.assistant_message_pk
       AND analysis_case.analysis_case_pk = dispatch.analysis_case_pk
       AND message.status = 'generating'
       AND analysis_case.retention_expires_at > v_now
       AND dispatch.lease_expires_at > v_now
    RETURNING TRUE INTO v_live;
    RETURN COALESCE(v_live, FALSE);
END;
$$;

CREATE OR REPLACE FUNCTION workspace.complete_conversation_message_v2(
    p_assistant_message_id UUID,
    p_processing_run_pk UUID,
    p_content TEXT,
    p_evidence_ids UUID[] DEFAULT '{}'::uuid[]
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, api, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_case_id UUID;
    v_owner_id UUID;
    v_requested_count INTEGER := COALESCE(cardinality(p_evidence_ids), 0);
    v_matched_count INTEGER;
BEGIN
    IF btrim(COALESCE(p_content, '')) = '' THEN
        RAISE EXCEPTION 'CHAT_CONTENT_REQUIRED' USING ERRCODE = '22023';
    END IF;
    SELECT dispatch.analysis_case_pk, analysis_case.user_id
      INTO v_case_id, v_owner_id
      FROM workspace.conversation_message_dispatch AS dispatch
      JOIN result.conversation_message AS message
        ON message.message_pk = dispatch.assistant_message_pk
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
     WHERE dispatch.assistant_message_pk = p_assistant_message_id
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND dispatch.lease_expires_at > v_now
       AND message.status = 'generating'
       AND analysis_case.retention_expires_at > v_now
     FOR UPDATE OF dispatch, message, analysis_case;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    SELECT count(DISTINCT evidence.evidence_snapshot_pk)
      INTO v_matched_count
     FROM result.evidence_snapshot AS evidence
     WHERE evidence.analysis_case_pk = v_case_id
       AND evidence.usage_scope = 'RESULT'
       AND evidence.sim_candidate_pk IS NULL
       AND evidence.evidence_snapshot_pk = ANY(COALESCE(p_evidence_ids, '{}'::uuid[]))
       AND EXISTS (
           SELECT 1
             FROM result.axis_result AS axis
             CROSS JOIN LATERAL result.axis_public_evidence_ids_v2(
                 axis.axis_type, axis.public_detail
             ) AS referenced
            WHERE axis.axis_result_pk = evidence.axis_result_pk
              AND axis.analysis_case_pk = evidence.analysis_case_pk
              AND referenced.evidence_id = evidence.evidence_snapshot_pk::text
       );
    IF v_requested_count <> v_matched_count
       OR v_requested_count <> cardinality(ARRAY(SELECT DISTINCT unnest(COALESCE(p_evidence_ids, '{}'::uuid[])))) THEN
        RAISE EXCEPTION 'CHAT_EVIDENCE_NOT_IN_PUBLIC_ALLOW_LIST' USING ERRCODE = '23514';
    END IF;

    UPDATE ops.processing_run
       SET status = 'succeeded', finished_at = v_now,
           error_code = NULL, error_message = NULL
     WHERE processing_run_pk = p_processing_run_pk
       AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'PROCESSING_RUN_NOT_LIVE' USING ERRCODE = 'P0001';
    END IF;
    UPDATE result.conversation_message
       SET content = btrim(p_content), status = 'completed',
           error_code = NULL, error_message = NULL
     WHERE message_pk = p_assistant_message_id;
    DELETE FROM result.conversation_reference WHERE message_pk = p_assistant_message_id;
    INSERT INTO result.conversation_reference (
        message_pk, evidence_snapshot_pk, reference_role
    )
    SELECT p_assistant_message_id, evidence.evidence_snapshot_pk, 'chat'
     FROM result.evidence_snapshot AS evidence
     WHERE evidence.analysis_case_pk = v_case_id
       AND evidence.usage_scope = 'RESULT'
       AND evidence.sim_candidate_pk IS NULL
       AND evidence.evidence_snapshot_pk = ANY(COALESCE(p_evidence_ids, '{}'::uuid[]))
       AND EXISTS (
           SELECT 1
             FROM result.axis_result AS axis
             CROSS JOIN LATERAL result.axis_public_evidence_ids_v2(
                 axis.axis_type, axis.public_detail
             ) AS referenced
            WHERE axis.axis_result_pk = evidence.axis_result_pk
              AND axis.analysis_case_pk = evidence.analysis_case_pk
              AND referenced.evidence_id = evidence.evidence_snapshot_pk::text
       );
    UPDATE workspace.conversation_message_dispatch
       SET processing_run_pk = NULL, claimed_by = NULL, claimed_at = NULL,
           heartbeat_at = NULL, lease_expires_at = NULL, result_payload = NULL,
           last_error_code = NULL, last_error_message = NULL
     WHERE assistant_message_pk = p_assistant_message_id;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.fail_conversation_message_v2(
    p_assistant_message_id UUID,
    p_processing_run_pk UUID,
    p_error_code TEXT,
    p_error_message TEXT,
    p_internal_error_message TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, workspace
AS $$
DECLARE
    v_failed BOOLEAN;
BEGIN
    v_failed := workspace.fail_conversation_message(
        p_assistant_message_id, p_processing_run_pk, p_error_code,
        p_error_message, p_internal_error_message
    );
    -- A failed/auto-requeued message has no worker-facing payload until its
    -- next fenced claim.  Leaving the previous public projection in dispatch
    -- would retain a stale result context beyond the failed attempt.
    IF v_failed THEN
        UPDATE workspace.conversation_message_dispatch
           SET result_payload = NULL
         WHERE assistant_message_pk = p_assistant_message_id;
    END IF;
    RETURN v_failed;
END;
$$;

REVOKE ALL ON FUNCTION workspace.prepare_conversation_messages_v2(UUID, UUID, TEXT, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.retry_conversation_message_v2(UUID, UUID, UUID, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.rpc_get_conversation_message_v2(UUID, UUID, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.rpc_get_conversation_history_v2(UUID, UUID, INTEGER, UUID, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.claim_next_conversation_message_v2(TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.heartbeat_conversation_message_v2(UUID, UUID, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.complete_conversation_message_v2(UUID, UUID, TEXT, UUID[]) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.fail_conversation_message_v2(UUID, UUID, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;

GRANT USAGE ON SCHEMA api, workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.prepare_conversation_messages_v2(UUID, UUID, TEXT, UUID),
                         workspace.retry_conversation_message_v2(UUID, UUID, UUID, UUID),
                         api.rpc_get_conversation_message_v2(UUID, UUID, UUID),
                         api.rpc_get_conversation_history_v2(UUID, UUID, INTEGER, UUID, INTEGER),
                         workspace.claim_next_conversation_message_v2(TEXT, INTEGER),
                         workspace.heartbeat_conversation_message_v2(UUID, UUID, INTEGER),
                         workspace.complete_conversation_message_v2(UUID, UUID, TEXT, UUID[]),
                         workspace.fail_conversation_message_v2(UUID, UUID, TEXT, TEXT, TEXT)
TO service_role;

COMMIT;
