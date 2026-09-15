-- Versioned Model 1 (support-type) classifications for Existing Profiles.
--
-- This is KB enrichment, not a browser result surface.  The raw prediction
-- remains auditable, while downstream consumers must use the CASE-gated
-- service function below so a withheld prediction is never promoted to a
-- usable support type.

BEGIN;

CREATE TABLE IF NOT EXISTS retrieval.classification_configuration (
    classification_config_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id TEXT NOT NULL
        CHECK (model_id = 'model_1_support_type'),
    artifact_sha256 TEXT NOT NULL
        CHECK (artifact_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    input_assembly_version TEXT NOT NULL
        CHECK (btrim(input_assembly_version) <> ''),
    producer_version TEXT NULL
        CHECK (producer_version IS NULL OR btrim(producer_version) <> ''),
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (model_id, artifact_sha256, input_assembly_version)
);

-- m32 enriches this identity, but the ledgerless apply script replays m31 on
-- every run.  Add/backfill this column here too, before its seed below, so a
-- database already upgraded by m32 can safely replay 01..32.
ALTER TABLE retrieval.classification_configuration
    ADD COLUMN IF NOT EXISTS runtime_manifest_sha256 TEXT;

UPDATE retrieval.classification_configuration
   SET is_active = FALSE,
       runtime_manifest_sha256 = CASE
       WHEN model_id = 'model_1_support_type'
        AND lower(artifact_sha256) = '8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779'
        AND input_assembly_version = 'existing-profile-model1-input-v1'
        AND producer_version IS NOT DISTINCT FROM 'serving.zip'
           THEN '374e69b07543eb304fd0a8ee92195637fcdf9c7486b7e0593fb80cb0f0d1ab5b'
       ELSE '211990862233fdd2348f06aa4a92e1b881415d79015d7bfaca22c60ba19aa2a3'
   END
 WHERE runtime_manifest_sha256 IS NULL;

ALTER TABLE retrieval.classification_configuration
    ALTER COLUMN runtime_manifest_sha256 SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'retrieval.classification_configuration'::regclass
           AND conname = 'ck_retrieval_classification_config_runtime_manifest'
    ) THEN
        ALTER TABLE retrieval.classification_configuration
            ADD CONSTRAINT ck_retrieval_classification_config_runtime_manifest
            CHECK (runtime_manifest_sha256 ~ '^[0-9A-Fa-f]{64}$');
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_retrieval_one_active_classification_configuration
    ON retrieval.classification_configuration ((is_active))
    WHERE is_active;

