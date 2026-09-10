\set ON_ERROR_STOP on

-- Runtime contract test for migrations 21-24. Everything is rolled back.
--
-- It deliberately reuses a current Existing-KB profile when one is present,
-- rather than creating a synthetic KB lineage.  A database without an
-- Existing corpus still validates the queue, fence, result, and retention
-- contracts; it emits a NOTICE and skips only the candidate/evidence branch.
BEGIN;

DO $$
BEGIN
    IF to_regprocedure(
        'workspace.complete_analysis_run(uuid,uuid,uuid)'
    ) IS NOT NULL THEN
        RAISE EXCEPTION
            'legacy state-only worker completion function is still callable';
    END IF;
END;
$$;

DO $$
DECLARE
    v_user_id UUID := gen_random_uuid();
    v_run_id UUID := gen_random_uuid();
    v_first RECORD;
    v_second RECORD;
    v_case_id UUID;
    v_existing_source_profile_id TEXT;
    v_existing_profile_version_pk UUID;
    v_expected_notice_title TEXT;
    v_expected_issuing_organization TEXT;
    v_expected_source_url TEXT;
    v_expected_notice_status TEXT;
    v_candidate_id UUID;
    v_candidate_profile_version_pk UUID;
    v_candidate_notice_title TEXT;
    v_candidate_issuing_organization TEXT;
    v_candidate_source_url TEXT;
    v_candidate_notice_status TEXT;
    v_evidence_candidate_id UUID;
    v_evidence_profile_version_pk UUID;
    v_result_payload JSONB;
    v_state TEXT;
    v_count INTEGER;
