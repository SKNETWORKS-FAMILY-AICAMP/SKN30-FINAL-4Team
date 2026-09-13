-- ============================================================================
-- Migration 34: v0.2 public result projection and evidence integrity
--
-- The old result_data/purpose_result/etc. columns remain audit/worker input.
-- Only the validated public_* columns below are readable through v0.2 RPCs.
-- ============================================================================

BEGIN;

ALTER TABLE result.axis_result
    ADD COLUMN IF NOT EXISTS public_detail JSONB NULL;

ALTER TABLE result.sim_candidate
    ADD COLUMN IF NOT EXISTS public_metadata JSONB NULL,
    ADD COLUMN IF NOT EXISTS public_axes JSONB NULL;

ALTER TABLE result.analysis_case
    ADD COLUMN IF NOT EXISTS sim_status TEXT NULL,
    ADD COLUMN IF NOT EXISTS sim_reason_code TEXT NULL,
    ADD COLUMN IF NOT EXISTS sim_summary TEXT NULL;

ALTER TABLE result.evidence_snapshot
    ADD COLUMN IF NOT EXISTS logical_code TEXT NULL,
    ADD COLUMN IF NOT EXISTS sim_axis_code TEXT NULL,
    ADD COLUMN IF NOT EXISTS evidence_role TEXT NULL,
    ADD COLUMN IF NOT EXISTS source_identity TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'result.evidence_snapshot'::regclass
           AND conname = 'ck_result_evidence_snapshot_v2_role'
    ) THEN
        ALTER TABLE result.evidence_snapshot
            ADD CONSTRAINT ck_result_evidence_snapshot_v2_role
            CHECK (evidence_role IS NULL OR evidence_role IN ('VALUE', 'LEFT', 'RIGHT'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'result.evidence_snapshot'::regclass
           AND conname = 'ck_result_evidence_snapshot_v2_sim_axis'
    ) THEN
        ALTER TABLE result.evidence_snapshot
            ADD CONSTRAINT ck_result_evidence_snapshot_v2_sim_axis
            CHECK (sim_axis_code IS NULL OR sim_axis_code IN ('purpose', 'target', 'support', 'delivery'));
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_result_evidence_v2_axis_context
    ON result.evidence_snapshot (
        analysis_case_pk, axis_result_pk, logical_code, evidence_role, side
    )
 WHERE usage_scope = 'RESULT' AND sim_candidate_pk IS NULL;

CREATE INDEX IF NOT EXISTS ix_result_evidence_v2_sim_context
    ON result.evidence_snapshot (
        analysis_case_pk, sim_candidate_pk, sim_axis_code, logical_code,
        evidence_role, side
    )
 WHERE usage_scope = 'RESULT' AND sim_candidate_pk IS NOT NULL;

-- --------------------------------------------------------------------------
-- Strict JSON helpers.  They remain private; their purpose is to make a bad
-- trusted-worker payload transactionally fail rather than become a browser
-- projection which accidentally contains raw data or dangling UUIDs.
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION result.jsonb_has_exact_keys_v2(
    p_value JSONB,
    p_keys TEXT[]
)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT jsonb_typeof(p_value) = 'object'
       AND p_value ?& p_keys
       AND p_value - p_keys = '{}'::jsonb;
$$;

CREATE OR REPLACE FUNCTION result.assert_nullable_text_v2(
    p_value JSONB,
    p_label TEXT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_value IS NULL
       OR jsonb_typeof(p_value) NOT IN ('string', 'null') THEN
        RAISE EXCEPTION 'INVALID_PUBLIC_RESULT: % must be string or null', p_label
            USING ERRCODE = '22023';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION result.assert_text_array_v2(
    p_value JSONB,
    p_label TEXT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF jsonb_typeof(p_value) <> 'array'
       OR EXISTS (
           SELECT 1
             FROM jsonb_array_elements(p_value) AS item(value)
            WHERE jsonb_typeof(item.value) <> 'string'
       ) THEN
        RAISE EXCEPTION 'INVALID_PUBLIC_RESULT: % must be string array', p_label
            USING ERRCODE = '22023';
    END IF;
END;
$$;

-- This checks both UUID syntax/duplication and relational context.  The same
-- evidence UUID can intentionally occur in an outer summary and a nested
-- value list, but an individual public list may never repeat it.
CREATE OR REPLACE FUNCTION result.assert_public_evidence_list_v2(
    p_evidence_ids JSONB,
    p_analysis_case_id UUID,
    p_axis_result_id UUID,
    p_sim_candidate_id UUID,
    p_sim_axis_code TEXT,
    p_logical_code TEXT,
    p_roles TEXT[],
    p_sides TEXT[],
    p_label TEXT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, result
AS $$
DECLARE
    v_count INTEGER;
    v_distinct_count INTEGER;
BEGIN
    IF jsonb_typeof(p_evidence_ids) <> 'array'
       OR EXISTS (
           SELECT 1
             FROM jsonb_array_elements(p_evidence_ids) AS item(value)
            WHERE jsonb_typeof(item.value) <> 'string'
               OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       ) THEN
        RAISE EXCEPTION 'INVALID_PUBLIC_EVIDENCE_IDS: %', p_label
            USING ERRCODE = '22023';
    END IF;

    SELECT count(*), count(DISTINCT (item.value #>> '{}')::uuid)
      INTO v_count, v_distinct_count
      FROM jsonb_array_elements(p_evidence_ids) AS item(value);
    IF v_count <> v_distinct_count THEN
        RAISE EXCEPTION 'DUPLICATE_PUBLIC_EVIDENCE_ID: %', p_label
            USING ERRCODE = '23505';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(p_evidence_ids) AS item(value)
         WHERE NOT EXISTS (
             SELECT 1
               FROM result.evidence_snapshot AS evidence
              WHERE evidence.evidence_snapshot_pk = (item.value #>> '{}')::uuid
                AND evidence.analysis_case_pk = p_analysis_case_id
                AND evidence.usage_scope = 'RESULT'
                AND evidence.axis_result_pk IS NOT DISTINCT FROM p_axis_result_id
                AND evidence.sim_candidate_pk IS NOT DISTINCT FROM p_sim_candidate_id
                AND evidence.sim_axis_code IS NOT DISTINCT FROM p_sim_axis_code
                AND evidence.logical_code IS NOT DISTINCT FROM p_logical_code
                AND evidence.evidence_role = ANY(p_roles)
                AND evidence.side = ANY(p_sides)
                AND (
                    (p_sim_candidate_id IS NOT NULL AND evidence.axis_type = 'SIM')
                    OR (
                        p_axis_result_id IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                              FROM result.axis_result AS axis
                             WHERE axis.axis_result_pk = p_axis_result_id
                               AND axis.analysis_case_pk = p_analysis_case_id
                               AND axis.axis_type = evidence.axis_type
                        )
                    )
                )
         )
    ) THEN
        RAISE EXCEPTION 'DANGLING_OR_CROSS_CONTEXT_PUBLIC_EVIDENCE: %', p_label
            USING ERRCODE = '23514';
    END IF;
END;
$$;

-- A v2 evidence row carries enough relational context to be checked directly
-- on the base table as well as through a public-detail update.  Legacy rows
-- have NULL evidence_role and intentionally bypass this additive guard.
CREATE OR REPLACE FUNCTION result.validate_evidence_snapshot_context_v2()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, result
AS $$
BEGIN
    IF NEW.evidence_role IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.usage_scope <> 'RESULT'
       OR btrim(COALESCE(NEW.logical_code, '')) = ''
       OR btrim(COALESCE(NEW.source_identity, '')) = '' THEN
        RAISE EXCEPTION 'INVALID_V2_EVIDENCE_CONTEXT' USING ERRCODE = '23514';
    END IF;
    IF NEW.axis_type IN ('CPL', 'FIT') THEN
        IF NEW.axis_result_pk IS NULL OR NEW.sim_candidate_pk IS NOT NULL
           OR NEW.sim_axis_code IS NOT NULL
           OR NOT EXISTS (
               SELECT 1 FROM result.axis_result AS axis
                WHERE axis.axis_result_pk = NEW.axis_result_pk
                  AND axis.analysis_case_pk = NEW.analysis_case_pk
                  AND axis.axis_type = NEW.axis_type
                  AND axis.axis_code = NEW.logical_code
           )
           OR (NEW.axis_type = 'CPL'
               AND (NEW.evidence_role <> 'VALUE' OR NEW.side <> 'REQUEST'))
           OR (NEW.axis_type = 'FIT'
               AND NOT (
                   (NEW.evidence_role = 'LEFT' AND NEW.side = 'REQUEST')
                   OR (NEW.evidence_role = 'RIGHT' AND NEW.side = 'REQUEST')
               )) THEN
            RAISE EXCEPTION 'INVALID_V2_AXIS_EVIDENCE_CONTEXT' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.axis_type = 'SIM' THEN
        IF NEW.axis_result_pk IS NOT NULL OR NEW.sim_candidate_pk IS NULL
           OR NEW.sim_axis_code NOT IN ('purpose','target','support','delivery')
           OR NOT EXISTS (
               SELECT 1 FROM result.sim_candidate AS candidate
                WHERE candidate.sim_candidate_pk = NEW.sim_candidate_pk
                  AND candidate.analysis_case_pk = NEW.analysis_case_pk
           )
           OR NOT (
               (NEW.evidence_role = 'LEFT' AND NEW.side = 'REQUEST')
               OR (NEW.evidence_role = 'RIGHT' AND NEW.side = 'EXISTING')
           ) THEN
            RAISE EXCEPTION 'INVALID_V2_SIM_EVIDENCE_CONTEXT' USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'INVALID_V2_EVIDENCE_AXIS_TYPE' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_result_evidence_snapshot_context_v2 ON result.evidence_snapshot;
CREATE TRIGGER trg_result_evidence_snapshot_context_v2
BEFORE INSERT OR UPDATE OF analysis_case_pk, axis_type, axis_result_pk,
    sim_candidate_pk, side, usage_scope, logical_code, sim_axis_code,
    evidence_role, source_identity
ON result.evidence_snapshot
FOR EACH ROW EXECUTE FUNCTION result.validate_evidence_snapshot_context_v2();

CREATE OR REPLACE FUNCTION result.assert_public_detail_shape_v2(
    p_axis_type TEXT,
    p_status TEXT,
    p_detail JSONB
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, result
AS $$
DECLARE
    v_value JSONB;
BEGIN
    IF p_axis_type = 'CPL' THEN
        IF p_status NOT IN ('confirmed', 'needs_confirmation', 'no_content', 'not_applicable')
           OR NOT result.jsonb_has_exact_keys_v2(
               p_detail,
               ARRAY['reason_code', 'reason', 'values', 'source_fields', 'evidence_ids']
           ) THEN
            RAISE EXCEPTION 'INVALID_CPL_PUBLIC_DETAIL' USING ERRCODE = '22023';
        END IF;
        PERFORM result.assert_nullable_text_v2(p_detail->'reason_code', 'cpl.reason_code');
        PERFORM result.assert_nullable_text_v2(p_detail->'reason', 'cpl.reason');
        PERFORM result.assert_text_array_v2(p_detail->'source_fields', 'cpl.source_fields');
        IF jsonb_typeof(p_detail->'values') <> 'array' THEN
            RAISE EXCEPTION 'INVALID_CPL_PUBLIC_VALUES' USING ERRCODE = '22023';
        END IF;
        FOR v_value IN SELECT value FROM jsonb_array_elements(p_detail->'values')
        LOOP
            IF NOT result.jsonb_has_exact_keys_v2(
                    v_value, ARRAY['label', 'value', 'evidence_ids']
               )
               OR jsonb_typeof(v_value->'label') <> 'string'
               OR jsonb_typeof(v_value->'value') <> 'string'
               OR jsonb_typeof(v_value->'evidence_ids') <> 'array' THEN
                RAISE EXCEPTION 'INVALID_CPL_PUBLIC_VALUE' USING ERRCODE = '22023';
            END IF;
        END LOOP;
    ELSIF p_axis_type = 'FIT' THEN
        IF p_status NOT IN ('FIT', 'NEEDS_REVIEW', 'CONFLICT', 'INSUFFICIENT', 'NOT_APPLICABLE')
           OR NOT result.jsonb_has_exact_keys_v2(
               p_detail,
               ARRAY['comparison_performed', 'reason_code', 'reason', 'left', 'right', 'evidence_ids']
           )
           OR jsonb_typeof(p_detail->'comparison_performed') <> 'boolean' THEN
            RAISE EXCEPTION 'INVALID_FIT_PUBLIC_DETAIL' USING ERRCODE = '22023';
        END IF;
        PERFORM result.assert_nullable_text_v2(p_detail->'reason_code', 'fit.reason_code');
        PERFORM result.assert_nullable_text_v2(p_detail->'reason', 'fit.reason');
        IF NOT result.jsonb_has_exact_keys_v2(p_detail->'left', ARRAY['value_summary', 'evidence_ids'])
           OR NOT result.jsonb_has_exact_keys_v2(p_detail->'right', ARRAY['value_summary', 'evidence_ids'])
           OR jsonb_typeof(p_detail#>'{left,value_summary}') <> 'string'
           OR jsonb_typeof(p_detail#>'{right,value_summary}') <> 'string'
           OR jsonb_typeof(p_detail#>'{left,evidence_ids}') <> 'array'
           OR jsonb_typeof(p_detail#>'{right,evidence_ids}') <> 'array'
           OR jsonb_typeof(p_detail->'evidence_ids') <> 'array' THEN
            RAISE EXCEPTION 'INVALID_FIT_PUBLIC_SIDE' USING ERRCODE = '22023';
        END IF;
        IF (p_detail->>'comparison_performed')::boolean = FALSE
           AND (jsonb_array_length(p_detail#>'{left,evidence_ids}') <> 0
                OR jsonb_array_length(p_detail#>'{right,evidence_ids}') <> 0
                OR jsonb_array_length(p_detail->'evidence_ids') <> 0) THEN
            RAISE EXCEPTION 'UNPERFORMED_FIT_HAS_EVIDENCE' USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'PUBLIC_DETAIL_UNSUPPORTED_AXIS_TYPE' USING ERRCODE = '22023';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION result.validate_axis_public_detail_v2()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, result
AS $$
DECLARE
    v_value JSONB;
BEGIN
    IF NEW.public_detail IS NULL THEN
        RETURN NEW;
    END IF;
    PERFORM result.assert_public_detail_shape_v2(NEW.axis_type, NEW.status, NEW.public_detail);

    IF NEW.axis_type = 'CPL' THEN
        PERFORM result.assert_public_evidence_list_v2(
            NEW.public_detail->'evidence_ids', NEW.analysis_case_pk,
            NEW.axis_result_pk, NULL, NULL, NEW.axis_code,
            ARRAY['VALUE'], ARRAY['REQUEST'], 'cpl.evidence_ids'
        );
        FOR v_value IN SELECT value FROM jsonb_array_elements(NEW.public_detail->'values')
        LOOP
            PERFORM result.assert_public_evidence_list_v2(
                v_value->'evidence_ids', NEW.analysis_case_pk,
                NEW.axis_result_pk, NULL, NULL, NEW.axis_code,
                ARRAY['VALUE'], ARRAY['REQUEST'], 'cpl.values.evidence_ids'
            );
        END LOOP;
    ELSE
        PERFORM result.assert_public_evidence_list_v2(
            NEW.public_detail->'evidence_ids', NEW.analysis_case_pk,
            NEW.axis_result_pk, NULL, NULL, NEW.axis_code,
            ARRAY['LEFT', 'RIGHT'], ARRAY['REQUEST'], 'fit.evidence_ids'
        );
        PERFORM result.assert_public_evidence_list_v2(
            NEW.public_detail#>'{left,evidence_ids}', NEW.analysis_case_pk,
            NEW.axis_result_pk, NULL, NULL, NEW.axis_code,
            ARRAY['LEFT'], ARRAY['REQUEST'], 'fit.left.evidence_ids'
        );
        PERFORM result.assert_public_evidence_list_v2(
            NEW.public_detail#>'{right,evidence_ids}', NEW.analysis_case_pk,
            NEW.axis_result_pk, NULL, NULL, NEW.axis_code,
            ARRAY['RIGHT'], ARRAY['REQUEST'], 'fit.right.evidence_ids'
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_result_axis_public_detail_v2 ON result.axis_result;
CREATE TRIGGER trg_result_axis_public_detail_v2
BEFORE INSERT OR UPDATE OF public_detail, status, axis_type, axis_code
ON result.axis_result
FOR EACH ROW EXECUTE FUNCTION result.validate_axis_public_detail_v2();

CREATE OR REPLACE FUNCTION result.validate_sim_candidate_public_v2()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, result
AS $$
DECLARE
    v_axis_name TEXT;
    v_axis JSONB;
    v_key TEXT;
BEGIN
    IF (NEW.public_metadata IS NULL) <> (NEW.public_axes IS NULL) THEN
        RAISE EXCEPTION 'SIM_PUBLIC_METADATA_AND_AXES_MUST_APPEAR_TOGETHER'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.public_metadata IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.status NOT IN ('similar', 'partial', 'different', 'insufficient')
       OR NOT result.jsonb_has_exact_keys_v2(
           NEW.public_metadata,
           ARRAY['title','support_field','apply_period','ministry','executing_agency',
                 'registered_at','notice_status','source_url']
       )
       OR NOT result.jsonb_has_exact_keys_v2(
           NEW.public_axes, ARRAY['purpose','target','support','delivery']
       ) THEN
        RAISE EXCEPTION 'INVALID_SIM_PUBLIC_PROJECTION' USING ERRCODE = '22023';
    END IF;
    FOREACH v_key IN ARRAY ARRAY[
        'title','support_field','apply_period','ministry','executing_agency',
        'registered_at','notice_status','source_url'
    ] LOOP
        PERFORM result.assert_nullable_text_v2(NEW.public_metadata->v_key, 'sim.metadata.' || v_key);
    END LOOP;

    FOREACH v_axis_name IN ARRAY ARRAY['purpose','target','support','delivery']
    LOOP
        v_axis := NEW.public_axes->v_axis_name;
        IF NOT result.jsonb_has_exact_keys_v2(
                v_axis,
                ARRAY['code','status','summary','reason_code','reason','common_points',
                      'differences','request_evidence_ids','existing_evidence_ids']
           )
           OR jsonb_typeof(v_axis->'code') <> 'string'
           OR jsonb_typeof(v_axis->'status') <> 'string'
           OR v_axis->>'status' NOT IN ('similar', 'partial', 'different', 'insufficient')
           OR jsonb_typeof(v_axis->'summary') <> 'string' THEN
            RAISE EXCEPTION 'INVALID_SIM_PUBLIC_AXIS: %', v_axis_name USING ERRCODE = '22023';
        END IF;
        PERFORM result.assert_nullable_text_v2(v_axis->'reason_code', 'sim.reason_code');
        PERFORM result.assert_nullable_text_v2(v_axis->'reason', 'sim.reason');
        PERFORM result.assert_text_array_v2(v_axis->'common_points', 'sim.common_points');
        PERFORM result.assert_text_array_v2(v_axis->'differences', 'sim.differences');
        PERFORM result.assert_public_evidence_list_v2(
            v_axis->'request_evidence_ids', NEW.analysis_case_pk,
            NULL, NEW.sim_candidate_pk, v_axis_name, v_axis->>'code',
            ARRAY['LEFT'], ARRAY['REQUEST'], 'sim.request_evidence_ids'
        );
        PERFORM result.assert_public_evidence_list_v2(
            v_axis->'existing_evidence_ids', NEW.analysis_case_pk,
            NULL, NEW.sim_candidate_pk, v_axis_name, v_axis->>'code',
            ARRAY['RIGHT'], ARRAY['EXISTING'], 'sim.existing_evidence_ids'
        );
    END LOOP;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_result_sim_candidate_public_v2 ON result.sim_candidate;
CREATE TRIGGER trg_result_sim_candidate_public_v2
BEFORE INSERT OR UPDATE OF public_metadata, public_axes, status
ON result.sim_candidate
FOR EACH ROW EXECUTE FUNCTION result.validate_sim_candidate_public_v2();

CREATE OR REPLACE FUNCTION result.validate_analysis_case_sim_public_v2()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.sim_status IS NULL
       AND NEW.sim_reason_code IS NULL
       AND NEW.sim_summary IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.sim_status NOT IN ('completed', 'skipped')
       OR NEW.sim_summary IS NULL
       OR (NEW.sim_status = 'skipped'
           AND NEW.sim_reason_code NOT IN ('RETRIEVAL_INPUT_MISSING', 'KB_EMPTY'))
       OR (NEW.sim_status = 'completed' AND NEW.sim_reason_code IS NOT NULL) THEN
        RAISE EXCEPTION 'INVALID_SIM_SECTION' USING ERRCODE = '22023';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_result_analysis_case_sim_public_v2 ON result.analysis_case;
CREATE TRIGGER trg_result_analysis_case_sim_public_v2
BEFORE INSERT OR UPDATE OF sim_status, sim_reason_code, sim_summary
ON result.analysis_case
FOR EACH ROW EXECUTE FUNCTION result.validate_analysis_case_sim_public_v2();

-- --------------------------------------------------------------------------
-- Fenced writer v2.  It validates the canonical worker payload, delegates to
-- the untouched migration-26 fenced+ML writer in this same transaction, then
-- replaces only RESULT evidence/projections.  Conversation evidence and
-- references are restored with their UUIDs if a legacy delegate deletion
-- touched them, so a result replay never erases chat provenance.
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION workspace.persist_analysis_result_core_v2(
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
    v_user_id UUID;
    v_case_pk UUID;
    v_old_case_pk UUID;
    v_axis JSONB;
    v_candidate JSONB;
    v_evidence JSONB;
    v_axis_pk UUID;
    v_candidate_pk UUID;
    v_previous_session_id UUID;
    v_previous_session_status TEXT;
    v_previous_session_expires_at TIMESTAMPTZ;
    v_previous_session_closed_at TIMESTAMPTZ;
    v_previous_session_reason TEXT;
    v_preserved_evidence JSONB := '[]'::jsonb;
    v_preserved_references JSONB := '[]'::jsonb;
    v_item JSONB;
    v_delegate_result JSONB;
BEGIN
    IF jsonb_typeof(p_result) <> 'object'
       OR NOT p_result ?& ARRAY['contract_version','program_name','sim','axes','candidates','evidences']
       OR p_result - ARRAY['contract_version','program_name','sim','axes','candidates','evidences','ml'] <> '{}'::jsonb
       OR p_result->>'contract_version' <> 'analysis_result/v0.2'
       OR jsonb_typeof(p_result->'program_name') NOT IN ('string','null')
       OR jsonb_typeof(p_result->'axes') <> 'array'
       OR jsonb_typeof(p_result->'candidates') <> 'array'
       OR jsonb_typeof(p_result->'evidences') <> 'array'
       OR NOT result.jsonb_has_exact_keys_v2(p_result->'sim', ARRAY['status','reason_code','summary']) THEN
        RAISE EXCEPTION 'INVALID_ANALYSIS_RESULT_V2' USING ERRCODE = '22023';
    END IF;

    IF p_result#>>'{sim,status}' NOT IN ('completed', 'skipped')
       OR jsonb_typeof(p_result#>'{sim,reason_code}') NOT IN ('string','null')
       OR jsonb_typeof(p_result#>'{sim,summary}') <> 'string' THEN
        RAISE EXCEPTION 'INVALID_SIM_SECTION' USING ERRCODE = '22023';
    END IF;

    FOR v_axis IN SELECT value FROM jsonb_array_elements(p_result->'axes')
    LOOP
        IF NOT result.jsonb_has_exact_keys_v2(
                v_axis, ARRAY['axis_type','axis_code','status','summary_text','result_data','public_detail']
           )
           OR v_axis->>'axis_type' NOT IN ('CPL','FIT')
           OR btrim(COALESCE(v_axis->>'axis_code','')) = ''
           OR jsonb_typeof(v_axis->'summary_text') NOT IN ('string','null')
           OR v_axis->'result_data' IS NULL
           OR jsonb_typeof(v_axis->'public_detail') <> 'object' THEN
            RAISE EXCEPTION 'INVALID_ANALYSIS_AXIS_V2' USING ERRCODE = '22023';
        END IF;
        PERFORM result.assert_public_detail_shape_v2(
            v_axis->>'axis_type', v_axis->>'status', v_axis->'public_detail'
        );
    END LOOP;

    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(p_result->'axes') AS item(value)
         GROUP BY item.value->>'axis_type', item.value->>'axis_code'
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'DUPLICATE_ANALYSIS_AXIS_V2' USING ERRCODE = '23505';
    END IF;

    FOR v_candidate IN SELECT value FROM jsonb_array_elements(p_result->'candidates')
    LOOP
        IF NOT result.jsonb_has_exact_keys_v2(
                v_candidate,
                ARRAY['source_profile_id','profile_version_pk','rank','similarity_score',
                      'priority_score','status','summary_text','comparable_axes','metadata',
                      'purpose_result','target_result','support_result','delivery_result','public_axes']
           )
           OR btrim(COALESCE(v_candidate->>'source_profile_id','')) = ''
           OR COALESCE(v_candidate->>'profile_version_pk','') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR COALESCE(v_candidate->>'rank','') !~ '^[1-9][0-9]*$'
           OR v_candidate->>'status' NOT IN ('similar','partial','different','insufficient')
           OR jsonb_typeof(v_candidate->'summary_text') NOT IN ('string','null')
           OR jsonb_typeof(v_candidate->'comparable_axes') <> 'array'
           OR jsonb_typeof(v_candidate->'metadata') <> 'object'
           OR jsonb_typeof(v_candidate->'public_axes') <> 'object' THEN
            RAISE EXCEPTION 'INVALID_SIM_CANDIDATE_V2' USING ERRCODE = '22023';
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_result->'candidates') AS item(value)
         GROUP BY (item.value->>'rank')::integer HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'DUPLICATE_SIM_CANDIDATE_RANK_V2' USING ERRCODE = '23505';
    END IF;

    FOR v_evidence IN SELECT value FROM jsonb_array_elements(p_result->'evidences')
    LOOP
        IF NOT result.jsonb_has_exact_keys_v2(
                v_evidence,
                ARRAY['evidence_id','logical_code','axis_type','sim_axis','role','side',
                      'field_name','raw_value','source_sha256','candidate_source_profile_id','source_identity',
                      'candidate_pack_block_id','common_ir_document_id','common_ir_block_id',
                      'common_ir_cell_id','common_ir_occurrence_ids']
           )
           OR COALESCE(v_evidence->>'evidence_id','') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR btrim(COALESCE(v_evidence->>'logical_code','')) = ''
           OR v_evidence->>'axis_type' NOT IN ('CPL','FIT','SIM')
           OR v_evidence->>'role' NOT IN ('VALUE','LEFT','RIGHT')
           OR v_evidence->>'side' NOT IN ('REQUEST','EXISTING')
           OR jsonb_typeof(v_evidence->'raw_value') <> 'string'
           OR jsonb_typeof(v_evidence->'source_identity') <> 'string'
           OR jsonb_typeof(v_evidence->'common_ir_occurrence_ids') <> 'array'
           OR (v_evidence->>'axis_type' = 'SIM'
               AND (v_evidence->>'sim_axis' NOT IN ('purpose','target','support','delivery')
                    OR btrim(COALESCE(v_evidence->>'candidate_source_profile_id','')) = ''))
           OR (v_evidence->>'axis_type' <> 'SIM'
               AND (v_evidence->'sim_axis' <> 'null'::jsonb
                    OR v_evidence->'candidate_source_profile_id' <> 'null'::jsonb)) THEN
            RAISE EXCEPTION 'INVALID_EVIDENCE_V2' USING ERRCODE = '22023';
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_result->'evidences') AS item(value)
         GROUP BY (item.value->>'evidence_id')::uuid HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'DUPLICATE_EVIDENCE_ID_V2' USING ERRCODE = '23505';
    END IF;

    -- Obtain the user before any result/session lock, then take the shared
    -- owner lock and re-read under the delegate's fenced transaction.
    SELECT user_id
      INTO v_user_id
      FROM workspace.analysis_run
     WHERE analysis_run_pk = p_analysis_run_pk;
    IF v_user_id IS NULL THEN
        RETURN NULL;
    END IF;
    PERFORM workspace.lock_analysis_lifecycle_user_v2(v_user_id);

    -- Free a stale active-session uniqueness slot while holding the same
    -- owner lock used by upload/close.  The legacy delegate below may create
    -- the ready session for this run, so this transition must precede it.
    UPDATE result.analysis_session
       SET status = 'expired', updated_at = clock_timestamp()
     WHERE user_id = v_user_id
       AND status = 'active'
       AND expires_at <= clock_timestamp();

    SELECT analysis_case_pk
      INTO v_old_case_pk
      FROM result.analysis_case
     WHERE source_analysis_run_id = p_analysis_run_pk;

    IF v_old_case_pk IS NOT NULL THEN
        SELECT analysis_session_pk, status, expires_at, closed_at, close_reason
          INTO v_previous_session_id, v_previous_session_status,
               v_previous_session_expires_at, v_previous_session_closed_at,
               v_previous_session_reason
          FROM result.analysis_session
         WHERE analysis_case_pk = v_old_case_pk
         FOR UPDATE;

        -- If a replay enters the legacy writer, it deletes result evidence and
        -- cascades references.  Preserve conversation-scoped evidence (and a
        -- previously referenced result snapshot) by UUID for restoration.
        SELECT COALESCE(jsonb_agg(to_jsonb(evidence)), '[]'::jsonb)
          INTO v_preserved_evidence
          FROM result.evidence_snapshot AS evidence
         WHERE evidence.analysis_case_pk = v_old_case_pk
           AND (
               evidence.usage_scope = 'CONVERSATION'
               OR EXISTS (
                   SELECT 1 FROM result.conversation_reference AS reference
                    WHERE reference.evidence_snapshot_pk = evidence.evidence_snapshot_pk
               )
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM jsonb_array_elements(p_result->'evidences') AS incoming(value)
                WHERE (incoming.value->>'evidence_id')::uuid = evidence.evidence_snapshot_pk
           );

        SELECT COALESCE(jsonb_agg(
            to_jsonb(reference)
            || jsonb_build_object(
                'v2_axis_type', axis.axis_type,
                'v2_axis_code', axis.axis_code,
                'v2_candidate_profile_version_pk', candidate.existing_profile_version_pk
            )
        ), '[]'::jsonb)
          INTO v_preserved_references
          FROM result.conversation_reference AS reference
          LEFT JOIN result.axis_result AS axis
            ON axis.axis_result_pk = reference.axis_result_pk
          LEFT JOIN result.sim_candidate AS candidate
            ON candidate.sim_candidate_pk = reference.sim_candidate_pk
          JOIN result.conversation_message AS message
            ON message.message_pk = reference.message_pk
          JOIN result.analysis_session AS session
            ON session.analysis_session_pk = message.analysis_session_pk
         WHERE session.analysis_case_pk = v_old_case_pk;
    END IF;

    -- Migration 26 validates/persists ML and applies the original fence in
    -- this transaction.  Any validation below rolls its terminal transition
    -- back too, as required by the v0.2 atomic boundary.
    -- ML is optional in the v0.2 worker payload.  The unchanged migration-26
    -- delegate still requires it, so absent ML is represented by its existing
    -- safe public unavailable object before delegation.
    v_delegate_result := CASE
        WHEN p_result ? 'ml' THEN p_result
        ELSE p_result || jsonb_build_object('ml', jsonb_build_object(
            'model_1', jsonb_build_object('status','UNAVAILABLE','support_type',NULL,'message','ML 모델이 실행되지 않았습니다.','reason_code','ML_RUNTIME_MISSING'),
            'model_2', jsonb_build_object('status','UNAVAILABLE','predicted_amount_won',NULL,'message','ML 모델이 실행되지 않았습니다.','reason_code','ML_RUNTIME_MISSING'),
            'model_3', jsonb_build_object('status','UNAVAILABLE','anomaly_level',NULL,'cause_axes','[]'::jsonb,'message','ML 모델이 실행되지 않았습니다.','reason_code','ML_RUNTIME_MISSING')
        ))
    END;
    v_case_pk := workspace.persist_analysis_result_core(
        p_analysis_run_pk, p_processing_run_pk, v_delegate_result
    );
    IF v_case_pk IS NULL THEN
        RETURN NULL;
    END IF;

    -- A completed-result replay may not revive an explicitly closed/expired
    -- session.  The legacy writer's upsert is retained for compatibility, and
    -- this v2 correction is protected by the same owner advisory lock.
    IF v_previous_session_id IS NOT NULL
       AND v_previous_session_status IN ('closed', 'expired') THEN
        UPDATE result.analysis_session
           SET status = v_previous_session_status,
               expires_at = v_previous_session_expires_at,
               closed_at = v_previous_session_closed_at,
               close_reason = v_previous_session_reason
         WHERE analysis_session_pk = v_previous_session_id;
    END IF;

    -- Restore conversation-only evidence before public-detail trigger checks.
    INSERT INTO result.evidence_snapshot
    SELECT (
        jsonb_populate_record(
            NULL::result.evidence_snapshot,
            (
                preserved.value
                - 'axis_result_pk' - 'sim_candidate_pk'
                || jsonb_build_object(
                    'analysis_case_pk', v_case_pk,
                    'axis_result_pk', NULL,
                    'sim_candidate_pk', NULL,
                    'usage_scope', 'CONVERSATION',
                    'logical_code', NULL,
                    'sim_axis_code', NULL,
                    'evidence_role', NULL
                )
            )
        )
    ).*
      FROM jsonb_array_elements(v_preserved_evidence) AS preserved(value)
     WHERE TRUE
    ON CONFLICT (evidence_snapshot_pk) DO NOTHING;

    DELETE FROM result.evidence_snapshot
     WHERE analysis_case_pk = v_case_pk
       AND usage_scope = 'RESULT';

    FOR v_evidence IN SELECT value FROM jsonb_array_elements(p_result->'evidences')
    LOOP
        v_axis_pk := NULL;
        v_candidate_pk := NULL;
        IF v_evidence->>'axis_type' IN ('CPL','FIT') THEN
            SELECT axis_result_pk
              INTO v_axis_pk
              FROM result.axis_result
             WHERE analysis_case_pk = v_case_pk
               AND axis_type = v_evidence->>'axis_type'
               AND axis_code = v_evidence->>'logical_code';
            IF v_axis_pk IS NULL
               OR (v_evidence->>'axis_type' = 'CPL'
                   AND (v_evidence->>'role' <> 'VALUE' OR v_evidence->>'side' <> 'REQUEST'))
               OR (v_evidence->>'axis_type' = 'FIT'
                   AND NOT (
                       (v_evidence->>'role' = 'LEFT' AND v_evidence->>'side' = 'REQUEST')
                       OR (v_evidence->>'role' = 'RIGHT' AND v_evidence->>'side' = 'REQUEST')
                   )) THEN
                RAISE EXCEPTION 'INVALID_AXIS_EVIDENCE_CONTEXT_V2' USING ERRCODE = '23514';
            END IF;
        ELSE
            SELECT candidate.sim_candidate_pk
              INTO v_candidate_pk
              FROM result.sim_candidate AS candidate
              JOIN kb.profile_version AS profile
                ON profile.profile_version_pk = candidate.existing_profile_version_pk
              JOIN kb.source_version AS source
                ON source.source_version_pk = profile.source_version_pk
              JOIN kb.source_profile AS source_profile
                ON source_profile.source_profile_pk = source.source_profile_pk
             WHERE candidate.analysis_case_pk = v_case_pk
               AND source_profile.source_profile_id = v_evidence->>'candidate_source_profile_id';
            IF v_candidate_pk IS NULL
               OR NOT (
                   (v_evidence->>'role' = 'LEFT' AND v_evidence->>'side' = 'REQUEST')
                   OR (v_evidence->>'role' = 'RIGHT' AND v_evidence->>'side' = 'EXISTING')
               ) THEN
                RAISE EXCEPTION 'INVALID_SIM_EVIDENCE_CONTEXT_V2' USING ERRCODE = '23514';
            END IF;
        END IF;

        INSERT INTO result.evidence_snapshot (
            evidence_snapshot_pk, analysis_case_pk, axis_type, axis_result_pk,
            sim_candidate_pk, side, usage_scope, field_name, raw_value,
            source_sha256, candidate_pack_block_id, common_ir_document_id,
            common_ir_block_id, common_ir_cell_id, common_ir_occurrence_ids,
            logical_code, sim_axis_code, evidence_role, source_identity
        ) VALUES (
            (v_evidence->>'evidence_id')::uuid, v_case_pk,
            v_evidence->>'axis_type', v_axis_pk, v_candidate_pk,
            v_evidence->>'side', 'RESULT', NULLIF(v_evidence->>'field_name',''),
            v_evidence->>'raw_value', NULLIF(v_evidence->>'source_sha256',''),
            NULLIF(v_evidence->>'candidate_pack_block_id',''),
            NULLIF(v_evidence->>'common_ir_document_id',''),
            NULLIF(v_evidence->>'common_ir_block_id',''),
            NULLIF(v_evidence->>'common_ir_cell_id',''),
            ARRAY(SELECT jsonb_array_elements_text(v_evidence->'common_ir_occurrence_ids')),
            v_evidence->>'logical_code', NULLIF(v_evidence->>'sim_axis',''),
            v_evidence->>'role', v_evidence->>'source_identity'
        );
    END LOOP;

    FOR v_axis IN SELECT value FROM jsonb_array_elements(p_result->'axes')
    LOOP
        UPDATE result.axis_result
           SET public_detail = v_axis->'public_detail'
         WHERE analysis_case_pk = v_case_pk
           AND axis_type = v_axis->>'axis_type'
           AND axis_code = v_axis->>'axis_code';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'PUBLIC_AXIS_NOT_MATERIALISED_V2' USING ERRCODE = '23514';
        END IF;
    END LOOP;

    FOR v_candidate IN SELECT value FROM jsonb_array_elements(p_result->'candidates')
    LOOP
        UPDATE result.sim_candidate
           SET public_metadata = v_candidate->'metadata',
               public_axes = v_candidate->'public_axes'
         WHERE analysis_case_pk = v_case_pk
           AND existing_profile_version_pk = (v_candidate->>'profile_version_pk')::uuid;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'PUBLIC_SIM_CANDIDATE_NOT_MATERIALISED_V2' USING ERRCODE = '23514';
        END IF;
    END LOOP;

    UPDATE result.analysis_case
       SET sim_status = p_result#>>'{sim,status}',
           sim_reason_code = NULLIF(p_result#>>'{sim,reason_code}',''),
           sim_summary = p_result#>>'{sim,summary}'
     WHERE analysis_case_pk = v_case_pk;

    -- Restore historical chat references after the legacy axis/candidate rows
    -- were rebuilt.  Old links are translated by immutable logical code or
    -- exact profile version; an unavailable target becomes NULL rather than a
    -- cross-case reference.
    FOR v_item IN SELECT value FROM jsonb_array_elements(v_preserved_references)
    LOOP
        INSERT INTO result.conversation_reference (
            conversation_reference_pk, message_pk, axis_result_pk,
            sim_candidate_pk, evidence_snapshot_pk, existing_fact_pk,
            reference_role, created_at
        ) VALUES (
            (v_item->>'conversation_reference_pk')::uuid,
            (v_item->>'message_pk')::uuid,
            (
                SELECT axis.axis_result_pk
                  FROM result.axis_result AS axis
                 WHERE axis.analysis_case_pk = v_case_pk
                   AND axis.axis_type = v_item->>'v2_axis_type'
                   AND axis.axis_code = v_item->>'v2_axis_code'
            ),
            (
                SELECT candidate.sim_candidate_pk
                  FROM result.sim_candidate AS candidate
                 WHERE candidate.analysis_case_pk = v_case_pk
                   AND candidate.existing_profile_version_pk =
                       NULLIF(v_item->>'v2_candidate_profile_version_pk','')::uuid
            ),
            CASE
                WHEN NULLIF(v_item->>'evidence_snapshot_pk','') IS NOT NULL
                 AND EXISTS (
                     SELECT 1 FROM result.evidence_snapshot AS evidence
                      WHERE evidence.evidence_snapshot_pk = (v_item->>'evidence_snapshot_pk')::uuid
                        AND evidence.analysis_case_pk = v_case_pk
                 ) THEN (v_item->>'evidence_snapshot_pk')::uuid
                ELSE NULL
            END,
            NULLIF(v_item->>'existing_fact_pk','')::uuid,
            NULLIF(v_item->>'reference_role',''),
            COALESCE(NULLIF(v_item->>'created_at','')::timestamptz, clock_timestamp())
        ) ON CONFLICT (conversation_reference_pk) DO NOTHING;
    END LOOP;

    RETURN v_case_pk;
END;
$$;

-- --------------------------------------------------------------------------
-- One public projection helper is shared by result reads and v2 chat claim.
-- It exposes no scores, fact ids, raw diagnostics, or source coordinates.
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION api.public_analysis_projection_v2(
    p_analysis_case_id UUID,
    p_user_id UUID
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
        'case', jsonb_build_object(
            'analysis_case_id', analysis_case.analysis_case_pk,
            'program_name', analysis_case.program_name,
            'original_filename', analysis_case.original_filename,
            'completed_at', analysis_case.analysis_completed_at
        ),
        'cpl', jsonb_build_object('items', COALESCE(cpl.items, '[]'::jsonb)),
        'fit', jsonb_build_object('items', COALESCE(fit.items, '[]'::jsonb)),
        'sim', jsonb_build_object(
            'status', COALESCE(analysis_case.sim_status, 'skipped'),
            'reason_code', COALESCE(analysis_case.sim_reason_code, 'LEGACY_PUBLIC_DETAIL_UNAVAILABLE'),
            'summary', COALESCE(analysis_case.sim_summary, '공개 결과 상세를 복원할 수 없습니다.'),
            'candidates', COALESCE(sim.candidates, '[]'::jsonb)
        ),
        'report', COALESCE(report.payload, jsonb_build_object(
            'status', 'generating', 'can_download', false,
            'can_regenerate', false, 'retry_count', 0
        )),
        'session', COALESCE(session.payload, jsonb_build_object(
            'analysis_session_id', NULL, 'is_active', false,
            'can_chat', false, 'expires_at', NULL
        )),
        'ml', analysis_case.ml_result,
        'evidences', COALESCE(evidence.items, '[]'::jsonb)
    )
      INTO v_payload
      FROM result.analysis_case AS analysis_case
      LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object(
              'code', axis.axis_code, 'status', axis.status,
              'summary', axis.summary_text,
              'detail', COALESCE(axis.public_detail, jsonb_build_object(
                  'reason_code', 'LEGACY_PUBLIC_DETAIL_UNAVAILABLE',
                  'reason', '공개 결과 상세를 복원할 수 없습니다.',
                  'values', '[]'::jsonb, 'source_fields', '[]'::jsonb,
                  'evidence_ids', '[]'::jsonb
              ))
          ) ORDER BY axis.ordinal, axis.axis_result_pk) AS items
            FROM result.axis_result AS axis
           WHERE axis.analysis_case_pk = analysis_case.analysis_case_pk
             AND axis.axis_type = 'CPL'
      ) AS cpl ON true
      LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object(
              'code', axis.axis_code, 'status', axis.status,
              'summary', axis.summary_text,
              'detail', COALESCE(axis.public_detail, jsonb_build_object(
                  'comparison_performed', false,
                  'reason_code', 'LEGACY_PUBLIC_DETAIL_UNAVAILABLE',
                  'reason', '공개 결과 상세를 복원할 수 없습니다.',
                  'left', jsonb_build_object('value_summary', '', 'evidence_ids', '[]'::jsonb),
                  'right', jsonb_build_object('value_summary', '', 'evidence_ids', '[]'::jsonb),
                  'evidence_ids', '[]'::jsonb
              ))
          ) ORDER BY axis.ordinal, axis.axis_result_pk) AS items
            FROM result.axis_result AS axis
           WHERE axis.analysis_case_pk = analysis_case.analysis_case_pk
             AND axis.axis_type = 'FIT'
      ) AS fit ON true
      LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object(
              'sim_candidate_id', candidate.sim_candidate_pk,
              'rank', candidate.rank_no,
              'title', candidate.public_metadata->'title',
              'comparison_status', candidate.status,
              'comparison_summary', candidate.summary_text
          ) ORDER BY candidate.rank_no, candidate.sim_candidate_pk) AS candidates
            FROM result.sim_candidate AS candidate
           WHERE candidate.analysis_case_pk = analysis_case.analysis_case_pk
             AND candidate.public_axes IS NOT NULL
      ) AS sim ON true
      LEFT JOIN LATERAL (
          SELECT jsonb_build_object(
              'status', report.status,
              'can_download', report.status = 'ready'
                  AND report.storage_bucket IS NOT NULL
                  AND report.storage_object_key IS NOT NULL,
              'can_regenerate', false,
              'retry_count', report.retry_count
          ) AS payload
            FROM result.report_artifact AS report
           WHERE report.analysis_case_pk = analysis_case.analysis_case_pk
           ORDER BY report.created_at DESC, report.report_artifact_pk DESC
           LIMIT 1
      ) AS report ON true
      LEFT JOIN LATERAL (
          SELECT jsonb_build_object(
              'analysis_session_id', session.analysis_session_pk,
              'is_active', session.status = 'active' AND session.expires_at > clock_timestamp(),
              'can_chat', session.status = 'active' AND session.expires_at > clock_timestamp(),
              'expires_at', session.expires_at
          ) AS payload
            FROM result.analysis_session AS session
           WHERE session.analysis_case_pk = analysis_case.analysis_case_pk
      ) AS session ON true
      LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object(
              'evidence_id', evidence.evidence_snapshot_pk,
              'side', lower(evidence.side),
              'field_name', evidence.field_name,
              'raw_value', evidence.raw_value,
              'excerpt', evidence.context_excerpt
          ) ORDER BY evidence.created_at, evidence.evidence_snapshot_pk) AS items
            FROM result.evidence_snapshot AS evidence
           WHERE evidence.analysis_case_pk = analysis_case.analysis_case_pk
             AND evidence.usage_scope = 'RESULT'
             AND evidence.sim_candidate_pk IS NULL
      ) AS evidence ON true
     WHERE analysis_case.analysis_case_pk = p_analysis_case_id
       AND analysis_case.user_id = p_user_id
       AND analysis_case.retention_expires_at > clock_timestamp();

    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'ANALYSIS_RESULT_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;
    RETURN v_payload;
END;
$$;

CREATE OR REPLACE FUNCTION api.rpc_get_analysis_result_v2(
    p_user_id UUID,
    p_analysis_case_id UUID
)
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, api
AS $$
    SELECT api.public_analysis_projection_v2(p_analysis_case_id, p_user_id);
$$;

CREATE OR REPLACE FUNCTION api.rpc_get_sim_candidate_detail_v2(
    p_user_id UUID,
    p_sim_candidate_id UUID
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
        'sim_candidate_id', candidate.sim_candidate_pk,
        'analysis_case_id', candidate.analysis_case_pk,
        'rank', candidate.rank_no,
        'metadata', candidate.public_metadata,
        'comparison', jsonb_build_object(
            'status', candidate.status,
            'summary', candidate.summary_text,
            'comparable_axes', COALESCE(to_jsonb(candidate.comparable_axes), '[]'::jsonb)
        ),
        'axes', candidate.public_axes,
        'evidences', COALESCE(evidence.items, '[]'::jsonb)
    )
      INTO v_payload
      FROM result.sim_candidate AS candidate
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = candidate.analysis_case_pk
      LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object(
              'evidence_id', evidence.evidence_snapshot_pk,
              'side', lower(evidence.side),
              'field_name', evidence.field_name,
              'raw_value', evidence.raw_value,
              'excerpt', evidence.context_excerpt
          ) ORDER BY evidence.created_at, evidence.evidence_snapshot_pk) AS items
            FROM result.evidence_snapshot AS evidence
           WHERE evidence.analysis_case_pk = candidate.analysis_case_pk
             AND evidence.sim_candidate_pk = candidate.sim_candidate_pk
             AND evidence.usage_scope = 'RESULT'
      ) AS evidence ON true
     WHERE candidate.sim_candidate_pk = p_sim_candidate_id
       AND analysis_case.user_id = p_user_id
       AND analysis_case.retention_expires_at > clock_timestamp()
       AND candidate.public_metadata IS NOT NULL
       AND candidate.public_axes IS NOT NULL;

    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'SIM_CANDIDATE_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;
    RETURN v_payload;
END;
$$;

REVOKE ALL ON FUNCTION result.jsonb_has_exact_keys_v2(JSONB, TEXT[]) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION result.assert_nullable_text_v2(JSONB, TEXT) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION result.assert_text_array_v2(JSONB, TEXT) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION result.assert_public_evidence_list_v2(JSONB, UUID, UUID, UUID, TEXT, TEXT, TEXT[], TEXT[], TEXT) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION result.assert_public_detail_shape_v2(TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION result.validate_evidence_snapshot_context_v2() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION result.validate_axis_public_detail_v2() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION result.validate_sim_candidate_public_v2() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION result.validate_analysis_case_sim_public_v2() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION workspace.persist_analysis_result_core_v2(UUID, UUID, JSONB) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.public_analysis_projection_v2(UUID, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.rpc_get_analysis_result_v2(UUID, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.rpc_get_sim_candidate_detail_v2(UUID, UUID) FROM PUBLIC, anon, authenticated;

GRANT USAGE ON SCHEMA api, workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.persist_analysis_result_core_v2(UUID, UUID, JSONB),
                         api.public_analysis_projection_v2(UUID, UUID),
                         api.rpc_get_analysis_result_v2(UUID, UUID),
                         api.rpc_get_sim_candidate_detail_v2(UUID, UUID)
TO service_role;

COMMIT;
