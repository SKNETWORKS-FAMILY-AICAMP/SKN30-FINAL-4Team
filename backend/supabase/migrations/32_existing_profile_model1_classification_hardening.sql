-- Model 1 classification provenance and activation hardening.
--
-- Migration 31 keeps the base contract plus the minimum compatibility needed
-- by the ledgerless full-replay installer. This upgrade supports both a
-- database where the original migration 31 was already applied and a fresh
-- 01..32 replay.

BEGIN;

ALTER TABLE retrieval.classification_configuration
    ADD COLUMN IF NOT EXISTS runtime_manifest_sha256 TEXT;

-- Only the original seeded artifact has known serving runtime bytes.  Do not
-- silently bless an operator-created legacy configuration with that digest.
UPDATE retrieval.classification_configuration
   SET is_active = FALSE,
       runtime_manifest_sha256 = CASE
       WHEN model_id = 'model_1_support_type'
        AND lower(artifact_sha256) = '8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779'
        AND input_assembly_version = 'existing-profile-model1-input-v1'
        AND producer_version IS NOT DISTINCT FROM 'serving.zip'
           THEN '374e69b07543eb304fd0a8ee92195637fcdf9c7486b7e0593fb80cb0f0d1ab5b'
       -- SHA-256("pre-review-legacy-unknown-runtime-manifest-v1").  It is a
       -- valid format sentinel, not a verified runtime; the batch command
       -- compares the selected configuration with measured runtime bytes.
       ELSE '211990862233fdd2348f06aa4a92e1b881415d79015d7bfaca22c60ba19aa2a3'
   END
 WHERE runtime_manifest_sha256 IS NULL;

ALTER TABLE retrieval.classification_configuration
    ALTER COLUMN runtime_manifest_sha256 SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'retrieval.classification_configuration'::regclass
           AND conname = 'ck_retrieval_classification_config_runtime_manifest'
    ) THEN
        ALTER TABLE retrieval.classification_configuration
            ADD CONSTRAINT ck_retrieval_classification_config_runtime_manifest
            CHECK (runtime_manifest_sha256 ~ '^[0-9A-Fa-f]{64}$');
    END IF;
END;
$$;

-- Replace m31's old (model, artifact, assembly) identity.  Use column names
-- rather than its generated name so a renamed legacy constraint is upgraded.
DO $$
DECLARE
    v_constraint_name NAME;
BEGIN
    FOR v_constraint_name IN
        SELECT constraint_row.conname
          FROM pg_constraint AS constraint_row
         WHERE constraint_row.conrelid =
                   'retrieval.classification_configuration'::regclass
           AND constraint_row.contype = 'u'
           AND (
               SELECT array_agg(attribute.attname::TEXT ORDER BY key_column.ordinality)
                 FROM unnest(constraint_row.conkey)
                          WITH ORDINALITY AS key_column(attnum, ordinality)
                 JOIN pg_attribute AS attribute
                   ON attribute.attrelid = constraint_row.conrelid
                  AND attribute.attnum = key_column.attnum
           ) = ARRAY['model_id', 'artifact_sha256', 'input_assembly_version']::TEXT[]
    LOOP
        EXECUTE format(
            'ALTER TABLE retrieval.classification_configuration DROP CONSTRAINT %I',
            v_constraint_name
        );
    END LOOP;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_retrieval_classification_configuration_identity
    ON retrieval.classification_configuration (
        model_id, lower(artifact_sha256), lower(runtime_manifest_sha256),
        input_assembly_version, COALESCE(producer_version, '')
    );

INSERT INTO retrieval.classification_configuration (
    model_id, artifact_sha256, runtime_manifest_sha256, input_assembly_version,
    producer_version, is_active
)
VALUES (
    'model_1_support_type',
    '8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779',
    '2903d0e90e71cd121af3185eeab3fefe3e1407175d14476e8d60f611b6861a60',
    'existing-profile-model1-input-v1', 'pre-review-existing-model1-runtime-v3', FALSE
)
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION retrieval.assert_classification_configuration_complete()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, retrieval, kb
AS $$
DECLARE
    v_current_profile_count BIGINT;
    v_completed_profile_count BIGINT;
