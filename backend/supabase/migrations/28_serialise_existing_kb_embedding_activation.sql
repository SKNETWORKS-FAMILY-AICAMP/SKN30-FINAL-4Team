-- Keep Existing KB current-version changes serial with v2 embedding
-- activation.  A current Profile/source change invalidates a complete v2
-- backfill for the new current set, so it must also demote v2 before commit.
-- The backfill script takes the same transaction-scoped advisory lock before
-- checking current rows and switching the active configuration.

BEGIN;

-- migration 27 and 28 are separate files.  An older/direct writer might
-- commit a current-version change after 27 but before these triggers exist.
-- Take the write-conflicting lock first: it waits for such a writer, blocks
-- new ones through this transaction, and gives trigger installation a stable
-- current set.
SELECT pg_advisory_xact_lock(
    hashtextextended('pre-review-existing-kb-current-and-embedding-v1', 0)
);
LOCK TABLE kb.source_version, kb.profile_version,
           kb.support_component, kb.fact_occurrence
    IN SHARE ROW EXCLUSIVE MODE;

-- Capture whether this is the first installation of the complete trigger
-- fence (or whether an earlier installation has drifted) *before* replacing
-- the function/triggers below.  apply_migrations.sh deliberately reapplies
-- every migration, so an already healthy migration 28 must not repeatedly
-- take a fully verified v2 corpus offline.
--
-- A SQL row-count check cannot prove that a pre-trigger writer did not alter
-- current Fact/component content.  A first installation or trigger drift
-- therefore requires one conservative v2 demotion; the byte/hash-verified
-- embed script is the only path that may promote v2 again.
CREATE TEMP TABLE migration_28_activation_gate (
    requires_one_time_revalidation BOOLEAN NOT NULL
) ON COMMIT DROP;

INSERT INTO pg_temp.migration_28_activation_gate (
    requires_one_time_revalidation
)
SELECT
    COALESCE(
        obj_description(
            to_regprocedure(
                'kb.serialise_current_version_embedding_activation()'
            ),
            'pg_proc'
        ),
        ''
    ) <> 'pre-review-migration-28-embedding-activation-v1'
    OR EXISTS (
        SELECT 1
          FROM (
              VALUES
                  (
                      'trg_kb_source_version_embedding_activation',
                      'kb.source_version'::regclass
                  ),
                  (
                      'trg_kb_profile_version_embedding_activation',
                      'kb.profile_version'::regclass
                  ),
                  (
                      'trg_kb_support_component_embedding_activation',
                      'kb.support_component'::regclass
                  ),
                  (
                      'trg_kb_fact_occurrence_embedding_activation',
                      'kb.fact_occurrence'::regclass
                  )
          ) AS expected(trigger_name, table_oid)
         WHERE NOT EXISTS (
             SELECT 1
               FROM pg_trigger AS trigger_row
              WHERE trigger_row.tgname = expected.trigger_name
                AND trigger_row.tgrelid = expected.table_oid
                AND NOT trigger_row.tgisinternal
                AND trigger_row.tgfoid = to_regprocedure(
                    'kb.serialise_current_version_embedding_activation()'
                )
                AND trigger_row.tgenabled = 'O'
                -- ROW + INSERT + DELETE + UPDATE; no BEFORE/INSTEAD/TRUNCATE.
                AND trigger_row.tgtype = 29
         )
    );

CREATE OR REPLACE FUNCTION kb.serialise_current_version_embedding_activation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, kb, retrieval
AS $$
DECLARE
    v1_pk UUID;
    v2_pk UUID;
    v_touched_profile_pks UUID[];
