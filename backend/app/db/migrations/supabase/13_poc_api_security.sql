-- PoC: browser boundary. Base workspace/result/kb tables remain private.

BEGIN;

CREATE SCHEMA IF NOT EXISTS api;
REVOKE ALL ON SCHEMA kb, workspace, result, ops FROM authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA kb, workspace, result, ops FROM authenticated;
GRANT USAGE ON SCHEMA api TO authenticated;

-- The only base-table browser read is the caller's current analysis run, used
-- for Realtime progress. All result and KB reads go through api.* objects.
GRANT USAGE ON SCHEMA workspace TO authenticated;
GRANT SELECT ON workspace.analysis_run TO authenticated;

-- Source documents are upload-only from the browser. Their exact path must be
-- reserved by the Edge Function in the caller's analysis_run row.
DROP POLICY IF EXISTS request_source_insert_own_reserved_path ON storage.objects;
CREATE POLICY request_source_insert_own_reserved_path ON storage.objects
  FOR INSERT TO authenticated
  WITH CHECK (
    bucket_id = 'request-temp'
    AND EXISTS (
      SELECT 1 FROM workspace.analysis_run ar
      WHERE ar.user_id = auth.uid()
        AND ar.status = 'uploading'
        AND ar.source_bucket = storage.objects.bucket_id
        AND ar.source_object_key = storage.objects.name
    )
  );

ALTER TABLE result.report_generation ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS report_generation_select_own ON result.report_generation;
CREATE POLICY report_generation_select_own ON result.report_generation
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM result.analysis_case c
      WHERE c.analysis_case_pk = report_generation.analysis_case_pk
        AND c.user_id = auth.uid()
    )
  );

CREATE OR REPLACE VIEW api.v_active_analysis_session
WITH (security_barrier = true)
AS
SELECT
  s.analysis_session_pk AS analysis_session_id,
  c.analysis_case_pk AS analysis_case_id,
  c.program_name,
  c.original_filename,
  s.expires_at AS session_expires_at
FROM result.analysis_session s
JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk
WHERE c.user_id = auth.uid()
  AND c.case_status = 'ready'
  AND c.retention_expires_at > now()
  AND s.status = 'active'
  AND s.expires_at > now();

CREATE OR REPLACE VIEW api.v_my_analysis_history
WITH (security_barrier = true)
AS
SELECT
  c.analysis_case_pk AS analysis_case_id,
  c.program_name,
  c.original_filename,
  c.analysis_completed_at AS completed_at,
  c.retention_expires_at
FROM result.analysis_case c
WHERE c.user_id = auth.uid()
  AND c.case_status = 'ready'
  AND c.retention_expires_at > now();

CREATE OR REPLACE VIEW api.v_conversation_messages
WITH (security_barrier = true)
AS
SELECT
  m.message_pk AS message_id,
  s.analysis_case_pk AS analysis_case_id,
  m.role,
  m.sequence_no,
  m.content,
  m.message_status,
  m.reply_to_message_pk AS reply_to_message_id,
  m.created_at,
  m.updated_at
FROM result.conversation_message m
JOIN result.analysis_session s ON s.analysis_session_pk = m.analysis_session_pk
JOIN result.analysis_case c ON c.analysis_case_pk = s.analysis_case_pk
WHERE c.user_id = auth.uid() AND c.retention_expires_at > now();

GRANT SELECT ON api.v_active_analysis_session, api.v_my_analysis_history,
  api.v_conversation_messages TO authenticated;

CREATE OR REPLACE FUNCTION api.rpc_touch_active_analysis_session(p_analysis_case_id UUID)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, result, api
AS $$
DECLARE v_expires_at TIMESTAMPTZ;
BEGIN
  UPDATE result.analysis_session s
     SET last_activity_at = now(), expires_at = now() + interval '30 minutes', updated_at = now()
    FROM result.analysis_case c
   WHERE s.analysis_case_pk = p_analysis_case_id
     AND c.analysis_case_pk = s.analysis_case_pk
     AND c.user_id = auth.uid()
     AND s.status = 'active'
     AND s.expires_at > now()
  RETURNING s.expires_at INTO v_expires_at;
  IF v_expires_at IS NULL THEN RAISE EXCEPTION 'active analysis session not found' USING ERRCODE = 'P0002'; END IF;
  RETURN jsonb_build_object('analysis_case_id', p_analysis_case_id, 'session_expires_at', v_expires_at);
END;
$$;

CREATE OR REPLACE FUNCTION api.rpc_close_active_analysis_session(p_analysis_case_id UUID)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, result, api
AS $$
BEGIN
  UPDATE result.analysis_session s
     SET status = 'closed', close_reason = 'new_analysis', closed_at = now(), updated_at = now()
    FROM result.analysis_case c
   WHERE s.analysis_case_pk = p_analysis_case_id
     AND c.analysis_case_pk = s.analysis_case_pk
     AND c.user_id = auth.uid()
     AND s.status = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'active analysis session not found' USING ERRCODE = 'P0002'; END IF;
  RETURN jsonb_build_object('analysis_case_id', p_analysis_case_id, 'status', 'closed');
END;
$$;

