-- ============================================================================
-- Migration 13: API Contract State Hardening
-- Date: 2026-09-08
--
-- Keeps browser-visible analysis_run state small, moves worker-only dispatch
-- metadata into a private table, and adds durable result snapshots required
-- after temporary workspace cleanup.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS workspace.analysis_run_dispatch (
    analysis_run_pk       UUID PRIMARY KEY
        REFERENCES workspace.analysis_run(analysis_run_pk)
        ON DELETE CASCADE,
    source_bucket         TEXT NOT NULL,
    source_object_key     TEXT NOT NULL,
    source_content_sha256 TEXT NULL
        CHECK (source_content_sha256 IS NULL OR source_content_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    worker_job_id         TEXT NULL,
    dispatched_at         TIMESTAMPTZ NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_bucket, source_object_key)
);

INSERT INTO workspace.analysis_run_dispatch (
    analysis_run_pk, source_bucket, source_object_key, source_content_sha256, worker_job_id
)
SELECT analysis_run_pk, source_bucket, source_object_key, source_content_sha256, worker_job_id
FROM workspace.analysis_run
WHERE source_bucket IS NOT NULL AND source_object_key IS NOT NULL
ON CONFLICT (analysis_run_pk) DO NOTHING;

ALTER TABLE workspace.analysis_run
    DROP COLUMN IF EXISTS source_bucket,
    DROP COLUMN IF EXISTS source_object_key,
    DROP COLUMN IF EXISTS source_content_sha256,
    DROP COLUMN IF EXISTS worker_job_id;

ALTER TABLE workspace.analysis_run
    ALTER COLUMN expires_at SET DEFAULT (now() + interval '1 hour');

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_analysis_run_one_active_per_user
    ON workspace.analysis_run(user_id)
    WHERE status IN ('uploading','queued','running');

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_dispatch_worker_job
    ON workspace.analysis_run_dispatch(worker_job_id)
    WHERE worker_job_id IS NOT NULL;

-- Realtime requires SELECT plus the existing ownership RLS policy.  Keep the
-- grant narrow so the worker dispatch metadata cannot be sent to browsers.
GRANT SELECT (
    analysis_run_pk, status, analysis_case_pk, error_code, error_message, updated_at
) ON workspace.analysis_run TO authenticated;

CREATE OR REPLACE FUNCTION workspace.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workspace_analysis_run_updated_at ON workspace.analysis_run;
CREATE TRIGGER trg_workspace_analysis_run_updated_at
BEFORE UPDATE ON workspace.analysis_run
FOR EACH ROW EXECUTE FUNCTION workspace.set_updated_at();

DROP TRIGGER IF EXISTS trg_workspace_analysis_run_dispatch_updated_at ON workspace.analysis_run_dispatch;
CREATE TRIGGER trg_workspace_analysis_run_dispatch_updated_at
BEFORE UPDATE ON workspace.analysis_run_dispatch
FOR EACH ROW EXECUTE FUNCTION workspace.set_updated_at();

ALTER TABLE result.analysis_case
    ADD COLUMN IF NOT EXISTS program_name TEXT NULL,
    ADD COLUMN IF NOT EXISTS original_filename TEXT NULL;

ALTER TABLE result.sim_candidate
    ADD COLUMN IF NOT EXISTS notice_title TEXT NULL,
    ADD COLUMN IF NOT EXISTS issuing_organization TEXT NULL,
    ADD COLUMN IF NOT EXISTS source_url TEXT NULL,
    ADD COLUMN IF NOT EXISTS notice_status TEXT NULL;

ALTER TABLE result.report_artifact
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'generating'
        CHECK (status IN ('generating','ready','failed')),
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (retry_count >= 0),
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS error_code TEXT NULL,
    ADD COLUMN IF NOT EXISTS error_message TEXT NULL,
    ALTER COLUMN storage_bucket DROP NOT NULL,
    ALTER COLUMN storage_object_key DROP NOT NULL,
    ALTER COLUMN content_sha256 DROP NOT NULL;

ALTER TABLE result.conversation_message
    DROP CONSTRAINT IF EXISTS conversation_message_retry_count_check,
    DROP COLUMN IF EXISTS retry_count,
    ADD COLUMN IF NOT EXISTS auto_retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (auto_retry_count >= 0 AND auto_retry_count <= 1),
    ADD COLUMN IF NOT EXISTS manual_retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (manual_retry_count >= 0 AND manual_retry_count <= 2),
    ADD COLUMN IF NOT EXISTS last_retry_at TIMESTAMPTZ NULL;

DROP TRIGGER IF EXISTS trg_result_conversation_message_updated_at ON result.conversation_message;
CREATE TRIGGER trg_result_conversation_message_updated_at
BEFORE UPDATE ON result.conversation_message
FOR EACH ROW EXECUTE FUNCTION workspace.set_updated_at();

CREATE OR REPLACE FUNCTION result.validate_conversation_reply()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, result
AS $$
DECLARE
    parent_session UUID;
    parent_role TEXT;
BEGIN
    IF NEW.reply_to_message_pk IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT analysis_session_pk, role
      INTO parent_session, parent_role
      FROM result.conversation_message
     WHERE message_pk = NEW.reply_to_message_pk;

    IF NOT FOUND OR parent_session <> NEW.analysis_session_pk
       OR parent_role <> 'user' OR NEW.role <> 'assistant' THEN
        RAISE EXCEPTION 'assistant reply must reference a user message in the same analysis session';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_result_validate_conversation_reply ON result.conversation_message;
CREATE TRIGGER trg_result_validate_conversation_reply
BEFORE INSERT OR UPDATE OF reply_to_message_pk, role, analysis_session_pk
ON result.conversation_message
FOR EACH ROW EXECUTE FUNCTION result.validate_conversation_reply();

-- Lock down future api RPCs before any SECURITY DEFINER function is added.
ALTER DEFAULT PRIVILEGES IN SCHEMA api
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon;

COMMIT;