BEGIN
    IF NOT NEW.is_active
       OR (TG_OP = 'UPDATE' AND OLD.is_active) THEN
        RETURN NEW;
    END IF;
    -- A direct update has acquired the target row lock before this trigger;
    -- forbid it rather than invert the promotion lock order.
    IF current_setting('pre_review.classification_promotion', true)
       IS DISTINCT FROM 'trusted' THEN
        RAISE EXCEPTION 'CLASSIFICATION_CONFIGURATION_DIRECT_ACTIVATION_FORBIDDEN'
            USING ERRCODE = '55000';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('pre-review-existing-kb-current-and-classification-v1', 0)
    );
    SELECT COUNT(*) INTO v_current_profile_count
      FROM kb.profile_version AS profile
      JOIN kb.source_version AS source ON source.source_version_pk = profile.source_version_pk
     WHERE profile.is_current AND source.is_current;
    IF v_current_profile_count = 0 THEN
        RAISE EXCEPTION 'CLASSIFICATION_CONFIGURATION_EMPTY_CURRENT_CORPUS'
            USING ERRCODE = '23514';
    END IF;
    SELECT COUNT(*) INTO v_completed_profile_count
      FROM retrieval.existing_profile_classification AS classification
      JOIN kb.profile_version AS profile
        ON profile.profile_version_pk = classification.profile_version_pk
      JOIN kb.source_version AS source ON source.source_version_pk = profile.source_version_pk
     WHERE classification.classification_config_pk = NEW.classification_config_pk
       AND classification.execution_status = 'OK'
       AND profile.is_current AND source.is_current;
    IF v_completed_profile_count <> v_current_profile_count THEN
        RAISE EXCEPTION 'CLASSIFICATION_CONFIGURATION_INCOMPLETE: expected %, got % completed current profiles',
            v_current_profile_count, v_completed_profile_count USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_retrieval_classification_configuration_complete
    ON retrieval.classification_configuration;
CREATE TRIGGER trg_retrieval_classification_configuration_complete
BEFORE INSERT OR UPDATE OF is_active ON retrieval.classification_configuration
FOR EACH ROW EXECUTE FUNCTION retrieval.assert_classification_configuration_complete();

CREATE OR REPLACE FUNCTION retrieval.promote_classification_configuration(
    p_classification_config_pk UUID
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, retrieval, kb
AS $$
DECLARE
    v_current_profile_count BIGINT;
    v_completed_profile_count BIGINT;
BEGIN
    IF p_classification_config_pk IS NULL THEN
        RAISE EXCEPTION 'classification configuration id is required';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('pre-review-existing-kb-current-and-classification-v1', 0)
    );
    PERFORM set_config('pre_review.classification_promotion', 'trusted', true);
    PERFORM 1 FROM retrieval.classification_configuration
     WHERE classification_config_pk = p_classification_config_pk FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'classification configuration was not found';
    END IF;
    SELECT COUNT(*) INTO v_current_profile_count
      FROM kb.profile_version AS profile
      JOIN kb.source_version AS source ON source.source_version_pk = profile.source_version_pk
     WHERE profile.is_current AND source.is_current;
    IF v_current_profile_count = 0 THEN
        RAISE EXCEPTION 'CLASSIFICATION_CONFIGURATION_EMPTY_CURRENT_CORPUS'
            USING ERRCODE = '23514';
    END IF;
    SELECT COUNT(*) INTO v_completed_profile_count
      FROM retrieval.existing_profile_classification AS classification
      JOIN kb.profile_version AS profile
        ON profile.profile_version_pk = classification.profile_version_pk
      JOIN kb.source_version AS source ON source.source_version_pk = profile.source_version_pk
     WHERE classification.classification_config_pk = p_classification_config_pk
       AND classification.execution_status = 'OK'
       AND profile.is_current AND source.is_current;
    IF v_completed_profile_count <> v_current_profile_count THEN
        RAISE EXCEPTION 'CLASSIFICATION_CONFIGURATION_INCOMPLETE: expected %, got % completed current profiles',
            v_current_profile_count, v_completed_profile_count USING ERRCODE = '23514';
    END IF;
    UPDATE retrieval.classification_configuration SET is_active = FALSE
     WHERE is_active AND classification_config_pk <> p_classification_config_pk;
    UPDATE retrieval.classification_configuration SET is_active = TRUE
     WHERE classification_config_pk = p_classification_config_pk;
