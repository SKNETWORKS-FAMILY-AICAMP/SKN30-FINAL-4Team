-- ============================================================================
-- Migration 16: Internal atomic conversation commands for Edge Functions
--
-- These functions are intentionally NOT callable by authenticated browsers.
-- Edge Functions validate the JWT then call them using the service-role key.
-- ============================================================================

BEGIN;

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

    SELECT s.analysis_session_pk
      INTO v_session_id
      FROM result.analysis_session s
      JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk
     WHERE c.analysis_case_pk = p_analysis_case_id
       AND c.user_id = p_user_id
       AND s.status = 'active'
       AND s.expires_at > now()
     FOR UPDATE OF s;

    IF v_session_id IS NULL THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_EXPIRED' USING ERRCODE = 'P0002';
    END IF;

    -- Serialise sequence allocation per conversation, not across all users.
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

    RETURN QUERY SELECT v_user_message_id, v_assistant_message_id, v_session_id;
END;
$$;

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
DECLARE
    v_message result.conversation_message%ROWTYPE;
    v_case_id UUID;
BEGIN
    SELECT m.*
      INTO v_message
      FROM result.conversation_message m
      JOIN result.analysis_session s ON s.analysis_session_pk = m.analysis_session_pk
      JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk
     WHERE m.message_pk = p_assistant_message_id
       AND m.role = 'assistant'
       AND c.user_id = p_user_id
       AND s.status = 'active'
       AND s.expires_at > now()
     FOR UPDATE OF m;

    IF v_message.message_pk IS NULL THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_EXPIRED_OR_MESSAGE_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;
    SELECT s.analysis_case_pk
      INTO v_case_id
      FROM result.analysis_session s
     WHERE s.analysis_session_pk = v_message.analysis_session_pk;
    IF v_message.status <> 'failed' THEN
        RAISE EXCEPTION 'CHAT_MESSAGE_NOT_RETRYABLE' USING ERRCODE = '22023';
    END IF;
    IF v_message.manual_retry_count >= 2 THEN
        RAISE EXCEPTION 'CHAT_RETRY_EXHAUSTED' USING ERRCODE = '22023';
    END IF;
    IF v_message.last_retry_at IS NOT NULL
       AND v_message.last_retry_at > now() - interval '5 seconds' THEN
        RAISE EXCEPTION 'CHAT_RETRY_COOLDOWN' USING ERRCODE = '55P03';
    END IF;

    UPDATE result.conversation_message
       SET status = 'generating',
           error_code = NULL,
           error_message = NULL,
           manual_retry_count = manual_retry_count + 1,
           last_retry_at = now()
     WHERE message_pk = v_message.message_pk
     RETURNING manual_retry_count INTO retry_count;

    assistant_message_id := v_message.message_pk;
    user_message_id := v_message.reply_to_message_pk;
    analysis_case_id := v_case_id;
    analysis_session_id := v_message.analysis_session_pk;
    RETURN NEXT;
END;
$$;

REVOKE ALL ON FUNCTION workspace.prepare_conversation_messages(UUID, UUID, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.retry_conversation_message(UUID, UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION workspace.prepare_conversation_messages(UUID, UUID, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION workspace.retry_conversation_message(UUID, UUID) TO service_role;

COMMIT;
