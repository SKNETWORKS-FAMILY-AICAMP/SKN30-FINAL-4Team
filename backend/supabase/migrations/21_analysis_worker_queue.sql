-- ============================================================================
-- Migration 21: PostgreSQL analysis-worker queue contract
--
-- ``workspace.analysis_run`` remains the small, browser-visible lifecycle
-- record.  Dispatch-only claim, lease, and fencing state belongs in
-- ``workspace.analysis_run_dispatch`` and is available only to trusted
-- backend/worker callers through the functions below.
--
-- Queue transport is PostgreSQL polling: claim_next_analysis_run() uses
-- FOR UPDATE SKIP LOCKED.  A lease defaults to 120 seconds; callers must
-- heartbeat every 30 seconds.  A run receives at most two total attempts.
-- Every claimed attempt inserts exactly one ops.processing_run row, and that
-- row's processing_run_pk is the fencing token for heartbeat and terminal
-- transitions.  A fenced failed attempt requeues until the second attempt;
-- only the exhausted attempt becomes publicly terminal failed.
-- ============================================================================

BEGIN;

-- Migration 13 introduced this table for source-dispatch data.  Keep all
-- scheduler-private state here rather than expanding the browser-visible run.
ALTER TABLE workspace.analysis_run_dispatch
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS processing_run_pk UUID NULL
        REFERENCES ops.processing_run(processing_run_pk) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS claimed_by TEXT NULL,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS last_error_code TEXT NULL,
    ADD COLUMN IF NOT EXISTS last_error_message TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'workspace.analysis_run_dispatch'::regclass
          AND conname = 'analysis_run_dispatch_attempt_count_check'
    ) THEN
        ALTER TABLE workspace.analysis_run_dispatch
            ADD CONSTRAINT analysis_run_dispatch_attempt_count_check
            CHECK (attempt_count BETWEEN 0 AND 2);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'workspace.analysis_run_dispatch'::regclass
          AND conname = 'analysis_run_dispatch_active_lease_check'
    ) THEN
        ALTER TABLE workspace.analysis_run_dispatch
            ADD CONSTRAINT analysis_run_dispatch_active_lease_check
            CHECK (
                (processing_run_pk IS NULL
                    AND claimed_by IS NULL
                    AND claimed_at IS NULL
                    AND heartbeat_at IS NULL
                    AND lease_expires_at IS NULL)
                OR
                (processing_run_pk IS NOT NULL
                    AND claimed_by IS NOT NULL
                    AND claimed_at IS NOT NULL
                    AND heartbeat_at IS NOT NULL
                    AND lease_expires_at IS NOT NULL)
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'workspace.analysis_run_dispatch'::regclass
          AND conname = 'analysis_run_dispatch_lease_order_check'
    ) THEN
        ALTER TABLE workspace.analysis_run_dispatch
            ADD CONSTRAINT analysis_run_dispatch_lease_order_check
            CHECK (lease_expires_at IS NULL OR lease_expires_at > heartbeat_at);
    END IF;
END;
$$;

-- The queue poll reads public status first and then its private dispatch row.
CREATE INDEX IF NOT EXISTS ix_workspace_analysis_run_queue_poll
    ON workspace.analysis_run (created_at, analysis_run_pk)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS ix_workspace_analysis_run_dispatch_lease
    ON workspace.analysis_run_dispatch (lease_expires_at, analysis_run_pk)
    WHERE processing_run_pk IS NOT NULL;

-- Dispatch contains source locations and worker operational details.  It was
-- created after the original RLS migration, so make that boundary explicit.
ALTER TABLE workspace.analysis_run_dispatch ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE workspace.analysis_run_dispatch FROM PUBLIC, anon, authenticated;

