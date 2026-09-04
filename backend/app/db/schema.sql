-- 사전협의 Pre-review MVP
-- PostgreSQL + pgvector consolidated physical schema v2.1
-- 기준일: 2026-08-21
-- Target: PostgreSQL 15+ / pgvector 0.7+
-- Fresh-install DDL. This file includes successful password-change auditing
-- and password_changed_at for invalidating pre-change sessions.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE SCHEMA IF NOT EXISTS sims;
SET search_path TO sims, public;

-- -----------------------------------------------------------------------------
-- Common functions
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION sims.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- -----------------------------------------------------------------------------
-- 1. Authentication and inspection ownership
-- -----------------------------------------------------------------------------

CREATE TABLE sims.app_user (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    login_id            citext NOT NULL UNIQUE,
    email               citext NOT NULL UNIQUE,
    password_hash       text NOT NULL,
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    is_active           boolean NOT NULL DEFAULT true,
    last_login_at       timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_app_user_login_id_not_blank
        CHECK (btrim(login_id::text) <> ''),
    CONSTRAINT ck_app_user_email_not_blank
        CHECK (btrim(email::text) <> ''),
    CONSTRAINT ck_app_user_password_hash_not_blank
        CHECK (btrim(password_hash) <> '')
);

CREATE TRIGGER trg_app_user_updated_at
BEFORE UPDATE ON sims.app_user
FOR EACH ROW EXECUTE FUNCTION sims.set_updated_at();

CREATE TABLE sims.password_change_history (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             bigint NOT NULL
                            REFERENCES sims.app_user(id) ON DELETE CASCADE,
    changed_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_password_change_history_user_changed
    ON sims.password_change_history (user_id, changed_at DESC);

CREATE OR REPLACE FUNCTION sims.record_successful_password_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN
        NEW.password_changed_at := statement_timestamp();

        INSERT INTO sims.password_change_history (user_id, changed_at)
        VALUES (OLD.id, NEW.password_changed_at);
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_app_user_password_change_history
BEFORE UPDATE OF password_hash ON sims.app_user
FOR EACH ROW EXECUTE FUNCTION sims.record_successful_password_change();

CREATE TABLE sims.inspection_case (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id       bigint NOT NULL
                            REFERENCES sims.app_user(id) ON DELETE CASCADE,
    status              text NOT NULL DEFAULT 'UPLOADED',
    top_k_used          smallint NOT NULL DEFAULT 5,
    failure_code        text,
    failure_message     text,
    result_frozen_at    timestamptz,
    completed_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_inspection_case_status
        CHECK (status IN (
            'UPLOADED', 'PARSING', 'CHECKING', 'RETRIEVING',
            'REPORTING', 'COMPLETED', 'FAILED'
        )),
    CONSTRAINT ck_inspection_case_top_k
        CHECK (top_k_used BETWEEN 1 AND 100),
    CONSTRAINT ck_inspection_case_completed
        CHECK (
            (status = 'COMPLETED' AND completed_at IS NOT NULL AND result_frozen_at IS NOT NULL)
            OR status <> 'COMPLETED'
        ),
    CONSTRAINT uq_inspection_case_id_owner
        UNIQUE (id, owner_user_id)
);

CREATE INDEX ix_inspection_case_owner_created
    ON sims.inspection_case (owner_user_id, created_at DESC);

CREATE INDEX ix_inspection_case_owner_status
    ON sims.inspection_case (owner_user_id, status);

CREATE TRIGGER trg_inspection_case_updated_at
BEFORE UPDATE ON sims.inspection_case
FOR EACH ROW EXECUTE FUNCTION sims.set_updated_at();

-- -----------------------------------------------------------------------------
-- 2. File assets and physical deletion outbox
-- -----------------------------------------------------------------------------

CREATE TABLE sims.file_asset (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_scope         text NOT NULL,
    owner_user_id       bigint
                            REFERENCES sims.app_user(id) ON DELETE CASCADE,
    inspection_case_id  bigint
                            REFERENCES sims.inspection_case(id) ON DELETE CASCADE,
    storage_key         text NOT NULL UNIQUE,
    original_filename   text NOT NULL,
    detected_mime_type  text,
    extension           text,
    size_bytes          bigint,
    sha256_hex          char(64),
    source_url          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_file_asset_scope
        CHECK (asset_scope IN ('USER', 'SHARED')),
    CONSTRAINT ck_file_asset_scope_owner
        CHECK (
            (asset_scope = 'USER' AND owner_user_id IS NOT NULL AND inspection_case_id IS NOT NULL)
            OR
            (asset_scope = 'SHARED' AND owner_user_id IS NULL AND inspection_case_id IS NULL)
        ),
    CONSTRAINT ck_file_asset_filename_not_blank
        CHECK (btrim(original_filename) <> ''),
    CONSTRAINT ck_file_asset_size
        CHECK (size_bytes IS NULL OR size_bytes >= 0),
    CONSTRAINT ck_file_asset_sha256
        CHECK (sha256_hex IS NULL OR sha256_hex ~ '^[0-9a-fA-F]{64}$'),
    CONSTRAINT uq_file_asset_id_scope
        UNIQUE (id, asset_scope),
    CONSTRAINT fk_file_asset_case_owner_consistent
        FOREIGN KEY (inspection_case_id, owner_user_id)
        REFERENCES sims.inspection_case (id, owner_user_id)
        ON DELETE CASCADE
);

CREATE INDEX ix_file_asset_case_fk
    ON sims.file_asset (inspection_case_id);

CREATE INDEX ix_file_asset_owner_fk
    ON sims.file_asset (owner_user_id);

CREATE INDEX ix_file_asset_user_case
    ON sims.file_asset (owner_user_id, inspection_case_id)
    WHERE asset_scope = 'USER';

CREATE INDEX ix_file_asset_shared_hash
    ON sims.file_asset (sha256_hex)
    WHERE asset_scope = 'SHARED' AND sha256_hex IS NOT NULL;

CREATE TABLE sims.object_delete_outbox (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    storage_key         text NOT NULL UNIQUE,
    requested_at        timestamptz NOT NULL DEFAULT now(),
    processed_at        timestamptz,
    attempt_count       integer NOT NULL DEFAULT 0,
    last_error          text,
    CONSTRAINT ck_object_delete_attempt_count
        CHECK (attempt_count >= 0)
);

CREATE INDEX ix_object_delete_outbox_pending
    ON sims.object_delete_outbox (requested_at)
    WHERE processed_at IS NULL;

CREATE OR REPLACE FUNCTION sims.enqueue_file_asset_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO sims.object_delete_outbox (storage_key)
    VALUES (OLD.storage_key)
    ON CONFLICT (storage_key) DO NOTHING;
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_file_asset_delete_outbox
AFTER DELETE ON sims.file_asset
FOR EACH ROW EXECUTE FUNCTION sims.enqueue_file_asset_delete();

CREATE OR REPLACE FUNCTION sims.prevent_storage_key_reuse()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM sims.object_delete_outbox o
         WHERE o.storage_key = NEW.storage_key
    ) THEN
        RAISE EXCEPTION
            'storage_key % was previously deleted and cannot be reused',
            NEW.storage_key;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_file_asset_storage_key_no_reuse
BEFORE INSERT OR UPDATE OF storage_key ON sims.file_asset
FOR EACH ROW EXECUTE FUNCTION sims.prevent_storage_key_reuse();

