-- Slice 4b: 공고 버전 하나의 ExistingProfile v0.2 저장소.
--
-- schema.sql 에도 같은 정의를 둔다. document_parse_run.structured_content 는
-- 파서 산출물(Common IR) 자리고, 프로파일은 그 뒤 단계에서 만들어져 여러
-- 케이스가 재사용하는 공고 버전 단위 산출물이다.
--
-- source_profile_id 는 P2 에서 보존하기로 한 출처 식별자다 (hwp:... / pdf:...).
-- 같은 공고의 hwp 본과 pdf 본은 서로 다른 프로파일이므로 notice_id 로 접지
-- 않는다. 실패도 행으로 남긴다: 한 공고의 실패가 다른 공고의 프로파일을
-- 지우거나 덮지 않는다 (초안 §9.4).
--
-- 재실행 가능하다. schema.sql 적용 뒤에 이 파일을 적용한다.

CREATE TABLE IF NOT EXISTS sims.announcement_profile (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    announcement_version_id bigint NOT NULL
                            REFERENCES sims.announcement_version(id) ON DELETE CASCADE,
    source_profile_id   text NOT NULL,
    profile_schema_version text NOT NULL,
    producer_version    text NOT NULL,
    source_sha256_hex   text,
    model_profile       text,
    prompt_bundle_version text,
    status              text NOT NULL,
    profile_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
    diagnostics         jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_announcement_profile_generation
        UNIQUE NULLS NOT DISTINCT (
            announcement_version_id, source_profile_id, profile_schema_version,
            source_sha256_hex, producer_version, model_profile,
            prompt_bundle_version
        ),
    CONSTRAINT ck_announcement_profile_status
        CHECK (status IN ('OK', 'FAILED')),
    CONSTRAINT ck_announcement_profile_source_id_not_blank
        CHECK (btrim(source_profile_id) <> ''),
    CONSTRAINT ck_announcement_profile_schema_version_not_blank
        CHECK (btrim(profile_schema_version) <> ''),
    CONSTRAINT ck_announcement_profile_producer_version_not_blank
        CHECK (btrim(producer_version) <> ''),
    CONSTRAINT ck_announcement_profile_source_sha256
        CHECK (source_sha256_hex IS NULL OR source_sha256_hex ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_announcement_profile_model_profile
        CHECK (model_profile IS NULL OR btrim(model_profile) <> ''),
    CONSTRAINT ck_announcement_profile_prompt_bundle
        CHECK (prompt_bundle_version IS NULL OR btrim(prompt_bundle_version) <> ''),
    CONSTRAINT ck_announcement_profile_generation_metadata
        CHECK (
            model_profile IS NOT NULL
            AND btrim(model_profile) <> ''
            AND prompt_bundle_version IS NOT NULL
            AND btrim(prompt_bundle_version) <> ''
        ),
    CONSTRAINT ck_announcement_profile_ok_generation_lineage
        CHECK (
            status <> 'OK'
            OR (
                source_sha256_hex IS NOT NULL
                AND model_profile IS NOT NULL
                AND btrim(model_profile) <> ''
                AND prompt_bundle_version IS NOT NULL
                AND btrim(prompt_bundle_version) <> ''
            )
        )
);

CREATE INDEX IF NOT EXISTS ix_announcement_profile_version_status
    ON sims.announcement_profile (announcement_version_id, status);

-- Existing pre-lineage rows keep NULL for the fields that did not exist yet.
-- New worker writes always provide the exact generation key above.
ALTER TABLE sims.announcement_profile
    ADD COLUMN IF NOT EXISTS source_sha256_hex text,
    ADD COLUMN IF NOT EXISTS model_profile text,
    ADD COLUMN IF NOT EXISTS prompt_bundle_version text;

ALTER TABLE sims.announcement_profile
    DROP CONSTRAINT IF EXISTS uq_announcement_profile;
ALTER TABLE sims.announcement_profile
    DROP CONSTRAINT IF EXISTS uq_announcement_profile_generation;
ALTER TABLE sims.announcement_profile
    ADD CONSTRAINT uq_announcement_profile_generation
    UNIQUE NULLS NOT DISTINCT (
        announcement_version_id, source_profile_id, profile_schema_version,
        source_sha256_hex, producer_version, model_profile,
        prompt_bundle_version
    );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'sims.announcement_profile'::regclass
           AND conname = 'ck_announcement_profile_source_sha256'
    ) THEN
        ALTER TABLE sims.announcement_profile
            ADD CONSTRAINT ck_announcement_profile_source_sha256
            CHECK (source_sha256_hex IS NULL OR source_sha256_hex ~ '^[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'sims.announcement_profile'::regclass
           AND conname = 'ck_announcement_profile_model_profile'
    ) THEN
        ALTER TABLE sims.announcement_profile
            ADD CONSTRAINT ck_announcement_profile_model_profile
            CHECK (model_profile IS NULL OR btrim(model_profile) <> '');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'sims.announcement_profile'::regclass
           AND conname = 'ck_announcement_profile_prompt_bundle'
    ) THEN
        ALTER TABLE sims.announcement_profile
            ADD CONSTRAINT ck_announcement_profile_prompt_bundle
            CHECK (prompt_bundle_version IS NULL OR btrim(prompt_bundle_version) <> '');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'sims.announcement_profile'::regclass
           AND conname = 'ck_announcement_profile_generation_metadata'
    ) THEN
        ALTER TABLE sims.announcement_profile
            ADD CONSTRAINT ck_announcement_profile_generation_metadata
            CHECK (
                model_profile IS NOT NULL
                AND btrim(model_profile) <> ''
                AND prompt_bundle_version IS NOT NULL
                AND btrim(prompt_bundle_version) <> ''
            ) NOT VALID;
    END IF;
END
$$;

-- The old table may contain pre-lineage OK rows.  ``NOT VALID`` keeps those
-- historical rows readable while enforcing the lineage requirement for every
-- new/updated OK row.  Fresh schema.sql installs the same CHECK as validated.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'sims.announcement_profile'::regclass
           AND conname = 'ck_announcement_profile_ok_generation_lineage'
    ) THEN
        ALTER TABLE sims.announcement_profile
            ADD CONSTRAINT ck_announcement_profile_ok_generation_lineage
            CHECK (
                status <> 'OK'
                OR (
                    source_sha256_hex IS NOT NULL
                    AND model_profile IS NOT NULL
                    AND btrim(model_profile) <> ''
                    AND prompt_bundle_version IS NOT NULL
                    AND btrim(prompt_bundle_version) <> ''
                )
            ) NOT VALID;
    END IF;
END
$$;

-- Embeddings produced by the 4b profile path retain the exact profile row.
-- Legacy corpus embeddings remain valid with NULL and are not rewritten here.
ALTER TABLE sims.announcement_embedding
    ADD COLUMN IF NOT EXISTS announcement_profile_id bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'sims.announcement_embedding'::regclass
           AND (
               conname = 'fk_announcement_embedding_profile_lineage'
               OR pg_get_constraintdef(oid) LIKE
                  'FOREIGN KEY (announcement_profile_id)%'
           )
    ) THEN
        ALTER TABLE sims.announcement_embedding
            ADD CONSTRAINT fk_announcement_embedding_profile_lineage
            FOREIGN KEY (announcement_profile_id)
            REFERENCES sims.announcement_profile(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_announcement_embedding_profile_lineage
    ON sims.announcement_embedding (announcement_profile_id, embedding_profile_id);
