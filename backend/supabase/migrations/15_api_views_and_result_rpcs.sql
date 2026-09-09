-- ============================================================================
-- Migration 15: Browser API read models and result/session RPCs
--
-- The browser is granted access only to the api schema.  Every view/function
-- derives the caller from auth.uid(); no caller-supplied user id is trusted.
-- ============================================================================

BEGIN;

-- Fields required by the fixed frontend response contract.  The worker writes
-- these projections; the RPCs below only compose browser read models.
ALTER TABLE result.sim_candidate
    ADD COLUMN IF NOT EXISTS summary_text TEXT NULL,
    ADD COLUMN IF NOT EXISTS comparable_axes TEXT[] NULL;

-- --------------------------------------------------------------------------
-- Views
-- --------------------------------------------------------------------------

CREATE OR REPLACE VIEW api.v_active_analysis_session AS
SELECT DISTINCT ON (c.user_id)
    s.analysis_session_pk AS analysis_session_id,
    c.analysis_case_pk AS analysis_case_id,
    c.program_name,
    c.original_filename,
    s.expires_at AS session_expires_at
FROM result.analysis_session s
JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk
WHERE c.user_id = (SELECT auth.uid())
  AND s.status = 'active'
  AND s.expires_at > now()
ORDER BY c.user_id, s.last_activity_at DESC, s.analysis_session_pk DESC;

CREATE OR REPLACE VIEW api.v_my_analysis_history AS
SELECT
    c.analysis_case_pk AS analysis_case_id,
    c.program_name,
    c.original_filename,
    c.analysis_completed_at AS completed_at,
    report.status AS report_status,
    report.completed_at AS report_completed_at
FROM result.analysis_case c
LEFT JOIN LATERAL (
    SELECT r.status, r.completed_at
    FROM result.report_artifact r
    WHERE r.analysis_case_pk = c.analysis_case_pk
    ORDER BY r.created_at DESC, r.report_artifact_pk DESC
    LIMIT 1
) report ON true
WHERE c.user_id = (SELECT auth.uid())
  AND c.analysis_completed_at IS NOT NULL
ORDER BY c.analysis_completed_at DESC, c.analysis_case_pk DESC;

CREATE OR REPLACE VIEW api.v_conversation_messages AS
SELECT
    m.message_pk AS message_id,
    c.analysis_case_pk AS analysis_case_id,
    m.role,
    m.sequence_no,
    m.content,
    m.status,
    m.reply_to_message_pk AS reply_to_message_id,
    (m.auto_retry_count + m.manual_retry_count) AS retry_count,
    m.error_code,
    m.created_at,
    m.updated_at
FROM result.conversation_message m
JOIN result.analysis_session s ON s.analysis_session_pk = m.analysis_session_pk
JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk
WHERE c.user_id = (SELECT auth.uid());

-- --------------------------------------------------------------------------
-- Result read RPCs
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION api.rpc_get_analysis_result(p_analysis_case_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, api, result
AS $$
DECLARE
    v_result JSONB;
BEGIN
    SELECT jsonb_build_object(
        'case', jsonb_build_object(
            'analysis_case_id', c.analysis_case_pk,
            'program_name', c.program_name,
            'original_filename', c.original_filename,
            'completed_at', c.analysis_completed_at
        ),
        'cpl', jsonb_build_object('items', COALESCE(cpl.items, '[]'::jsonb)),
        'fit', jsonb_build_object('items', COALESCE(fit.items, '[]'::jsonb)),
        'sim', jsonb_build_object('candidates', COALESCE(sim.candidates, '[]'::jsonb)),
        'report', COALESCE(report.payload, jsonb_build_object(
            'status', 'generating', 'can_download', false,
            'can_regenerate', false, 'retry_count', 0
        )),
        'session', COALESCE(session.payload, jsonb_build_object(
            'analysis_session_id', NULL, 'is_active', false,
            'can_chat', false, 'expires_at', NULL
        )),
        'evidences', COALESCE(evidence.items, '[]'::jsonb)
    )
    INTO v_result
    FROM result.analysis_case c
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
            'code', a.axis_code,
            'status', a.status,
            'summary', a.summary_text,
            'detail', COALESCE(a.result_data, '{}'::jsonb)
        ) ORDER BY a.ordinal, a.axis_result_pk) AS items
        FROM result.axis_result a
        WHERE a.analysis_case_pk = c.analysis_case_pk AND a.axis_type = 'CPL'
    ) cpl ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
            'code', a.axis_code,
            'status', a.status,
            'summary', a.summary_text,
            'detail', COALESCE(a.result_data, '{}'::jsonb)
        ) ORDER BY a.ordinal, a.axis_result_pk) AS items
        FROM result.axis_result a
        WHERE a.analysis_case_pk = c.analysis_case_pk AND a.axis_type = 'FIT'
    ) fit ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
            'sim_candidate_id', sc.sim_candidate_pk,
            'rank', sc.rank_no,
            'title', sc.notice_title
        ) ORDER BY sc.rank_no, sc.sim_candidate_pk) AS candidates
        FROM result.sim_candidate sc
        WHERE sc.analysis_case_pk = c.analysis_case_pk
    ) sim ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_build_object(
            'status', r.status,
            'can_download', r.status = 'ready'
                AND r.storage_bucket IS NOT NULL AND r.storage_object_key IS NOT NULL,
            'can_regenerate', false,
            'retry_count', r.retry_count
        ) AS payload
        FROM result.report_artifact r
        WHERE r.analysis_case_pk = c.analysis_case_pk
        ORDER BY r.created_at DESC, r.report_artifact_pk DESC
        LIMIT 1
    ) report ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_build_object(
            'analysis_session_id', s.analysis_session_pk,
            'is_active', s.status = 'active' AND s.expires_at > now(),
            'can_chat', s.status = 'active' AND s.expires_at > now(),
            'expires_at', s.expires_at
        ) AS payload
        FROM result.analysis_session s
        WHERE s.analysis_case_pk = c.analysis_case_pk
    ) session ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
            'evidence_id', e.evidence_snapshot_pk,
            'side', lower(e.side),
            'field_name', e.field_name,
            'raw_value', e.raw_value,
            'excerpt', e.context_excerpt
        ) ORDER BY e.created_at, e.evidence_snapshot_pk) AS items
        FROM result.evidence_snapshot e
        WHERE e.analysis_case_pk = c.analysis_case_pk
          AND e.usage_scope = 'RESULT'
    ) evidence ON true
    WHERE c.analysis_case_pk = p_analysis_case_id
      AND c.user_id = (SELECT auth.uid());

    IF v_result IS NULL THEN
        RAISE EXCEPTION 'analysis result not found'
            USING ERRCODE = 'P0002';
    END IF;
    RETURN v_result;
