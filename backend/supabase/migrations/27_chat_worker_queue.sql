-- ============================================================================
-- Migration 26: asynchronous result-grounded conversation queue
--
-- HTTP only creates the two conversation rows and this private dispatch row.
-- A trusted chat worker claims the row, calls the LLM, then completes/fails it
-- with the processing-run UUID as a fencing token.  Browsers never receive
-- table or RPC privileges for this queue.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS workspace.conversation_message_dispatch (
    assistant_message_pk UUID PRIMARY KEY
        REFERENCES result.conversation_message(message_pk)
        ON DELETE CASCADE,
    analysis_case_pk UUID NOT NULL
        REFERENCES result.analysis_case(analysis_case_pk)
        ON DELETE CASCADE,
    analysis_session_pk UUID NOT NULL
        REFERENCES result.analysis_session(analysis_session_pk)
        ON DELETE CASCADE,
    user_message_pk UUID NOT NULL
        REFERENCES result.conversation_message(message_pk)
        ON DELETE CASCADE,
    attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 2),
    processing_run_pk UUID NULL
        REFERENCES ops.processing_run(processing_run_pk)
        ON DELETE RESTRICT,
    claimed_by TEXT NULL,
    claimed_at TIMESTAMPTZ NULL,
    heartbeat_at TIMESTAMPTZ NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    result_payload JSONB NULL,
    last_error_code TEXT NULL,
    last_error_message TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (processing_run_pk IS NULL
            AND claimed_by IS NULL
            AND claimed_at IS NULL
            AND heartbeat_at IS NULL
            AND lease_expires_at IS NULL)
        OR
        (processing_run_pk IS NOT NULL
            AND claimed_by IS NOT NULL
            AND claimed_at IS NOT NULL
            AND heartbeat_at IS NOT NULL
            AND lease_expires_at IS NOT NULL)
    ),
    CHECK (lease_expires_at IS NULL OR lease_expires_at > heartbeat_at)
);

CREATE INDEX IF NOT EXISTS ix_workspace_conversation_dispatch_poll
    ON workspace.conversation_message_dispatch (created_at, assistant_message_pk)
    WHERE processing_run_pk IS NULL;

CREATE INDEX IF NOT EXISTS ix_workspace_conversation_dispatch_lease
    ON workspace.conversation_message_dispatch (lease_expires_at, assistant_message_pk)
    WHERE processing_run_pk IS NOT NULL;

ALTER TABLE workspace.conversation_message_dispatch ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE workspace.conversation_message_dispatch
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_workspace_conversation_dispatch_updated_at
    ON workspace.conversation_message_dispatch;
CREATE TRIGGER trg_workspace_conversation_dispatch_updated_at
BEFORE UPDATE ON workspace.conversation_message_dispatch
FOR EACH ROW EXECUTE FUNCTION workspace.set_updated_at();

