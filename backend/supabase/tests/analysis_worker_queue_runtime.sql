\set ON_ERROR_STOP on

-- Runtime contract test for migrations 21-25. Everything is rolled back.
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

-- Exercise the exact migration-25 quarantine helper against a pre-trigger
-- legacy shape. Trigger disabling is transaction-local test setup only; every
-- trigger is re-enabled before the helper is called and the file rolls back.
DO $$
DECLARE
    v_user_id UUID := gen_random_uuid();
    v_run_id UUID := gen_random_uuid();
    v_processing_run_pk UUID;
    v_quarantined INTEGER;
    v_status TEXT;
    v_count INTEGER;
BEGIN
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_user_id, 'queue-quarantine@example.invalid', now(), now());

    INSERT INTO workspace.analysis_run (
        analysis_run_pk, user_id, status, original_filename,
        declared_mime_type, declared_size_bytes
    ) VALUES (
        v_run_id, v_user_id, 'uploading', 'legacy.hwpx',
        'application/vnd.hancom.hwpx', 4
    );
    INSERT INTO workspace.analysis_run_dispatch (
        analysis_run_pk, source_bucket, source_object_key, source_content_sha256
    ) VALUES (
        v_run_id, 'request-temp', v_run_id || '/source/legacy.hwpx',
        NULL
    );

    -- Recreate a pre-migration queued row without disabling dispatch's new
    -- deferred constraint trigger. Dispatch is validly reserved while the run
    -- is uploading; only the guarded legacy status edge is injected.
    EXECUTE 'ALTER TABLE workspace.analysis_run DISABLE TRIGGER trg_workspace_analysis_run_queued_source_invariant';
    UPDATE workspace.analysis_run
       SET status = 'queued'
     WHERE analysis_run_pk = v_run_id;
    EXECUTE 'ALTER TABLE workspace.analysis_run ENABLE TRIGGER trg_workspace_analysis_run_queued_source_invariant';

    INSERT INTO ops.processing_run (
        source_analysis_run_id, run_type, status, started_at
    ) VALUES (
        v_run_id, 'analysis', 'running', clock_timestamp()
    ) RETURNING processing_run_pk INTO v_processing_run_pk;
    UPDATE workspace.analysis_run_dispatch
       SET attempt_count = 1,
           processing_run_pk = v_processing_run_pk,
           claimed_by = 'legacy-worker',
           claimed_at = clock_timestamp(),
           heartbeat_at = clock_timestamp(),
           lease_expires_at = clock_timestamp() + interval '120 seconds'
     WHERE analysis_run_pk = v_run_id;

    v_quarantined := workspace.quarantine_invalid_queued_analysis_runs();
    IF v_quarantined < 1 THEN
        RAISE EXCEPTION 'invalid legacy queued row was not quarantined';
    END IF;

    SELECT status INTO STRICT v_status
      FROM workspace.analysis_run
     WHERE analysis_run_pk = v_run_id;
    IF v_status <> 'failed' THEN
        RAISE EXCEPTION 'invalid legacy run was not failed: %', v_status;
    END IF;
    SELECT count(*) INTO v_count
      FROM ops.processing_run
     WHERE processing_run_pk = v_processing_run_pk
       AND status = 'failed'
       AND error_code = 'QUEUED_SOURCE_INVARIANT_VIOLATION';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'legacy live processing attempt was not failed';
    END IF;
    SELECT count(*) INTO v_count
      FROM workspace.analysis_run_dispatch
     WHERE analysis_run_pk = v_run_id
       AND processing_run_pk IS NULL
       AND claimed_by IS NULL
       AND claimed_at IS NULL
       AND heartbeat_at IS NULL
       AND lease_expires_at IS NULL;
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'legacy dispatch lease was not cleared';
    END IF;
END;
$$;

-- Every element of the dispatch/source tuple is independently fail-closed.
DO $$
DECLARE
    v_user_id UUID := gen_random_uuid();
    v_run_id UUID;
    v_case INTEGER;
    v_status TEXT;
    v_dispatch_key TEXT;
