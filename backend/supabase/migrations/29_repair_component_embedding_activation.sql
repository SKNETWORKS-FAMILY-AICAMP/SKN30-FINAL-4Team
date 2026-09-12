-- Correct deployments where migration 28 from the initial rollout promoted
-- v2 before its Existing Profile embeddings were backfilled.
--
-- If v2 is already active and has a complete current-profile × four-scope
-- set, retain it.  Otherwise, restore v1 as the only active configuration.
-- In particular, this repair must never promote an inactive v2 from a row
-- count: migration 30 deliberately demotes v2 after any retrieval-input
-- mutation while retaining its old rows, and only the byte/hash-verified
-- Python backfill command may promote it again.

BEGIN;

DO $$
DECLARE
    v1_pk UUID;
    v2_pk UUID;
    current_profile_count BIGINT;
    v2_complete_count BIGINT;
    v2_is_active BOOLEAN;
    v_any_active BOOLEAN;
BEGIN
    -- Must match the importer and migration 30.  This correction can be
    -- applied to a live DB, so its current-set snapshot must not race an
    -- Existing KB current-version transition.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('pre-review-existing-kb-current-and-embedding-v1', 0)
    );

    SELECT embedding_config_pk
      INTO v1_pk
      FROM retrieval.embedding_configuration
     WHERE provider = 'openai'
       AND model_id = 'text-embedding-3-small'
       AND dimensions = 1536
       AND distance_metric = 'cosine'
       AND assembly_version = 'approved-facts-role-aware-v1'
     FOR UPDATE;

    SELECT embedding_config_pk
      INTO v2_pk
      FROM retrieval.embedding_configuration
     WHERE provider = 'openai'
       AND model_id = 'text-embedding-3-small'
       AND dimensions = 1536
       AND distance_metric = 'cosine'
       AND assembly_version = 'approved-facts-components-role-aware-v2'
     FOR UPDATE;

    IF v1_pk IS NULL OR v2_pk IS NULL THEN
        RAISE EXCEPTION 'expected v1 and v2 embedding configurations';
    END IF;

    SELECT is_active
      INTO v2_is_active
      FROM retrieval.embedding_configuration
     WHERE embedding_config_pk = v2_pk;

    SELECT EXISTS (
        SELECT 1
          FROM retrieval.embedding_configuration
         WHERE is_active
    )
      INTO v_any_active;

    SELECT COUNT(*)
      INTO current_profile_count
      FROM kb.profile_version AS profile
      JOIN kb.source_version AS source
        ON source.source_version_pk = profile.source_version_pk
     WHERE profile.is_current
       AND source.is_current;

    SELECT COUNT(*)
      INTO v2_complete_count
      FROM (
          SELECT embedding.profile_version_pk
            FROM retrieval.existing_profile_embedding AS embedding
            JOIN kb.profile_version AS profile
              ON profile.profile_version_pk = embedding.profile_version_pk
            JOIN kb.source_version AS source
              ON source.source_version_pk = profile.source_version_pk
           WHERE embedding.embedding_config_pk = v2_pk
             AND profile.is_current
             AND source.is_current
           GROUP BY embedding.profile_version_pk
          HAVING COUNT(DISTINCT embedding.scope) = 4
      ) AS complete_profiles;

    IF v2_is_active
       AND (
           current_profile_count = 0
           OR v2_complete_count <> current_profile_count
       ) THEN
        -- Clear v2 before restoring v1 to satisfy the unique active-config
        -- index.  Do not touch a complete-but-inactive v2: it may have been
        -- invalidated by migration 30 and needs full hash revalidation.
        UPDATE retrieval.embedding_configuration
           SET is_active = FALSE
         WHERE embedding_config_pk = v2_pk
           AND is_active;

        UPDATE retrieval.embedding_configuration
           SET is_active = TRUE
         WHERE embedding_config_pk = v1_pk
           AND NOT is_active;
    ELSIF NOT v_any_active THEN
        -- A broken/no-active legacy state needs a deterministic bootstrap,
        -- but never replace a future active assembly config with v1.
        UPDATE retrieval.embedding_configuration
           SET is_active = TRUE
         WHERE embedding_config_pk = v1_pk
           AND NOT is_active;
    END IF;
END
$$;

COMMIT;
