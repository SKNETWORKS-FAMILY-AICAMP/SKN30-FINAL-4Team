-- ============================================================================
-- Migration 33: v0.2 lifecycle ownership, serialization, and API boundary
--
-- This is intentionally additive.  The v0.1 browser/PostgREST functions stay
-- available for a rollback pod, while all v0.2 commands below are callable
-- only by the trusted FastAPI service role.
-- ============================================================================

BEGIN;

-- A request idempotency key belongs to an owner, rather than being inferred
-- from a globally supplied analysis-run UUID.  Backfilling the old primary
-- key preserves every existing reservation as its own exact replay key.
ALTER TABLE workspace.analysis_run
    ADD COLUMN IF NOT EXISTS idempotency_key UUID;

UPDATE workspace.analysis_run
   SET idempotency_key = analysis_run_pk
 WHERE idempotency_key IS NULL;

ALTER TABLE workspace.analysis_run
    ALTER COLUMN idempotency_key SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_analysis_run_owner_idempotency
    ON workspace.analysis_run (user_id, idempotency_key);

-- analysis_session used to derive its owner only through its case.  Store the
-- trusted value directly so that the one-live-session invariant and its lock
-- have a single, indexed ownership key.
ALTER TABLE result.analysis_session
    ADD COLUMN IF NOT EXISTS user_id UUID;

UPDATE result.analysis_session AS session
   SET user_id = analysis_case.user_id
  FROM result.analysis_case AS analysis_case
 WHERE analysis_case.analysis_case_pk = session.analysis_case_pk
   AND session.user_id IS NULL;

ALTER TABLE result.analysis_session
    ALTER COLUMN user_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'result.analysis_session'::regclass
           AND conname = 'fk_result_analysis_session_user'
    ) THEN
        ALTER TABLE result.analysis_session
            ADD CONSTRAINT fk_result_analysis_session_user
            FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE RESTRICT;
    END IF;
END;
$$;

-- Never let an expired row consume the active-session partial unique index.
UPDATE result.analysis_session
   SET status = 'expired',
       updated_at = clock_timestamp()
 WHERE status = 'active'
   AND expires_at <= clock_timestamp();

-- A pre-v0.2 database can contain multiple live sessions for a user.  The
-- deterministic keeper is the most recently active row, with the UUID as the
-- tie breaker.  Closing (rather than deleting) preserves retention/audit data.
WITH ranked_active_sessions AS (
    SELECT
        analysis_session_pk,
        row_number() OVER (
            PARTITION BY user_id
            ORDER BY last_activity_at DESC, analysis_session_pk DESC
        ) AS position
    FROM result.analysis_session
    WHERE status = 'active'
)
UPDATE result.analysis_session AS session
   SET status = 'closed',
       closed_at = COALESCE(session.closed_at, clock_timestamp()),
       close_reason = 'migration_reconciled',
       updated_at = clock_timestamp()
  FROM ranked_active_sessions AS ranked
 WHERE ranked.analysis_session_pk = session.analysis_session_pk
   AND ranked.position > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_result_analysis_session_one_active_owner
    ON result.analysis_session (user_id)
 WHERE status = 'active';

CREATE INDEX IF NOT EXISTS ix_result_analysis_session_owner_lifecycle
    ON result.analysis_session (user_id, status, expires_at, last_activity_at DESC);

CREATE INDEX IF NOT EXISTS ix_workspace_analysis_run_owner_lifecycle
    ON workspace.analysis_run (user_id, status, expires_at, created_at);

-- This trigger makes session ownership trusted-derived even for a direct
-- service write.  It is deliberately a database check, not an application
-- convention, because later partial unique/index predicates rely on it.
CREATE OR REPLACE FUNCTION result.validate_analysis_session_owner_v2()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, result
AS $$
DECLARE
    v_case_owner UUID;
BEGIN
    SELECT user_id
      INTO v_case_owner
      FROM result.analysis_case
     WHERE analysis_case_pk = NEW.analysis_case_pk;

    IF v_case_owner IS NULL THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_OWNER_MISMATCH'
            USING ERRCODE = '23514';
    END IF;
    -- Preserve the migration-26 writer signature: it inserts only the case
    -- key and this trigger derives the denormalised owner before NOT NULL is
    -- checked.  A supplied non-matching owner is always rejected.
    IF NEW.user_id IS NULL THEN
        NEW.user_id := v_case_owner;
    ELSIF NEW.user_id IS DISTINCT FROM v_case_owner THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_OWNER_MISMATCH'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_result_analysis_session_owner_v2
    ON result.analysis_session;
CREATE TRIGGER trg_result_analysis_session_owner_v2
BEFORE INSERT OR UPDATE OF analysis_case_pk, user_id
ON result.analysis_session
FOR EACH ROW EXECUTE FUNCTION result.validate_analysis_session_owner_v2();