BEGIN
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_user_id, 'queue-mismatch@example.invalid', now(), now());

    FOR v_case IN 1..4 LOOP
        v_run_id := gen_random_uuid();
        v_dispatch_key := v_run_id || '/source/' || repeat('a', 64) || '.hwpx';

        INSERT INTO workspace.analysis_run (
            analysis_run_pk, user_id, status, original_filename,
            declared_mime_type, declared_size_bytes
        ) VALUES (
            v_run_id, v_user_id, 'uploading', 'mismatch.hwpx',
            'application/vnd.hancom.hwpx', 4
        );
        INSERT INTO workspace.analysis_run_dispatch (
            analysis_run_pk, source_bucket, source_object_key,
            source_content_sha256
        ) VALUES (
            v_run_id, 'request-temp', v_dispatch_key, repeat('a', 64)
        );
        INSERT INTO workspace.source_artifact (
            analysis_run_pk, artifact_type, storage_bucket,
            storage_object_key, content_sha256, mime_type, size_bytes
        ) VALUES (
            v_run_id,
            'source',
            CASE WHEN v_case = 1 THEN 'analysis-reports' ELSE 'request-temp' END,
            CASE WHEN v_case = 2 THEN v_run_id || '/source/wrong.hwpx' ELSE v_dispatch_key END,
            CASE WHEN v_case = 3 THEN repeat('b', 64) ELSE repeat('a', 64) END,
            'application/vnd.hancom.hwpx',
            CASE WHEN v_case = 4 THEN 5 ELSE 4 END
        );

        BEGIN
            UPDATE workspace.analysis_run
               SET status = 'queued'
             WHERE analysis_run_pk = v_run_id;
            RAISE EXCEPTION 'mismatched source tuple case % was accepted', v_case;
        EXCEPTION WHEN check_violation THEN
            NULL;
        END;
        SELECT status INTO STRICT v_status
          FROM workspace.analysis_run
         WHERE analysis_run_pk = v_run_id;
        IF v_status <> 'uploading' THEN
            RAISE EXCEPTION 'mismatch case % changed run state: %', v_case, v_status;
        END IF;

        UPDATE workspace.analysis_run
           SET status = 'failed', completed_at = clock_timestamp()
         WHERE analysis_run_pk = v_run_id;
    END LOOP;
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
    v_dummy_processing_run_pk UUID;