-- --------------------------------------------------------------------------
-- API command RPCs.  These replace migration 16's row-only commands while
-- retaining their signatures for existing trusted callers.
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION workspace.prepare_conversation_messages(
    p_user_id UUID,
    p_analysis_case_id UUID,
    p_content TEXT
)
RETURNS TABLE (
    user_message_id UUID,
    assistant_message_id UUID,
    analysis_session_id UUID
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
DECLARE
    v_session_id UUID;
    v_user_message_id UUID := gen_random_uuid();
    v_assistant_message_id UUID := gen_random_uuid();
    v_sequence_no INTEGER;
BEGIN
    IF btrim(COALESCE(p_content, '')) = '' THEN
        RAISE EXCEPTION 'CHAT_CONTENT_REQUIRED' USING ERRCODE = '22023';
    END IF;
    IF char_length(btrim(p_content)) > 4000 THEN
        RAISE EXCEPTION 'CHAT_CONTENT_TOO_LONG' USING ERRCODE = '22023';
    END IF;

    SELECT s.analysis_session_pk
      INTO v_session_id
      FROM result.analysis_session s
      JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk
     WHERE c.analysis_case_pk = p_analysis_case_id
       AND c.user_id = p_user_id
       AND c.retention_expires_at > now()
       AND s.status = 'active'
       AND s.expires_at > now()
     FOR UPDATE OF c, s;

    IF v_session_id IS NULL THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_EXPIRED' USING ERRCODE = 'P0002';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(v_session_id::text, 0));
    SELECT COALESCE(MAX(m.sequence_no), 0)
      INTO v_sequence_no
      FROM result.conversation_message m
     WHERE m.analysis_session_pk = v_session_id;

    INSERT INTO result.conversation_message (
        message_pk, analysis_session_pk, role, sequence_no, content, status
    ) VALUES (
        v_user_message_id, v_session_id, 'user', v_sequence_no + 1,
        btrim(p_content), 'completed'
    );

    INSERT INTO result.conversation_message (
        message_pk, analysis_session_pk, role, sequence_no, content, status,
        reply_to_message_pk
    ) VALUES (
        v_assistant_message_id, v_session_id, 'assistant', v_sequence_no + 2,
        '', 'generating', v_user_message_id
    );

    INSERT INTO workspace.conversation_message_dispatch (
        assistant_message_pk, analysis_case_pk, analysis_session_pk,
        user_message_pk
    ) VALUES (
        v_assistant_message_id, p_analysis_case_id, v_session_id,
        v_user_message_id
    );

    RETURN QUERY SELECT v_user_message_id, v_assistant_message_id, v_session_id;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.retry_conversation_message(
    p_user_id UUID,
    p_assistant_message_id UUID,
    p_analysis_case_id UUID
)
RETURNS TABLE (
    assistant_message_id UUID,
    user_message_id UUID,
    analysis_case_id UUID,
    analysis_session_id UUID,
    retry_count INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, ops, workspace, result
AS $$
DECLARE
    v_message_id UUID;
    v_message_status TEXT;
    v_manual_retry_count INTEGER;
    v_session_id UUID;
    v_user_message_id UUID;
    v_case_id UUID;
    v_dispatch workspace.conversation_message_dispatch%ROWTYPE;
BEGIN
    SELECT m.message_pk, m.status, m.manual_retry_count,
           m.analysis_session_pk, m.reply_to_message_pk,
           c.analysis_case_pk
      INTO v_message_id, v_message_status, v_manual_retry_count,
           v_session_id, v_user_message_id, v_case_id
      FROM result.conversation_message m
      JOIN result.analysis_session s ON s.analysis_session_pk = m.analysis_session_pk
      JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk
     WHERE m.message_pk = p_assistant_message_id
       AND m.role = 'assistant'
       AND c.analysis_case_pk = COALESCE(p_analysis_case_id, c.analysis_case_pk)
       AND c.user_id = p_user_id
       AND c.retention_expires_at > now()
       AND s.status = 'active'
       AND s.expires_at > now()
     FOR UPDATE OF m, c, s;

    IF v_message_id IS NULL THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_EXPIRED_OR_MESSAGE_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;
    IF v_message_status <> 'failed' THEN
        RAISE EXCEPTION 'CHAT_MESSAGE_NOT_RETRYABLE' USING ERRCODE = '22023';
    END IF;
    IF v_manual_retry_count >= 2 THEN
        RAISE EXCEPTION 'CHAT_RETRY_EXHAUSTED' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM result.conversation_message m
         WHERE m.message_pk = v_message_id
           AND m.last_retry_at IS NOT NULL
           AND m.last_retry_at > now() - interval '5 seconds'
    ) THEN
        RAISE EXCEPTION 'CHAT_RETRY_COOLDOWN' USING ERRCODE = '55P03';
    END IF;

    SELECT d.*
      INTO v_dispatch
      FROM workspace.conversation_message_dispatch d
     WHERE d.assistant_message_pk = v_message_id
     FOR UPDATE;

    IF v_dispatch.processing_run_pk IS NOT NULL
       AND v_dispatch.lease_expires_at > now() THEN
        RAISE EXCEPTION 'CHAT_MESSAGE_BUSY' USING ERRCODE = '55P03';
    END IF;

    IF v_dispatch.processing_run_pk IS NOT NULL THEN
        UPDATE ops.processing_run
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error_code = 'CHAT_RETRY_RECLAIMED',
               error_message = 'A stale chat worker lease was reclaimed.'
         WHERE processing_run_pk = v_dispatch.processing_run_pk
           AND status IN ('queued', 'running');
    END IF;

    UPDATE result.conversation_message
       SET status = 'generating',
           error_code = NULL,
           error_message = NULL,
           manual_retry_count = manual_retry_count + 1,
           last_retry_at = now()
     WHERE message_pk = v_message_id;

    INSERT INTO workspace.conversation_message_dispatch (
        assistant_message_pk, analysis_case_pk, analysis_session_pk,
        user_message_pk, attempt_count, processing_run_pk, claimed_by,
        claimed_at, heartbeat_at, lease_expires_at, result_payload,
        last_error_code, last_error_message
    ) VALUES (
        v_message_id, v_case_id, v_session_id,
        v_user_message_id, 0, NULL, NULL, NULL, NULL, NULL,
        NULL, NULL, NULL
    )
    ON CONFLICT (assistant_message_pk) DO UPDATE
       SET analysis_case_pk = EXCLUDED.analysis_case_pk,
           analysis_session_pk = EXCLUDED.analysis_session_pk,
           user_message_pk = EXCLUDED.user_message_pk,
           attempt_count = 0,
           processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL,
           result_payload = NULL,
           last_error_code = NULL,
           last_error_message = NULL;

    SELECT auto_retry_count + manual_retry_count
      INTO retry_count
      FROM result.conversation_message
     WHERE message_pk = v_message_id;

    assistant_message_id := v_message_id;
    user_message_id := v_user_message_id;
    analysis_case_id := v_case_id;
    analysis_session_id := v_session_id;
    RETURN NEXT;
END;
$$;

-- Migration 16 exposed a two-argument trusted command. Keep it working while
-- the HTTP adapter uses the case-fenced three-argument overload above.
CREATE OR REPLACE FUNCTION workspace.retry_conversation_message(
    p_user_id UUID,
    p_assistant_message_id UUID
)
RETURNS TABLE (
    assistant_message_id UUID,
    user_message_id UUID,
    analysis_case_id UUID,
    analysis_session_id UUID,
    retry_count INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
BEGIN
    RETURN QUERY
    SELECT *
      FROM workspace.retry_conversation_message(
          p_user_id, p_assistant_message_id, NULL::uuid
      );
END;
$$;

-- --------------------------------------------------------------------------
-- Worker queue RPCs
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION workspace.claim_next_conversation_message(
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
SET search_path = pg_catalog, public, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_assistant_id UUID;
    v_case_id UUID;
    v_session_id UUID;
    v_user_message_id UUID;
    v_owner_id UUID;
    v_question TEXT;
    v_user_sequence_no INTEGER;
    v_previous_processing_run_pk UUID;
    v_processing_run_pk UUID;
    v_attempt_count INTEGER;
    v_lease_expires_at TIMESTAMPTZ;
    v_conversation JSONB;
    v_result_payload JSONB;
BEGIN
    IF btrim(COALESCE(p_worker_id, '')) = '' THEN
        RAISE EXCEPTION 'WORKER_ID_REQUIRED' USING ERRCODE = '22023';
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds < 30
       OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'LEASE_SECONDS_OUT_OF_RANGE' USING ERRCODE = '22023';
    END IF;

    -- A second expired attempt is terminal. This is done before selecting a
    -- fresh row so an abandoned worker can never leave a generating message
    -- forever.
    UPDATE ops.processing_run p
       SET status = 'failed',
           finished_at = v_now,
           error_code = 'WORKER_LEASE_EXPIRED',
           error_message = 'Chat worker lease expired after two attempts.'
      FROM workspace.conversation_message_dispatch d
      JOIN result.conversation_message m
        ON m.message_pk = d.assistant_message_pk
     WHERE p.processing_run_pk = d.processing_run_pk
       AND d.attempt_count >= 2
       AND d.lease_expires_at <= v_now
       AND m.status = 'generating'
       AND p.status IN ('queued', 'running');

    UPDATE result.conversation_message m
       SET status = 'failed',
           error_code = 'WORKER_MAX_ATTEMPTS_EXCEEDED',
           error_message = 'AI 답변 생성 시간이 초과되었습니다.'
      FROM workspace.conversation_message_dispatch d
      JOIN result.analysis_case c ON c.analysis_case_pk = d.analysis_case_pk
     WHERE m.message_pk = d.assistant_message_pk
       AND d.attempt_count >= 2
       AND d.lease_expires_at <= v_now
       AND m.status = 'generating';

    UPDATE workspace.conversation_message_dispatch d
       SET processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL,
           last_error_code = 'WORKER_MAX_ATTEMPTS_EXCEEDED',
           last_error_message = 'Chat worker lease expired after two attempts.'
      FROM result.conversation_message m
     WHERE m.message_pk = d.assistant_message_pk
       AND d.attempt_count >= 2
       AND d.lease_expires_at <= v_now
       AND m.status = 'failed';

    SELECT
        d.assistant_message_pk,
        d.analysis_case_pk,
        d.analysis_session_pk,
        d.user_message_pk,
        c.user_id,
        u.content,
        u.sequence_no,
        d.processing_run_pk,
        d.attempt_count
      INTO
        v_assistant_id,
        v_case_id,
        v_session_id,
        v_user_message_id,
        v_owner_id,
        v_question,
        v_user_sequence_no,
        v_previous_processing_run_pk,
        v_attempt_count
      FROM workspace.conversation_message_dispatch d
      JOIN result.conversation_message a
        ON a.message_pk = d.assistant_message_pk
      JOIN result.conversation_message u
        ON u.message_pk = d.user_message_pk
      JOIN result.analysis_session s
        ON s.analysis_session_pk = d.analysis_session_pk
      JOIN result.analysis_case c
        ON c.analysis_case_pk = d.analysis_case_pk
     WHERE a.status = 'generating'
       AND d.attempt_count < 2
       AND c.retention_expires_at > v_now
       AND s.status = 'active'
       AND s.expires_at > v_now
       AND (d.processing_run_pk IS NULL OR d.lease_expires_at <= v_now)
     ORDER BY d.created_at, d.assistant_message_pk
     LIMIT 1
     FOR UPDATE OF d, a, u, s, c SKIP LOCKED;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF v_previous_processing_run_pk IS NOT NULL THEN
        UPDATE ops.processing_run
           SET status = 'failed',
               finished_at = v_now,
               error_code = 'WORKER_LEASE_EXPIRED',
               error_message = 'Chat worker lease expired before completion.'
         WHERE processing_run_pk = v_previous_processing_run_pk
           AND status IN ('queued', 'running');
    END IF;

    v_attempt_count := v_attempt_count + 1;
    v_lease_expires_at := v_now + make_interval(secs => p_lease_seconds);

    SELECT COALESCE(jsonb_agg(
        jsonb_build_object('role', prior.role, 'content', prior.content)
        ORDER BY prior.sequence_no
    ), '[]'::jsonb)
      INTO v_conversation
      FROM (
          SELECT m.role, m.content, m.sequence_no
            FROM result.conversation_message m
           WHERE m.analysis_session_pk = v_session_id
             AND m.sequence_no < v_user_sequence_no
             AND m.status = 'completed'
             AND m.role IN ('user', 'assistant')
             AND btrim(m.content) <> ''
           ORDER BY m.sequence_no DESC
           LIMIT 20
      ) prior;

    SELECT jsonb_build_object(
        'case', jsonb_build_object(
            'analysis_case_id', c.analysis_case_pk,
            'program_name', c.program_name,
            'original_filename', c.original_filename,
            'completed_at', c.analysis_completed_at
        ),
        'cpl', jsonb_build_object(
            'items', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'code', a.axis_code,
                    'status', a.status,
                    'summary', a.summary_text,
                    'detail', COALESCE(a.result_data, '{}'::jsonb)
                ) ORDER BY a.ordinal, a.axis_result_pk)
                FROM result.axis_result a
                WHERE a.analysis_case_pk = c.analysis_case_pk
                  AND a.axis_type = 'CPL'
            ), '[]'::jsonb)
        ),
        'fit', jsonb_build_object(
            'items', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'code', a.axis_code,
                    'status', a.status,
                    'summary', a.summary_text,
                    'detail', COALESCE(a.result_data, '{}'::jsonb)
                ) ORDER BY a.ordinal, a.axis_result_pk)
                FROM result.axis_result a
                WHERE a.analysis_case_pk = c.analysis_case_pk
                  AND a.axis_type = 'FIT'
            ), '[]'::jsonb)
        ),
        'sim', jsonb_build_object(
            'candidates', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'sim_candidate_id', sc.sim_candidate_pk,
                    'rank', sc.rank_no,
                    'title', sc.notice_title,
                    'summary', sc.summary_text,
                    'comparable_axes', COALESCE(to_jsonb(sc.comparable_axes), '[]'::jsonb),
                    'axes', jsonb_build_object(
                        'purpose', COALESCE(sc.purpose_result, '{}'::jsonb),
                        'target', COALESCE(sc.target_result, '{}'::jsonb),
                        'support', COALESCE(sc.support_result, '{}'::jsonb),
                        'delivery', COALESCE(sc.delivery_result, '{}'::jsonb)
                    )
                ) ORDER BY sc.rank_no, sc.sim_candidate_pk)
                FROM result.sim_candidate sc
                WHERE sc.analysis_case_pk = c.analysis_case_pk
            ), '[]'::jsonb)
        ),
        'ml', COALESCE(c.ml_result, '{}'::jsonb),
        'evidences', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'evidence_id', e.evidence_snapshot_pk,
                'side', lower(e.side),
                'axis_type', e.axis_type,
                'field_name', e.field_name,
                'raw_value', e.raw_value,
                'excerpt', e.context_excerpt
            ) ORDER BY e.created_at, e.evidence_snapshot_pk)
            FROM result.evidence_snapshot e
            WHERE e.analysis_case_pk = c.analysis_case_pk
              AND e.usage_scope = 'RESULT'
        ), '[]'::jsonb)
    )
      INTO v_result_payload
      FROM result.analysis_case c
     WHERE c.analysis_case_pk = v_case_id;

    INSERT INTO ops.processing_run AS processing_attempt (
        source_analysis_run_id, run_type, status, started_at, run_metadata
    ) VALUES (
        NULL, 'chat', 'running', v_now,
        jsonb_build_object(
            'assistant_message_id', v_assistant_id,
            'analysis_case_id', v_case_id,
            'worker_id', btrim(p_worker_id),
            'attempt_no', v_attempt_count,
            'lease_seconds', p_lease_seconds
        )
    )
    RETURNING processing_attempt.processing_run_pk INTO v_processing_run_pk;

    UPDATE workspace.conversation_message_dispatch d
       SET attempt_count = v_attempt_count,
           processing_run_pk = v_processing_run_pk,
           claimed_by = btrim(p_worker_id),
           claimed_at = v_now,
           heartbeat_at = v_now,
           lease_expires_at = v_lease_expires_at,
           result_payload = v_result_payload,
           last_error_code = NULL,
           last_error_message = NULL
     WHERE d.assistant_message_pk = v_assistant_id;

    RETURN QUERY SELECT
        v_assistant_id,
        v_case_id,
        v_session_id,
        v_user_message_id,
        v_owner_id,
        v_question,
        v_conversation,
        v_result_payload,
        v_processing_run_pk,
        v_attempt_count,
        v_lease_expires_at,
        30;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.heartbeat_conversation_message(
    p_assistant_message_id UUID,
    p_processing_run_pk UUID,
    p_lease_seconds INTEGER DEFAULT 120
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_updated BOOLEAN := FALSE;
BEGIN
    IF p_lease_seconds IS NULL OR p_lease_seconds < 30
       OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'LEASE_SECONDS_OUT_OF_RANGE' USING ERRCODE = '22023';
    END IF;

    UPDATE workspace.conversation_message_dispatch d
       SET heartbeat_at = v_now,
           lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
      FROM result.conversation_message m
     WHERE d.assistant_message_pk = p_assistant_message_id
       AND d.processing_run_pk = p_processing_run_pk
       AND m.message_pk = d.assistant_message_pk
       AND m.status = 'generating'
       AND d.lease_expires_at > v_now
    RETURNING TRUE INTO v_updated;

    RETURN COALESCE(v_updated, FALSE);
END;
$$;

CREATE OR REPLACE FUNCTION workspace.complete_conversation_message(
    p_assistant_message_id UUID,
    p_processing_run_pk UUID,
    p_content TEXT,
    p_evidence_ids UUID[] DEFAULT '{}'::uuid[]
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_case_id UUID;
BEGIN
    IF btrim(COALESCE(p_content, '')) = '' THEN
        RAISE EXCEPTION 'CHAT_CONTENT_REQUIRED' USING ERRCODE = '22023';
    END IF;

    SELECT d.analysis_case_pk
      INTO v_case_id
      FROM workspace.conversation_message_dispatch d
      JOIN result.conversation_message m
        ON m.message_pk = d.assistant_message_pk
     WHERE d.assistant_message_pk = p_assistant_message_id
       AND d.processing_run_pk = p_processing_run_pk
       AND d.lease_expires_at > v_now
       AND m.status = 'generating'
     FOR UPDATE OF d, m;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE ops.processing_run
       SET status = 'succeeded',
           finished_at = v_now,
           error_code = NULL,
           error_message = NULL
     WHERE processing_run_pk = p_processing_run_pk
       AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'PROCESSING_RUN_NOT_LIVE' USING ERRCODE = 'P0001';
    END IF;

    UPDATE result.conversation_message
       SET content = btrim(p_content),
           status = 'completed',
           error_code = NULL,
           error_message = NULL
     WHERE message_pk = p_assistant_message_id;

    DELETE FROM result.conversation_reference
     WHERE message_pk = p_assistant_message_id;

    INSERT INTO result.conversation_reference (
        message_pk, evidence_snapshot_pk, reference_role
    )
    SELECT p_assistant_message_id, e.evidence_snapshot_pk, 'chat'
      FROM result.evidence_snapshot e
     WHERE e.analysis_case_pk = v_case_id
       AND e.usage_scope IN ('RESULT', 'CONVERSATION')
       AND e.evidence_snapshot_pk = ANY(COALESCE(p_evidence_ids, '{}'::uuid[]))
     GROUP BY e.evidence_snapshot_pk;

    UPDATE workspace.conversation_message_dispatch
       SET processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL,
           result_payload = NULL,
           last_error_code = NULL,
           last_error_message = NULL
     WHERE assistant_message_pk = p_assistant_message_id;

    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.fail_conversation_message(
    p_assistant_message_id UUID,
    p_processing_run_pk UUID,
    p_error_code TEXT,
    p_error_message TEXT,
    p_internal_error_message TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_auto_retry_count INTEGER;
BEGIN
    IF btrim(COALESCE(p_error_code, '')) = ''
       OR btrim(COALESCE(p_error_message, '')) = '' THEN
        RAISE EXCEPTION 'ATTEMPT_ERROR_DETAILS_REQUIRED' USING ERRCODE = '22023';
    END IF;

    SELECT m.auto_retry_count
      INTO v_auto_retry_count
      FROM workspace.conversation_message_dispatch d
      JOIN result.conversation_message m
        ON m.message_pk = d.assistant_message_pk
     WHERE d.assistant_message_pk = p_assistant_message_id
       AND d.processing_run_pk = p_processing_run_pk
       AND d.lease_expires_at > v_now
       AND m.status = 'generating'
     FOR UPDATE OF d, m;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE ops.processing_run
       SET status = 'failed',
           finished_at = v_now,
           error_code = btrim(p_error_code),
           error_message = COALESCE(NULLIF(btrim(p_internal_error_message), ''), btrim(p_error_message))
     WHERE processing_run_pk = p_processing_run_pk
       AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'PROCESSING_RUN_NOT_LIVE' USING ERRCODE = 'P0001';
    END IF;

    IF v_auto_retry_count < 1 THEN
        UPDATE result.conversation_message
           SET auto_retry_count = auto_retry_count + 1,
               status = 'generating',
               error_code = NULL,
               error_message = NULL
         WHERE message_pk = p_assistant_message_id;
    ELSE
        UPDATE result.conversation_message
           SET status = 'failed',
               error_code = btrim(p_error_code),
               error_message = btrim(p_error_message)
         WHERE message_pk = p_assistant_message_id;
    END IF;

    UPDATE workspace.conversation_message_dispatch
       SET attempt_count = 0,
           processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL,
           last_error_code = btrim(p_error_code),
           last_error_message = btrim(p_error_message)
     WHERE assistant_message_pk = p_assistant_message_id;

    RETURN TRUE;
END;
$$;

COMMENT ON TABLE workspace.conversation_message_dispatch IS
    'Private PostgreSQL polling queue for one asynchronous assistant response per row.';
COMMENT ON FUNCTION workspace.claim_next_conversation_message(TEXT, INTEGER) IS
    'Trusted fenced chat claim. Returns an owner-checked immutable result payload for the worker.';
COMMENT ON FUNCTION workspace.complete_conversation_message(UUID, UUID, TEXT, UUID[]) IS
    'Trusted fenced chat completion. Stores assistant content and valid result evidence references atomically.';
COMMENT ON FUNCTION workspace.fail_conversation_message(UUID, UUID, TEXT, TEXT, TEXT) IS
    'Trusted fenced chat failure with one automatic retry and bounded manual retries.';

REVOKE ALL ON FUNCTION workspace.prepare_conversation_messages(UUID, UUID, TEXT)
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.retry_conversation_message(UUID, UUID)
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.retry_conversation_message(UUID, UUID, UUID)
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.claim_next_conversation_message(TEXT, INTEGER)
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.heartbeat_conversation_message(UUID, UUID, INTEGER)
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.complete_conversation_message(UUID, UUID, TEXT, UUID[])
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.fail_conversation_message(UUID, UUID, TEXT, TEXT, TEXT)
FROM PUBLIC, anon, authenticated;

GRANT USAGE ON SCHEMA workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.prepare_conversation_messages(UUID, UUID, TEXT),
                         workspace.retry_conversation_message(UUID, UUID),
                         workspace.retry_conversation_message(UUID, UUID, UUID),
                         workspace.claim_next_conversation_message(TEXT, INTEGER),
                         workspace.heartbeat_conversation_message(UUID, UUID, INTEGER),
                         workspace.complete_conversation_message(UUID, UUID, TEXT, UUID[]),
                         workspace.fail_conversation_message(UUID, UUID, TEXT, TEXT, TEXT)
TO service_role;

COMMIT;
