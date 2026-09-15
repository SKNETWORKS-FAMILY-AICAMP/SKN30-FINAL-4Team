-- ============================================================================
-- Migration 23: Result read retention and candidate-evidence projections
--
-- Completed cases are visible in history only while their explicit retention
-- window is live.  The SIM-detail read model also returns the evidence
-- snapshots materialised for that candidate, using the same public evidence
-- object shape as the complete analysis result.
--
-- ``candidate_source_profile_id`` is an optional field on each worker result
-- evidence item.  When it is supplied, the fenced materialiser resolves it
-- against a candidate of the same analysis case and persists the relationship
-- in ``result.evidence_snapshot.sim_candidate_pk``.  This keeps the browser
-- read model relational: it never needs to infer candidate ownership from
-- opaque JSON or accept a candidate identifier from the client.
-- ============================================================================

BEGIN;

-- A NULL expiry is not a valid completed-result retention window.  Fail
-- closed for history so a legacy or partially materialised row cannot become
-- an indefinitely visible item merely because cleanup has not run yet.
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
  AND c.retention_expires_at > now()
ORDER BY c.analysis_completed_at DESC, c.analysis_case_pk DESC;

-- Keep the full result projection byte-for-byte compatible with migration 15
-- except for the ownership-plus-live-retention predicate.  This closes the
-- interval between expiry and asynchronous physical cleanup: guessing a UUID
-- cannot reveal a still-present expired result.
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
      AND c.user_id = (SELECT auth.uid())
      AND c.retention_expires_at > now();

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
        ),
        'evidences', COALESCE(evidence.items, '[]'::jsonb)
    ) INTO v_result
    FROM result.sim_candidate sc
    JOIN result.analysis_case c ON c.analysis_case_pk = sc.analysis_case_pk
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
            'evidence_id', e.evidence_snapshot_pk,
            'side', lower(e.side),
            'axis_type', e.axis_type,
            'field_name', e.field_name,
            'raw_value', e.raw_value,
            'excerpt', e.context_excerpt
        ) ORDER BY e.created_at, e.evidence_snapshot_pk) AS items
        FROM result.evidence_snapshot e
        WHERE e.analysis_case_pk = sc.analysis_case_pk
          AND e.sim_candidate_pk = sc.sim_candidate_pk
          AND e.usage_scope = 'RESULT'
    ) evidence ON true
    WHERE sc.sim_candidate_pk = p_sim_candidate_id
      AND c.user_id = (SELECT auth.uid())
      AND c.retention_expires_at > now();

    IF v_result IS NULL THEN
        RAISE EXCEPTION 'similarity candidate not found'
            USING ERRCODE = 'P0002';
    END IF;
    RETURN v_result;
END;
$$;

