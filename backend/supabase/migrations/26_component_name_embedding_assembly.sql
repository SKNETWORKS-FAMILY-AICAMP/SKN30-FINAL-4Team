-- Include structural support-component names in both Existing and Request
-- support-axis inputs.
--
-- This is deliberately a *staged* configuration migration.  The v1 vectors
-- remain the live retrieval space until the embedding backfill command has
-- verified every current Existing Profile in all four scopes and atomically
-- promotes v2.  Activating an empty v2 configuration would turn a transient
-- migration state into a misleading, successful zero-candidate match.

BEGIN;

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
    'approved-facts-components-role-aware-v2',
    8192,
    'fact-boundary-token-weighted-mean-v1',
    FALSE
)
ON CONFLICT (provider, model_id, dimensions, distance_metric, assembly_version)
DO UPDATE SET
    max_input_tokens = EXCLUDED.max_input_tokens,
    -- Reapplying the migration must not demote a v2 configuration already
    -- promoted by the verified backfill command; is_active is intentionally
    -- absent from this update list.
    chunking_strategy = EXCLUDED.chunking_strategy;

COMMIT;