BEGIN
    -- Parent and retrieval-input child mutations share this lock with the
    -- backfill promoter.  It also makes the current-parent check below see a
    -- serial order rather than an unrelated in-flight version transition.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('pre-review-existing-kb-current-and-embedding-v1', 0)
    );

    IF TG_TABLE_NAME IN ('support_component', 'fact_occurrence') THEN
        IF TG_OP = 'DELETE' THEN
            v_touched_profile_pks := ARRAY[OLD.profile_version_pk];
        ELSIF TG_OP = 'INSERT' THEN
            v_touched_profile_pks := ARRAY[NEW.profile_version_pk];
        ELSE
            v_touched_profile_pks := ARRAY[
                OLD.profile_version_pk, NEW.profile_version_pk
            ];
        END IF;

        -- The vector assembly copies approved Fact values and component
        -- names.  These rows have no immutable-write constraint, so any
        -- mutation under a current Profile invalidates its v2 provenance.
        IF NOT EXISTS (
            SELECT 1
              FROM kb.profile_version AS profile
              JOIN kb.source_version AS source
                ON source.source_version_pk = profile.source_version_pk
             WHERE profile.profile_version_pk = ANY(v_touched_profile_pks)
               AND profile.is_current
               AND source.is_current
        ) THEN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        IF NOT OLD.is_current THEN
            RETURN OLD;
        END IF;
    ELSIF TG_OP = 'INSERT' THEN
        IF NOT NEW.is_current THEN
            RETURN NEW;
        END IF;
    ELSIF NEW.is_current IS DISTINCT FROM OLD.is_current THEN
        -- Promotion/demotion of either side changes the current corpus.
        NULL;
    ELSIF TG_TABLE_NAME = 'source_version' THEN
        -- A current source's profile identity or source bytes changed.  It
        -- no longer represents the vector corpus that v2 was verified for.
        IF NOT OLD.is_current
           OR (
               NEW.source_profile_pk IS NOT DISTINCT FROM OLD.source_profile_pk
               AND NEW.source_sha256 IS NOT DISTINCT FROM OLD.source_sha256
           ) THEN
            RETURN NEW;
        END IF;
    ELSIF TG_TABLE_NAME = 'profile_version' THEN
        -- v2 vectors are derived from this exact structured Profile/version.
        -- Changes to a non-current row do not alter the searchable corpus.
        IF NOT OLD.is_current
           OR (
               NEW.source_version_pk IS NOT DISTINCT FROM OLD.source_version_pk
               AND NEW.schema_version IS NOT DISTINCT FROM OLD.schema_version
               AND NEW.profile_sha256 IS NOT DISTINCT FROM OLD.profile_sha256
               AND NEW.structured_artifact_pk
                   IS NOT DISTINCT FROM OLD.structured_artifact_pk
           ) THEN
            RETURN NEW;
        END IF;
    ELSE
        RETURN NEW;
    END IF;

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

    IF EXISTS (
        SELECT 1
          FROM retrieval.embedding_configuration
         WHERE embedding_config_pk = v2_pk
           AND is_active
    ) THEN
        -- Clear v2 before restoring v1 to satisfy the single-active partial
        -- index.  Both writes are in the importer's transaction.
        UPDATE retrieval.embedding_configuration
           SET is_active = FALSE
         WHERE embedding_config_pk = v2_pk;

        UPDATE retrieval.embedding_configuration
           SET is_active = TRUE
         WHERE embedding_config_pk = v1_pk;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

COMMENT ON FUNCTION kb.serialise_current_version_embedding_activation()
    IS 'pre-review-migration-28-embedding-activation-v1';

REVOKE ALL ON FUNCTION kb.serialise_current_version_embedding_activation() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_kb_source_version_embedding_activation
    ON kb.source_version;
CREATE TRIGGER trg_kb_source_version_embedding_activation
AFTER INSERT OR UPDATE OR DELETE ON kb.source_version
FOR EACH ROW
EXECUTE FUNCTION kb.serialise_current_version_embedding_activation();

DROP TRIGGER IF EXISTS trg_kb_profile_version_embedding_activation
    ON kb.profile_version;
CREATE TRIGGER trg_kb_profile_version_embedding_activation
AFTER INSERT OR UPDATE OR DELETE ON kb.profile_version
FOR EACH ROW
EXECUTE FUNCTION kb.serialise_current_version_embedding_activation();

DROP TRIGGER IF EXISTS trg_kb_support_component_embedding_activation
    ON kb.support_component;
CREATE TRIGGER trg_kb_support_component_embedding_activation
AFTER INSERT OR UPDATE OR DELETE ON kb.support_component
FOR EACH ROW
EXECUTE FUNCTION kb.serialise_current_version_embedding_activation();

DROP TRIGGER IF EXISTS trg_kb_fact_occurrence_embedding_activation
    ON kb.fact_occurrence;
CREATE TRIGGER trg_kb_fact_occurrence_embedding_activation
AFTER INSERT OR UPDATE OR DELETE ON kb.fact_occurrence
FOR EACH ROW
EXECUTE FUNCTION kb.serialise_current_version_embedding_activation();

DO $$
DECLARE
    v1_pk UUID;
    v2_pk UUID;
    v_requires_one_time_revalidation BOOLEAN;
BEGIN
    SELECT requires_one_time_revalidation
      INTO v_requires_one_time_revalidation
      FROM pg_temp.migration_28_activation_gate;

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

    -- Do this only on the first complete trigger installation or when its
    -- function/trigger catalog state was incomplete.  A routine migration
    -- reapply sees the pre-DDL healthy marker and preserves active v2.
    IF v_requires_one_time_revalidation
       AND EXISTS (
        SELECT 1
          FROM retrieval.embedding_configuration
         WHERE embedding_config_pk = v2_pk
           AND is_active
    ) THEN
        UPDATE retrieval.embedding_configuration
           SET is_active = FALSE
         WHERE embedding_config_pk = v2_pk;

        UPDATE retrieval.embedding_configuration
           SET is_active = TRUE
         WHERE embedding_config_pk = v1_pk;
    END IF;
END
$$;

COMMIT;