BEGIN
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_user_id, 'queue-contract@example.invalid', now(), now());

    INSERT INTO workspace.analysis_run (
        analysis_run_pk, user_id, status, original_filename,
        declared_mime_type, declared_size_bytes
    ) VALUES (
        v_run_id, v_user_id, 'queued', 'contract.hwpx',
        'application/vnd.hancom.hwpx', 4
    );
    INSERT INTO workspace.analysis_run_dispatch (
        analysis_run_pk, source_bucket, source_object_key,
        source_content_sha256
    ) VALUES (
        v_run_id, 'request-temp', v_run_id || '/source/' || repeat('a', 64) || '.hwpx',
        repeat('a', 64)
    );

    SELECT * INTO STRICT v_first
      FROM workspace.claim_next_analysis_run('runtime-contract-worker', 120);
    IF v_first.analysis_run_pk <> v_run_id OR v_first.attempt_count <> 1
       OR v_first.processing_run_pk IS NULL THEN
        RAISE EXCEPTION 'first claim contract failed';
    END IF;
    IF NOT workspace.heartbeat_analysis_run(
        v_run_id, v_first.processing_run_pk, 120
    ) THEN
        RAISE EXCEPTION 'heartbeat contract failed';
    END IF;

    -- First explicit failure consumes one processing attempt and requeues.
    IF NOT workspace.fail_analysis_run(
        v_run_id,
        v_first.processing_run_pk,
        'ANALYSIS_FAILED',
        '안전한 사용자 문구',
        'runtime contract internal diagnostic'
    ) THEN
        RAISE EXCEPTION 'first failure was fenced unexpectedly';
    END IF;
    SELECT status INTO v_state
      FROM workspace.analysis_run WHERE analysis_run_pk = v_run_id;
    IF v_state <> 'queued' THEN
        RAISE EXCEPTION 'first failure did not requeue: %', v_state;
    END IF;

    SELECT * INTO STRICT v_second
      FROM workspace.claim_next_analysis_run('runtime-contract-worker', 120);
    IF v_second.analysis_run_pk <> v_run_id OR v_second.attempt_count <> 2
       OR v_second.processing_run_pk = v_first.processing_run_pk THEN
        RAISE EXCEPTION 'second claim contract failed';
    END IF;

    -- This is the same trusted current-lineage lookup used by the fenced
    -- materialiser.  Do not manufacture a KB profile in this test: that would
    -- hide importer/lineage regressions and would not exercise real metadata.
    SELECT
        sp.source_profile_id,
        pv.profile_version_pk,
        NULLIF(n.portal_metadata ->> 'title', ''),
        NULLIF(n.portal_metadata ->> 'executing_agency', ''),
        COALESCE(
            NULLIF(n.portal_metadata ->> 'detail_url', ''),
            sv.notice_detail_url,
            sv.source_url
        ),
        NULLIF(n.portal_metadata ->> 'source_state', '')
      INTO v_existing_source_profile_id,
           v_existing_profile_version_pk,
           v_expected_notice_title,
           v_expected_issuing_organization,
           v_expected_source_url,
           v_expected_notice_status
      FROM kb.profile_version pv
      JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk
      JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk
      JOIN kb.notice n ON n.notice_pk = sp.notice_pk
     WHERE pv.is_current
       AND sv.is_current
     ORDER BY n.notice_id, sp.source_profile_id
     LIMIT 1;

    v_result_payload := jsonb_build_object(
        'program_name', '런타임 계약 검증',
        'axes', jsonb_build_array(jsonb_build_object(
            'axis_type', 'CPL',
            'axis_code', 'CPL-01',
            'status', 'confirmed',
            'summary_text', '계약 검증',
            'result_data', '{}'::jsonb
        )),
        'candidates', '[]'::jsonb,
        'evidences', '[]'::jsonb
    );

    IF v_existing_source_profile_id IS NOT NULL THEN
        v_result_payload := jsonb_set(
            v_result_payload,
            '{candidates}',
            jsonb_build_array(jsonb_build_object(
                'source_profile_id', v_existing_source_profile_id,
                'profile_version_pk', v_existing_profile_version_pk,
                'rank', 1,
                'similarity_score', 0.99,
                'priority_score', 0.88,
                'status', 'recommended',
                'summary_text', 'KB snapshot candidate',
                'comparable_axes', jsonb_build_array('purpose', 'target'),
                'purpose_result', jsonb_build_object('status', 'matched'),
                'target_result', jsonb_build_object('status', 'matched'),
                'support_result', '{}'::jsonb,
                'delivery_result', '{}'::jsonb
            ))
        );
        v_result_payload := jsonb_set(
            v_result_payload,
            '{evidences}',
            jsonb_build_array(jsonb_build_object(
                'candidate_source_profile_id', v_existing_source_profile_id,
                'axis_type', 'SIM',
                'side', 'EXISTING',
                'field_name', 'purpose_goal',
                'raw_value', 'runtime candidate-linked evidence',
                'context_excerpt', 'runtime candidate-linked excerpt',
                'source_sha256', repeat('b', 64),
                'common_ir_document_id', 'runtime-common-ir-document',
                'common_ir_block_id', 'runtime-common-ir-block'
            ))
        );
    ELSE
        RAISE NOTICE 'No current Existing-KB profile found; candidate/evidence validation skipped.';
    END IF;

    -- Simulate a KB rollover after retrieval but before result persistence.
    -- The result must retain the exact version that was actually compared,
    -- even when it is no longer marked current at commit time.
    IF v_existing_profile_version_pk IS NOT NULL THEN
        UPDATE kb.profile_version
           SET is_current = FALSE
         WHERE profile_version_pk = v_existing_profile_version_pk;
    END IF;

    v_case_id := workspace.persist_analysis_result_core(
        v_run_id,
        v_second.processing_run_pk,
        v_result_payload
    );
    IF v_case_id IS NULL THEN
        RAISE EXCEPTION 'live fenced result was not persisted';
    END IF;

    SELECT status INTO v_state
      FROM workspace.analysis_run WHERE analysis_run_pk = v_run_id;
    IF v_state <> 'succeeded' THEN
        RAISE EXCEPTION 'successful result did not complete run: %', v_state;
    END IF;
    SELECT count(*) INTO v_count
      FROM ops.processing_run WHERE source_analysis_run_id = v_run_id;
    IF v_count <> 2 THEN
        RAISE EXCEPTION 'processing attempt history count is %, expected 2', v_count;
    END IF;

    -- The old attempt is fenced and must not alter the materialised result.
    IF workspace.persist_analysis_result_core(
        v_run_id,
        v_first.processing_run_pk,
        jsonb_build_object(
            'program_name', 'stale overwrite',
            'axes', '[]'::jsonb,
            'candidates', '[]'::jsonb,
            'evidences', '[]'::jsonb
        )
    ) IS NOT NULL THEN
        RAISE EXCEPTION 'stale processing token unexpectedly published';
    END IF;
    SELECT count(*) INTO v_count
      FROM result.axis_result WHERE analysis_case_pk = v_case_id;
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'stale write changed result axes';
    END IF;

    IF v_existing_source_profile_id IS NOT NULL THEN
        SELECT
            sc.sim_candidate_pk,
            sc.existing_profile_version_pk,
            sc.notice_title,
            sc.issuing_organization,
            sc.source_url,
            sc.notice_status
          INTO STRICT v_candidate_id,
                      v_candidate_profile_version_pk,
                      v_candidate_notice_title,
                      v_candidate_issuing_organization,
                      v_candidate_source_url,
                      v_candidate_notice_status
          FROM result.sim_candidate sc
         WHERE sc.analysis_case_pk = v_case_id
           AND sc.rank_no = 1;
        IF v_candidate_profile_version_pk IS DISTINCT FROM v_existing_profile_version_pk
           OR v_candidate_notice_title IS DISTINCT FROM v_expected_notice_title
           OR v_candidate_issuing_organization IS DISTINCT FROM v_expected_issuing_organization
           OR v_candidate_source_url IS DISTINCT FROM v_expected_source_url
           OR v_candidate_notice_status IS DISTINCT FROM v_expected_notice_status THEN
            RAISE EXCEPTION 'candidate display metadata was not derived from Existing-KB lineage';
        END IF;

        SELECT e.sim_candidate_pk, e.existing_profile_version_pk
          INTO STRICT v_evidence_candidate_id, v_evidence_profile_version_pk
          FROM result.evidence_snapshot e
         WHERE e.analysis_case_pk = v_case_id
           AND e.raw_value = 'runtime candidate-linked evidence';
        IF v_evidence_candidate_id IS DISTINCT FROM v_candidate_id
           OR v_evidence_profile_version_pk IS DISTINCT FROM v_existing_profile_version_pk THEN
            RAISE EXCEPTION 'candidate-linked evidence did not retain KB candidate lineage';
        END IF;
    END IF;

    -- Preserve only opaque identifiers in transaction-local settings so the
    -- following authenticated-role section can exercise the browser read API.
    PERFORM set_config('runtime_contract.user_id', v_user_id::text, true);
    PERFORM set_config('runtime_contract.case_id', v_case_id::text, true);
    PERFORM set_config('runtime_contract.candidate_id', COALESCE(v_candidate_id::text, ''), true);
