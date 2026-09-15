-- ============================================================================
-- Migration 22: Fenced analysis result materialisation
--
-- Result rows and terminal queue state must be committed together.  The
-- worker supplies the processing_run_pk returned by migration 21 claim; a
-- stale, expired, or replaced token returns NULL before any result mutation.
-- The older api.ingest_comparison_result_core(UUID, JSONB) has no such fence
-- and is retired from service-role use by this migration.
-- ============================================================================

BEGIN;

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
    v_ordinal INTEGER := 0;
BEGIN
    -- This is the fencing boundary.  Row locks are held until the enclosing
    -- function transaction commits, so an expired worker cannot publish after
    -- a replacement claim has taken the dispatch row.
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

    -- NULL is an expected fenced-out outcome, not an error.  Because this
    -- happens before materialisation, it creates or changes no cases, axes,
    -- candidates, evidence, sessions, or public queue state.
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

    -- A valid retry replaces materialised data for its own run only.  A
    -- fenced-out retry returned above before it could reach these deletes.
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
           OR NULLIF(v_item->>'rank', '') IS NULL
           OR (v_item->>'rank')::integer < 1
           OR COALESCE(v_item->>'status', '') = '' THEN
            RAISE EXCEPTION 'INVALID_COMPARISON_CANDIDATE' USING ERRCODE = '22023';
        END IF;
        SELECT pv.profile_version_pk INTO v_profile_version_pk
          FROM kb.profile_version pv
          JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk
          JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk
         WHERE pv.is_current AND sv.is_current
           AND sp.source_profile_id = v_item->>'source_profile_id';
        IF v_profile_version_pk IS NULL THEN
            RAISE EXCEPTION 'EXISTING_PROFILE_NOT_FOUND: %', v_item->>'source_profile_id' USING ERRCODE = '22023';
        END IF;
        INSERT INTO result.sim_candidate (
            analysis_case_pk, existing_profile_version_pk, rank_no, similarity_score,
            priority_score, status, summary_text, comparable_axes,
            purpose_result, target_result, support_result, delivery_result
        ) VALUES (
            v_case_pk, v_profile_version_pk, (v_item->>'rank')::integer,
            NULLIF(v_item->>'similarity_score', '')::numeric,
            NULLIF(v_item->>'priority_score', '')::numeric,
            v_item->>'status', NULLIF(v_item->>'summary_text', ''),
            ARRAY(SELECT jsonb_array_elements_text(COALESCE(v_item->'comparable_axes', '[]'::jsonb))),
            COALESCE(v_item->'purpose_result', '{}'::jsonb), COALESCE(v_item->'target_result', '{}'::jsonb),
            COALESCE(v_item->'support_result', '{}'::jsonb), COALESCE(v_item->'delivery_result', '{}'::jsonb)
        );
    END LOOP;

    FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_result->'evidences', '[]'::jsonb)) LOOP
        INSERT INTO result.evidence_snapshot (
            analysis_case_pk, axis_type, side, field_name, raw_value, context_excerpt,
            source_sha256, candidate_pack_block_id, common_ir_document_id, common_ir_block_id,
            common_ir_cell_id, common_ir_occurrence_ids
        ) VALUES (
            v_case_pk, NULLIF(v_item->>'axis_type', ''), COALESCE(v_item->>'side', 'REQUEST'),
            NULLIF(v_item->>'field_name', ''), COALESCE(v_item->>'raw_value', ''),
            NULLIF(v_item->>'context_excerpt', ''), NULLIF(v_item->>'source_sha256', ''),
            NULLIF(v_item->>'candidate_pack_block_id', ''), NULLIF(v_item->>'common_ir_document_id', ''),
            NULLIF(v_item->>'common_ir_block_id', ''), NULLIF(v_item->>'common_ir_cell_id', ''),
            ARRAY(SELECT jsonb_array_elements_text(COALESCE(v_item->'common_ir_occurrence_ids', '[]'::jsonb)))
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
    'Trusted fenced result ingest. Returns the analysis_case UUID on success and NULL when the processing_run fence is stale, replaced, or expired; NULL performs no result or state mutation.';

-- Migration 18 remains on disk for history and can be called by its owner,
-- but it is intentionally unavailable to the service-role worker/legacy Edge
-- callback because it lacks a processing_run_pk fence.
COMMENT ON FUNCTION api.ingest_comparison_result_core(UUID, JSONB) IS
    'Deprecated unfenced legacy Edge callback. Service-role execution is revoked; use workspace.persist_analysis_result_core(UUID, UUID, JSONB).';
REVOKE ALL ON FUNCTION api.ingest_comparison_result_core(UUID, JSONB)
FROM PUBLIC, anon, authenticated, service_role;

REVOKE ALL ON FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB)
FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB)
TO service_role;

COMMIT;
