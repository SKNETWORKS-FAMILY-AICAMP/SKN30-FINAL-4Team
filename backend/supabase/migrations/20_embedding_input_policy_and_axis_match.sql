-- Reproducible embedding input policy and three-axis (B strategy) matching.

BEGIN;

ALTER TABLE retrieval.embedding_configuration
    ADD COLUMN IF NOT EXISTS max_input_tokens INTEGER NOT NULL DEFAULT 8192,
    ADD COLUMN IF NOT EXISTS chunking_strategy TEXT NOT NULL
        DEFAULT 'fact-boundary-token-weighted-mean-v1';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'embedding_configuration_max_input_tokens_check'
          AND conrelid = 'retrieval.embedding_configuration'::regclass
    ) THEN
        ALTER TABLE retrieval.embedding_configuration
            ADD CONSTRAINT embedding_configuration_max_input_tokens_check
            CHECK (max_input_tokens BETWEEN 1 AND 8192);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'embedding_configuration_chunking_strategy_check'
          AND conrelid = 'retrieval.embedding_configuration'::regclass
    ) THEN
        ALTER TABLE retrieval.embedding_configuration
            ADD CONSTRAINT embedding_configuration_chunking_strategy_check
            CHECK (btrim(chunking_strategy) <> '');
    END IF;
END
$$;

-- The original generic configuration is retained for provenance but is no
-- longer active.  Do not deactivate a later recognised configuration here:
-- apply_migrations.sh replays this migration, and a verified v2 must survive
-- that replay.  Restrict this cleanup to the known pre-policy legacy row so
-- future assembly versions are not silently demoted either.
UPDATE retrieval.embedding_configuration
SET is_active = FALSE
WHERE is_active
  AND provider = 'openai'
  AND model_id = 'text-embedding-3-small'
  AND dimensions = 1536
  AND distance_metric = 'cosine'
  AND assembly_version = 'existing-profile-v1';

INSERT INTO retrieval.embedding_configuration (
    provider,
    model_id,
    dimensions,
    distance_metric,
    assembly_version,
    max_input_tokens,
    chunking_strategy,
    is_active
)
VALUES (
    'openai',
    'text-embedding-3-small',
    1536,
    'cosine',
    'approved-facts-role-aware-v1',
    8192,
    'fact-boundary-token-weighted-mean-v1',
    FALSE
)
ON CONFLICT (provider, model_id, dimensions, distance_metric, assembly_version)
DO UPDATE SET
    max_input_tokens = EXCLUDED.max_input_tokens,
    chunking_strategy = EXCLUDED.chunking_strategy;

-- Bootstrap v1 only when the generic cleanup left no active configuration.
-- This is deliberately separate from the UPSERT: an existing active v2 must
-- remain active on replay, and an INSERT must not violate the one-active
-- partial unique index while another configuration is active.
UPDATE retrieval.embedding_configuration AS v1
SET is_active = TRUE
WHERE v1.provider = 'openai'
  AND v1.model_id = 'text-embedding-3-small'
  AND v1.dimensions = 1536
  AND v1.distance_metric = 'cosine'
  AND v1.assembly_version = 'approved-facts-role-aware-v1'
  AND NOT v1.is_active
  AND NOT EXISTS (
      SELECT 1
      FROM retrieval.embedding_configuration AS active_config
      WHERE active_config.is_active
  );

CREATE OR REPLACE FUNCTION retrieval.match_existing_profiles_three_axis(
    p_query_purpose extensions.vector(1536),
    p_query_target extensions.vector(1536),
    p_query_support extensions.vector(1536),
    p_limit INTEGER DEFAULT 10,
    p_embedding_config_pk UUID DEFAULT NULL
)
RETURNS TABLE (
    profile_version_pk UUID,
    average_cosine_similarity DOUBLE PRECISION,
    purpose_similarity DOUBLE PRECISION,
    target_similarity DOUBLE PRECISION,
    support_similarity DOUBLE PRECISION
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, retrieval, kb, extensions
AS $$
    WITH query_vectors(scope, embedding) AS (
        VALUES
            ('purpose'::TEXT, p_query_purpose),
            ('target'::TEXT, p_query_target),
            ('support'::TEXT, p_query_support)
    ), scored AS (
        SELECT
            embedding_row.profile_version_pk,
            embedding_row.scope,
            1 - (
                embedding_row.embedding
                OPERATOR(extensions.<=>) query_vectors.embedding
            ) AS similarity
        FROM retrieval.existing_profile_embedding AS embedding_row
        JOIN query_vectors
          ON query_vectors.scope = embedding_row.scope
        JOIN retrieval.embedding_configuration AS config
          ON config.embedding_config_pk = embedding_row.embedding_config_pk
        JOIN kb.profile_version AS profile
          ON profile.profile_version_pk = embedding_row.profile_version_pk
        JOIN kb.source_version AS source
          ON source.source_version_pk = profile.source_version_pk
        WHERE profile.is_current
          AND source.is_current
          AND (
              (p_embedding_config_pk IS NULL AND config.is_active)
              OR config.embedding_config_pk = p_embedding_config_pk
          )
    )
    SELECT
        scored.profile_version_pk,
        AVG(scored.similarity)::DOUBLE PRECISION AS average_cosine_similarity,
        MAX(scored.similarity) FILTER (
            WHERE scored.scope = 'purpose'
        )::DOUBLE PRECISION AS purpose_similarity,
        MAX(scored.similarity) FILTER (
            WHERE scored.scope = 'target'
        )::DOUBLE PRECISION AS target_similarity,
        MAX(scored.similarity) FILTER (
            WHERE scored.scope = 'support'
        )::DOUBLE PRECISION AS support_similarity
    FROM scored
    GROUP BY scored.profile_version_pk
    HAVING COUNT(*) = 3
    ORDER BY AVG(scored.similarity) DESC, scored.profile_version_pk
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 10), 1), 100);
$$;

REVOKE ALL ON FUNCTION retrieval.match_existing_profiles_three_axis(
    extensions.vector,
    extensions.vector,
    extensions.vector,
    INTEGER,
    UUID
) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION retrieval.match_existing_profiles_three_axis(
    extensions.vector,
    extensions.vector,
    extensions.vector,
    INTEGER,
    UUID
) TO service_role;

COMMIT;