-- Recreate the fenced materialiser so candidate-specific evidence can be
-- attached while preserving the migration-22 atomic fence and output shape.
CREATE OR REPLACE FUNCTION workspace.persist_analysis_result_core(
    p_analysis_run_pk UUID,
    p_processing_run_pk UUID,
    p_result JSONB
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, kb, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_case_pk UUID;
    v_user_id UUID;
    v_filename TEXT;
    v_item JSONB;
    v_profile_version_pk UUID;
    v_notice_title TEXT;
    v_issuing_organization TEXT;
    v_source_url TEXT;
    v_notice_status TEXT;
    v_evidence_candidate_pk UUID;
    v_evidence_profile_version_pk UUID;
    v_ordinal INTEGER := 0;
BEGIN
    -- Keep the migration-22 fence as the first mutating boundary.  A stale
    -- token must return before creating any result, operation, or queue row.
    SELECT ar.user_id, ar.original_filename
      INTO v_user_id, v_filename
      FROM workspace.analysis_run ar
      JOIN workspace.analysis_run_dispatch dispatch
        ON dispatch.analysis_run_pk = ar.analysis_run_pk
     WHERE ar.analysis_run_pk = p_analysis_run_pk
       AND ar.status = 'running'
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND dispatch.lease_expires_at > v_now
     FOR UPDATE OF ar, dispatch;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF jsonb_typeof(p_result) <> 'object' THEN
        RAISE EXCEPTION 'COMPARISON_RESULT_OBJECT_REQUIRED' USING ERRCODE = '22023';
    END IF;

    INSERT INTO result.analysis_case (
        source_analysis_run_id, user_id, case_status, analysis_completed_at,
        retention_expires_at, program_name, original_filename
    ) VALUES (
        p_analysis_run_pk, v_user_id, 'ready', v_now, v_now + interval '90 days',
        NULLIF(p_result->>'program_name', ''), v_filename
    ) ON CONFLICT (source_analysis_run_id) DO UPDATE
        SET case_status = 'ready', analysis_completed_at = v_now,
            retention_expires_at = v_now + interval '90 days',
            program_name = COALESCE(NULLIF(EXCLUDED.program_name, ''), result.analysis_case.program_name),
            original_filename = COALESCE(EXCLUDED.original_filename, result.analysis_case.original_filename),
            updated_at = v_now
    RETURNING analysis_case_pk INTO v_case_pk;

    DELETE FROM result.evidence_snapshot WHERE analysis_case_pk = v_case_pk;
    DELETE FROM result.axis_result WHERE analysis_case_pk = v_case_pk;
    DELETE FROM result.sim_candidate WHERE analysis_case_pk = v_case_pk;

    FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_result->'axes', '[]'::jsonb)) LOOP
        IF v_item->>'axis_type' NOT IN ('CPL', 'FIT', 'BEN', 'DIF')
           OR COALESCE(v_item->>'axis_code', '') = ''
           OR COALESCE(v_item->>'status', '') = '' THEN
            RAISE EXCEPTION 'INVALID_COMPARISON_AXIS' USING ERRCODE = '22023';
        END IF;
        INSERT INTO result.axis_result (
            analysis_case_pk, axis_type, axis_code, status, summary_text, result_data, ordinal
        ) VALUES (
            v_case_pk, v_item->>'axis_type', v_item->>'axis_code', v_item->>'status',
            NULLIF(v_item->>'summary_text', ''), COALESCE(v_item->'result_data', '{}'::jsonb), v_ordinal
        );
        v_ordinal := v_ordinal + 1;
    END LOOP;

    FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_result->'candidates', '[]'::jsonb)) LOOP
        IF COALESCE(v_item->>'source_profile_id', '') = ''
           OR COALESCE(v_item->>'profile_version_pk', '') = ''
           OR NULLIF(v_item->>'rank', '') IS NULL
           OR (v_item->>'rank')::integer < 1
           OR COALESCE(v_item->>'status', '') = '' THEN
            RAISE EXCEPTION 'INVALID_COMPARISON_CANDIDATE' USING ERRCODE = '22023';
        END IF;
        -- Candidate display metadata is an Existing-KB snapshot, not worker
        -- text.  Bind it to the exact profile version that retrieval and the
        -- LLM compared.  Looking up the merely-current version here would
        -- create false lineage if the KB rolled over during analysis.
        v_profile_version_pk := NULL;
        v_notice_title := NULL;
        v_issuing_organization := NULL;
        v_source_url := NULL;
        v_notice_status := NULL;
        SELECT
            pv.profile_version_pk,
            NULLIF(n.portal_metadata ->> 'title', ''),
            NULLIF(n.portal_metadata ->> 'executing_agency', ''),
            COALESCE(
                NULLIF(n.portal_metadata ->> 'detail_url', ''),
                sv.notice_detail_url,
                sv.source_url
            ),
            NULLIF(n.portal_metadata ->> 'source_state', '')
          INTO v_profile_version_pk,
               v_notice_title,
               v_issuing_organization,
               v_source_url,
               v_notice_status
          FROM kb.profile_version pv
          JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk
          JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk
          JOIN kb.notice n ON n.notice_pk = sp.notice_pk
         WHERE pv.profile_version_pk = (v_item->>'profile_version_pk')::uuid
           AND sp.source_profile_id = v_item->>'source_profile_id';
        IF v_profile_version_pk IS NULL THEN
            RAISE EXCEPTION 'EXISTING_PROFILE_NOT_FOUND: %', v_item->>'source_profile_id' USING ERRCODE = '22023';
        END IF;
        INSERT INTO result.sim_candidate (
            analysis_case_pk, existing_profile_version_pk, rank_no, similarity_score,
            priority_score, status, notice_title, issuing_organization, source_url,
            notice_status, summary_text, comparable_axes,
            purpose_result, target_result, support_result, delivery_result
        ) VALUES (
            v_case_pk, v_profile_version_pk, (v_item->>'rank')::integer,
            NULLIF(v_item->>'similarity_score', '')::numeric,
            NULLIF(v_item->>'priority_score', '')::numeric,
            v_item->>'status', v_notice_title, v_issuing_organization, v_source_url,
            v_notice_status, NULLIF(v_item->>'summary_text', ''),
            ARRAY(SELECT jsonb_array_elements_text(COALESCE(v_item->'comparable_axes', '[]'::jsonb))),
            COALESCE(v_item->'purpose_result', '{}'::jsonb), COALESCE(v_item->'target_result', '{}'::jsonb),
            COALESCE(v_item->'support_result', '{}'::jsonb), COALESCE(v_item->'delivery_result', '{}'::jsonb)
        );
    END LOOP;

    FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_result->'evidences', '[]'::jsonb)) LOOP
        v_evidence_candidate_pk := NULL;
        v_evidence_profile_version_pk := NULL;

        -- The field is optional for backward compatibility.  A supplied
        -- value is fail-closed: it must name one of this result's candidates,
        -- not merely any current profile in the knowledge base.
        IF NULLIF(v_item->>'candidate_source_profile_id', '') IS NOT NULL THEN
            SELECT sc.sim_candidate_pk, sc.existing_profile_version_pk
              INTO v_evidence_candidate_pk, v_evidence_profile_version_pk
              FROM result.sim_candidate sc
              JOIN kb.profile_version pv
                ON pv.profile_version_pk = sc.existing_profile_version_pk
              JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk
              JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk
             WHERE sc.analysis_case_pk = v_case_pk
               AND sp.source_profile_id = v_item->>'candidate_source_profile_id';

            IF v_evidence_candidate_pk IS NULL THEN
                RAISE EXCEPTION 'EVIDENCE_CANDIDATE_NOT_FOUND: %',
                    v_item->>'candidate_source_profile_id'
                    USING ERRCODE = '22023';
            END IF;
        END IF;

        INSERT INTO result.evidence_snapshot (
            analysis_case_pk, axis_type, sim_candidate_pk, side, field_name,
            raw_value, context_excerpt, source_sha256, candidate_pack_block_id,
            common_ir_document_id, common_ir_block_id, common_ir_cell_id,
            common_ir_occurrence_ids, existing_profile_version_pk
        ) VALUES (
            v_case_pk, NULLIF(v_item->>'axis_type', ''), v_evidence_candidate_pk,
            COALESCE(v_item->>'side', 'REQUEST'), NULLIF(v_item->>'field_name', ''),
            COALESCE(v_item->>'raw_value', ''), NULLIF(v_item->>'context_excerpt', ''),
            NULLIF(v_item->>'source_sha256', ''), NULLIF(v_item->>'candidate_pack_block_id', ''),
            NULLIF(v_item->>'common_ir_document_id', ''), NULLIF(v_item->>'common_ir_block_id', ''),
            NULLIF(v_item->>'common_ir_cell_id', ''),
            ARRAY(SELECT jsonb_array_elements_text(COALESCE(v_item->'common_ir_occurrence_ids', '[]'::jsonb))),
            v_evidence_profile_version_pk
        );
    END LOOP;

    INSERT INTO result.analysis_session (analysis_case_pk, status, expires_at)
    VALUES (v_case_pk, 'active', v_now + interval '30 minutes')
    ON CONFLICT (analysis_case_pk) DO UPDATE
        SET status = 'active', last_activity_at = v_now,
            expires_at = v_now + interval '30 minutes', closed_at = NULL;

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

    UPDATE workspace.analysis_run_dispatch
       SET processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL,
           last_error_code = NULL,
           last_error_message = NULL
     WHERE analysis_run_pk = p_analysis_run_pk;

    UPDATE workspace.analysis_run
       SET status = 'succeeded',
           completed_at = v_now,
           analysis_case_pk = v_case_pk,
           error_code = NULL,
           error_message = NULL
     WHERE analysis_run_pk = p_analysis_run_pk;

    RETURN v_case_pk;
END;
$$;

COMMENT ON FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB) IS
    'Trusted fenced result ingest. Returns the analysis_case UUID on success and NULL when the processing_run fence is stale, replaced, or expired. Evidence may include optional candidate_source_profile_id, which must resolve to a candidate in the same result and is persisted for candidate-detail reads.';

REVOKE ALL ON FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB)
FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB)
TO service_role;

REVOKE ALL ON FUNCTION api.rpc_get_sim_candidate_detail(UUID)
FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION api.rpc_get_analysis_result(UUID)
FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION api.rpc_get_analysis_result(UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION api.rpc_get_sim_candidate_detail(UUID) TO authenticated;
GRANT SELECT ON api.v_my_analysis_history TO authenticated;

COMMIT;