CREATE TABLE sims.uploaded_document (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_case_id  bigint NOT NULL UNIQUE
                            REFERENCES sims.inspection_case(id) ON DELETE CASCADE,
    file_asset_id       bigint NOT NULL UNIQUE
                            REFERENCES sims.file_asset(id) ON DELETE CASCADE,
    asset_scope         text NOT NULL DEFAULT 'USER',
    declared_format     text NOT NULL,
    uploaded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_uploaded_document_format
        CHECK (declared_format IN ('HWP', 'HWPX', 'PDF')),
    CONSTRAINT ck_uploaded_document_scope
        CHECK (asset_scope = 'USER'),
    CONSTRAINT fk_uploaded_document_asset_scope
        FOREIGN KEY (file_asset_id, asset_scope)
        REFERENCES sims.file_asset (id, asset_scope)
        ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- 3. Parsing, OCR and chunks
-- -----------------------------------------------------------------------------

CREATE TABLE sims.document_parse_run (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_asset_id       bigint NOT NULL
                            REFERENCES sims.file_asset(id) ON DELETE CASCADE,
    attempt_no          integer NOT NULL,
    parser_name         text NOT NULL,
    parser_version      text NOT NULL,
    status              text NOT NULL DEFAULT 'PENDING',
    used_ocr            boolean NOT NULL DEFAULT false,
    ocr_confidence      numeric(5,4),
    extracted_text      text,
    structured_content  jsonb NOT NULL DEFAULT '{}'::jsonb,
    text_sha256_hex     char(64),
    error_code          text,
    error_message       text,
    started_at          timestamptz,
    finished_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_parse_attempt
        UNIQUE (file_asset_id, attempt_no),
    CONSTRAINT ck_document_parse_attempt_no
        CHECK (attempt_no > 0),
    CONSTRAINT ck_document_parse_status
        CHECK (status IN (
            'PENDING', 'PARSING', 'SUCCESS', 'PARTIAL_SUCCESS', 'FAILED'
        )),
    CONSTRAINT ck_document_parse_ocr_confidence
        CHECK (ocr_confidence IS NULL OR ocr_confidence BETWEEN 0 AND 1),
    CONSTRAINT ck_document_parse_text_sha256
        CHECK (text_sha256_hex IS NULL OR text_sha256_hex ~ '^[0-9a-fA-F]{64}$'),
    CONSTRAINT ck_document_parse_finished
        CHECK (
            (status IN ('SUCCESS', 'PARTIAL_SUCCESS', 'FAILED') AND finished_at IS NOT NULL)
            OR status IN ('PENDING', 'PARSING')
        )
);

CREATE INDEX ix_document_parse_file_status
    ON sims.document_parse_run (file_asset_id, status, attempt_no DESC);

CREATE TABLE sims.chunking_profile (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_name        text NOT NULL,
    version_no          integer NOT NULL,
    strategy            text NOT NULL,
    configuration       jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active           boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_chunking_profile
        UNIQUE (profile_name, version_no),
    CONSTRAINT ck_chunking_profile_version
        CHECK (version_no > 0)
);

CREATE TABLE sims.document_chunk_set (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parse_run_id        bigint NOT NULL
                            REFERENCES sims.document_parse_run(id) ON DELETE CASCADE,
    chunking_profile_id bigint NOT NULL
                            REFERENCES sims.chunking_profile(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_chunk_set
        UNIQUE (parse_run_id, chunking_profile_id)
);

CREATE TABLE sims.document_chunk (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chunk_set_id        bigint NOT NULL
                            REFERENCES sims.document_chunk_set(id) ON DELETE CASCADE,
    chunk_no            integer NOT NULL,
    content             text NOT NULL,
    content_sha256_hex  char(64) NOT NULL,
    page_no             integer,
    section_name        text,
    source_locator      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_chunk_no
        UNIQUE (chunk_set_id, chunk_no),
    CONSTRAINT ck_document_chunk_no
        CHECK (chunk_no >= 0),
    CONSTRAINT ck_document_chunk_content_not_blank
        CHECK (btrim(content) <> ''),
    CONSTRAINT ck_document_chunk_sha256
        CHECK (content_sha256_hex ~ '^[0-9a-fA-F]{64}$'),
    CONSTRAINT ck_document_chunk_page
        CHECK (page_no IS NULL OR page_no > 0)
);

CREATE INDEX ix_document_chunk_set
    ON sims.document_chunk (chunk_set_id, chunk_no);

CREATE INDEX ix_document_chunk_fts
    ON sims.document_chunk
    USING gin (to_tsvector('simple', content));

-- -----------------------------------------------------------------------------
-- 4. Form schema, extracted fields and missing checks
-- -----------------------------------------------------------------------------

CREATE TABLE sims.form_schema (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    schema_name         text NOT NULL,
    version_no          integer NOT NULL,
    source_file_asset_id bigint
                            REFERENCES sims.file_asset(id) ON DELETE SET NULL,
    effective_from      date,
    description         text,
    is_active           boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_form_schema
        UNIQUE (schema_name, version_no),
    CONSTRAINT ck_form_schema_version
        CHECK (version_no > 0)
);

CREATE TABLE sims.form_field_definition (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    form_schema_id      bigint NOT NULL
                            REFERENCES sims.form_schema(id) ON DELETE CASCADE,
    field_code          text NOT NULL,
    field_label         text NOT NULL,
    parent_field_code   text,
    data_type           text NOT NULL DEFAULT 'TEXT',
    required_rule       jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_sensitive        boolean NOT NULL DEFAULT false,
    display_order       integer NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_form_field_code
        UNIQUE (form_schema_id, field_code),
    CONSTRAINT ck_form_field_data_type
        CHECK (data_type IN ('TEXT', 'DATE', 'NUMBER', 'BOOLEAN', 'CHOICE', 'JSON')),
    CONSTRAINT ck_form_field_display_order
        CHECK (display_order >= 0)
);

CREATE INDEX ix_form_field_order
    ON sims.form_field_definition (form_schema_id, display_order);

CREATE TABLE sims.request_extraction (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_case_id  bigint NOT NULL UNIQUE
                            REFERENCES sims.inspection_case(id) ON DELETE CASCADE,
    form_schema_id      bigint NOT NULL
                            REFERENCES sims.form_schema(id),
    parse_run_id        bigint NOT NULL
                            REFERENCES sims.document_parse_run(id) ON DELETE CASCADE,
    request_reason      text NOT NULL DEFAULT 'UNKNOWN',
    status              text NOT NULL DEFAULT 'PENDING',
    extractor_name      text NOT NULL,
    extractor_version   text NOT NULL,
    confidence          numeric(5,4),
    raw_extraction      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    CONSTRAINT ck_request_extraction_reason
        CHECK (request_reason IN (
            'DETAIL_NEW', 'SUBPROGRAM_NEW', 'SUBSUBPROGRAM_NEW',
            'CONTENT_CHANGE', 'UNKNOWN'
        )),
    CONSTRAINT ck_request_extraction_status
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCESS', 'PARTIAL_SUCCESS', 'FAILED')),
    CONSTRAINT ck_request_extraction_confidence
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE sims.request_field_value (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_extraction_id bigint NOT NULL
                            REFERENCES sims.request_extraction(id) ON DELETE CASCADE,
    field_definition_id bigint NOT NULL
                            REFERENCES sims.form_field_definition(id),
    raw_text            text,
    normalized_value    jsonb,
    confidence          numeric(5,4),
    page_no             integer,
    source_bbox         jsonb,
    source_locator      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_request_field_value
        UNIQUE (request_extraction_id, field_definition_id),
    CONSTRAINT ck_request_field_confidence
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    CONSTRAINT ck_request_field_page
        CHECK (page_no IS NULL OR page_no > 0)
);

CREATE TABLE sims.missing_check_run (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_case_id  bigint NOT NULL
                            REFERENCES sims.inspection_case(id) ON DELETE CASCADE,
    request_extraction_id bigint NOT NULL
                            REFERENCES sims.request_extraction(id) ON DELETE CASCADE,
    ruleset_version     text NOT NULL,
    status              text NOT NULL DEFAULT 'PENDING',
    started_at          timestamptz,
    completed_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_missing_check_run_status
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED'))
);

CREATE INDEX ix_missing_check_case_created
    ON sims.missing_check_run (inspection_case_id, created_at DESC);

CREATE TABLE sims.missing_check_item (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    missing_check_run_id bigint NOT NULL
                            REFERENCES sims.missing_check_run(id) ON DELETE CASCADE,
    field_definition_id bigint NOT NULL
                            REFERENCES sims.form_field_definition(id),
    evidence_field_value_id bigint
                            REFERENCES sims.request_field_value(id) ON DELETE SET NULL,
    result_status       text NOT NULL,
    reason_code         text,
    explanation         text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_missing_check_item
        UNIQUE (missing_check_run_id, field_definition_id),
    CONSTRAINT ck_missing_check_item_status
        CHECK (result_status IN (
            'PRESENT', 'MISSING', 'NOT_APPLICABLE',
            'NEEDS_CONFIRMATION', 'PARSE_FAILED'
        ))
);

-- -----------------------------------------------------------------------------
-- 5. Data sources, daily synchronization and announcement versions
-- -----------------------------------------------------------------------------

CREATE TABLE sims.data_source (
    source_code         text PRIMARY KEY,
    source_name         text NOT NULL,
    source_type         text NOT NULL,
    is_search_source    boolean NOT NULL DEFAULT false,
    description         text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_data_source_type
        CHECK (source_type IN ('API', 'FILE'))
);

CREATE TABLE sims.api_sync_run (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_code         text NOT NULL
                            REFERENCES sims.data_source(source_code),
    sync_date_kst       date NOT NULL,
    status              text NOT NULL DEFAULT 'PENDING',
    attempt_count       integer NOT NULL DEFAULT 1,
    is_initial_load     boolean NOT NULL DEFAULT false,
    started_at          timestamptz,
    completed_at        timestamptz,
    latest_source_created_at timestamptz,
    resume_cursor       text,
    rows_fetched        integer NOT NULL DEFAULT 0,
    rows_inserted       integer NOT NULL DEFAULT 0,
    rows_versioned      integer NOT NULL DEFAULT 0,
    rows_unchanged      integer NOT NULL DEFAULT 0,
    error_code          text,
    error_message       text,
    statistics          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_api_sync_day
        UNIQUE (source_code, sync_date_kst),
    CONSTRAINT ck_api_sync_status
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_api_sync_counts
        CHECK (
            attempt_count > 0
            AND rows_fetched >= 0
            AND rows_inserted >= 0
            AND rows_versioned >= 0
            AND rows_unchanged >= 0
        )
);

CREATE INDEX ix_api_sync_last_success
    ON sims.api_sync_run (source_code, sync_date_kst DESC)
    WHERE status = 'SUCCEEDED';

CREATE TABLE sims.announcement (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_code         text NOT NULL
                            REFERENCES sims.data_source(source_code),
    pblanc_id           text NOT NULL UNIQUE,
    first_seen_at       timestamptz NOT NULL,
    last_seen_at        timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_announcement_pblanc_id_not_blank
        CHECK (btrim(pblanc_id) <> ''),
    CONSTRAINT ck_announcement_seen_order
        CHECK (last_seen_at >= first_seen_at)
);

CREATE TABLE sims.announcement_version (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    announcement_id     bigint NOT NULL
                            REFERENCES sims.announcement(id),
    source_sync_run_id  bigint
                            REFERENCES sims.api_sync_run(id) ON DELETE SET NULL,
    version_no          integer NOT NULL,
    content_sha256_hex  char(64) NOT NULL,
    is_current          boolean NOT NULL DEFAULT true,
    valid_from          timestamptz NOT NULL DEFAULT now(),
    valid_to            timestamptz,

    pblanc_nm           text NOT NULL,
    pblanc_url          text NOT NULL,
    jrsd_instt_nm       text,
    exc_instt_nm        text,
    bsns_sumry_html     text,
    bsns_sumry_text     text NOT NULL,
    purpose             text NOT NULL,
    target              text NOT NULL,
    content             text NOT NULL,
    detail_ref_fields   text[] NOT NULL DEFAULT '{}'::text[],
    has_detail_ref      boolean
                            GENERATED ALWAYS AS
                            (cardinality(detail_ref_fields) > 0) STORED,
    category_name       text,
    source_created_at   timestamptz NOT NULL,
    source_updated_at   timestamptz,
    target_name         text,
    view_count          bigint,
    hashtags            text[] NOT NULL DEFAULT '{}'::text[],
    request_method_papers text,
    reference_contact   text,
    receipt_homepage_url text,

    period_raw_text     text NOT NULL,
    period_type         text NOT NULL,
    period_start_date   date,
    period_end_date     date,
    period_display_text text NOT NULL,
    search_status       text NOT NULL DEFAULT 'UNKNOWN',
    status_checked_at   timestamptz,
    status_source       text,

    raw_payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_announcement_version_no
        UNIQUE (announcement_id, version_no),
    CONSTRAINT ck_announcement_version_no
        CHECK (version_no > 0),
    CONSTRAINT ck_announcement_version_sha256
        CHECK (content_sha256_hex ~ '^[0-9a-fA-F]{64}$'),
    CONSTRAINT ck_announcement_version_validity
        CHECK (
            (is_current AND valid_to IS NULL)
            OR
            (NOT is_current AND valid_to IS NOT NULL AND valid_to >= valid_from)
        ),
    CONSTRAINT ck_announcement_period_type
        CHECK (period_type IN (
            'FIXED', 'UNTIL_EXHAUSTED', 'ALWAYS', 'UNTIL_FILLED',
            'BY_SUBPROGRAM', 'VARIABLE', 'UNKNOWN'
        )),
    CONSTRAINT ck_announcement_period_order
        CHECK (
            period_start_date IS NULL
            OR period_end_date IS NULL
            OR period_end_date >= period_start_date
        ),
    CONSTRAINT ck_announcement_fixed_period
        CHECK (
            period_type <> 'FIXED'
            OR (period_start_date IS NOT NULL AND period_end_date IS NOT NULL)
        ),
    CONSTRAINT ck_announcement_search_status
        CHECK (search_status IN ('OPEN', 'CLOSED', 'UNKNOWN')),
    CONSTRAINT ck_announcement_view_count
        CHECK (view_count IS NULL OR view_count >= 0),
    CONSTRAINT ck_announcement_detail_ref_fields
        CHECK (
            array_position(detail_ref_fields, NULL) IS NULL
            AND detail_ref_fields <@ ARRAY['target', 'content']::text[]
        )
);

CREATE TRIGGER trg_announcement_version_updated_at
BEFORE UPDATE ON sims.announcement_version
FOR EACH ROW EXECUTE FUNCTION sims.set_updated_at();

CREATE UNIQUE INDEX uq_announcement_one_current_version
    ON sims.announcement_version (announcement_id)
    WHERE is_current;

CREATE INDEX ix_announcement_version_hash
    ON sims.announcement_version (announcement_id, content_sha256_hex);

CREATE INDEX ix_announcement_version_sync
    ON sims.announcement_version (source_sync_run_id);

CREATE INDEX ix_announcement_version_status_period
    ON sims.announcement_version (search_status, period_end_date, source_created_at DESC)
    WHERE is_current;

CREATE INDEX ix_announcement_version_created
    ON sims.announcement_version (source_created_at DESC)
    WHERE is_current;

CREATE INDEX ix_announcement_version_jurisdiction
    ON sims.announcement_version (jrsd_instt_nm)
    WHERE is_current;

CREATE INDEX ix_announcement_version_executor
    ON sims.announcement_version (exc_instt_nm)
    WHERE is_current;

CREATE INDEX ix_announcement_version_hashtags
    ON sims.announcement_version USING gin (hashtags)
    WHERE is_current;

CREATE INDEX ix_announcement_version_summary_fts
    ON sims.announcement_version
    USING gin (to_tsvector('simple', bsns_sumry_text))
    WHERE is_current;

CREATE TABLE sims.announcement_attachment (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    announcement_version_id bigint NOT NULL
                            REFERENCES sims.announcement_version(id) ON DELETE CASCADE,
    attachment_role     text NOT NULL,
    ordinal_no          integer NOT NULL,
    source_url          text NOT NULL,
    original_filename   text NOT NULL,
    extension           text,
    detected_mime_type  text,
    fetch_status        text NOT NULL DEFAULT 'NOT_REQUESTED',
    file_asset_id       bigint
                            REFERENCES sims.file_asset(id) ON DELETE SET NULL,
    last_fetch_error    text,
    fetched_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_announcement_attachment
        UNIQUE (announcement_version_id, attachment_role, ordinal_no),
    CONSTRAINT ck_announcement_attachment_role
        CHECK (attachment_role IN ('PRIMARY', 'AUXILIARY')),
    CONSTRAINT ck_announcement_attachment_ordinal
        CHECK (ordinal_no >= 0),
    CONSTRAINT ck_announcement_attachment_fetch
        CHECK (fetch_status IN (
            'NOT_REQUESTED', 'DOWNLOADING', 'DOWNLOADED', 'FAILED'
        ))
);

CREATE INDEX ix_announcement_attachment_fetch
    ON sims.announcement_attachment (fetch_status, announcement_version_id);

CREATE TABLE sims.announcement_subprogram_period (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    announcement_version_id bigint NOT NULL
                            REFERENCES sims.announcement_version(id) ON DELETE CASCADE,
    subprogram_name     text NOT NULL,
    start_date          date,
    end_date            date,
    raw_period_text     text NOT NULL,
    source_page         integer,
    source_locator      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_subprogram_period_order
        CHECK (start_date IS NULL OR end_date IS NULL OR end_date >= start_date),
    CONSTRAINT ck_subprogram_source_page
        CHECK (source_page IS NULL OR source_page > 0)
);

CREATE INDEX ix_subprogram_period_announcement
    ON sims.announcement_subprogram_period (announcement_version_id, start_date, end_date);

-- -----------------------------------------------------------------------------
-- 6. Storage-only historical sources (not used for retrieval)
-- -----------------------------------------------------------------------------

CREATE TABLE sims.archive_import_batch (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_code         text NOT NULL
                            REFERENCES sims.data_source(source_code),
    source_file_asset_id bigint
                            REFERENCES sims.file_asset(id) ON DELETE SET NULL,
    status              text NOT NULL DEFAULT 'PENDING',
    expected_rows       integer,
    imported_rows       integer NOT NULL DEFAULT 0,
    error_rows          integer NOT NULL DEFAULT 0,
    imported_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_archive_import_status
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_archive_import_counts
        CHECK (
            (expected_rows IS NULL OR expected_rows >= 0)
            AND imported_rows >= 0
            AND error_rows >= 0
        )
);

CREATE TABLE sims.archive_bizinfo_listing (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_batch_id     bigint NOT NULL
                            REFERENCES sims.archive_import_batch(id) ON DELETE CASCADE,
    source_row_no       integer NOT NULL,
    list_no             bigint,
    category_name       text,
    program_name        text NOT NULL,
    application_start_date date,
    application_end_date date,
    jurisdiction_org    text,
    executing_org       text,
    registered_date     date,
    detail_url          text,
    parsed_pblanc_id    text,
    raw_row             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_archive_bizinfo_row
        UNIQUE (import_batch_id, source_row_no),
    CONSTRAINT ck_archive_bizinfo_row_no
        CHECK (source_row_no > 0),
    CONSTRAINT ck_archive_bizinfo_period
        CHECK (
            application_start_date IS NULL
            OR application_end_date IS NULL
            OR application_end_date >= application_start_date
        )
);

CREATE INDEX ix_archive_bizinfo_registered
    ON sims.archive_bizinfo_listing (registered_date DESC);

CREATE INDEX ix_archive_bizinfo_pblanc
    ON sims.archive_bizinfo_listing (parsed_pblanc_id)
    WHERE parsed_pblanc_id IS NOT NULL;

CREATE TABLE sims.archive_central_program (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_batch_id     bigint NOT NULL
                            REFERENCES sims.archive_import_batch(id) ON DELETE CASCADE,
    source_row_no       integer NOT NULL,
    ministry_name       text NOT NULL,
    category_large      text,
    category_middle     text,
    industry_name       text,
    executing_org       text,
    announcement_url    text,
    program_type        text,
    program_name        text NOT NULL,
    purpose             text,
    content             text,
    target              text,
    scale_text          text,
    description         text,
    raw_row             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_archive_central_row
        UNIQUE (import_batch_id, source_row_no),
    CONSTRAINT ck_archive_central_row_no
        CHECK (source_row_no > 0)
);

CREATE INDEX ix_archive_central_ministry
    ON sims.archive_central_program (ministry_name);

-- -----------------------------------------------------------------------------
-- 7. Embedding models, profiles and vectors
-- -----------------------------------------------------------------------------

CREATE TABLE sims.embedding_model (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider            text NOT NULL,
    model_name          text NOT NULL,
    model_version       text NOT NULL DEFAULT '',
    dimension           integer NOT NULL,
    distance_metric     text NOT NULL DEFAULT 'COSINE',
    is_enabled          boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_embedding_model
        UNIQUE (provider, model_name, model_version, dimension),
    CONSTRAINT ck_embedding_model_dimension
        CHECK (dimension > 0),
    CONSTRAINT ck_embedding_model_metric
        CHECK (distance_metric IN ('COSINE', 'L2', 'INNER_PRODUCT'))
);

CREATE TABLE sims.embedding_profile (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    embedding_model_id  bigint NOT NULL
                            REFERENCES sims.embedding_model(id),
    profile_name        text NOT NULL,
    version_no          integer NOT NULL,
    profile_kind        text NOT NULL,
    field_codes         text[] NOT NULL DEFAULT '{}'::text[],
    input_template      text,
    chunking_profile_id bigint
                            REFERENCES sims.chunking_profile(id),
    configuration       jsonb NOT NULL DEFAULT '{}'::jsonb,
    preprocessing_version text NOT NULL DEFAULT 'detail-ref-v1',
    is_active           boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_embedding_profile
        UNIQUE (profile_name, version_no),
    CONSTRAINT ck_embedding_profile_version
        CHECK (version_no > 0),
    CONSTRAINT ck_embedding_profile_kind
        CHECK (profile_kind IN ('SUMMARY', 'CHUNK')),
    CONSTRAINT ck_embedding_profile_preprocessing_version_not_blank
        CHECK (btrim(preprocessing_version) <> '')
);

CREATE INDEX ix_embedding_profile_active
    ON sims.embedding_profile (profile_kind, is_active);

CREATE OR REPLACE FUNCTION sims.guard_embedding_model_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.model_name IS DISTINCT FROM OLD.model_name
       OR NEW.model_version IS DISTINCT FROM OLD.model_version
       OR NEW.dimension IS DISTINCT FROM OLD.dimension
       OR NEW.distance_metric IS DISTINCT FROM OLD.distance_metric THEN
        RAISE EXCEPTION
            'embedding model identity is immutable; create a new model row';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_embedding_model_identity_immutable
BEFORE UPDATE OF provider, model_name, model_version, dimension, distance_metric
ON sims.embedding_model
FOR EACH ROW EXECUTE FUNCTION sims.guard_embedding_model_identity();

CREATE OR REPLACE FUNCTION sims.guard_embedding_profile_definition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.embedding_model_id IS DISTINCT FROM OLD.embedding_model_id
       OR NEW.profile_name IS DISTINCT FROM OLD.profile_name
       OR NEW.version_no IS DISTINCT FROM OLD.version_no
       OR NEW.profile_kind IS DISTINCT FROM OLD.profile_kind
       OR NEW.field_codes IS DISTINCT FROM OLD.field_codes
       OR NEW.input_template IS DISTINCT FROM OLD.input_template
       OR NEW.chunking_profile_id IS DISTINCT FROM OLD.chunking_profile_id
       OR NEW.configuration IS DISTINCT FROM OLD.configuration
       OR NEW.preprocessing_version IS DISTINCT FROM OLD.preprocessing_version THEN
        RAISE EXCEPTION
            'embedding profile definition is immutable; create a new profile version';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_embedding_profile_definition_immutable
BEFORE UPDATE OF
    embedding_model_id,
    profile_name,
    version_no,
    profile_kind,
    field_codes,
    input_template,
    chunking_profile_id,
    configuration,
    preprocessing_version
ON sims.embedding_profile
FOR EACH ROW EXECUTE FUNCTION sims.guard_embedding_profile_definition();

CREATE TABLE sims.inspection_embedding (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_case_id  bigint NOT NULL
                            REFERENCES sims.inspection_case(id) ON DELETE CASCADE,
    embedding_profile_id bigint NOT NULL
                            REFERENCES sims.embedding_profile(id),
    input_text          text NOT NULL,
    input_sha256_hex    char(64) NOT NULL,
    embedding           vector NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_inspection_embedding
        UNIQUE (inspection_case_id, embedding_profile_id),
    CONSTRAINT ck_inspection_embedding_input_text_not_blank
        CHECK (btrim(input_text) <> ''),
    CONSTRAINT ck_inspection_embedding_sha256
        CHECK (input_sha256_hex ~ '^[0-9a-fA-F]{64}$')
);

CREATE INDEX ix_inspection_embedding_profile
    ON sims.inspection_embedding (embedding_profile_id, inspection_case_id);

CREATE TABLE sims.announcement_embedding (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    announcement_version_id bigint NOT NULL
                            REFERENCES sims.announcement_version(id) ON DELETE CASCADE,
    embedding_profile_id bigint NOT NULL
                            REFERENCES sims.embedding_profile(id),
    input_text          text NOT NULL,
    input_sha256_hex    char(64) NOT NULL,
    embedding           vector NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_announcement_embedding
        UNIQUE (announcement_version_id, embedding_profile_id),
    CONSTRAINT ck_announcement_embedding_input_text_not_blank
        CHECK (btrim(input_text) <> ''),
    CONSTRAINT ck_announcement_embedding_sha256
        CHECK (input_sha256_hex ~ '^[0-9a-fA-F]{64}$')
);

CREATE INDEX ix_announcement_embedding_profile
    ON sims.announcement_embedding (embedding_profile_id, announcement_version_id);

CREATE TABLE sims.chunk_embedding (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_chunk_id   bigint NOT NULL
                            REFERENCES sims.document_chunk(id) ON DELETE CASCADE,
    embedding_profile_id bigint NOT NULL
                            REFERENCES sims.embedding_profile(id),
    input_text          text NOT NULL,
    input_sha256_hex    char(64) NOT NULL,
    embedding           vector NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_chunk_embedding
        UNIQUE (document_chunk_id, embedding_profile_id),
    CONSTRAINT ck_chunk_embedding_input_text_not_blank
        CHECK (btrim(input_text) <> ''),
    CONSTRAINT ck_chunk_embedding_sha256
        CHECK (input_sha256_hex ~ '^[0-9a-fA-F]{64}$')
);

CREATE INDEX ix_chunk_embedding_profile
    ON sims.chunk_embedding (embedding_profile_id, document_chunk_id);

CREATE OR REPLACE FUNCTION sims.check_embedding_dimension()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    expected integer;
    actual   integer := vector_dims(NEW.embedding);
BEGIN
    SELECT m.dimension
      INTO expected
      FROM sims.embedding_profile p
      JOIN sims.embedding_model m ON m.id = p.embedding_model_id
     WHERE p.id = NEW.embedding_profile_id;

    IF expected IS NULL THEN
        RAISE EXCEPTION
            'embedding_profile % has no registered model dimension',
            NEW.embedding_profile_id;
    END IF;

    IF actual <> expected THEN
        RAISE EXCEPTION
            'embedding dimension mismatch: profile % expects %, got %',
            NEW.embedding_profile_id, expected, actual;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_inspection_embedding_dim
BEFORE INSERT OR UPDATE OF embedding, embedding_profile_id
ON sims.inspection_embedding
FOR EACH ROW EXECUTE FUNCTION sims.check_embedding_dimension();

CREATE TRIGGER trg_announcement_embedding_dim
BEFORE INSERT OR UPDATE OF embedding, embedding_profile_id
ON sims.announcement_embedding
FOR EACH ROW EXECUTE FUNCTION sims.check_embedding_dimension();

CREATE TRIGGER trg_chunk_embedding_dim
BEFORE INSERT OR UPDATE OF embedding, embedding_profile_id
ON sims.chunk_embedding
FOR EACH ROW EXECUTE FUNCTION sims.check_embedding_dimension();

-- HNSW indexes are deliberately not created in the MVP experiment baseline.
-- Create a model/profile-specific partial index only after the model dimension
-- and production profile are selected. The ORDER BY expression must match the
-- index cast expression. Supply the query vector as a bound parameter or as a
-- scalar subquery (InitPlan), not as a direct JOIN-column distance expression.
-- Example template:
--
-- CREATE INDEX ix_announcement_embedding_profile_1_hnsw
-- ON sims.announcement_embedding
-- USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
-- WHERE embedding_profile_id = 1;
--
-- SELECT ae.announcement_version_id,
--        ae.embedding::vector(1024) <=> (
--            SELECT ie.embedding::vector(1024)
--            FROM sims.inspection_embedding ie
--            WHERE ie.id = $1
--        ) AS distance
-- FROM sims.announcement_embedding ae
-- WHERE ae.embedding_profile_id = 1
-- ORDER BY ae.embedding::vector(1024) <=> (
--            SELECT ie.embedding::vector(1024)
--            FROM sims.inspection_embedding ie
--            WHERE ie.id = $1
--          )
-- LIMIT 5;

-- -----------------------------------------------------------------------------
-- 8. Retrieval runs, candidates and evidence
-- -----------------------------------------------------------------------------

CREATE TABLE sims.retrieval_run (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_case_id  bigint NOT NULL
                            REFERENCES sims.inspection_case(id) ON DELETE CASCADE,
    inspection_embedding_id bigint NOT NULL
                            REFERENCES sims.inspection_embedding(id) ON DELETE CASCADE,
    source_sync_run_id  bigint
                            REFERENCES sims.api_sync_run(id) ON DELETE SET NULL,
    status              text NOT NULL DEFAULT 'PENDING',
    top_k_used          smallint NOT NULL,
    corpus_snapshot_at  timestamptz NOT NULL,
    filter_snapshot     jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at          timestamptz,
    completed_at        timestamptz,
    error_code          text,
    error_message       text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_retrieval_run_status
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED')),
    CONSTRAINT ck_retrieval_run_top_k
        CHECK (top_k_used BETWEEN 1 AND 100)
);

CREATE INDEX ix_retrieval_run_case_created
    ON sims.retrieval_run (inspection_case_id, created_at DESC);

CREATE TABLE sims.retrieval_candidate (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    retrieval_run_id    bigint NOT NULL
                            REFERENCES sims.retrieval_run(id) ON DELETE CASCADE,
    announcement_version_id bigint NOT NULL
                            REFERENCES sims.announcement_version(id),
    rank_no             smallint NOT NULL,
    vector_distance     double precision NOT NULL,
    vector_similarity   double precision,
    status_verification text NOT NULL DEFAULT 'NEEDS_CONFIRMATION',
    is_presented        boolean NOT NULL DEFAULT true,
    same_jurisdiction_org boolean,
    same_executing_org  boolean,
    period_relation     text NOT NULL DEFAULT 'UNKNOWN',
    detail_parse_status text NOT NULL DEFAULT 'NOT_REQUESTED',
    comparison_summary  text,
    comparison_result   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_retrieval_candidate_rank
        UNIQUE (retrieval_run_id, rank_no),
    CONSTRAINT uq_retrieval_candidate_announcement
        UNIQUE (retrieval_run_id, announcement_version_id),
    CONSTRAINT ck_retrieval_candidate_rank
        CHECK (rank_no > 0),
    CONSTRAINT ck_retrieval_candidate_similarity
        CHECK (vector_similarity IS NULL OR vector_similarity BETWEEN -1 AND 1),
    CONSTRAINT ck_retrieval_candidate_status
        CHECK (status_verification IN (
            'VERIFIED_OPEN', 'VERIFIED_CLOSED', 'NEEDS_CONFIRMATION'
        )),
    CONSTRAINT ck_retrieval_candidate_period
        CHECK (period_relation IN (
            'OVERLAP', 'NO_OVERLAP', 'OPEN_ENDED', 'BY_SUBPROGRAM', 'UNKNOWN'
        )),
    CONSTRAINT ck_retrieval_candidate_parse
        CHECK (detail_parse_status IN (
            'NOT_REQUESTED', 'PARSING', 'SUCCESS', 'PARTIAL_SUCCESS', 'FAILED'
        ))
);

CREATE INDEX ix_retrieval_candidate_run_rank
    ON sims.retrieval_candidate (retrieval_run_id, rank_no);

CREATE INDEX ix_retrieval_candidate_announcement
    ON sims.retrieval_candidate (announcement_version_id);

CREATE TABLE sims.candidate_evidence (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    retrieval_candidate_id bigint NOT NULL
                            REFERENCES sims.retrieval_candidate(id) ON DELETE CASCADE,
    evidence_side       text NOT NULL,
    field_code          text,
    document_chunk_id   bigint
                            REFERENCES sims.document_chunk(id) ON DELETE SET NULL,
    page_no             integer,
    source_locator      jsonb NOT NULL DEFAULT '{}'::jsonb,
    excerpt             text,
    explanation         text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_candidate_evidence_side
        CHECK (evidence_side IN ('REQUEST', 'ANNOUNCEMENT', 'COMPARISON')),
    CONSTRAINT ck_candidate_evidence_page
        CHECK (page_no IS NULL OR page_no > 0)
);

CREATE INDEX ix_candidate_evidence_candidate
    ON sims.candidate_evidence (retrieval_candidate_id, evidence_side);

-- -----------------------------------------------------------------------------
-- 9. Immutable report, PDF output and post-result chat
-- -----------------------------------------------------------------------------

CREATE TABLE sims.inspection_report (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_case_id  bigint NOT NULL UNIQUE
                            REFERENCES sims.inspection_case(id) ON DELETE CASCADE,
    missing_check_run_id bigint NOT NULL
                            REFERENCES sims.missing_check_run(id) ON DELETE CASCADE,
    retrieval_run_id    bigint NOT NULL
                            REFERENCES sims.retrieval_run(id) ON DELETE CASCADE,
    report_schema_version text NOT NULL,
    report_json         jsonb NOT NULL,
    finalized_at        timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION sims.prevent_report_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'inspection_report is immutable; create a new inspection case instead';
END;
$$;

CREATE TRIGGER trg_inspection_report_no_update
BEFORE UPDATE ON sims.inspection_report
FOR EACH ROW EXECUTE FUNCTION sims.prevent_report_update();

CREATE TABLE sims.output_artifact (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_report_id bigint NOT NULL UNIQUE
                            REFERENCES sims.inspection_report(id) ON DELETE CASCADE,
    file_asset_id       bigint NOT NULL UNIQUE
                            REFERENCES sims.file_asset(id) ON DELETE CASCADE,
    output_format       text NOT NULL DEFAULT 'PDF',
    template_version    text NOT NULL,
    generated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_output_artifact_format
        CHECK (output_format = 'PDF')
);

CREATE TABLE sims.chat_session (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_case_id  bigint NOT NULL UNIQUE
                            REFERENCES sims.inspection_case(id) ON DELETE CASCADE,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sims.chat_message (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_session_id     bigint NOT NULL
                            REFERENCES sims.chat_session(id) ON DELETE CASCADE,
    sequence_no         integer NOT NULL,
    role                text NOT NULL,
    content             text NOT NULL,
    model_name          text,
    model_version       text,
    input_tokens        integer,
    output_tokens       integer,
    evidence_refs       jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_chat_message_sequence
        UNIQUE (chat_session_id, sequence_no),
    CONSTRAINT ck_chat_message_sequence
        CHECK (sequence_no > 0),
    CONSTRAINT ck_chat_message_role
        CHECK (role IN ('USER', 'ASSISTANT')),
    CONSTRAINT ck_chat_message_content_not_blank
        CHECK (btrim(content) <> ''),
    CONSTRAINT ck_chat_message_tokens
        CHECK (
            (input_tokens IS NULL OR input_tokens >= 0)
            AND (output_tokens IS NULL OR output_tokens >= 0)
        )
);

CREATE INDEX ix_chat_message_session_sequence
    ON sims.chat_message (chat_session_id, sequence_no);

-- -----------------------------------------------------------------------------
-- 9.5 Supporting indexes for foreign-key joins and cascades
-- -----------------------------------------------------------------------------

CREATE INDEX ix_request_extraction_parse_run
    ON sims.request_extraction (parse_run_id);
CREATE INDEX ix_request_extraction_schema
    ON sims.request_extraction (form_schema_id);
CREATE INDEX ix_request_field_value_field
    ON sims.request_field_value (field_definition_id);
CREATE INDEX ix_missing_check_run_extraction
    ON sims.missing_check_run (request_extraction_id);
CREATE INDEX ix_missing_check_item_field
    ON sims.missing_check_item (field_definition_id);
CREATE INDEX ix_missing_check_item_evidence
    ON sims.missing_check_item (evidence_field_value_id);
CREATE INDEX ix_retrieval_run_embedding
    ON sims.retrieval_run (inspection_embedding_id);
CREATE INDEX ix_retrieval_run_sync
    ON sims.retrieval_run (source_sync_run_id);
CREATE INDEX ix_inspection_report_missing_run
    ON sims.inspection_report (missing_check_run_id);
CREATE INDEX ix_inspection_report_retr_run
    ON sims.inspection_report (retrieval_run_id);
CREATE INDEX ix_candidate_evidence_chunk
    ON sims.candidate_evidence (document_chunk_id);
CREATE INDEX ix_announcement_attach_asset
    ON sims.announcement_attachment (file_asset_id);
CREATE INDEX ix_form_schema_source_asset
    ON sims.form_schema (source_file_asset_id);
CREATE INDEX ix_archive_batch_source_asset
    ON sims.archive_import_batch (source_file_asset_id);
CREATE INDEX ix_chunk_set_chunking_profile
    ON sims.document_chunk_set (chunking_profile_id);
CREATE INDEX ix_embedding_profile_chunking
    ON sims.embedding_profile (chunking_profile_id);
CREATE INDEX ix_embedding_profile_model
    ON sims.embedding_profile (embedding_model_id);
CREATE INDEX ix_announcement_source_code
    ON sims.announcement (source_code);
CREATE INDEX ix_archive_batch_source_code
    ON sims.archive_import_batch (source_code);

-- -----------------------------------------------------------------------------
-- 10. Read views
-- -----------------------------------------------------------------------------

CREATE VIEW sims.v_current_announcement AS
SELECT
    a.id AS announcement_key,
    a.source_code,
    a.pblanc_id,
    a.first_seen_at,
    a.last_seen_at,
    av.*
FROM sims.announcement a
JOIN sims.announcement_version av
  ON av.announcement_id = a.id
 AND av.is_current;

CREATE VIEW sims.v_searchable_announcement AS
SELECT *
FROM sims.v_current_announcement
WHERE search_status IN ('OPEN', 'UNKNOWN');

-- -----------------------------------------------------------------------------
-- 10.5 Design-rationale comments
-- -----------------------------------------------------------------------------

COMMENT ON COLUMN sims.app_user.password_changed_at IS
'Timestamp of the latest successful password-hash change. Backends must reject '
'sessions or tokens issued before this value. Initial account creation sets the '
'baseline timestamp but does not create a password_change_history row.';

COMMENT ON TABLE sims.password_change_history IS
'Successful password-change event timestamps only. Password plaintext and old/new '
'hashes are never stored. Rows are deleted with the owning app_user account.';

COMMENT ON TABLE sims.announcement_version IS
'Open API announcement SCD Type 2 snapshot. API content changes create a new '
'version. Operational search_status fields may be updated in place; historical '
'retrieval status is preserved by retrieval_candidate.status_verification.';

COMMENT ON COLUMN sims.announcement_version.detail_ref_fields IS
'Semantic axes containing measured detail-reference boilerplate. Allowed values '
'are target and content. Original purpose/target/content text is never removed.';

COMMENT ON COLUMN sims.announcement_version.has_detail_ref IS
'Generated convenience flag derived from detail_ref_fields; never written directly.';

COMMENT ON COLUMN sims.announcement_version.period_start_date IS
'Actual application start date when it is clearly present in the source period. '
'Do not silently convert an announcement registration timestamp into source truth.';

COMMENT ON COLUMN sims.announcement_version.search_status IS
'OPEN and UNKNOWN remain searchable. Status verification failure produces UNKNOWN, '
'not silent exclusion. A condition such as 모집 완료시 is not by itself CLOSED.';

COMMENT ON COLUMN sims.embedding_profile.preprocessing_version IS
'Version of deterministic cleanup and input assembly rules. Any change requires a '
'new embedding profile version and new vectors; existing vectors are preserved.';

COMMENT ON COLUMN sims.announcement_embedding.input_text IS
'Exact five-axis text sent to the embedding provider. Reference boilerplate is '
'omitted only from this derived input. If one axis is empty, omit that axis and '
'embed the remaining fields; do not restore boilerplate.';

COMMENT ON COLUMN sims.chunk_embedding.input_text IS
'Exact chunk text sent to the embedding provider for this chunk and profile.';

COMMENT ON COLUMN sims.announcement_embedding.embedding IS
'Dimensionless vector storage for multi-model experiments. Triggers enforce the '
'dimension registered by embedding_profile. Compare only within one profile. '
'A profile-specific expression HNSW query must use the identical cast expression, '
'ORDER BY the distance operator ascending, and LIMIT. Supply the query vector as a '
'bound parameter or scalar subquery rather than a direct JOIN-column expression.';

COMMENT ON COLUMN sims.retrieval_candidate.detail_parse_status IS
'All Top-K candidates are eligible for attachment parsing. detail_ref_fields '
'prioritizes axes needing supplementation but is not the sole parsing gate. '
'Unsupported, damaged, or encrypted files remain FAILED while summary results remain.';

COMMENT ON TABLE sims.object_delete_outbox IS
'Transactional object-delete outbox and permanent storage-key tombstone ledger. '
'Processed rows must be retained; storage keys are globally unique and never reused.';

COMMENT ON TABLE sims.missing_check_run IS
'One inspection case may have multiple runs. Each recheck creates a new row; '
'existing completed results are not overwritten.';

-- -----------------------------------------------------------------------------
-- 10.6 테이블 용도
--
-- 각 테이블이 무엇을 담는지 한 줄로 적는다. 10.5 의 설계 근거 주석과 겹치는
-- 4개 테이블(password_change_history, announcement_version, object_delete_outbox,
-- missing_check_run)은 이미 설명이 있어 여기서 다시 달지 않는다. 한 객체에
-- COMMENT 는 하나뿐이라 다시 달면 기존 설명이 지워진다.
-- -----------------------------------------------------------------------------

COMMENT ON TABLE sims.app_user IS
'로그인 계정. login_id 와 email 이 각각 유일하며 로그인 조회는 email 로 한다. 비밀번호가 바뀌면 password_changed_at 이 갱신되고 그 전에 발급된 토큰은 전부 거부된다.';

COMMENT ON TABLE sims.inspection_case IS
'검사 1건. 업로드부터 완료까지의 상태 기계이며 파일·추출·검색·보고서·대화가 모두 이 행에 매달린다. 사용자가 보는 분석 이력 1줄이 이 행 하나다.';

COMMENT ON TABLE sims.file_asset IS
'저장소에 올라간 파일 1개의 메타데이터. asset_scope 가 USER 면 사용자가 올린 요청서라 소유자와 검사 건이 반드시 있고, SHARED 면 공고 첨부나 서식처럼 전체가 공유하는 파일이라 둘 다 비어 있다.';

COMMENT ON TABLE sims.uploaded_document IS
'검사 건에 붙은 요청서 원본 1개. 검사 건당 정확히 한 행이며 HWP·HWPX·PDF 만 받는다.';

COMMENT ON TABLE sims.document_parse_run IS
'파일 1개를 파싱한 시도 1회. 재시도는 같은 파일에 attempt_no 를 올려 쌓이므로 실패 이력이 남는다. 추출 텍스트와 구조화 결과가 여기 있다.';

COMMENT ON TABLE sims.chunking_profile IS
'문서를 조각내는 전략 설정. 이름과 버전으로 유일하며, 전략이 바뀌면 새 버전을 만들어 과거 청크와 섞이지 않게 한다.';

COMMENT ON TABLE sims.document_chunk_set IS
'파싱 결과 1개를 청킹 프로파일 1개로 자른 묶음. 같은 파싱 결과를 다른 전략으로 여러 번 자를 수 있다.';

COMMENT ON TABLE sims.document_chunk IS
'잘린 조각 1개. 근거를 인용하는 최소 단위이며 페이지와 위치를 함께 들고 있다. 전문 검색용 GIN 색인이 걸려 있다.';

COMMENT ON TABLE sims.form_schema IS
'요청서 서식의 버전 1개. 서식이 바뀌면 새 버전을 만들고 과거 검사 결과는 그대로 둔다.';

COMMENT ON TABLE sims.form_field_definition IS
'서식이 요구하는 항목 1개의 정의. CPL 점검이 무엇을 확인해야 하는지가 여기서 정해진다. required_rule 이 필수 여부 판정 규칙이다.';

COMMENT ON TABLE sims.request_extraction IS
'요청서에서 항목 값을 뽑아낸 실행 1회. 검사 건당 한 행이며, 어느 서식과 어느 파싱 결과를 썼는지를 함께 남긴다.';

COMMENT ON TABLE sims.request_field_value IS
'뽑아낸 항목 값 1개. 원문 텍스트와 정규화 값, 그리고 문서 어디에서 나왔는지를 함께 담아 근거 추적을 가능하게 한다.';

COMMENT ON TABLE sims.missing_check_item IS
'항목 1개의 CPL 점검 결과. 있음·누락·해당없음·확인필요·파싱실패 중 하나이며, 판정 근거가 된 값을 evidence_field_value_id 로 가리킨다. 화면의 CPL 표 한 줄이 이 행이다.';

COMMENT ON TABLE sims.data_source IS
'외부 자료 출처 정의. is_search_source 가 참인 출처만 유사 공고 검색 대상이 된다.';

COMMENT ON TABLE sims.api_sync_run IS
'출처 1개의 하루치 동기화 실행. 출처와 날짜로 유일해 하루에 두 번 돌지 않는다. 가져온 건수와 새 버전 건수가 여기 쌓인다.';

COMMENT ON TABLE sims.announcement IS
'공고의 불변 신원. 기관이 부여한 pblanc_id 하나에 행 하나이며, 내용은 전부 announcement_version 에 있다.';

COMMENT ON TABLE sims.announcement_attachment IS
'공고 버전에 딸린 파일. attachment_role 이 PRIMARY 면 공고 본문 출력물이고 AUXILIARY 면 서식 등 부속 자료다. 내려받기 전에는 file_asset_id 가 비어 있다.';

COMMENT ON TABLE sims.announcement_subprogram_period IS
'공고 하나 안에 세부사업별 접수기간이 여러 개일 때 그 기간들. 공고 전체 기간과 별개다.';

COMMENT ON TABLE sims.archive_import_batch IS
'과거 파일 자료를 1회 적재한 배치. 적재 성공·실패 건수를 남긴다.';

COMMENT ON TABLE sims.archive_bizinfo_listing IS
'기업마당 과거 목록의 행 1개. 보관 전용이며 검색·임베딩 대상이 아니다.';

COMMENT ON TABLE sims.archive_central_program IS
'중앙부처 과거 지원사업의 행 1개. 보관 전용이며 검색·임베딩 대상이 아니다.';

COMMENT ON TABLE sims.embedding_model IS
'임베딩 모델 1개. 차원과 거리 척도를 여기서 고정하며, 벡터를 넣을 때 트리거가 이 차원과 맞는지 검사한다.';

COMMENT ON TABLE sims.embedding_profile IS
'모델과 전처리 방식과 대상 필드를 묶은 조합. 같은 프로파일로 만든 벡터끼리만 비교가 성립하므로, 벡터 테이블은 모두 이 프로파일을 함께 들고 있다. profile_kind 가 SUMMARY 면 문서 전체를 한 벡터로, CHUNK 면 조각별 벡터를 만든다.';

COMMENT ON TABLE sims.inspection_embedding IS
'요청서 1건의 벡터. 유사 공고 검색의 질의 쪽이다.';

COMMENT ON TABLE sims.announcement_embedding IS
'공고 버전 1건의 벡터. 유사 공고 검색의 대상 쪽이다.';

COMMENT ON TABLE sims.chunk_embedding IS
'문서 조각 1개의 벡터. 근거 문장을 찾을 때 쓴다.';

COMMENT ON TABLE sims.retrieval_run IS
'유사 공고 검색 실행 1회. corpus_snapshot_at 으로 어느 시점의 공고 집합을 봤는지 고정해, 나중에 공고가 늘어도 과거 결과를 재현할 수 있게 한다.';

COMMENT ON TABLE sims.retrieval_candidate IS
'검색 결과 후보 1건. 순위와 벡터 거리, 그리고 소관기관 일치 여부나 기간 관계 같은 비교 결과를 담는다. 화면의 유사 공고 카드 하나가 이 행이다.';

COMMENT ON TABLE sims.candidate_evidence IS
'후보 1건에 대한 근거 조각. evidence_side 로 요청서 쪽인지 공고 쪽인지 비교 자체인지를 구분하고, 인용문과 출처 위치를 담는다.';

COMMENT ON TABLE sims.inspection_report IS
'완성된 검사 결과 전체를 담은 JSON. 검사 건당 한 행이며, 어느 CPL 실행과 어느 검색 실행을 근거로 만들었는지를 함께 가리킨다. API 가 돌려주는 결과가 이 report_json 이다.';

COMMENT ON TABLE sims.output_artifact IS
'보고서를 파일로 뽑은 산출물. 보고서 1건에 파일 1개다.';

COMMENT ON TABLE sims.chat_session IS
'검사 건에 딸린 질의응답 세션. 검사 건당 한 행이다.';

COMMENT ON TABLE sims.chat_message IS
'대화 메시지 1개. sequence_no 로 순서를 매기고 커서 페이지네이션의 기준이 된다. 모델 이름과 토큰 수도 함께 남긴다.';
-- -----------------------------------------------------------------------------
-- 11. Seed the three confirmed sources
-- -----------------------------------------------------------------------------

INSERT INTO sims.data_source (
    source_code, source_name, source_type, is_search_source, description
) VALUES
    (
        'BIZINFO_OPEN_API',
        '기업마당 중소기업 지원사업 공고 Open API',
        'API',
        true,
        'MVP 유사·중복 검색 대상'
    ),
    (
        'BIZINFO_ARCHIVE',
        '기업마당 중소기업 지원사업 과거 목록',
        'FILE',
        false,
        '약 97,794건 보관 전용; 검색·임베딩 제외'
    ),
    (
        'CENTRAL_2023_H1',
        '중앙부처 2023년 상반기 중소기업지원사업',
        'FILE',
        false,
        '909건 보관 전용; 검색·임베딩 제외'
    )
ON CONFLICT (source_code) DO NOTHING;

COMMIT;
