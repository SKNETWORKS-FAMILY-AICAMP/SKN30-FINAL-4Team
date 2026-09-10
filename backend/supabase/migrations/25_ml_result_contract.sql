-- Migration 25: atomic ML reference persistence and public result projection.
--
-- ML is reference information, not a CPL/FIT/SIM judgement.  Keep the
-- migration-23 fenced writer/read logic intact behind private renamed
-- functions and add the smallest possible wrapper around each boundary.

BEGIN;

ALTER TABLE result.analysis_case
    ADD COLUMN ml_result JSONB NOT NULL DEFAULT '{
      "model_1":{"status":"UNAVAILABLE","support_type":null,"message":"ML 모델이 실행되지 않았습니다.","reason_code":"ML_RUNTIME_MISSING"},
      "model_2":{"status":"UNAVAILABLE","predicted_amount_won":null,"message":"ML 모델이 실행되지 않았습니다.","reason_code":"ML_RUNTIME_MISSING"},
      "model_3":{"status":"UNAVAILABLE","anomaly_level":null,"cause_axes":[],"message":"ML 모델이 실행되지 않았습니다.","reason_code":"ML_RUNTIME_MISSING"}
    }'::jsonb;

COMMENT ON COLUMN result.analysis_case.ml_result IS
    'Public Model 1/2/3 reference result. Scores, confidence, percentiles, and raw model responses are not stored here.';

ALTER FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB)
    RENAME TO persist_analysis_result_core_without_ml;

REVOKE ALL ON FUNCTION workspace.persist_analysis_result_core_without_ml(UUID, UUID, JSONB)
FROM PUBLIC, anon, authenticated, service_role;

CREATE FUNCTION workspace.persist_analysis_result_core(
    p_analysis_run_pk UUID,
    p_processing_run_pk UUID,
    p_result JSONB
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
DECLARE
    v_case_pk UUID;
    v_ml JSONB := p_result->'ml';
    v_model JSONB;
    v_model_name TEXT;
    v_allowed_keys TEXT[];
BEGIN
    IF v_ml IS NULL
       OR jsonb_typeof(v_ml) <> 'object'
       OR NOT v_ml ?& ARRAY['model_1', 'model_2', 'model_3']
       OR v_ml - ARRAY['model_1', 'model_2', 'model_3'] <> '{}'::jsonb THEN
        RAISE EXCEPTION 'INVALID_ML_RESULT' USING ERRCODE = '22023';
    END IF;

    FOREACH v_model_name IN ARRAY ARRAY['model_1', 'model_2', 'model_3'] LOOP
        v_model := v_ml->v_model_name;
        v_allowed_keys := CASE v_model_name
            WHEN 'model_1' THEN ARRAY['status', 'support_type', 'message', 'reason_code']
            WHEN 'model_2' THEN ARRAY['status', 'predicted_amount_won', 'message', 'reason_code']
            ELSE ARRAY['status', 'anomaly_level', 'cause_axes', 'message', 'reason_code']
        END;
        IF jsonb_typeof(v_model) <> 'object'
           OR NOT v_model ?& v_allowed_keys
           OR v_model - v_allowed_keys <> '{}'::jsonb
           OR v_model->>'status' NOT IN ('OK', 'UNAVAILABLE', 'FAILED') THEN
            RAISE EXCEPTION 'INVALID_ML_RESULT: %', v_model_name USING ERRCODE = '22023';
        END IF;
        IF (v_model->'message' <> 'null'::jsonb AND jsonb_typeof(v_model->'message') <> 'string')
           OR (v_model->'reason_code' <> 'null'::jsonb AND jsonb_typeof(v_model->'reason_code') <> 'string') THEN
            RAISE EXCEPTION 'INVALID_ML_RESULT: % text fields', v_model_name USING ERRCODE = '22023';
        END IF;
    END LOOP;

    IF (v_ml#>'{model_1,support_type}' <> 'null'::jsonb
        AND jsonb_typeof(v_ml#>'{model_1,support_type}') <> 'string')
       OR (v_ml#>'{model_2,predicted_amount_won}' <> 'null'::jsonb
        AND jsonb_typeof(v_ml#>'{model_2,predicted_amount_won}') <> 'number')
       OR (v_ml#>'{model_3,anomaly_level}' <> 'null'::jsonb
        AND jsonb_typeof(v_ml#>'{model_3,anomaly_level}') <> 'string') THEN
        RAISE EXCEPTION 'INVALID_ML_RESULT: public value type' USING ERRCODE = '22023';
    END IF;

    IF jsonb_typeof(v_ml#>'{model_3,cause_axes}') <> 'array'
       OR EXISTS (
           SELECT 1
           FROM jsonb_array_elements(v_ml#>'{model_3,cause_axes}') AS axis(value)
           WHERE jsonb_typeof(axis.value) <> 'string'
       ) THEN
        RAISE EXCEPTION 'INVALID_ML_RESULT: model_3.cause_axes' USING ERRCODE = '22023';
    END IF;

    v_case_pk := workspace.persist_analysis_result_core_without_ml(
        p_analysis_run_pk,
        p_processing_run_pk,
        p_result
    );
    IF v_case_pk IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE result.analysis_case
       SET ml_result = v_ml
     WHERE analysis_case_pk = v_case_pk;
    RETURN v_case_pk;
END;
$$;

COMMENT ON FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB) IS
    'Migration-23 fenced result ingest plus atomic public ML reference persistence.';

REVOKE ALL ON FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB)
FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION workspace.persist_analysis_result_core(UUID, UUID, JSONB)
TO service_role;

ALTER FUNCTION api.rpc_get_analysis_result(UUID)
    RENAME TO rpc_get_analysis_result_without_ml;

REVOKE ALL ON FUNCTION api.rpc_get_analysis_result_without_ml(UUID)
FROM PUBLIC, anon, authenticated;

CREATE FUNCTION api.rpc_get_analysis_result(p_analysis_case_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, api, result
AS $$
DECLARE
    v_result JSONB;
    v_ml JSONB;
BEGIN
    v_result := api.rpc_get_analysis_result_without_ml(p_analysis_case_id);

    SELECT c.ml_result
      INTO v_ml
      FROM result.analysis_case c
     WHERE c.analysis_case_pk = p_analysis_case_id
       AND c.user_id = (SELECT auth.uid())
       AND c.retention_expires_at > now();

    RETURN v_result || jsonb_build_object('ml', v_ml);
END;
$$;

COMMENT ON FUNCTION api.rpc_get_analysis_result(UUID) IS
    'Owned, live-retention analysis result including the fixed public Model 1/2/3 reference object.';

REVOKE ALL ON FUNCTION api.rpc_get_analysis_result(UUID) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION api.rpc_get_analysis_result(UUID) TO authenticated;

COMMIT;