END;
$$;

-- Exercise the granted browser read surface under the same request claim that
-- PostgREST would set.  The worker/materialiser portion above intentionally
-- remains a privileged operation.
SELECT set_config(
    'request.jwt.claim.sub', current_setting('runtime_contract.user_id'), true
);
SET LOCAL ROLE authenticated;

DO $$
DECLARE
    v_case_id UUID := current_setting('runtime_contract.case_id')::UUID;
    v_candidate_id_text TEXT := current_setting('runtime_contract.candidate_id', true);
    v_result JSONB;
    v_candidate_detail JSONB;
    v_count INTEGER;
BEGIN
    v_result := api.rpc_get_analysis_result(v_case_id);
    IF v_result #>> '{case,analysis_case_id}' <> v_case_id::text THEN
        RAISE EXCEPTION 'authenticated result RPC returned the wrong case';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM jsonb_array_elements(v_result -> 'cpl' -> 'items') item
         WHERE item ->> 'code' = 'CPL-01'
    ) THEN
        RAISE EXCEPTION 'authenticated result RPC omitted CPL projection';
    END IF;

    SELECT count(*) INTO v_count
      FROM api.v_my_analysis_history
     WHERE analysis_case_id = v_case_id;
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'authenticated history view did not expose live case';
    END IF;

    IF COALESCE(v_candidate_id_text, '') <> '' THEN
        IF NOT EXISTS (
            SELECT 1
              FROM jsonb_array_elements(v_result -> 'sim' -> 'candidates') item
             WHERE item ->> 'sim_candidate_id' = v_candidate_id_text
        ) THEN
            RAISE EXCEPTION 'authenticated result RPC omitted KB candidate';
        END IF;
        v_candidate_detail := api.rpc_get_sim_candidate_detail(v_candidate_id_text::UUID);
        IF v_candidate_detail ->> 'sim_candidate_id' <> v_candidate_id_text
           OR NOT EXISTS (
                SELECT 1
                  FROM jsonb_array_elements(v_candidate_detail -> 'evidences') item
                 WHERE item ->> 'raw_value' = 'runtime candidate-linked evidence'
           ) THEN
            RAISE EXCEPTION 'candidate detail RPC omitted linked evidence';
        END IF;
    END IF;
END;
$$;

-- Expiry must hide the existing row immediately, even before cleanup.  Switch
-- back to the owner role only for the controlled expiry update, then return to
-- the authenticated browser role for the negative reads.
RESET ROLE;
UPDATE result.analysis_case
   SET retention_expires_at = now() - interval '1 second'
 WHERE analysis_case_pk = current_setting('runtime_contract.case_id')::UUID;
SET LOCAL ROLE authenticated;

DO $$
DECLARE
    v_case_id UUID := current_setting('runtime_contract.case_id')::UUID;
    v_candidate_id_text TEXT := current_setting('runtime_contract.candidate_id', true);
    v_count INTEGER;
BEGIN
    SELECT count(*) INTO v_count
      FROM api.v_my_analysis_history
     WHERE analysis_case_id = v_case_id;
    IF v_count <> 0 THEN
        RAISE EXCEPTION 'expired case remained visible in history';
    END IF;

    BEGIN
        PERFORM api.rpc_get_analysis_result(v_case_id);
        RAISE EXCEPTION 'expired case remained visible through result RPC';
    EXCEPTION WHEN SQLSTATE 'P0002' THEN
        NULL;
    END;

    IF COALESCE(v_candidate_id_text, '') <> '' THEN
        BEGIN
            PERFORM api.rpc_get_sim_candidate_detail(v_candidate_id_text::UUID);
            RAISE EXCEPTION 'expired candidate remained visible through detail RPC';
        EXCEPTION WHEN SQLSTATE 'P0002' THEN
            NULL;
        END;
    END IF;
END;
$$;

RESET ROLE;

ROLLBACK;
