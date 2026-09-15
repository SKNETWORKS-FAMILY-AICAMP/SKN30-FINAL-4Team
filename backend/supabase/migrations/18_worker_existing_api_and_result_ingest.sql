-- Worker-facing KB retrieval and terminal comparison-result ingestion.
-- These functions are callable only by the service role through Edge Functions.
BEGIN;

CREATE OR REPLACE FUNCTION api.worker_list_existing_candidates(
    p_limit INTEGER DEFAULT 100,
    p_offset INTEGER DEFAULT 0
) RETURNS TABLE (
    profile_version_id UUID,
    source_profile_id TEXT,
    notice_id TEXT,
    source_kind TEXT,
    portal_metadata JSONB
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, kb, api
AS $$
    SELECT
        pv.profile_version_pk,
        sp.source_profile_id,
        n.notice_id,
        sp.source_kind,
        jsonb_strip_nulls(jsonb_build_object(
            'title', n.portal_metadata->'title',
            'support_field', n.portal_metadata->'support_field',
            'category', n.portal_metadata->'category',
            'apply_period', n.portal_metadata->'apply_period',
            'ministry', n.portal_metadata->'ministry',
            'executing_agency', n.portal_metadata->'executing_agency',
            'registered_at', n.portal_metadata->'registered_at',
            'detail_url', n.portal_metadata->'detail_url',
            'source_state', n.portal_metadata->'source_state'
        ))
    FROM kb.profile_version pv
    JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk
    JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk
    JOIN kb.notice n ON n.notice_pk = sp.notice_pk
    WHERE pv.is_current AND sv.is_current
      -- The imported Bizinfo package carries normalized list/census metadata.
      -- This excludes unrelated historical fixtures from worker candidate search.
      AND n.portal_metadata ? 'support_field'
    ORDER BY n.notice_id, sp.source_profile_id
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 100), 1), 500)
    OFFSET GREATEST(COALESCE(p_offset, 0), 0);
$$;

CREATE OR REPLACE FUNCTION api.worker_get_existing_profile(
    p_source_profile_id TEXT
) RETURNS TABLE (
    profile_version_id UUID,
    source_profile_id TEXT,
    notice_id TEXT,
    source_kind TEXT,
    portal_metadata JSONB,
    storage_bucket TEXT,
    storage_object_key TEXT,
    schema_version TEXT
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, kb, api
AS $$
    SELECT
        pv.profile_version_pk,
        sp.source_profile_id,
        n.notice_id,
        sp.source_kind,
        jsonb_strip_nulls(jsonb_build_object(
            'title', n.portal_metadata->'title',
            'support_field', n.portal_metadata->'support_field',
            'category', n.portal_metadata->'category',
            'apply_period', n.portal_metadata->'apply_period',
            'ministry', n.portal_metadata->'ministry',
            'executing_agency', n.portal_metadata->'executing_agency',
            'registered_at', n.portal_metadata->'registered_at',
            'detail_url', n.portal_metadata->'detail_url',
            'source_state', n.portal_metadata->'source_state'
        )),
        a.storage_bucket,
        a.storage_object_key,
        pv.schema_version
    FROM kb.profile_version pv
    JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk
    JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk
    JOIN kb.notice n ON n.notice_pk = sp.notice_pk
    JOIN kb.artifact a ON a.artifact_pk = pv.structured_artifact_pk
    WHERE pv.is_current
      AND sv.is_current
      AND n.portal_metadata ? 'support_field'
      AND sp.source_profile_id = p_source_profile_id;
$$;

CREATE OR REPLACE FUNCTION api.ingest_comparison_result_core(
    p_analysis_run_id UUID,
    p_result JSONB
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, kb, workspace, result, api
AS $$
DECLARE
    v_case_pk UUID;
    v_user_id UUID;
    v_filename TEXT;
    v_item JSONB;
    v_profile_version_pk UUID;
    v_ordinal INTEGER := 0;
BEGIN
    IF jsonb_typeof(p_result) <> 'object' THEN
        RAISE EXCEPTION 'COMPARISON_RESULT_OBJECT_REQUIRED' USING ERRCODE = '22023';
    END IF;

    SELECT user_id, original_filename INTO v_user_id, v_filename
      FROM workspace.analysis_run
     WHERE analysis_run_pk = p_analysis_run_id
       AND status IN ('queued', 'running');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ANALYSIS_RUN_NOT_ACCEPTING_RESULT' USING ERRCODE = 'P0002';
    END IF;

    INSERT INTO result.analysis_case (
        source_analysis_run_id, user_id, case_status, analysis_completed_at,
        retention_expires_at, program_name, original_filename
    ) VALUES (
        p_analysis_run_id, v_user_id, 'ready', now(), now() + interval '90 days',
        NULLIF(p_result->>'program_name', ''), v_filename
    ) ON CONFLICT (source_analysis_run_id) DO UPDATE
        SET case_status = 'ready', analysis_completed_at = now(),
            retention_expires_at = now() + interval '90 days',
            program_name = COALESCE(NULLIF(EXCLUDED.program_name, ''), result.analysis_case.program_name),
            original_filename = COALESCE(EXCLUDED.original_filename, result.analysis_case.original_filename),
            updated_at = now()
    RETURNING analysis_case_pk INTO v_case_pk;

    -- A callback retry replaces its own materialised result, not another run's.
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
    VALUES (v_case_pk, 'active', now() + interval '30 minutes')
    ON CONFLICT (analysis_case_pk) DO UPDATE
        SET status = 'active', last_activity_at = now(), expires_at = now() + interval '30 minutes', closed_at = NULL;

    UPDATE workspace.analysis_run
       SET status = 'succeeded', completed_at = now(), analysis_case_pk = v_case_pk,
           error_code = NULL, error_message = NULL
     WHERE analysis_run_pk = p_analysis_run_id;
    RETURN v_case_pk;
END;
$$;

REVOKE ALL ON FUNCTION api.worker_list_existing_candidates(INTEGER, INTEGER),
                       api.worker_get_existing_profile(TEXT),
                       api.ingest_comparison_result_core(UUID, JSONB)
FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA api TO service_role;
GRANT EXECUTE ON FUNCTION api.worker_list_existing_candidates(INTEGER, INTEGER),
                         api.worker_get_existing_profile(TEXT),
                         api.ingest_comparison_result_core(UUID, JSONB)
TO service_role;

COMMIT;
