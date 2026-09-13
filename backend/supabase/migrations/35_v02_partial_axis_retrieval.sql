-- ============================================================================
-- Migration 35: v0.2 partial-axis Existing Profile retrieval
--
-- No zero vector is manufactured for a missing request axis.  The worker
-- invokes this only when one to three genuine request embeddings exist.
-- ============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION retrieval.match_existing_profiles_partial_axes(
    p_embedding_config_pk UUID,
    p_request_vectors JSONB,
    p_limit INTEGER DEFAULT 5
)
RETURNS TABLE (
    profile_version_pk UUID,
    average_cosine_similarity DOUBLE PRECISION,
    purpose_similarity DOUBLE PRECISION,
    target_similarity DOUBLE PRECISION,
    support_similarity DOUBLE PRECISION,
    source_profile_id TEXT,
    notice_id TEXT,
    portal_metadata JSONB
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, extensions, retrieval, kb
AS $$
DECLARE
    v_axis_count INTEGER;
BEGIN
    IF p_embedding_config_pk IS NULL
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 5
       OR jsonb_typeof(p_request_vectors) <> 'object'
       OR p_request_vectors = '{}'::jsonb
       OR p_request_vectors - ARRAY['purpose', 'target', 'support'] <> '{}'::jsonb THEN
        RAISE EXCEPTION 'INVALID_PARTIAL_AXIS_RETRIEVAL_REQUEST' USING ERRCODE = '22023';
    END IF;

    SELECT count(*) INTO v_axis_count
      FROM jsonb_object_keys(p_request_vectors);
    IF v_axis_count NOT BETWEEN 1 AND 3
       OR EXISTS (
           SELECT 1
             FROM jsonb_each(p_request_vectors) AS request_axis(axis_code, vector_value)
            WHERE axis_code NOT IN ('purpose', 'target', 'support')
               OR jsonb_typeof(vector_value) <> 'array'
               OR jsonb_array_length(vector_value) <> 1536
               OR EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements(vector_value) AS coordinate(value)
                    WHERE jsonb_typeof(coordinate.value) <> 'number'
               )
       ) THEN
        RAISE EXCEPTION 'INVALID_PARTIAL_AXIS_VECTOR' USING ERRCODE = '22023';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM retrieval.embedding_configuration AS config
         WHERE config.embedding_config_pk = p_embedding_config_pk
           AND config.is_active
    ) THEN
        RAISE EXCEPTION 'ACTIVE_EMBEDDING_CONFIGURATION_REQUIRED' USING ERRCODE = '23514';
    END IF;

    RETURN QUERY
    WITH query_vectors AS (
        SELECT request_axis.axis_code AS scope,
               (request_axis.vector_value #>> '{}')::extensions.vector(1536) AS embedding
          FROM jsonb_each(p_request_vectors) AS request_axis(axis_code, vector_value)
    ), scored AS (
        SELECT
            embedding.profile_version_pk,
            embedding.scope,
            1 - (embedding.embedding OPERATOR(extensions.<=>) query_vectors.embedding)
                AS similarity
          FROM query_vectors
          JOIN retrieval.existing_profile_embedding AS embedding
            ON embedding.scope = query_vectors.scope
           AND embedding.embedding_config_pk = p_embedding_config_pk
          JOIN kb.profile_version AS profile
            ON profile.profile_version_pk = embedding.profile_version_pk
          JOIN kb.source_version AS source
            ON source.source_version_pk = profile.source_version_pk
         WHERE profile.is_current
           AND source.is_current
    ), matched AS (
        SELECT
            scored.profile_version_pk,
            AVG(scored.similarity)::double precision AS average_cosine_similarity,
            MAX(scored.similarity) FILTER (WHERE scored.scope = 'purpose')::double precision
                AS purpose_similarity,
            MAX(scored.similarity) FILTER (WHERE scored.scope = 'target')::double precision
                AS target_similarity,
            MAX(scored.similarity) FILTER (WHERE scored.scope = 'support')::double precision
                AS support_similarity
          FROM scored
         GROUP BY scored.profile_version_pk
        HAVING count(*) = v_axis_count
    ), complete_existing_coverage AS (
        -- Candidate eligibility is stricter than the request's available
        -- axis set: every Existing Profile must own all core embeddings under
        -- this exact active configuration.  Only scoring uses the smaller A.
        SELECT embedding.profile_version_pk
          FROM retrieval.existing_profile_embedding AS embedding
          JOIN kb.profile_version AS profile
            ON profile.profile_version_pk = embedding.profile_version_pk
          JOIN kb.source_version AS source
            ON source.source_version_pk = profile.source_version_pk
         WHERE embedding.embedding_config_pk = p_embedding_config_pk
           AND embedding.scope IN ('purpose', 'target', 'support')
           AND profile.is_current
           AND source.is_current
         GROUP BY embedding.profile_version_pk
        HAVING count(DISTINCT embedding.scope) = 3
    )
    SELECT
        matched.profile_version_pk,
        matched.average_cosine_similarity,
        matched.purpose_similarity,
        matched.target_similarity,
        matched.support_similarity,
        source_profile.source_profile_id,
        notice.notice_id,
        notice.portal_metadata
      FROM matched
      JOIN complete_existing_coverage AS coverage
        ON coverage.profile_version_pk = matched.profile_version_pk
      JOIN kb.profile_version AS profile
        ON profile.profile_version_pk = matched.profile_version_pk
      JOIN kb.source_version AS source
        ON source.source_version_pk = profile.source_version_pk
      JOIN kb.source_profile AS source_profile
        ON source_profile.source_profile_pk = source.source_profile_pk
      JOIN kb.notice AS notice
        ON notice.notice_pk = source_profile.notice_pk
     WHERE profile.is_current
       AND source.is_current
     ORDER BY matched.average_cosine_similarity DESC, matched.profile_version_pk
     LIMIT p_limit;
END;
$$;

COMMENT ON FUNCTION retrieval.match_existing_profiles_partial_axes(UUID, JSONB, INTEGER) IS
    'Trusted v0.2 partial-axis retrieval. p_request_vectors contains only genuine purpose/target/support embeddings; every returned profile owns every listed scope under one active exact configuration and the average denominator is the listed-axis count.';

REVOKE ALL ON FUNCTION retrieval.match_existing_profiles_partial_axes(UUID, JSONB, INTEGER)
FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA retrieval TO service_role;
GRANT EXECUTE ON FUNCTION retrieval.match_existing_profiles_partial_axes(UUID, JSONB, INTEGER)
TO service_role;

COMMIT;