END;
$$;

CREATE OR REPLACE FUNCTION retrieval.prevent_classification_configuration_identity_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, retrieval
AS $$
BEGIN
    IF NEW.model_id IS DISTINCT FROM OLD.model_id
       OR NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256
       OR NEW.runtime_manifest_sha256 IS DISTINCT FROM OLD.runtime_manifest_sha256
       OR NEW.input_assembly_version IS DISTINCT FROM OLD.input_assembly_version
       OR NEW.producer_version IS DISTINCT FROM OLD.producer_version THEN
        RAISE EXCEPTION 'CLASSIFICATION_CONFIGURATION_IDENTITY_IMMUTABLE'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_retrieval_classification_configuration_identity_immutable
    ON retrieval.classification_configuration;
CREATE TRIGGER trg_retrieval_classification_configuration_identity_immutable
BEFORE UPDATE ON retrieval.classification_configuration
FOR EACH ROW EXECUTE FUNCTION retrieval.prevent_classification_configuration_identity_mutation();

CREATE OR REPLACE FUNCTION retrieval.invalidate_active_classification_on_kb_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, retrieval, kb
AS $$
DECLARE
    v_touched_profile_pks UUID[];
    v_invalidates BOOLEAN := FALSE;
BEGIN
    IF TG_TABLE_NAME = 'notice' THEN
        IF OLD.portal_metadata ->> 'title'
           IS NOT DISTINCT FROM NEW.portal_metadata ->> 'title' THEN
            RETURN NEW;
        END IF;
    END IF;
    -- Row triggers may run after PostgreSQL acquired tuple locks. Do not wait
    -- for a promoter/importer that holds C then E; abort retryably instead.
    IF NOT pg_try_advisory_xact_lock(
        hashtextextended('pre-review-existing-kb-current-and-classification-v1', 0)
    ) THEN
        RAISE EXCEPTION 'CLASSIFICATION_INVALIDATION_LOCK_UNAVAILABLE'
            USING ERRCODE = '40001';
    END IF;
    IF TG_TABLE_NAME = 'notice' THEN
        SELECT EXISTS (
            SELECT 1 FROM kb.source_profile AS source_profile
            JOIN kb.source_version AS source
              ON source.source_profile_pk = source_profile.source_profile_pk
            JOIN kb.profile_version AS profile
              ON profile.source_version_pk = source.source_version_pk
             WHERE source_profile.notice_pk = NEW.notice_pk
               AND source.is_current AND profile.is_current
        ) INTO v_invalidates;
    ELSIF TG_TABLE_NAME = 'source_profile' THEN
        SELECT EXISTS (
            SELECT 1 FROM kb.source_version AS source
            JOIN kb.profile_version AS profile
              ON profile.source_version_pk = source.source_version_pk
             WHERE source.source_profile_pk = NEW.source_profile_pk
               AND source.is_current AND profile.is_current
        ) INTO v_invalidates;
    ELSIF TG_TABLE_NAME = 'fact_occurrence' THEN
        v_touched_profile_pks := CASE
            WHEN TG_OP = 'INSERT' THEN ARRAY[NEW.profile_version_pk]
            WHEN TG_OP = 'DELETE' THEN ARRAY[OLD.profile_version_pk]
            ELSE ARRAY[OLD.profile_version_pk, NEW.profile_version_pk]
        END;
        SELECT EXISTS (
            SELECT 1 FROM kb.profile_version AS profile
            JOIN kb.source_version AS source ON source.source_version_pk = profile.source_version_pk
             WHERE profile.profile_version_pk = ANY(v_touched_profile_pks)
               AND profile.is_current AND source.is_current
        ) INTO v_invalidates;
    ELSIF TG_OP = 'INSERT' THEN
        v_invalidates := NEW.is_current;
    ELSIF TG_OP = 'DELETE' THEN
        v_invalidates := OLD.is_current;
    ELSE
        v_invalidates := OLD.is_current OR NEW.is_current;
    END IF;
    IF v_invalidates THEN
        UPDATE retrieval.classification_configuration SET is_active = FALSE WHERE is_active;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_kb_source_version_classification_activation ON kb.source_version;
