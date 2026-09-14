-- Persist the exact embedding configuration selected by each analysis attempt.
--
-- Active configuration is deployment state and may change after an analysis
-- succeeds.  The E2E/reproducibility path must therefore read this immutable
-- attempt snapshot, never the configuration that happens to be active later.

BEGIN;

CREATE OR REPLACE FUNCTION workspace.record_analysis_embedding_provenance_v2(
    p_analysis_run_pk UUID,
    p_processing_run_pk UUID,
    p_embedding_config_pk UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, ops, workspace, retrieval
AS $$
DECLARE
    v_provenance JSONB;
    v_existing_provenance JSONB;
BEGIN
    -- Take the lifecycle fence first, in the same run/dispatch order as the
    -- claim and terminal-result functions.  A worker whose lease was
    -- replaced cannot attach evidence to the replacement attempt.
    PERFORM 1
      FROM workspace.analysis_run AS run
      JOIN workspace.analysis_run_dispatch AS dispatch
        ON dispatch.analysis_run_pk = run.analysis_run_pk
     WHERE run.analysis_run_pk = p_analysis_run_pk
       AND run.status = 'running'
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND dispatch.lease_expires_at > clock_timestamp()
     FOR UPDATE OF run, dispatch;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    -- Serialise concurrent writes to this exact processing attempt after the
    -- lifecycle fence has been acquired.  The second predicate prevents a
    -- forged token from borrowing another run's live dispatch fence.
    SELECT processing.run_metadata -> 'embedding_configuration'
      INTO v_existing_provenance
      FROM ops.processing_run AS processing
     WHERE processing.processing_run_pk = p_processing_run_pk
       AND processing.source_analysis_run_id = p_analysis_run_pk
       AND processing.run_type = 'analysis'
       AND processing.status = 'running'
     FOR UPDATE;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    -- Do not re-read a configuration merely to service a retry.  A
    -- configuration row is deployment state, while this processing attempt's
    -- snapshot is historical evidence: the same selected identity is
    -- idempotent even if an operator later changes configuration metadata.
    IF v_existing_provenance IS NOT NULL THEN
        IF p_embedding_config_pk IS NULL THEN
            RETURN v_existing_provenance ->> 'selected' = 'false'
               AND v_existing_provenance -> 'configuration_id' = 'null'::jsonb;
        END IF;
        RETURN v_existing_provenance ->> 'selected' = 'true'
           AND v_existing_provenance ->> 'configuration_id' = p_embedding_config_pk::text;
    END IF;

    IF p_embedding_config_pk IS NULL THEN
        v_provenance := jsonb_build_object(
            'selected', false,
            'configuration_id', NULL,
            'provider', NULL,
            'model_id', NULL,
            'dimensions', NULL,
            'max_input_tokens', NULL,
            'assembly_version', NULL
        );
    ELSE
        SELECT jsonb_build_object(
            'selected', true,
            'configuration_id', configuration.embedding_config_pk::text,
            'provider', configuration.provider,
            'model_id', configuration.model_id,
            'dimensions', configuration.dimensions,
            'max_input_tokens', configuration.max_input_tokens,
            'assembly_version', configuration.assembly_version
        )
          INTO v_provenance
          FROM retrieval.embedding_configuration AS configuration
         WHERE configuration.embedding_config_pk = p_embedding_config_pk;
        IF v_provenance IS NULL THEN
            RAISE EXCEPTION 'EMBEDDING_CONFIGURATION_NOT_FOUND' USING ERRCODE = 'P0002';
        END IF;
    END IF;

    UPDATE ops.processing_run AS processing
       SET run_metadata = COALESCE(processing.run_metadata, '{}'::jsonb)
            || jsonb_build_object('embedding_configuration', v_provenance)
     WHERE processing.processing_run_pk = p_processing_run_pk
       AND processing.run_metadata -> 'embedding_configuration' IS NULL;
    RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION workspace.record_analysis_embedding_provenance_v2(UUID, UUID, UUID)
FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.record_analysis_embedding_provenance_v2(UUID, UUID, UUID)
TO service_role;

COMMIT;