-- The artifact supplied with this repository is registered but deliberately
-- inactive.  Only a verified Existing-Profile backfill may activate it.
INSERT INTO retrieval.classification_configuration (
    model_id,
    artifact_sha256,
    runtime_manifest_sha256,
    input_assembly_version,
    producer_version,
    is_active
)
VALUES (
    'model_1_support_type',
    '8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779',
    '2903d0e90e71cd121af3185eeab3fefe3e1407175d14476e8d60f611b6861a60',
    'existing-profile-model1-input-v1',
    'pre-review-existing-model1-runtime-v3',
    FALSE
)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS retrieval.existing_profile_classification (
    profile_version_pk UUID NOT NULL
        REFERENCES kb.profile_version(profile_version_pk) ON DELETE CASCADE,
    classification_config_pk UUID NOT NULL
        REFERENCES retrieval.classification_configuration(classification_config_pk)
        ON DELETE RESTRICT,
    input_sha256 TEXT NOT NULL
        CHECK (input_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    support_type_pred TEXT NULL
        CHECK (support_type_pred IN (
            'SW·솔루션', '경진대회', '고용보조', '교육훈련', '기술·IP평가',
            '보증', '사업화', '상담', '설비', '성능인증', '수출물류',
            '수출통관', '연구개발', '융자', '창업보육', '컨설팅', '판로',
            '해외수주·실증', '해외인증'
        )),
    confidence DOUBLE PRECISION NULL
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    prediction_status TEXT NULL
        CHECK (prediction_status IN ('판단보류', '참고용', '신뢰')),
    execution_status TEXT NOT NULL
        CHECK (execution_status IN ('OK', 'UNAVAILABLE', 'FAILED')),
    reason_code TEXT NULL
        CHECK (reason_code IS NULL OR btrim(reason_code) <> ''),
    processing_run_pk UUID NULL
        REFERENCES ops.processing_run(processing_run_pk) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_version_pk, classification_config_pk),
    CHECK (
        (
            execution_status = 'OK'
            AND support_type_pred IS NOT NULL
            AND confidence IS NOT NULL
            AND prediction_status IS NOT NULL
        )
        OR (
            execution_status IN ('UNAVAILABLE', 'FAILED')
            AND support_type_pred IS NULL
            AND confidence IS NULL
            AND prediction_status IS NULL
            AND reason_code IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_retrieval_existing_profile_classification_config_status
    ON retrieval.existing_profile_classification (
        classification_config_pk, execution_status, profile_version_pk
    );
CREATE INDEX IF NOT EXISTS ix_retrieval_existing_profile_classification_processing_run
    ON retrieval.existing_profile_classification (processing_run_pk)
    WHERE processing_run_pk IS NOT NULL;

-- Activation is a promotion boundary.  It is valid only when every current
-- Existing Profile has a genuine Model 1 response; 판단보류 is a completed
-- response, whereas unavailable/failed runtime rows are not.
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

    IF current_setting('pre_review.classification_promotion', true)
       IS DISTINCT FROM 'trusted' THEN
        RAISE EXCEPTION 'CLASSIFICATION_CONFIGURATION_DIRECT_ACTIVATION_FORBIDDEN'
            USING ERRCODE = '55000';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended('pre-review-existing-kb-current-and-classification-v1', 0)
    );

    SELECT COUNT(*)
      INTO v_current_profile_count
      FROM kb.profile_version AS profile
      JOIN kb.source_version AS source
        ON source.source_version_pk = profile.source_version_pk
     WHERE profile.is_current
       AND source.is_current;

    IF v_current_profile_count = 0 THEN
        RAISE EXCEPTION 'CLASSIFICATION_CONFIGURATION_EMPTY_CURRENT_CORPUS'
            USING ERRCODE = '23514';
    END IF;

    SELECT COUNT(*)
      INTO v_completed_profile_count
      FROM retrieval.existing_profile_classification AS classification
      JOIN kb.profile_version AS profile
        ON profile.profile_version_pk = classification.profile_version_pk
      JOIN kb.source_version AS source
        ON source.source_version_pk = profile.source_version_pk
     WHERE classification.classification_config_pk = NEW.classification_config_pk
       AND classification.execution_status = 'OK'
       AND profile.is_current
       AND source.is_current;

    IF v_completed_profile_count <> v_current_profile_count THEN
        RAISE EXCEPTION
            'CLASSIFICATION_CONFIGURATION_INCOMPLETE: expected %, got % completed current profiles',
            v_current_profile_count, v_completed_profile_count
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_retrieval_classification_configuration_complete
    ON retrieval.classification_configuration;
CREATE TRIGGER trg_retrieval_classification_configuration_complete
BEFORE INSERT OR UPDATE OF is_active ON retrieval.classification_configuration
FOR EACH ROW EXECUTE FUNCTION retrieval.assert_classification_configuration_complete();

-- A complete active corpus is immutable.  A backfill must first demote its
-- configuration, then replace rows, then pass the completeness gate again.
CREATE OR REPLACE FUNCTION retrieval.prevent_active_classification_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, retrieval
AS $$
DECLARE
    v_config_pk UUID := CASE
        WHEN TG_OP = 'DELETE' THEN OLD.classification_config_pk
        ELSE NEW.classification_config_pk
    END;
BEGIN
    IF EXISTS (
        SELECT 1
          FROM retrieval.classification_configuration
         WHERE classification_config_pk = v_config_pk
           AND is_active
    ) THEN
        RAISE EXCEPTION 'ACTIVE_CLASSIFICATION_CONFIGURATION_IMMUTABLE'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_retrieval_active_classification_immutable
    ON retrieval.existing_profile_classification;
CREATE TRIGGER trg_retrieval_active_classification_immutable
BEFORE UPDATE OR DELETE ON retrieval.existing_profile_classification
FOR EACH ROW EXECUTE FUNCTION retrieval.prevent_active_classification_mutation();

-- Current-profile or retrieval-input changes invalidate the active
-- classification corpus before cascades can mutate its result rows.  The
-- backfill promoter takes this same advisory lock when it checks and enables
-- a configuration.
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
    IF NOT pg_try_advisory_xact_lock(
        hashtextextended('pre-review-existing-kb-current-and-classification-v1', 0)
    ) THEN
        RAISE EXCEPTION 'CLASSIFICATION_INVALIDATION_LOCK_UNAVAILABLE'
            USING ERRCODE = '40001';
    END IF;

    IF TG_TABLE_NAME = 'notice' THEN
        SELECT EXISTS (
            SELECT 1
              FROM kb.source_profile AS source_profile
              JOIN kb.source_version AS source
                ON source.source_profile_pk = source_profile.source_profile_pk
              JOIN kb.profile_version AS profile
                ON profile.source_version_pk = source.source_version_pk
             WHERE source_profile.notice_pk = NEW.notice_pk
               AND source.is_current AND profile.is_current
        ) INTO v_invalidates;
    ELSIF TG_TABLE_NAME = 'source_profile' THEN
        SELECT EXISTS (
            SELECT 1
              FROM kb.source_version AS source
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
            SELECT 1
              FROM kb.profile_version AS profile
              JOIN kb.source_version AS source
                ON source.source_version_pk = profile.source_version_pk
             WHERE profile.profile_version_pk = ANY(v_touched_profile_pks)
               AND profile.is_current
               AND source.is_current
        ) INTO v_invalidates;
    ELSIF TG_OP = 'INSERT' THEN
        v_invalidates := NEW.is_current;
    ELSIF TG_OP = 'DELETE' THEN
        v_invalidates := OLD.is_current;
    ELSE
        -- Any write to a current parent can change the assembled Model 1
        -- input or the current corpus.  Demote conservatively.
        v_invalidates := OLD.is_current OR NEW.is_current;
    END IF;

    IF v_invalidates THEN
        UPDATE retrieval.classification_configuration
           SET is_active = FALSE
         WHERE is_active;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_kb_source_version_classification_activation
    ON kb.source_version;
CREATE TRIGGER trg_kb_source_version_classification_activation
BEFORE INSERT OR UPDATE OR DELETE ON kb.source_version
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();

DROP TRIGGER IF EXISTS trg_kb_profile_version_classification_activation
    ON kb.profile_version;
CREATE TRIGGER trg_kb_profile_version_classification_activation
BEFORE INSERT OR UPDATE OR DELETE ON kb.profile_version
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();

DROP TRIGGER IF EXISTS trg_kb_support_component_classification_activation
    ON kb.support_component;

DROP TRIGGER IF EXISTS trg_kb_fact_occurrence_classification_activation
    ON kb.fact_occurrence;
CREATE TRIGGER trg_kb_fact_occurrence_classification_activation
BEFORE INSERT OR UPDATE OR DELETE ON kb.fact_occurrence
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();

DROP TRIGGER IF EXISTS trg_kb_notice_classification_title_activation ON kb.notice;
CREATE TRIGGER trg_kb_notice_classification_title_activation
BEFORE UPDATE OF portal_metadata ON kb.notice
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();
DROP TRIGGER IF EXISTS trg_kb_source_profile_classification_notice_activation ON kb.source_profile;
CREATE TRIGGER trg_kb_source_profile_classification_notice_activation
BEFORE UPDATE OF notice_pk ON kb.source_profile
FOR EACH ROW EXECUTE FUNCTION retrieval.invalidate_active_classification_on_kb_change();

-- Service-only effective projection.  The raw label is retained for audit,
-- but 판단보류 deliberately becomes an unavailable effective label with a
-- stable reason code for Model 2/3 and retrieval consumers.
-- Keep the m32 signature here too: ledgerless replay must never commit a
-- legacy four-column SECURITY INVOKER projection between m31 and m32.
DROP FUNCTION IF EXISTS retrieval.get_active_existing_profile_classifications();
CREATE OR REPLACE FUNCTION retrieval.get_active_existing_profile_classifications()
RETURNS TABLE (
    profile_version_pk UUID,
    classification_config_pk UUID,
    model_id TEXT,
    artifact_sha256 TEXT,
    runtime_manifest_sha256 TEXT,
    input_assembly_version TEXT,
    confidence DOUBLE PRECISION,
    prediction_status TEXT,
    support_type TEXT,
    effective_status TEXT,
    effective_reason_code TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, retrieval, kb
AS $$
    SELECT
        classification.profile_version_pk,
        config.classification_config_pk,
        config.model_id,
        config.artifact_sha256,
        config.runtime_manifest_sha256,
        config.input_assembly_version,
        classification.confidence,
        classification.prediction_status,
        CASE
            WHEN classification.execution_status = 'OK'
             AND classification.prediction_status <> '판단보류'
                THEN classification.support_type_pred
            ELSE NULL
        END AS support_type,
        CASE
            WHEN classification.execution_status = 'OK'
             AND classification.prediction_status <> '판단보류'
                THEN 'OK'
            ELSE 'UNAVAILABLE'
        END AS effective_status,
        CASE
            WHEN classification.execution_status = 'OK'
             AND classification.prediction_status = '판단보류'
                THEN 'PREDICTION_WITHHELD'
            WHEN classification.execution_status = 'OK' THEN NULL
            ELSE classification.reason_code
        END AS effective_reason_code
      FROM retrieval.existing_profile_classification AS classification
      JOIN retrieval.classification_configuration AS config
        ON config.classification_config_pk = classification.classification_config_pk
      JOIN kb.profile_version AS profile
        ON profile.profile_version_pk = classification.profile_version_pk
      JOIN kb.source_version AS source
        ON source.source_version_pk = profile.source_version_pk
     WHERE config.is_active
       AND profile.is_current
       AND source.is_current;
$$;

ALTER TABLE retrieval.classification_configuration ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval.existing_profile_classification ENABLE ROW LEVEL SECURITY;
GRANT USAGE ON SCHEMA retrieval TO service_role;
REVOKE ALL ON TABLE
    retrieval.classification_configuration,
    retrieval.existing_profile_classification
FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    retrieval.classification_configuration,
    retrieval.existing_profile_classification
TO service_role;

REVOKE ALL ON FUNCTION retrieval.get_active_existing_profile_classifications()
FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION retrieval.get_active_existing_profile_classifications()
TO service_role;
REVOKE ALL ON FUNCTION retrieval.assert_classification_configuration_complete()
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION retrieval.prevent_active_classification_mutation()
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION retrieval.invalidate_active_classification_on_kb_change()
FROM PUBLIC, anon, authenticated;

COMMENT ON TABLE retrieval.classification_configuration IS
    'Versioned Model 1 artifact/input configuration; activation requires a complete successful current Existing corpus.';
COMMENT ON TABLE retrieval.existing_profile_classification IS
    'Existing Profile Model 1 result. Raw 판단보류 labels are audit-only; consumers use retrieval.get_active_existing_profile_classifications().';

COMMIT;