CREATE TRIGGER trg_kb_source_version_classification_activation
BEFORE INSERT OR UPDATE OR DELETE ON kb.source_version
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();
DROP TRIGGER IF EXISTS trg_kb_profile_version_classification_activation ON kb.profile_version;
CREATE TRIGGER trg_kb_profile_version_classification_activation
BEFORE INSERT OR UPDATE OR DELETE ON kb.profile_version
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();
DROP TRIGGER IF EXISTS trg_kb_fact_occurrence_classification_activation ON kb.fact_occurrence;
CREATE TRIGGER trg_kb_fact_occurrence_classification_activation
BEFORE INSERT OR UPDATE OR DELETE ON kb.fact_occurrence
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();
DROP TRIGGER IF EXISTS trg_kb_notice_classification_title_activation ON kb.notice;
CREATE TRIGGER trg_kb_notice_classification_title_activation
BEFORE UPDATE OF portal_metadata ON kb.notice
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();
DROP TRIGGER IF EXISTS trg_kb_support_component_classification_activation ON kb.support_component;
DROP TRIGGER IF EXISTS trg_kb_source_profile_classification_notice_activation ON kb.source_profile;
CREATE TRIGGER trg_kb_source_profile_classification_notice_activation
BEFORE UPDATE OF notice_pk ON kb.source_profile
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();

-- CREATE OR REPLACE cannot change a table-returning function's result type.
DROP FUNCTION IF EXISTS retrieval.get_active_existing_profile_classifications();
CREATE OR REPLACE FUNCTION retrieval.get_active_existing_profile_classifications()
RETURNS TABLE (
    profile_version_pk UUID, classification_config_pk UUID, model_id TEXT,
    artifact_sha256 TEXT, runtime_manifest_sha256 TEXT, input_assembly_version TEXT,
    confidence DOUBLE PRECISION, prediction_status TEXT, support_type TEXT,
    effective_status TEXT, effective_reason_code TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, retrieval, kb
AS $$
    SELECT classification.profile_version_pk, config.classification_config_pk,
           config.model_id, config.artifact_sha256, config.runtime_manifest_sha256,
           config.input_assembly_version, classification.confidence,
           classification.prediction_status,
           CASE WHEN classification.execution_status = 'OK'
                     AND classification.prediction_status <> '판단보류'
                THEN classification.support_type_pred ELSE NULL END,
           CASE WHEN classification.execution_status = 'OK'
                     AND classification.prediction_status <> '판단보류'
                THEN 'OK' ELSE 'UNAVAILABLE' END,
           CASE WHEN classification.execution_status = 'OK'
                     AND classification.prediction_status = '판단보류'
                THEN 'PREDICTION_WITHHELD'
                WHEN classification.execution_status = 'OK' THEN NULL
                ELSE classification.reason_code END
      FROM retrieval.existing_profile_classification AS classification
      JOIN retrieval.classification_configuration AS config
        ON config.classification_config_pk = classification.classification_config_pk
      JOIN kb.profile_version AS profile
        ON profile.profile_version_pk = classification.profile_version_pk
      JOIN kb.source_version AS source ON source.source_version_pk = profile.source_version_pk
     WHERE config.is_active AND profile.is_current AND source.is_current;
$$;

GRANT USAGE ON SCHEMA retrieval TO service_role;
REVOKE ALL ON FUNCTION retrieval.get_active_existing_profile_classifications()
FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION retrieval.get_active_existing_profile_classifications() TO service_role;
REVOKE ALL ON FUNCTION retrieval.promote_classification_configuration(UUID)
FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION retrieval.promote_classification_configuration(UUID) TO service_role;
REVOKE ALL ON FUNCTION retrieval.assert_classification_configuration_complete()
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION retrieval.prevent_classification_configuration_identity_mutation()
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION retrieval.invalidate_active_classification_on_kb_change()
FROM PUBLIC, anon, authenticated;

COMMENT ON TABLE retrieval.classification_configuration IS
    'Immutable Model 1 weight/runtime/input/producer identity; activation requires a non-empty complete successful current Existing corpus.';

COMMIT;
