-- Existing-profile vector retrieval.
-- Request Profile embeddings are deliberately ephemeral and are never stored.

BEGIN;

CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

CREATE TABLE IF NOT EXISTS retrieval.embedding_configuration (
    embedding_config_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider            TEXT NOT NULL CHECK (btrim(provider) <> ''),
    model_id            TEXT NOT NULL CHECK (btrim(model_id) <> ''),
    dimensions          INTEGER NOT NULL CHECK (dimensions = 1536),
    distance_metric     TEXT NOT NULL CHECK (distance_metric = 'cosine'),
    assembly_version    TEXT NOT NULL CHECK (btrim(assembly_version) <> ''),
    is_active           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, model_id, dimensions, distance_metric, assembly_version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_retrieval_one_active_embedding_configuration
    ON retrieval.embedding_configuration ((is_active))
    WHERE is_active;

INSERT INTO retrieval.embedding_configuration (
    provider,
    model_id,
    dimensions,
    distance_metric,
    assembly_version,
    is_active
)
VALUES (
    'openai',
    'text-embedding-3-small',
    1536,
    'cosine',
    'existing-profile-v1',
    TRUE
)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS retrieval.existing_profile_embedding (
    profile_version_pk   UUID NOT NULL
        REFERENCES kb.profile_version(profile_version_pk)
        ON DELETE CASCADE,
    embedding_config_pk  UUID NOT NULL
        REFERENCES retrieval.embedding_configuration(embedding_config_pk)
        ON DELETE RESTRICT,
    scope                TEXT NOT NULL
        CHECK (scope IN ('purpose', 'target', 'support', 'combined')),
    input_sha256         TEXT NOT NULL
        CHECK (input_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    embedding            extensions.vector(1536) NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_version_pk, embedding_config_pk, scope)
);

CREATE INDEX IF NOT EXISTS ix_retrieval_existing_embedding_config_scope
    ON retrieval.existing_profile_embedding(embedding_config_pk, scope);

COMMENT ON TABLE retrieval.existing_profile_embedding IS
    'Persistent embeddings for Existing Profiles only; Request embeddings are generated in worker memory and discarded.';
COMMENT ON COLUMN retrieval.existing_profile_embedding.scope IS
    'One of purpose, target, support, or combined.';

CREATE OR REPLACE FUNCTION retrieval.match_existing_profiles(
    p_query_embedding extensions.vector(1536),
    p_scope TEXT DEFAULT 'combined',
    p_limit INTEGER DEFAULT 10,
    p_embedding_config_pk UUID DEFAULT NULL
)
RETURNS TABLE (
    profile_version_pk UUID,
    cosine_distance DOUBLE PRECISION,
    cosine_similarity DOUBLE PRECISION
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, retrieval, kb, extensions
AS $$
    SELECT
        embedding_row.profile_version_pk,
        embedding_row.embedding OPERATOR(extensions.<=>) p_query_embedding AS cosine_distance,
        1 - (
            embedding_row.embedding OPERATOR(extensions.<=>) p_query_embedding
        ) AS cosine_similarity
    FROM retrieval.existing_profile_embedding AS embedding_row
    JOIN retrieval.embedding_configuration AS config
      ON config.embedding_config_pk = embedding_row.embedding_config_pk
    JOIN kb.profile_version AS profile
      ON profile.profile_version_pk = embedding_row.profile_version_pk
    JOIN kb.source_version AS source
      ON source.source_version_pk = profile.source_version_pk
    WHERE embedding_row.scope = p_scope
      AND p_scope IN ('purpose', 'target', 'support', 'combined')
      AND profile.is_current
      AND source.is_current
      AND (
          (p_embedding_config_pk IS NULL AND config.is_active)
          OR config.embedding_config_pk = p_embedding_config_pk
      )
    ORDER BY embedding_row.embedding OPERATOR(extensions.<=>) p_query_embedding
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 10), 1), 100);
$$;

REVOKE ALL ON TABLE
    retrieval.embedding_configuration,
    retrieval.existing_profile_embedding
FROM anon, authenticated;

REVOKE ALL ON FUNCTION retrieval.match_existing_profiles(
    extensions.vector,
    TEXT,
    INTEGER,
    UUID
) FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    retrieval.embedding_configuration,
    retrieval.existing_profile_embedding
TO service_role;

GRANT EXECUTE ON FUNCTION retrieval.match_existing_profiles(
    extensions.vector,
    TEXT,
    INTEGER,
    UUID
) TO service_role;

COMMIT;