BEGIN
    INSERT INTO auth.users (id, email, created_at, updated_at)
    VALUES (v_user_id, 'queue-contract@example.invalid', now(), now());

    INSERT INTO workspace.analysis_run (
        analysis_run_pk, user_id, status, original_filename,
        declared_mime_type, declared_size_bytes
    ) VALUES (
        v_run_id, v_user_id, 'uploading', 'contract.hwpx',
        'application/vnd.hancom.hwpx', 4
    );
    INSERT INTO workspace.analysis_run_dispatch (
        analysis_run_pk, source_bucket, source_object_key,
        source_content_sha256
    ) VALUES (
        v_run_id, 'request-temp', v_run_id || '/source/' || repeat('a', 64) || '.hwpx',
        repeat('a', 64)
    );

    -- A reservation cannot become worker-visible until its exact immutable
    -- source artifact exists.  The failed statement is contained in a PL/pgSQL
    -- subtransaction and must leave the run in uploading state.
    BEGIN
        UPDATE workspace.analysis_run
           SET status = 'queued'
         WHERE analysis_run_pk = v_run_id;
        RAISE EXCEPTION 'queued transition without a source artifact was accepted';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;
    SELECT status INTO v_state
      FROM workspace.analysis_run WHERE analysis_run_pk = v_run_id;
    IF v_state <> 'uploading' THEN
        RAISE EXCEPTION 'rejected queued transition changed run state: %', v_state;
    END IF;

    INSERT INTO workspace.source_artifact (
        analysis_run_pk, artifact_type, artifact_logical_id,
        storage_bucket, storage_object_key, content_sha256,
        mime_type, size_bytes
    ) VALUES (
        v_run_id, 'source', 'contract.hwpx',
        'request-temp', v_run_id || '/source/' || repeat('a', 64) || '.hwpx',
        repeat('a', 64), 'application/vnd.hancom.hwpx', 4
    );

    -- The partial unique index rejects a second authoritative source even when
    -- its Storage tuple itself is unique.
    BEGIN
        INSERT INTO workspace.source_artifact (
            analysis_run_pk, artifact_type, artifact_logical_id,
            storage_bucket, storage_object_key, content_sha256,
            mime_type, size_bytes
        ) VALUES (
            v_run_id, 'source', 'duplicate-contract.hwpx',
            'request-temp', v_run_id || '/source/' || repeat('b', 64) || '.hwpx',
            repeat('b', 64), 'application/vnd.hancom.hwpx', 4
        );
        RAISE EXCEPTION 'second source artifact was accepted';
    EXCEPTION WHEN unique_violation THEN
        NULL;
    END;

    -- A complete-looking but non-empty lease is not queueable. Use a terminal
    -- ops row solely as a valid foreign-key target, then remove the test lease
    -- before the valid transition.
    INSERT INTO ops.processing_run (
        run_type, status, finished_at
    ) VALUES (
        'analysis', 'succeeded', clock_timestamp()
    ) RETURNING processing_run_pk INTO v_dummy_processing_run_pk;
    UPDATE workspace.analysis_run_dispatch
       SET processing_run_pk = v_dummy_processing_run_pk,
           claimed_by = 'stale-test-worker',
           claimed_at = clock_timestamp(),
           heartbeat_at = clock_timestamp(),
           lease_expires_at = clock_timestamp() + interval '120 seconds'
     WHERE analysis_run_pk = v_run_id;
    BEGIN
        UPDATE workspace.analysis_run
           SET status = 'queued'
         WHERE analysis_run_pk = v_run_id;
        RAISE EXCEPTION 'queued transition with an occupied lease was accepted';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;
    UPDATE workspace.analysis_run_dispatch
       SET processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL
     WHERE analysis_run_pk = v_run_id;

    UPDATE workspace.analysis_run
       SET status = 'queued'
     WHERE analysis_run_pk = v_run_id;

    -- The invariant must remain true after queue publication as well. A direct
    -- lease mutation is serialized and rejected by the deferred dispatch
    -- constraint unless the same transaction establishes a valid running
    -- fence.
    BEGIN
        UPDATE workspace.analysis_run_dispatch
           SET processing_run_pk = v_dummy_processing_run_pk,
               claimed_by = 'rogue-test-worker',
               claimed_at = clock_timestamp(),
               heartbeat_at = clock_timestamp(),
               lease_expires_at = clock_timestamp() + interval '120 seconds'
         WHERE analysis_run_pk = v_run_id;
        SET CONSTRAINTS workspace.trg_workspace_enforce_dispatch_fence IMMEDIATE;
        RAISE EXCEPTION 'queued dispatch accepted a post-publication lease';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;

    -- A direct queued->running state edit has no matching processing token and
    -- lease, and must be rejected before it becomes worker-visible.
    BEGIN
        UPDATE workspace.analysis_run
           SET status = 'running'
         WHERE analysis_run_pk = v_run_id;
        RAISE EXCEPTION 'unfenced running transition was accepted';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;

    -- Likewise, a rogue live ops attempt cannot commit beside a queued run.
    -- Force the deferred constraint inside this subtransaction so its expected
    -- failure rolls back only the synthetic row.
    BEGIN
        INSERT INTO ops.processing_run (
            source_analysis_run_id, run_type, status, started_at
        ) VALUES (
            v_run_id, 'analysis', 'running', clock_timestamp()
        );
        SET CONSTRAINTS ops.trg_ops_enforce_live_processing_attempt_fence IMMEDIATE;
        RAISE EXCEPTION 'unfenced live processing attempt was accepted';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;
    SELECT count(*) INTO v_count
      FROM ops.processing_run
     WHERE source_analysis_run_id = v_run_id
       AND status IN ('queued', 'running');
    IF v_count <> 0 THEN
        RAISE EXCEPTION 'rejected live processing attempt survived';
    END IF;

    -- The exact source tuple remains immutable while queued/running. These
    -- failed statements each run in a subtransaction and must leave the valid
    -- queue item untouched.
    BEGIN
        UPDATE workspace.source_artifact
           SET size_bytes = 5
         WHERE analysis_run_pk = v_run_id
           AND artifact_type = 'source';
        RAISE EXCEPTION 'active source artifact update was accepted';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;
    BEGIN
        DELETE FROM workspace.source_artifact
         WHERE analysis_run_pk = v_run_id
           AND artifact_type = 'source';
        RAISE EXCEPTION 'active source artifact delete was accepted';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;
    BEGIN
        UPDATE workspace.analysis_run_dispatch
           SET source_content_sha256 = repeat('b', 64)
         WHERE analysis_run_pk = v_run_id;
        RAISE EXCEPTION 'active dispatch source update was accepted';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;
    BEGIN
        UPDATE workspace.analysis_run
           SET declared_size_bytes = 5
         WHERE analysis_run_pk = v_run_id;
        RAISE EXCEPTION 'active source size update was accepted';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;

    SELECT count(*) INTO v_count
      FROM workspace.source_artifact
     WHERE analysis_run_pk = v_run_id
       AND artifact_type = 'source'
       AND content_sha256 = repeat('a', 64)
       AND size_bytes = 4;
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'active source artifact changed after rejected mutation';
    END IF;

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