CREATE OR REPLACE FUNCTION api.rpc_get_analysis_result(p_analysis_case_id UUID)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, result, api
AS $$
DECLARE v_case result.analysis_case%ROWTYPE; v_session result.analysis_session%ROWTYPE;
BEGIN
  SELECT * INTO v_case FROM result.analysis_case
   WHERE analysis_case_pk = p_analysis_case_id AND user_id = auth.uid()
     AND case_status = 'ready' AND retention_expires_at > now();
  IF NOT FOUND THEN RAISE EXCEPTION 'analysis result not found' USING ERRCODE = 'P0002'; END IF;
  SELECT * INTO v_session FROM result.analysis_session WHERE analysis_case_pk = v_case.analysis_case_pk;
  RETURN jsonb_build_object(
    'case', jsonb_build_object('analysis_case_id', v_case.analysis_case_pk, 'program_name', v_case.program_name,
      'original_filename', v_case.original_filename, 'completed_at', v_case.analysis_completed_at),
    'cpl', jsonb_build_object('items', COALESCE((SELECT jsonb_agg(jsonb_build_object('code', a.axis_code, 'status', a.status, 'summary', a.summary_text, 'detail', a.result_data) ORDER BY a.ordinal)
      FROM result.axis_result a WHERE a.analysis_case_pk = v_case.analysis_case_pk AND a.axis_type = 'CPL'), '[]'::jsonb)),
    'fit', jsonb_build_object('items', COALESCE((SELECT jsonb_agg(jsonb_build_object('code', a.axis_code, 'status', a.status, 'summary', a.summary_text, 'detail', a.result_data) ORDER BY a.ordinal)
      FROM result.axis_result a WHERE a.analysis_case_pk = v_case.analysis_case_pk AND a.axis_type = 'FIT'), '[]'::jsonb)),
    'sim', jsonb_build_object('candidates', COALESCE((SELECT jsonb_agg(jsonb_build_object('sim_candidate_id', sc.sim_candidate_pk, 'rank', sc.rank_no, 'title', sc.announcement_title) ORDER BY sc.rank_no)
      FROM result.sim_candidate sc WHERE sc.analysis_case_pk = v_case.analysis_case_pk), '[]'::jsonb)),
    'report', COALESCE((SELECT jsonb_build_object('status', rg.status,
      'can_download', rg.status = 'ready', 'can_regenerate', rg.status IN ('retryable_failed', 'failed'),
      'retry_count', rg.retry_count)
      FROM result.report_generation rg WHERE rg.analysis_case_pk = v_case.analysis_case_pk AND rg.report_type = 'final_pdf'),
      jsonb_build_object('status', 'generating', 'can_download', false, 'can_regenerate', false, 'retry_count', 0)),
    'session', jsonb_build_object('analysis_session_id', v_session.analysis_session_pk, 'is_active', v_session.status = 'active' AND v_session.expires_at > now(),
      'can_chat', v_session.status = 'active' AND v_session.expires_at > now(), 'expires_at', v_session.expires_at),
    'evidences', COALESCE((SELECT jsonb_agg(jsonb_build_object('evidence_id', e.evidence_snapshot_pk,
      'side', e.side, 'field_name', e.field_name, 'raw_value', e.raw_value, 'excerpt', e.context_excerpt))
      FROM result.evidence_snapshot e WHERE e.analysis_case_pk = v_case.analysis_case_pk), '[]'::jsonb)
  );
END;
$$;

CREATE OR REPLACE FUNCTION api.rpc_get_sim_candidate_detail(p_sim_candidate_id UUID)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, result, api
AS $$
DECLARE v_candidate result.sim_candidate%ROWTYPE;
BEGIN
  SELECT sc.* INTO v_candidate
    FROM result.sim_candidate sc
    JOIN result.analysis_case c ON c.analysis_case_pk = sc.analysis_case_pk
   WHERE sc.sim_candidate_pk = p_sim_candidate_id
     AND c.user_id = auth.uid()
     AND c.retention_expires_at > now();
  IF NOT FOUND THEN RAISE EXCEPTION 'similarity candidate not found' USING ERRCODE = 'P0002'; END IF;
  RETURN jsonb_build_object(
    'sim_candidate_id', v_candidate.sim_candidate_pk,
    'analysis_case_id', v_candidate.analysis_case_pk,
    'rank', v_candidate.rank_no,
    'title', v_candidate.announcement_title,
    'issuing_organization', v_candidate.issuing_organization,
    'source_url', v_candidate.source_url,
    'notice_status', v_candidate.notice_status,
    'status', v_candidate.status,
    'summary', v_candidate.summary_text,
    'comparable_axes', v_candidate.comparable_axes,
    'axes', jsonb_build_object(
      'purpose', v_candidate.purpose_result,
      'target', v_candidate.target_result,
      'support', v_candidate.support_result,
      'delivery', v_candidate.delivery_result
    )
  );
END;
$$;

GRANT EXECUTE ON FUNCTION api.rpc_touch_active_analysis_session(UUID),
  api.rpc_close_active_analysis_session(UUID), api.rpc_get_analysis_result(UUID),
  api.rpc_get_sim_candidate_detail(UUID) TO authenticated;

COMMIT;