END;
$$;

CREATE OR REPLACE FUNCTION api.rpc_get_sim_candidate_detail(p_sim_candidate_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, api, result
AS $$
DECLARE
    v_result JSONB;
BEGIN
    SELECT jsonb_build_object(
        'sim_candidate_id', sc.sim_candidate_pk,
        'analysis_case_id', sc.analysis_case_pk,
        'rank', sc.rank_no,
        'title', sc.notice_title,
        'issuing_organization', sc.issuing_organization,
        'source_url', sc.source_url,
        'notice_status', sc.notice_status,
        'status', sc.status,
        'summary', sc.summary_text,
        'comparable_axes', COALESCE(to_jsonb(sc.comparable_axes), '[]'::jsonb),
        'axes', jsonb_build_object(
            'purpose', COALESCE(sc.purpose_result, '{}'::jsonb),
            'target', COALESCE(sc.target_result, '{}'::jsonb),
            'support', COALESCE(sc.support_result, '{}'::jsonb),
            'delivery', COALESCE(sc.delivery_result, '{}'::jsonb)
        )
    ) INTO v_result
    FROM result.sim_candidate sc
    JOIN result.analysis_case c ON c.analysis_case_pk = sc.analysis_case_pk
    WHERE sc.sim_candidate_pk = p_sim_candidate_id
      AND c.user_id = (SELECT auth.uid());

    IF v_result IS NULL THEN
        RAISE EXCEPTION 'similarity candidate not found'
            USING ERRCODE = 'P0002';
    END IF;
    RETURN v_result;
END;
$$;

-- --------------------------------------------------------------------------
-- Active-session mutation RPCs
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION api.rpc_touch_active_analysis_session(p_analysis_case_id UUID)
RETURNS TABLE (
    analysis_session_id UUID,
    analysis_case_id UUID,
    status TEXT,
    session_expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, api, result
AS $$
BEGIN
    RETURN QUERY
    UPDATE result.analysis_session s
       SET last_activity_at = now(),
           expires_at = now() + interval '30 minutes'
      FROM result.analysis_case c
     WHERE s.analysis_case_pk = c.analysis_case_pk
       AND c.analysis_case_pk = p_analysis_case_id
       AND c.user_id = (SELECT auth.uid())
       AND s.status = 'active'
       AND s.expires_at > now()
    RETURNING s.analysis_session_pk, s.analysis_case_pk, s.status, s.expires_at;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'active analysis session not found or expired'
            USING ERRCODE = 'P0002';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION api.rpc_close_active_analysis_session(p_analysis_case_id UUID)
RETURNS TABLE (
    analysis_session_id UUID,
    analysis_case_id UUID,
    status TEXT,
    close_reason TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, api, result
AS $$
BEGIN
    RETURN QUERY
    UPDATE result.analysis_session s
       SET status = 'closed', closed_at = now(), close_reason = 'new_analysis'
      FROM result.analysis_case c
     WHERE s.analysis_case_pk = c.analysis_case_pk
       AND c.analysis_case_pk = p_analysis_case_id
       AND c.user_id = (SELECT auth.uid())
       AND s.status = 'active'
    RETURNING s.analysis_session_pk, s.analysis_case_pk, s.status, s.close_reason;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'active analysis session not found'
            USING ERRCODE = 'P0002';
    END IF;
END;
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA api FROM PUBLIC, anon;
GRANT USAGE ON SCHEMA api TO authenticated;
GRANT SELECT ON api.v_active_analysis_session,
                api.v_my_analysis_history,
                api.v_conversation_messages TO authenticated;
GRANT EXECUTE ON FUNCTION api.rpc_get_analysis_result(UUID),
                  api.rpc_get_sim_candidate_detail(UUID),
                  api.rpc_touch_active_analysis_session(UUID),
                  api.rpc_close_active_analysis_session(UUID) TO authenticated;

COMMIT;
