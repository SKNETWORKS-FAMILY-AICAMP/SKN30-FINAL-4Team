-- ============================================================================
-- Migration 10: Frontend Supabase API Contract Foundation
-- Date: 2026-09-08
--
-- Adds the durable state needed by the direct React -> Supabase flow.
-- Views, RPCs and Edge Functions are deliberately introduced in later
-- migrations once the complete response-contract JSON is available.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS api;

-- An analysis run is reserved before the browser uploads its source object.
-- The Edge Function owns state transitions; the worker writes the terminal
-- state and the linked analysis case after asynchronous processing.
ALTER TABLE workspace.analysis_run
    ADD COLUMN IF NOT EXISTS original_filename TEXT NULL,
    ADD COLUMN IF NOT EXISTS declared_mime_type TEXT NULL,
    ADD COLUMN IF NOT EXISTS declared_size_bytes BIGINT NULL
        CHECK (declared_size_bytes IS NULL OR declared_size_bytes >= 0),
    ADD COLUMN IF NOT EXISTS source_bucket TEXT NULL,
    ADD COLUMN IF NOT EXISTS source_object_key TEXT NULL,
    ADD COLUMN IF NOT EXISTS source_content_sha256 TEXT NULL
        CHECK (source_content_sha256 IS NULL OR source_content_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    ADD COLUMN IF NOT EXISTS worker_job_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS analysis_case_pk UUID NULL
        REFERENCES result.analysis_case(analysis_case_pk)
        ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS error_code TEXT NULL,
    ADD COLUMN IF NOT EXISTS error_message TEXT NULL;

ALTER TABLE workspace.analysis_run
    DROP CONSTRAINT IF EXISTS analysis_run_status_check;

ALTER TABLE workspace.analysis_run
    ADD CONSTRAINT analysis_run_status_check
    CHECK (status IN (
        'uploading','queued','running','succeeded','failed','cancelled','cleanup_pending'
    ));

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_analysis_run_case
    ON workspace.analysis_run(analysis_case_pk)
    WHERE analysis_case_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_workspace_analysis_run_worker_job
    ON workspace.analysis_run(worker_job_id)
    WHERE worker_job_id IS NOT NULL;

-- The frontend contract displays message progress and permits bounded retry.
ALTER TABLE result.conversation_message
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('generating','completed','failed')),
    ADD COLUMN IF NOT EXISTS reply_to_message_pk UUID NULL
        REFERENCES result.conversation_message(message_pk)
        ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (retry_count >= 0 AND retry_count <= 2),
    ADD COLUMN IF NOT EXISTS error_code TEXT NULL,
    ADD COLUMN IF NOT EXISTS error_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE result.analysis_session
    ADD COLUMN IF NOT EXISTS close_reason TEXT NULL;

-- Only the curated api schema will be exposed through PostgREST. Base tables
-- remain implementation details and retain their existing RLS policies.
GRANT USAGE ON SCHEMA api TO authenticated;

COMMIT;