-- Every user lifecycle mutation uses this exact advisory key.  Callers obtain
-- it before taking a session/run row lock and then re-check predicates under
-- those row locks.  hashtextextended is stable for the transaction and does
-- not expose a user UUID outside trusted SQL.
CREATE OR REPLACE FUNCTION workspace.lock_analysis_lifecycle_user_v2(
    p_user_id UUID
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_user_id IS NULL THEN
        RAISE EXCEPTION 'ANALYSIS_OWNER_REQUIRED' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
END;
$$;

-- Reconcile only this owner's stale pre-processing lifecycle rows.  A caller
-- can safely retry it: terminal/cleanup rows no longer match the predicate.
-- Only expired upload reservations are returned because those are the sole
-- rows whose unregistered Storage object may be deleted by FastAPI.  Queued
-- and exhausted-running failures retain their registered source provenance.
CREATE OR REPLACE FUNCTION workspace.reconcile_stale_analysis_runs_for_user_core_v2(
    p_user_id UUID,
    p_collect_upload_cleanup BOOLEAN
)
RETURNS TABLE (
    analysis_run_id UUID,
    source_bucket TEXT,
    source_object_key TEXT,
    terminal_status TEXT,
    error_code TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, workspace
AS $$
DECLARE
    v_candidate RECORD;
    v_run workspace.analysis_run%ROWTYPE;
    v_dispatch workspace.analysis_run_dispatch%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    PERFORM workspace.lock_analysis_lifecycle_user_v2(p_user_id);

    -- Candidate discovery intentionally takes no row locks.  The common user
    -- advisory lock is acquired first; each candidate is then locked and its
    -- expiry predicate is checked again below.
    FOR v_candidate IN
        SELECT analysis_run_pk
          FROM workspace.analysis_run
         WHERE user_id = p_user_id
           AND (
                (p_collect_upload_cleanup AND status = 'cleanup_pending')
                OR (p_collect_upload_cleanup AND status = 'uploading' AND expires_at <= v_now)
                OR (status = 'queued' AND expires_at <= v_now)
                OR status = 'running'
           )
         ORDER BY created_at, analysis_run_pk
    LOOP
        SELECT run.*
          INTO v_run
          FROM workspace.analysis_run AS run
         WHERE run.analysis_run_pk = v_candidate.analysis_run_pk
           AND run.user_id = p_user_id
         FOR UPDATE;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        SELECT dispatch.*
          INTO v_dispatch
          FROM workspace.analysis_run_dispatch AS dispatch
         WHERE dispatch.analysis_run_pk = v_run.analysis_run_pk
         FOR UPDATE;

        IF v_run.status = 'cleanup_pending' AND p_collect_upload_cleanup THEN
            analysis_run_id := v_run.analysis_run_pk;
            source_bucket := v_dispatch.source_bucket;
            source_object_key := v_dispatch.source_object_key;
            terminal_status := 'cleanup_pending';
            error_code := v_run.error_code;
            RETURN NEXT;
        ELSIF v_run.status = 'uploading'
              AND p_collect_upload_cleanup
              AND v_run.expires_at <= v_now THEN
            UPDATE workspace.analysis_run
               SET status = 'cleanup_pending',
                   completed_at = COALESCE(completed_at, v_now),
                   error_code = 'UPLOAD_RESERVATION_EXPIRED',
                   error_message = '파일 업로드 시간이 만료되었습니다. 다시 시도해 주세요.'
             WHERE analysis_run_pk = v_run.analysis_run_pk
               AND status = 'uploading';
            analysis_run_id := v_run.analysis_run_pk;
            source_bucket := v_dispatch.source_bucket;
            source_object_key := v_dispatch.source_object_key;
            terminal_status := 'cleanup_pending';
            error_code := 'UPLOAD_RESERVATION_EXPIRED';
            RETURN NEXT;
        ELSIF v_run.status = 'queued' AND v_run.expires_at <= v_now
              AND v_dispatch.processing_run_pk IS NULL THEN
            UPDATE workspace.analysis_run
               SET status = 'failed',
                   completed_at = COALESCE(completed_at, v_now),
                   error_code = 'ANALYSIS_QUEUE_EXPIRED',
                   error_message = '분석 대기 시간이 만료되었습니다. 다시 시도해 주세요.'
             WHERE analysis_run_pk = v_run.analysis_run_pk
               AND status = 'queued';
        ELSIF v_run.status = 'running'
              AND v_dispatch.attempt_count >= 2
              AND (
                  v_dispatch.processing_run_pk IS NULL
                  OR v_dispatch.lease_expires_at <= v_now
              ) THEN
            IF v_dispatch.processing_run_pk IS NOT NULL THEN
                UPDATE ops.processing_run AS processing_attempt
                   SET status = 'failed',
                       finished_at = v_now,
                       error_code = 'WORKER_LEASE_EXPIRED',
                       error_message = 'Worker lease expired before completion.',
                       run_metadata = COALESCE(run_metadata, '{}'::jsonb)
                           || jsonb_build_object('lease_expired_at', v_now)
                 WHERE processing_attempt.processing_run_pk = v_dispatch.processing_run_pk
                   AND processing_attempt.status IN ('queued', 'running');
            END IF;

            UPDATE workspace.analysis_run_dispatch
               SET processing_run_pk = NULL,
                   claimed_by = NULL,
                   claimed_at = NULL,
                   heartbeat_at = NULL,
                   lease_expires_at = NULL,
                   last_error_code = 'WORKER_MAX_ATTEMPTS_EXCEEDED',
                   last_error_message = 'The worker lease expired after two attempts.'
             WHERE analysis_run_pk = v_run.analysis_run_pk;

            UPDATE workspace.analysis_run
               SET status = 'failed',
                   completed_at = COALESCE(completed_at, v_now),
                   error_code = 'WORKER_MAX_ATTEMPTS_EXCEEDED',
                   error_message = '분석 작업이 시간 내에 완료되지 않았습니다. 다시 시도해 주세요.'
             WHERE analysis_run_pk = v_run.analysis_run_pk
               AND status = 'running';
        END IF;
    END LOOP;
END;
$$;

-- Mutating callers that can actually delete private Storage objects use this
-- public wrapper. Read-only current-state polling calls the core helper with
-- ``FALSE`` below, so it may terminalise stale queued/running work but can
-- never claim an upload cleanup key and then discard it.
CREATE OR REPLACE FUNCTION workspace.reconcile_stale_analysis_runs_for_user_v2(
    p_user_id UUID
)
RETURNS TABLE (
    analysis_run_id UUID,
    source_bucket TEXT,
    source_object_key TEXT,
    terminal_status TEXT,
    error_code TEXT
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, workspace
AS $$
    SELECT *
      FROM workspace.reconcile_stale_analysis_runs_for_user_core_v2(
          p_user_id, TRUE
      );
$$;

-- Migration 21's queue claim predates the uploading/queued TTL contract.  A
-- polling worker must reconcile an expired queued row even when no browser
-- calls upload/current again, and the actual candidate predicate must repeat
-- the deadline check to close the expiry-boundary race.
CREATE OR REPLACE FUNCTION workspace.claim_next_analysis_run(
    p_worker_id TEXT,
    p_lease_seconds INTEGER DEFAULT 120
)
RETURNS TABLE (
    analysis_run_pk UUID,
    source_bucket TEXT,
    source_object_key TEXT,
    source_content_sha256 TEXT,
    processing_run_pk UUID,
    attempt_count INTEGER,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_interval_seconds INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, ops, workspace
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_heartbeat_seconds CONSTANT INTEGER := 30;
    v_analysis_run_pk UUID;
    v_source_bucket TEXT;
    v_source_object_key TEXT;
    v_source_content_sha256 TEXT;
    v_previous_processing_run_pk UUID;
    v_processing_run_pk UUID;
    v_attempt_count INTEGER;
    v_lease_expires_at TIMESTAMPTZ;
BEGIN
    IF btrim(COALESCE(p_worker_id, '')) = '' THEN
        RAISE EXCEPTION 'WORKER_ID_REQUIRED' USING ERRCODE = '22023';
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds < v_heartbeat_seconds
       OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'LEASE_SECONDS_OUT_OF_RANGE' USING ERRCODE = '22023';
    END IF;

    -- Reconcile every expired queued row before taking a live job.  Registered
    -- source provenance remains in dispatch/Storage for the normal retention
    -- policy; only an uncompleted upload reservation is cleanup-pending.
    LOOP
        SELECT ar.analysis_run_pk, dispatch.processing_run_pk
          INTO v_analysis_run_pk, v_previous_processing_run_pk
          FROM workspace.analysis_run AS ar
          JOIN workspace.analysis_run_dispatch AS dispatch
            ON dispatch.analysis_run_pk = ar.analysis_run_pk
         WHERE ar.status = 'queued'
           AND ar.expires_at <= v_now
         ORDER BY ar.created_at, ar.analysis_run_pk
         LIMIT 1
         FOR UPDATE OF ar, dispatch SKIP LOCKED;

        EXIT WHEN NOT FOUND;

        IF v_previous_processing_run_pk IS NOT NULL THEN
            UPDATE ops.processing_run AS processing_attempt
               SET status = 'failed',
                   finished_at = v_now,
                   error_code = 'ANALYSIS_QUEUE_EXPIRED',
                   error_message = 'Analysis queue deadline expired before claim.'
             WHERE processing_attempt.processing_run_pk = v_previous_processing_run_pk
               AND processing_attempt.status IN ('queued', 'running');
        END IF;

        UPDATE workspace.analysis_run_dispatch AS dispatch
           SET processing_run_pk = NULL,
               claimed_by = NULL,
               claimed_at = NULL,
               heartbeat_at = NULL,
               lease_expires_at = NULL,
               last_error_code = 'ANALYSIS_QUEUE_EXPIRED',
               last_error_message = 'Analysis queue deadline expired before claim.'
         WHERE dispatch.analysis_run_pk = v_analysis_run_pk;

        UPDATE workspace.analysis_run AS ar
           SET status = 'failed',
               completed_at = COALESCE(completed_at, v_now),
               error_code = 'ANALYSIS_QUEUE_EXPIRED',
               error_message = '분석 대기 시간이 만료되었습니다. 다시 시도해 주세요.'
         WHERE ar.analysis_run_pk = v_analysis_run_pk
           AND ar.status = 'queued';
    END LOOP;

    -- A stale second attempt cannot be reclaimed again.
    LOOP
        SELECT ar.analysis_run_pk, dispatch.processing_run_pk
          INTO v_analysis_run_pk, v_previous_processing_run_pk
          FROM workspace.analysis_run AS ar
          JOIN workspace.analysis_run_dispatch AS dispatch
            ON dispatch.analysis_run_pk = ar.analysis_run_pk
         WHERE ar.status = 'running'
           AND dispatch.attempt_count >= 2
           AND (
                dispatch.processing_run_pk IS NULL
                OR dispatch.lease_expires_at <= v_now
           )
         ORDER BY ar.created_at, ar.analysis_run_pk
         LIMIT 1
         FOR UPDATE OF ar, dispatch SKIP LOCKED;

        EXIT WHEN NOT FOUND;

        IF v_previous_processing_run_pk IS NOT NULL THEN
            UPDATE ops.processing_run AS processing_attempt
               SET status = 'failed',
                   finished_at = v_now,
                   error_code = 'WORKER_LEASE_EXPIRED',
                   error_message = 'Worker lease expired before completion.',
                   run_metadata = COALESCE(run_metadata, '{}'::jsonb)
                       || jsonb_build_object('lease_expired_at', v_now)
             WHERE processing_attempt.processing_run_pk = v_previous_processing_run_pk
               AND processing_attempt.status IN ('queued', 'running');
        END IF;

        UPDATE workspace.analysis_run_dispatch AS dispatch
           SET processing_run_pk = NULL,
               claimed_by = NULL,
               claimed_at = NULL,
               heartbeat_at = NULL,
               lease_expires_at = NULL,
               last_error_code = 'WORKER_MAX_ATTEMPTS_EXCEEDED',
               last_error_message = 'The worker lease expired after two attempts.'
         WHERE dispatch.analysis_run_pk = v_analysis_run_pk;

        UPDATE workspace.analysis_run AS ar
           SET status = 'failed',
               completed_at = v_now,
               error_code = 'WORKER_MAX_ATTEMPTS_EXCEEDED',
               error_message = '분석 작업이 시간 내에 완료되지 않았습니다. 다시 시도해 주세요.'
         WHERE ar.analysis_run_pk = v_analysis_run_pk;
    END LOOP;

    SELECT
        ar.analysis_run_pk,
        dispatch.source_bucket,
        dispatch.source_object_key,
        dispatch.source_content_sha256,
        dispatch.processing_run_pk,
        dispatch.attempt_count
      INTO
        v_analysis_run_pk,
        v_source_bucket,
        v_source_object_key,
        v_source_content_sha256,
        v_previous_processing_run_pk,
        v_attempt_count
      FROM workspace.analysis_run AS ar
      JOIN workspace.analysis_run_dispatch AS dispatch
        ON dispatch.analysis_run_pk = ar.analysis_run_pk
     WHERE dispatch.attempt_count < 2
       AND (
            (ar.status = 'queued' AND ar.expires_at > v_now)
            OR (
                ar.status = 'running'
                AND (
                    dispatch.processing_run_pk IS NULL
                    OR dispatch.lease_expires_at <= v_now
                )
            )
       )
     ORDER BY ar.created_at, ar.analysis_run_pk
     LIMIT 1
     FOR UPDATE OF ar, dispatch SKIP LOCKED;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF v_previous_processing_run_pk IS NOT NULL THEN
        UPDATE ops.processing_run AS processing_attempt
           SET status = 'failed',
               finished_at = v_now,
               error_code = 'WORKER_LEASE_EXPIRED',
               error_message = 'Worker lease expired before completion.',
               run_metadata = COALESCE(run_metadata, '{}'::jsonb)
                   || jsonb_build_object('lease_expired_at', v_now)
         WHERE processing_attempt.processing_run_pk = v_previous_processing_run_pk
           AND processing_attempt.status IN ('queued', 'running');
    END IF;

    v_attempt_count := v_attempt_count + 1;
    v_lease_expires_at := v_now + make_interval(secs => p_lease_seconds);

    INSERT INTO ops.processing_run AS processing_attempt (
        source_analysis_run_id,
        run_type,
        status,
        started_at,
        run_metadata
    ) VALUES (
        v_analysis_run_pk,
        'analysis',
        'running',
        v_now,
        jsonb_build_object(
            'attempt_no', v_attempt_count,
            'worker_id', btrim(p_worker_id),
            'lease_seconds', p_lease_seconds
        )
    )
    RETURNING processing_attempt.processing_run_pk INTO v_processing_run_pk;

    UPDATE workspace.analysis_run_dispatch AS dispatch
       SET attempt_count = v_attempt_count,
           processing_run_pk = v_processing_run_pk,
           claimed_by = btrim(p_worker_id),
           claimed_at = v_now,
           heartbeat_at = v_now,
           lease_expires_at = v_lease_expires_at,
           last_error_code = NULL,
           last_error_message = NULL
     WHERE dispatch.analysis_run_pk = v_analysis_run_pk;

    UPDATE workspace.analysis_run AS ar
       SET status = 'running',
           started_at = COALESCE(started_at, v_now),
           completed_at = NULL,
           error_code = NULL,
           error_message = NULL
     WHERE ar.analysis_run_pk = v_analysis_run_pk;

    RETURN QUERY
    SELECT
        v_analysis_run_pk,
        v_source_bucket,
        v_source_object_key,
        v_source_content_sha256,
        v_processing_run_pk,
        v_attempt_count,
        v_lease_expires_at,
        v_heartbeat_seconds;
END;
$$;

-- Create/replay the upload reservation in the required order:
-- exact idempotency replay, stale reconciliation, active queue check, then
-- active result-session check.  The immutable source identity is held in the
-- existing dispatch record and remains compatible with migration 25 guards.
--
-- FastAPI must derive p_source_object_key deterministically from the owner and
-- Idempotency-Key before this call (rather than from a speculative run UUID).
-- This function is the sole run-ID allocator: on both a new reservation and
-- an exact replay the handler must thread the returned analysis_run_id through
-- source_artifact registration, queued finalisation, the 202 body, and later
-- status reads.  A locally generated UUID must never be used as a substitute.
-- The final JSONB return column was added while v0.2 was still unreleased.
-- Drop the prior signature explicitly so replaying this migration can replace
-- the table return type on an already-migrated development database.
DROP FUNCTION IF EXISTS workspace.reserve_analysis_upload_v2(
    UUID, UUID, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, INTEGER
);

CREATE OR REPLACE FUNCTION workspace.reserve_analysis_upload_v2(
    p_user_id UUID,
    p_idempotency_key UUID,
    p_original_filename TEXT,
    p_declared_mime_type TEXT,
    p_declared_size_bytes BIGINT,
    p_source_bucket TEXT,
    p_source_object_key TEXT,
    p_source_content_sha256 TEXT,
    p_upload_ttl_seconds INTEGER DEFAULT 900
)
RETURNS TABLE (
    analysis_run_id UUID,
    status TEXT,
    replayed BOOLEAN,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    cleanup_objects JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
DECLARE
    v_existing_run workspace.analysis_run%ROWTYPE;
    v_existing_dispatch workspace.analysis_run_dispatch%ROWTYPE;
    v_new_run_id UUID := gen_random_uuid();
    v_now TIMESTAMPTZ := clock_timestamp();
    v_cleanup_objects JSONB := '[]'::jsonb;
BEGIN
    IF p_user_id IS NULL OR p_idempotency_key IS NULL
       OR btrim(COALESCE(p_original_filename, '')) = ''
       OR p_declared_size_bytes IS NULL OR p_declared_size_bytes < 0
       OR btrim(COALESCE(p_source_bucket, '')) = ''
       OR btrim(COALESCE(p_source_object_key, '')) = ''
       OR COALESCE(p_source_content_sha256, '') !~* '^[0-9a-f]{64}$'
       OR p_upload_ttl_seconds IS NULL
       OR p_upload_ttl_seconds NOT BETWEEN 60 AND 3600 THEN
        RAISE EXCEPTION 'INVALID_UPLOAD_RESERVATION' USING ERRCODE = '22023';
    END IF;

    PERFORM workspace.lock_analysis_lifecycle_user_v2(p_user_id);

    SELECT run.*
      INTO v_existing_run
      FROM workspace.analysis_run AS run
     WHERE run.user_id = p_user_id
       AND run.idempotency_key = p_idempotency_key
     FOR UPDATE;

    IF FOUND THEN
        SELECT dispatch.*
          INTO v_existing_dispatch
          FROM workspace.analysis_run_dispatch AS dispatch
         WHERE dispatch.analysis_run_pk = v_existing_run.analysis_run_pk
         FOR UPDATE;

        IF v_existing_dispatch.analysis_run_pk IS NULL
           OR ROW(
               v_existing_run.original_filename,
               v_existing_run.declared_mime_type,
               v_existing_run.declared_size_bytes,
               v_existing_dispatch.source_bucket,
               v_existing_dispatch.source_object_key,
               lower(v_existing_dispatch.source_content_sha256)
           ) IS DISTINCT FROM ROW(
               p_original_filename,
               p_declared_mime_type,
               p_declared_size_bytes,
               p_source_bucket,
               p_source_object_key,
               lower(p_source_content_sha256)
           ) THEN
            RAISE EXCEPTION 'IDEMPOTENCY_KEY_CONFLICT' USING ERRCODE = '23505';
        END IF;

        -- An exact retry of an unfinished upload is a resume operation.  Give
        -- the deterministic Storage write a fresh window before releasing the
        -- per-owner lifecycle lock so a concurrent current-state read cannot
        -- immediately expire the reservation.
        IF v_existing_run.status = 'uploading' THEN
            UPDATE workspace.analysis_run
               SET expires_at = v_now + make_interval(secs => p_upload_ttl_seconds)
             WHERE analysis_run_pk = v_existing_run.analysis_run_pk
            RETURNING * INTO v_existing_run;
        END IF;

        IF v_existing_run.status = 'cleanup_pending' THEN
            v_cleanup_objects := jsonb_build_array(jsonb_build_object(
                'analysis_run_pk', v_existing_run.analysis_run_pk,
                'source_bucket', v_existing_dispatch.source_bucket,
                'source_object_key', v_existing_dispatch.source_object_key
            ));
        END IF;

        RETURN QUERY SELECT
            v_existing_run.analysis_run_pk,
            v_existing_run.status,
            TRUE,
            v_existing_run.error_code,
            v_existing_run.error_message,
            v_existing_run.created_at,
            v_existing_run.updated_at,
            v_cleanup_objects;
        RETURN;
    END IF;

    SELECT COALESCE(
               jsonb_agg(jsonb_build_object(
                   'analysis_run_pk', cleanup.analysis_run_id,
                   'source_bucket', cleanup.source_bucket,
                   'source_object_key', cleanup.source_object_key
               )),
               '[]'::jsonb
           )
      INTO v_cleanup_objects
      FROM workspace.reconcile_stale_analysis_runs_for_user_v2(p_user_id) AS cleanup;

    IF EXISTS (
        SELECT 1
          FROM workspace.analysis_run AS run
         WHERE run.user_id = p_user_id
           AND run.status IN ('uploading', 'queued', 'running')
         FOR KEY SHARE
    ) THEN
        RAISE EXCEPTION 'ANALYSIS_RUN_ACTIVE' USING ERRCODE = '23505';
    END IF;

    UPDATE result.analysis_session AS session
       SET status = 'expired', updated_at = v_now
      FROM result.analysis_case AS analysis_case
     WHERE session.analysis_case_pk = analysis_case.analysis_case_pk
       AND session.user_id = p_user_id
       AND analysis_case.user_id = p_user_id
       AND session.status = 'active'
       AND session.expires_at <= v_now;

    IF EXISTS (
        SELECT 1
          FROM result.analysis_session AS session
         WHERE session.user_id = p_user_id
           AND session.status = 'active'
           AND session.expires_at > v_now
         FOR KEY SHARE
    ) THEN
        RAISE EXCEPTION 'ACTIVE_RESULT_SESSION' USING ERRCODE = '23505';
    END IF;

    INSERT INTO workspace.analysis_run (
        analysis_run_pk, user_id, idempotency_key, status, original_filename,
        declared_mime_type, declared_size_bytes, expires_at
    ) VALUES (
        v_new_run_id, p_user_id, p_idempotency_key, 'uploading',
        p_original_filename, p_declared_mime_type, p_declared_size_bytes,
        v_now + make_interval(secs => p_upload_ttl_seconds)
    )
    RETURNING * INTO v_existing_run;

    INSERT INTO workspace.analysis_run_dispatch (
        analysis_run_pk, source_bucket, source_object_key, source_content_sha256
    ) VALUES (
        v_new_run_id, p_source_bucket, p_source_object_key, p_source_content_sha256
    );

    RETURN QUERY SELECT
        v_existing_run.analysis_run_pk,
        v_existing_run.status,
        FALSE,
        v_existing_run.error_code,
        v_existing_run.error_message,
        v_existing_run.created_at,
        v_existing_run.updated_at,
        v_cleanup_objects;
END;
$$;

-- Close exactly the supplied session.  A retry of the same close is success,
-- while a guessed/other-owner session remains indistinguishable from absent.
CREATE OR REPLACE FUNCTION api.rpc_close_analysis_session_v2(
    p_user_id UUID,
    p_analysis_session_id UUID
)
RETURNS TABLE (
    analysis_session_id UUID,
    status TEXT,
    closed_at TIMESTAMPTZ,
    close_reason TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, api, result
AS $$
DECLARE
    v_session result.analysis_session%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    PERFORM workspace.lock_analysis_lifecycle_user_v2(p_user_id);

    SELECT session.*
      INTO v_session
      FROM result.analysis_session AS session
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = session.analysis_case_pk
     WHERE session.analysis_session_pk = p_analysis_session_id
       AND session.user_id = p_user_id
       AND analysis_case.user_id = p_user_id
     FOR UPDATE OF session, analysis_case;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ANALYSIS_SESSION_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;

    IF v_session.status = 'active' AND v_session.expires_at <= v_now THEN
        UPDATE result.analysis_session
           SET status = 'expired', updated_at = v_now
         WHERE analysis_session_pk = v_session.analysis_session_pk
        RETURNING * INTO v_session;
    ELSIF v_session.status = 'active' THEN
        UPDATE result.analysis_session
           SET status = 'closed', closed_at = v_now,
               close_reason = 'new_analysis', updated_at = v_now
         WHERE analysis_session_pk = v_session.analysis_session_pk
        RETURNING * INTO v_session;
    END IF;

    RETURN QUERY SELECT
        v_session.analysis_session_pk,
        v_session.status,
        v_session.closed_at,
        v_session.close_reason;
END;
$$;

-- FastAPI owns HMAC cursor verification.  PostgreSQL receives only a parsed,
-- owner-bound snapshot/key and applies the owner predicate again.  Six rows
-- are read to tell FastAPI whether the fixed five-row page has a successor.
CREATE OR REPLACE FUNCTION api.rpc_get_analysis_history_v2(
    p_user_id UUID,
    p_snapshot_at TIMESTAMPTZ DEFAULT NULL,
    p_before_completed_at TIMESTAMPTZ DEFAULT NULL,
    p_before_analysis_case_id UUID DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, api, result
AS $$
DECLARE
    v_snapshot_at TIMESTAMPTZ := COALESCE(p_snapshot_at, clock_timestamp());
    v_items JSONB;
    v_next_completed_at TIMESTAMPTZ;
    v_next_case_id UUID;
BEGIN
    IF p_user_id IS NULL
       OR (p_before_completed_at IS NULL) <> (p_before_analysis_case_id IS NULL) THEN
        RAISE EXCEPTION 'INVALID_HISTORY_CURSOR' USING ERRCODE = '22023';
    END IF;

    WITH page AS MATERIALIZED (
        SELECT c.analysis_case_pk, c.program_name, c.original_filename,
               c.analysis_completed_at
          FROM result.analysis_case AS c
         WHERE c.user_id = p_user_id
           AND c.analysis_completed_at IS NOT NULL
           AND c.analysis_completed_at <= v_snapshot_at
           AND c.retention_expires_at > v_snapshot_at
           -- A signed cursor stabilizes ordering; it must never extend the
           -- hard retention boundary after the underlying case expires.
           AND c.retention_expires_at > clock_timestamp()
           -- Historical session eligibility is evaluated at snapshot_at, not
           -- against a later close that happens between cursor pages.
           AND NOT EXISTS (
               SELECT 1
                 FROM result.analysis_session AS session
                WHERE session.analysis_case_pk = c.analysis_case_pk
                  AND session.started_at <= v_snapshot_at
                  AND session.expires_at > v_snapshot_at
                  AND (session.closed_at IS NULL OR session.closed_at > v_snapshot_at)
           )
           AND (
               p_before_completed_at IS NULL
               OR (c.analysis_completed_at, c.analysis_case_pk)
                    < (p_before_completed_at, p_before_analysis_case_id)
           )
         ORDER BY c.analysis_completed_at DESC, c.analysis_case_pk DESC
         LIMIT 6
    ), visible AS (
        SELECT * FROM page ORDER BY analysis_completed_at DESC, analysis_case_pk DESC LIMIT 5
    ), successor AS (
        SELECT analysis_completed_at, analysis_case_pk
          FROM page
         ORDER BY analysis_completed_at DESC, analysis_case_pk DESC
         OFFSET 5 LIMIT 1
    ), next_cursor AS (
        -- The cursor is the last returned row, never the sixth probe row:
        -- the follow-up predicate is strict '<' and would otherwise skip it.
        SELECT visible.analysis_completed_at, visible.analysis_case_pk
          FROM visible
         WHERE EXISTS (SELECT 1 FROM successor)
         ORDER BY visible.analysis_completed_at ASC, visible.analysis_case_pk ASC
         LIMIT 1
    )
    SELECT
        COALESCE(jsonb_agg(jsonb_build_object(
            'analysis_case_id', visible.analysis_case_pk,
            'program_name', visible.program_name,
            'original_filename', visible.original_filename,
            'completed_at', visible.analysis_completed_at
        ) ORDER BY visible.analysis_completed_at DESC, visible.analysis_case_pk DESC), '[]'::jsonb),
        (SELECT analysis_completed_at FROM next_cursor),
        (SELECT analysis_case_pk FROM next_cursor)
      INTO v_items, v_next_completed_at, v_next_case_id
      FROM visible;

    RETURN jsonb_build_object(
        'snapshot_at', v_snapshot_at,
        'items', v_items,
        'next_completed_at', v_next_completed_at,
        'next_analysis_case_id', v_next_case_id
    );
END;
$$;

-- Current is a single transaction snapshot.  Stale uploading/queued rows and
-- exhausted running attempts are reconciled before selection; a database
-- failure therefore cannot resemble the public idle state.
CREATE OR REPLACE FUNCTION api.rpc_get_analysis_current_v2(
    p_user_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, api, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_run RECORD;
    v_session RECORD;
BEGIN
    PERFORM workspace.lock_analysis_lifecycle_user_v2(p_user_id);
    PERFORM workspace.reconcile_stale_analysis_runs_for_user_core_v2(
        p_user_id, FALSE
    );

    UPDATE result.analysis_session
       SET status = 'expired', updated_at = v_now
     WHERE user_id = p_user_id
       AND status = 'active'
       AND expires_at <= v_now;

    SELECT run.analysis_run_pk, run.status, run.original_filename,
           run.created_at, run.updated_at
      INTO v_run
      FROM workspace.analysis_run AS run
      LEFT JOIN workspace.analysis_run_dispatch AS dispatch
        ON dispatch.analysis_run_pk = run.analysis_run_pk
     WHERE run.user_id = p_user_id
       AND (
           (run.status = 'uploading' AND run.expires_at > v_now)
           OR (run.status = 'queued' AND run.expires_at > v_now)
           OR run.status = 'running'
       )
     ORDER BY run.created_at DESC, run.analysis_run_pk DESC
     LIMIT 1;

    IF FOUND THEN
        RETURN jsonb_build_object(
            'state', 'processing',
            'run', jsonb_build_object(
                'analysis_run_id', v_run.analysis_run_pk,
                'status', v_run.status,
                'original_filename', v_run.original_filename,
                'created_at', v_run.created_at,
                'updated_at', v_run.updated_at
            ),
            'session', NULL
        );
    END IF;

    SELECT session.analysis_session_pk, session.analysis_case_pk,
           analysis_case.program_name, analysis_case.original_filename,
           session.expires_at
      INTO v_session
      FROM result.analysis_session AS session
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = session.analysis_case_pk
     WHERE session.user_id = p_user_id
       AND analysis_case.user_id = p_user_id
       AND session.status = 'active'
       AND session.expires_at > v_now
       AND analysis_case.retention_expires_at > v_now
     ORDER BY session.last_activity_at DESC, session.analysis_session_pk DESC
     LIMIT 1;

    IF FOUND THEN
        RETURN jsonb_build_object(
            'state', 'ready', 'run', NULL,
            'session', jsonb_build_object(
                'analysis_session_id', v_session.analysis_session_pk,
                'analysis_case_id', v_session.analysis_case_pk,
                'program_name', v_session.program_name,
                'original_filename', v_session.original_filename,
                'session_expires_at', v_session.expires_at
            )
        );
    END IF;

    RETURN jsonb_build_object('state', 'idle', 'run', NULL, 'session', NULL);
END;
$$;

-- FastAPI-only boundary: authenticated browsers no longer receive direct KB,
-- legacy profile, or request-temp upload grants/policies.  Storage mutations
-- use the server credential; this role switch is required because Storage
-- owns storage.objects in self-hosted Supabase.
REVOKE SELECT ON app.user_profile FROM authenticated;
REVOKE SELECT ON ALL TABLES IN SCHEMA kb FROM authenticated;
REVOKE USAGE ON SCHEMA kb FROM authenticated;
DROP POLICY IF EXISTS user_profile_select_own ON app.user_profile;

-- v0.2 browser clients use Supabase for Auth only.  Retire every legacy
-- PostgREST/Realtime business surface so raw result_data, candidate internals,
-- and the case-id based close RPC cannot bypass FastAPI's strict DTO and
-- owner/session contracts.  Keep the objects themselves for rollback and
-- trusted migration compatibility; only authenticated-browser privileges go.
REVOKE SELECT ON api.v_active_analysis_session,
                 api.v_my_analysis_history,
                 api.v_conversation_messages
FROM authenticated;
REVOKE EXECUTE ON FUNCTION api.rpc_get_analysis_result(UUID),
                           api.rpc_get_sim_candidate_detail(UUID),
                           api.rpc_touch_active_analysis_session(UUID),
                           api.rpc_close_active_analysis_session(UUID)
FROM authenticated;
REVOKE SELECT (
    analysis_run_pk, status, analysis_case_pk,
    error_code, error_message, updated_at
) ON workspace.analysis_run FROM authenticated;
DROP POLICY IF EXISTS analysis_run_select_own ON workspace.analysis_run;
REVOKE USAGE ON SCHEMA app, workspace, result, api FROM authenticated;

DO $$
DECLARE
    v_table TEXT;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'notice','source_profile','source_version','artifact','artifact_lineage',
        'profile_version','support_component','fact_occurrence','fact_evidence',
        'fact_context','fact_relation','fact_component_link','delivery_role',
        'delivery_role_organization','target_constraint','target_constraint_source',
        'target_constraint_dimension','support_facet','support_facet_source',
        'support_facet_value','support_scale_projection','support_scale_measure'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS kb_read_authenticated ON kb.%I', v_table);
    END LOOP;
END;
$$;

REVOKE EXECUTE ON FUNCTION workspace.can_manage_own_reserved_source(TEXT, TEXT[])
FROM authenticated;

SET LOCAL ROLE supabase_storage_admin;
DROP POLICY IF EXISTS request_temp_insert_reserved_source ON storage.objects;
DROP POLICY IF EXISTS request_temp_delete_unprocessed_source ON storage.objects;
SET LOCAL ROLE postgres;

REVOKE ALL ON FUNCTION result.validate_analysis_session_owner_v2() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION workspace.lock_analysis_lifecycle_user_v2(UUID) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION workspace.reconcile_stale_analysis_runs_for_user_core_v2(UUID, BOOLEAN) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION workspace.reconcile_stale_analysis_runs_for_user_v2(UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.claim_next_analysis_run(TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.reserve_analysis_upload_v2(UUID, UUID, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.rpc_close_analysis_session_v2(UUID, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.rpc_get_analysis_history_v2(UUID, TIMESTAMPTZ, TIMESTAMPTZ, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.rpc_get_analysis_current_v2(UUID) FROM PUBLIC, anon, authenticated;

GRANT USAGE ON SCHEMA api, workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.reconcile_stale_analysis_runs_for_user_v2(UUID),
                         workspace.claim_next_analysis_run(TEXT, INTEGER),
                         workspace.reserve_analysis_upload_v2(UUID, UUID, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, INTEGER),
                         api.rpc_close_analysis_session_v2(UUID, UUID),
                         api.rpc_get_analysis_history_v2(UUID, TIMESTAMPTZ, TIMESTAMPTZ, UUID),
                         api.rpc_get_analysis_current_v2(UUID)
TO service_role;

COMMIT;