-- Recover one or more expired final attempts before looking for a claimable
-- row.  The same procedure is inlined in claim_next_analysis_run() so a
-- polling worker has one atomic database call and needs no external queue.
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

    -- A stale second attempt cannot be reclaimed again.  Finalise it here so
    -- it never remains publicly "running" forever.  SKIP LOCKED means a
    -- concurrent worker finalising the same run is left alone.
    LOOP
        SELECT ar.analysis_run_pk, dispatch.processing_run_pk
          INTO v_analysis_run_pk, v_previous_processing_run_pk
          FROM workspace.analysis_run ar
          JOIN workspace.analysis_run_dispatch dispatch
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

    -- A legacy running row with no processing_run_pk is deliberately treated
    -- as unleased/stale during rollout.  It is safely reclaimed under the row
    -- lock and receives the first fenced processing-run attempt below.
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
      FROM workspace.analysis_run ar
      JOIN workspace.analysis_run_dispatch dispatch
        ON dispatch.analysis_run_pk = ar.analysis_run_pk
     WHERE dispatch.attempt_count < 2
       AND (
            ar.status = 'queued'
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

    -- A stale first attempt is terminal in ops before its replacement is
    -- created.  Thus every claim creates exactly one processing-run row and
    -- an attempt is never simultaneously represented by two live rows.
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

CREATE OR REPLACE FUNCTION workspace.heartbeat_analysis_run(
    p_analysis_run_pk UUID,
    p_processing_run_pk UUID,
    p_lease_seconds INTEGER DEFAULT 120
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_updated BOOLEAN := FALSE;
BEGIN
    IF p_lease_seconds IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'LEASE_SECONDS_OUT_OF_RANGE' USING ERRCODE = '22023';
    END IF;

    -- A heartbeat after expiry is fenced out even if no other worker has
    -- reclaimed the row yet.  The caller must stop work when this returns
    -- false; continuing would risk producing an unfenced late result.
    UPDATE workspace.analysis_run_dispatch dispatch
       SET heartbeat_at = v_now,
           lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
      FROM workspace.analysis_run ar
     WHERE dispatch.analysis_run_pk = p_analysis_run_pk
       AND ar.analysis_run_pk = dispatch.analysis_run_pk
       AND ar.status = 'running'
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND dispatch.lease_expires_at > v_now
    RETURNING TRUE INTO v_updated;

    RETURN COALESCE(v_updated, FALSE);
END;
$$;

CREATE OR REPLACE FUNCTION workspace.complete_analysis_run(
    p_analysis_run_pk UUID,
    p_processing_run_pk UUID,
    p_analysis_case_pk UUID DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, ops, workspace
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    -- Lock both rows after checking the processing-run primary key.  A worker
    -- that lost its lease receives false and must discard its late output.
    PERFORM 1
      FROM workspace.analysis_run ar
      JOIN workspace.analysis_run_dispatch dispatch
        ON dispatch.analysis_run_pk = ar.analysis_run_pk
     WHERE ar.analysis_run_pk = p_analysis_run_pk
       AND ar.status = 'running'
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND dispatch.lease_expires_at > v_now
     FOR UPDATE OF ar, dispatch;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE ops.processing_run
       SET status = 'succeeded',
           finished_at = v_now,
           error_code = NULL,
           error_message = NULL
     WHERE processing_run_pk = p_processing_run_pk
       AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'PROCESSING_RUN_NOT_LIVE' USING ERRCODE = 'P0001';
    END IF;

    UPDATE workspace.analysis_run_dispatch
       SET processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL,
           last_error_code = NULL,
           last_error_message = NULL
     WHERE analysis_run_pk = p_analysis_run_pk;

    UPDATE workspace.analysis_run
       SET status = 'succeeded',
           completed_at = v_now,
           analysis_case_pk = COALESCE(p_analysis_case_pk, analysis_case_pk),
           error_code = NULL,
           error_message = NULL
     WHERE analysis_run_pk = p_analysis_run_pk;

    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.fail_analysis_run(
    p_analysis_run_pk UUID,
    p_processing_run_pk UUID,
    p_error_code TEXT,
    p_error_message TEXT,
    p_internal_error_message TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, ops, workspace
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_attempt_count INTEGER;
BEGIN
    IF btrim(COALESCE(p_error_code, '')) = ''
       OR btrim(COALESCE(p_error_message, '')) = '' THEN
        RAISE EXCEPTION 'ATTEMPT_ERROR_DETAILS_REQUIRED' USING ERRCODE = '22023';
    END IF;

    SELECT dispatch.attempt_count
      INTO v_attempt_count
      FROM workspace.analysis_run ar
      JOIN workspace.analysis_run_dispatch dispatch
        ON dispatch.analysis_run_pk = ar.analysis_run_pk
     WHERE ar.analysis_run_pk = p_analysis_run_pk
       AND ar.status = 'running'
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND dispatch.lease_expires_at > v_now
     FOR UPDATE OF ar, dispatch;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE ops.processing_run
       SET status = 'failed',
           finished_at = v_now,
           error_code = btrim(p_error_code),
           error_message = COALESCE(NULLIF(btrim(p_internal_error_message), ''), btrim(p_error_message))
     WHERE processing_run_pk = p_processing_run_pk
       AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'PROCESSING_RUN_NOT_LIVE' USING ERRCODE = 'P0001';
    END IF;

    UPDATE workspace.analysis_run_dispatch
       SET processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL,
           last_error_code = btrim(p_error_code),
           last_error_message = COALESCE(NULLIF(btrim(p_internal_error_message), ''), btrim(p_error_message))
     WHERE analysis_run_pk = p_analysis_run_pk;

    -- Explicit failures use the same two-attempt budget as stale leases.
    -- The first failed processing_run remains a durable ops audit row, while
    -- the public run returns to queued with no contradictory terminal error.
    IF v_attempt_count < 2 THEN
        UPDATE workspace.analysis_run
           SET status = 'queued',
               completed_at = NULL,
               error_code = NULL,
               error_message = NULL
         WHERE analysis_run_pk = p_analysis_run_pk;
    ELSE
        UPDATE workspace.analysis_run
           SET status = 'failed',
               completed_at = v_now,
               error_code = btrim(p_error_code),
               error_message = btrim(p_error_message)
         WHERE analysis_run_pk = p_analysis_run_pk;
    END IF;

    RETURN TRUE;
END;
$$;

COMMENT ON FUNCTION workspace.claim_next_analysis_run(TEXT, INTEGER) IS
    'Trusted PostgreSQL polling claim. Defaults to a 120-second lease and returns a 30-second heartbeat cadence; processing_run_pk is the fencing token.';
COMMENT ON FUNCTION workspace.heartbeat_analysis_run(UUID, UUID, INTEGER) IS
    'Trusted worker heartbeat. Returns false when the processing_run_pk fence is no longer live.';
COMMENT ON FUNCTION workspace.complete_analysis_run(UUID, UUID, UUID) IS
    'Trusted fenced terminal-success transition. Returns false after lease expiry or a replaced processing-run token.';
COMMENT ON FUNCTION workspace.fail_analysis_run(UUID, UUID, TEXT, TEXT, TEXT) IS
    'Trusted fenced failed-attempt transition. Returns false after lease expiry or a replaced processing-run token; first failure requeues and the second is terminal.';

REVOKE ALL ON FUNCTION workspace.claim_next_analysis_run(TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.heartbeat_analysis_run(UUID, UUID, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.complete_analysis_run(UUID, UUID, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.fail_analysis_run(UUID, UUID, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;

GRANT USAGE ON SCHEMA workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.claim_next_analysis_run(TEXT, INTEGER),
                         workspace.heartbeat_analysis_run(UUID, UUID, INTEGER),
                         workspace.complete_analysis_run(UUID, UUID, UUID),
                         workspace.fail_analysis_run(UUID, UUID, TEXT, TEXT, TEXT)
TO service_role;

COMMIT;
