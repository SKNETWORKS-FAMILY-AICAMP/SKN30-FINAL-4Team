-- ============================================================================
-- Migration 25: Queued source-artifact invariant
--
-- A polling worker must never claim a queued analysis whose immutable source
-- metadata is missing or contradictory.  Existing inconsistent queued rows
-- are failed closed under writer-draining table locks before this transaction
-- commits; no Storage object is deleted by this migration.
-- ============================================================================

BEGIN;

-- Installation and legacy-row quarantine must observe one stable writer-free
-- snapshot. These locks conflict with INSERT/UPDATE/DELETE while still allowing
-- ordinary readers; the deployment runbook already requires API/worker drain.
LOCK TABLE workspace.analysis_run,
           workspace.analysis_run_dispatch,
           workspace.source_artifact,
           ops.processing_run
IN SHARE ROW EXCLUSIVE MODE;

-- Deleting or relabelling duplicate source records would destroy provenance.
-- Stop with an actionable error instead, so an operator can reconcile the
-- conflicting records before this invariant is installed.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM workspace.source_artifact
         WHERE artifact_type = 'source'
         GROUP BY analysis_run_pk
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'DUPLICATE_SOURCE_ARTIFACTS_REQUIRE_REPAIR'
            USING ERRCODE = '23505';
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_source_artifact_one_source_per_run
    ON workspace.source_artifact (analysis_run_pk)
    WHERE artifact_type = 'source';

-- Quarantine pre-existing queued rows which could not satisfy the new
-- transition contract.  This remains an internal function so the runtime
-- contract can exercise the exact migration repair logic; browser and service
-- roles cannot invoke it.
CREATE OR REPLACE FUNCTION workspace.quarantine_invalid_queued_analysis_runs()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_analysis_run_pk UUID;
    v_now TIMESTAMPTZ;
    v_quarantined_count INTEGER := 0;
BEGIN
    -- The helper is deliberately owner-only, but it can also be invoked by an
    -- operator after rollout.  Take the same writer-blocking locks used during
    -- installation so the candidate scan and all three quarantine updates are
    -- one stable operation rather than a cross-table check/use race.
    LOCK TABLE workspace.analysis_run,
               workspace.analysis_run_dispatch,
               workspace.source_artifact,
               ops.processing_run
    IN SHARE ROW EXCLUSIVE MODE;

    FOR v_analysis_run_pk IN
        SELECT ar.analysis_run_pk
          FROM workspace.analysis_run AS ar
         WHERE ar.status = 'queued'
           AND NOT (
                ar.declared_size_bytes IS NOT NULL
                AND EXISTS (
                    SELECT 1
                      FROM workspace.analysis_run_dispatch AS dispatch
                      JOIN workspace.source_artifact AS source
                        ON source.analysis_run_pk = dispatch.analysis_run_pk
                       AND source.artifact_type = 'source'
                       AND source.storage_bucket = dispatch.source_bucket
                       AND source.storage_object_key = dispatch.source_object_key
                       AND source.content_sha256 = dispatch.source_content_sha256
                       AND source.size_bytes = ar.declared_size_bytes
                     WHERE dispatch.analysis_run_pk = ar.analysis_run_pk
                       AND dispatch.source_content_sha256 IS NOT NULL
                       AND dispatch.processing_run_pk IS NULL
                       AND dispatch.claimed_by IS NULL
                       AND dispatch.claimed_at IS NULL
                       AND dispatch.heartbeat_at IS NULL
                       AND dispatch.lease_expires_at IS NULL
                )
                AND NOT EXISTS (
                    SELECT 1
                      FROM ops.processing_run AS processing_attempt
                     WHERE processing_attempt.source_analysis_run_id = ar.analysis_run_pk
                       AND processing_attempt.status IN ('queued', 'running')
                )
           )
         ORDER BY ar.created_at, ar.analysis_run_pk
         FOR UPDATE OF ar
    LOOP
        v_now := clock_timestamp();

        UPDATE ops.processing_run AS processing_attempt
           SET status = 'failed',
               finished_at = v_now,
               error_code = 'QUEUED_SOURCE_INVARIANT_VIOLATION',
               error_message = 'Queued analysis was quarantined because its source contract was incomplete.'
         WHERE processing_attempt.source_analysis_run_id = v_analysis_run_pk
           AND processing_attempt.status IN ('queued', 'running');

        UPDATE workspace.analysis_run_dispatch AS dispatch
           SET processing_run_pk = NULL,
               claimed_by = NULL,
               claimed_at = NULL,
               heartbeat_at = NULL,
               lease_expires_at = NULL,
               last_error_code = 'QUEUED_SOURCE_INVARIANT_VIOLATION',
               last_error_message = 'Queued analysis was quarantined because its source contract was incomplete.'
         WHERE dispatch.analysis_run_pk = v_analysis_run_pk;

        UPDATE workspace.analysis_run AS ar
           SET status = 'failed',
               completed_at = v_now,
               error_code = 'QUEUED_SOURCE_INVARIANT_VIOLATION',
               error_message = '요청 원본 파일을 확인할 수 없어 분석을 시작하지 못했습니다.'
         WHERE ar.analysis_run_pk = v_analysis_run_pk
           AND ar.status = 'queued';

        v_quarantined_count := v_quarantined_count + 1;
    END LOOP;

    RETURN v_quarantined_count;
END;
$$;

COMMENT ON FUNCTION workspace.quarantine_invalid_queued_analysis_runs() IS
    'Internal migration/reconciliation helper. Fails invalid queued runs and their live processing attempts without deleting retained source provenance.';

REVOKE ALL ON FUNCTION workspace.quarantine_invalid_queued_analysis_runs()
FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION workspace.enforce_queued_source_invariant()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    -- INSERT may create only the pre-queue uploading reservation. UPDATE may
    -- enter an active state only through the supported state-machine edges.
    -- The UPDATE statement itself holds this analysis_run row lock; every
    -- source/dispatch trigger below locks the same parent row before deciding.
    IF TG_OP = 'INSERT' AND NEW.status IN ('queued', 'running') THEN
        RAISE EXCEPTION 'ACTIVE_ANALYSIS_RUN_REQUIRES_UPLOAD_RESERVATION'
            USING ERRCODE = '23514';
    ELSIF TG_OP = 'UPDATE'
          AND NEW.status IS DISTINCT FROM OLD.status
          AND (
              (NEW.status = 'uploading')
              OR (NEW.status = 'queued' AND OLD.status NOT IN ('uploading', 'running'))
              OR (NEW.status = 'running' AND OLD.status <> 'queued')
          ) THEN
        RAISE EXCEPTION 'INVALID_ACTIVE_ANALYSIS_RUN_TRANSITION'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.status = 'queued' AND NOT (
        NEW.declared_size_bytes IS NOT NULL
        AND EXISTS (
            SELECT 1
              FROM workspace.analysis_run_dispatch AS dispatch
              JOIN workspace.source_artifact AS source
                ON source.analysis_run_pk = dispatch.analysis_run_pk
               AND source.artifact_type = 'source'
               AND source.storage_bucket = dispatch.source_bucket
               AND source.storage_object_key = dispatch.source_object_key
               AND source.content_sha256 = dispatch.source_content_sha256
               AND source.size_bytes = NEW.declared_size_bytes
             WHERE dispatch.analysis_run_pk = NEW.analysis_run_pk
               AND dispatch.source_content_sha256 IS NOT NULL
               AND dispatch.processing_run_pk IS NULL
               AND dispatch.claimed_by IS NULL
               AND dispatch.claimed_at IS NULL
               AND dispatch.heartbeat_at IS NULL
               AND dispatch.lease_expires_at IS NULL
        )
        AND NOT EXISTS (
            SELECT 1
              FROM ops.processing_run AS processing_attempt
             WHERE processing_attempt.source_analysis_run_id = NEW.analysis_run_pk
               AND processing_attempt.status IN ('queued', 'running')
        )
    ) THEN
        RAISE EXCEPTION 'QUEUED_SOURCE_INVARIANT_VIOLATION'
            USING ERRCODE = '23514';
    END IF;

    -- A direct status edit must not manufacture an unfenced running job.  The
    -- polling claim function first creates its processing attempt and lease,
    -- then performs this transition in the same transaction, so its complete
    -- final tuple is already visible here.
    IF NEW.status = 'running' AND NOT EXISTS (
        SELECT 1
          FROM workspace.analysis_run_dispatch AS dispatch
          JOIN ops.processing_run AS processing_attempt
            ON processing_attempt.processing_run_pk = dispatch.processing_run_pk
           AND processing_attempt.source_analysis_run_id = NEW.analysis_run_pk
           AND processing_attempt.status = 'running'
         WHERE dispatch.analysis_run_pk = NEW.analysis_run_pk
           AND dispatch.claimed_by IS NOT NULL
           AND dispatch.claimed_at IS NOT NULL
           AND dispatch.heartbeat_at IS NOT NULL
           AND dispatch.lease_expires_at > clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'RUNNING_ANALYSIS_REQUIRES_LIVE_FENCE'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.status IN ('succeeded', 'failed', 'cancelled', 'cleanup_pending')
       AND (
            EXISTS (
                SELECT 1
                  FROM workspace.analysis_run_dispatch AS dispatch
                 WHERE dispatch.analysis_run_pk = NEW.analysis_run_pk
                   AND (
                        dispatch.processing_run_pk IS NOT NULL
                        OR dispatch.claimed_by IS NOT NULL
                        OR dispatch.claimed_at IS NOT NULL
                        OR dispatch.heartbeat_at IS NOT NULL
                        OR dispatch.lease_expires_at IS NOT NULL
                   )
            )
            OR EXISTS (
                SELECT 1
                  FROM ops.processing_run AS processing_attempt
                 WHERE processing_attempt.source_analysis_run_id = NEW.analysis_run_pk
                   AND processing_attempt.status IN ('queued', 'running')
            )
       ) THEN
        RAISE EXCEPTION 'TERMINAL_ANALYSIS_REQUIRES_CLEARED_FENCE'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION workspace.enforce_queued_source_invariant() IS
    'Fail-closed trigger guard: queued runs require one exact immutable source artifact, a non-null dispatch SHA-256, matching size, and an empty worker lease.';

REVOKE ALL ON FUNCTION workspace.enforce_queued_source_invariant()
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_workspace_analysis_run_queued_source_invariant
ON workspace.analysis_run;
CREATE TRIGGER trg_workspace_analysis_run_queued_source_invariant
BEFORE INSERT OR UPDATE OF status
ON workspace.analysis_run
FOR EACH ROW
EXECUTE FUNCTION workspace.enforce_queued_source_invariant();

-- Once a source is visible to the queue, its metadata must remain the same for
-- the entire active attempt.  The queue trigger alone cannot prevent a later
-- direct UPDATE/DELETE on the two metadata tables.
CREATE OR REPLACE FUNCTION workspace.protect_active_source_artifact()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_analysis_run_pk UUID;
    v_run_status TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.artifact_type <> 'source' THEN
            RETURN NEW;
        END IF;
        v_analysis_run_pk := NEW.analysis_run_pk;
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.artifact_type <> 'source' THEN
            RETURN OLD;
        END IF;
        v_analysis_run_pk := OLD.analysis_run_pk;
    ELSE
        IF OLD.artifact_type <> 'source' AND NEW.artifact_type <> 'source' THEN
            RETURN NEW;
        END IF;
        v_analysis_run_pk := OLD.analysis_run_pk;
    END IF;

    -- This is the cross-table serialization point shared with the status-row
    -- lock held by queued/running transitions.
    SELECT ar.status
      INTO v_run_status
      FROM workspace.analysis_run AS ar
     WHERE ar.analysis_run_pk = v_analysis_run_pk
     FOR UPDATE;

    IF TG_OP = 'INSERT' THEN
        IF v_run_status IS DISTINCT FROM 'uploading' THEN
            RAISE EXCEPTION 'SOURCE_ARTIFACT_REQUIRES_UPLOAD_RESERVATION'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    -- Source artifacts are content-addressed immutable records. Repair creates
    -- a new reservation; it never changes an existing source record in place.
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'SOURCE_ARTIFACT_IMMUTABLE'
            USING ERRCODE = '23514';
    END IF;

    IF v_run_status IN ('uploading', 'queued', 'running') THEN
        RAISE EXCEPTION 'ACTIVE_SOURCE_ARTIFACT_IMMUTABLE'
            USING ERRCODE = '23514';
    END IF;

    RETURN OLD;
END;
$$;

REVOKE ALL ON FUNCTION workspace.protect_active_source_artifact()
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_workspace_protect_active_source_artifact
ON workspace.source_artifact;
CREATE TRIGGER trg_workspace_protect_active_source_artifact
BEFORE INSERT OR UPDATE OR DELETE
ON workspace.source_artifact
FOR EACH ROW
EXECUTE FUNCTION workspace.protect_active_source_artifact();

CREATE OR REPLACE FUNCTION workspace.protect_active_dispatch_source()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_source_changed BOOLEAN := TRUE;
    v_analysis_run_pk UUID;
    v_run_status TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_analysis_run_pk := NEW.analysis_run_pk;
    ELSIF TG_OP = 'UPDATE' THEN
        v_source_changed := ROW(
            OLD.analysis_run_pk,
            OLD.source_bucket,
            OLD.source_object_key,
            OLD.source_content_sha256
        ) IS DISTINCT FROM ROW(
            NEW.analysis_run_pk,
            NEW.source_bucket,
            NEW.source_object_key,
            NEW.source_content_sha256
        );
        v_analysis_run_pk := OLD.analysis_run_pk;
    ELSE
        v_analysis_run_pk := OLD.analysis_run_pk;
    END IF;

    SELECT ar.status
      INTO v_run_status
      FROM workspace.analysis_run AS ar
     WHERE ar.analysis_run_pk = v_analysis_run_pk
     FOR UPDATE;

    IF TG_OP = 'INSERT' THEN
        IF v_run_status IS DISTINCT FROM 'uploading' THEN
            RAISE EXCEPTION 'DISPATCH_SOURCE_REQUIRES_UPLOAD_RESERVATION'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF v_source_changed THEN
            RAISE EXCEPTION 'DISPATCH_SOURCE_IMMUTABLE'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF v_run_status IN ('uploading', 'queued', 'running') THEN
        RAISE EXCEPTION 'ACTIVE_DISPATCH_SOURCE_IMMUTABLE'
            USING ERRCODE = '23514';
    END IF;

    RETURN OLD;
END;
$$;

REVOKE ALL ON FUNCTION workspace.protect_active_dispatch_source()
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_workspace_protect_active_dispatch_source
ON workspace.analysis_run_dispatch;
CREATE TRIGGER trg_workspace_protect_active_dispatch_source
BEFORE INSERT OR UPDATE OR DELETE
ON workspace.analysis_run_dispatch
FOR EACH ROW
EXECUTE FUNCTION workspace.protect_active_dispatch_source();

-- Validate the final transaction state for both dispatch-lease and live-ops
-- changes. Immediate triggers take the common parent row lock; this helper is
-- deferred so the existing claim/fail/result functions may perform their
-- ordered multi-row transition before the invariant is evaluated.
CREATE OR REPLACE FUNCTION workspace.assert_analysis_run_fence(
    p_analysis_run_pk UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_run_status TEXT;
    v_processing_run_pk UUID;
    v_claimed_by TEXT;
    v_claimed_at TIMESTAMPTZ;
    v_heartbeat_at TIMESTAMPTZ;
    v_lease_expires_at TIMESTAMPTZ;
BEGIN
    SELECT ar.status,
           dispatch.processing_run_pk,
           dispatch.claimed_by,
           dispatch.claimed_at,
           dispatch.heartbeat_at,
           dispatch.lease_expires_at
      INTO v_run_status,
           v_processing_run_pk,
           v_claimed_by,
           v_claimed_at,
           v_heartbeat_at,
           v_lease_expires_at
      FROM workspace.analysis_run AS ar
      LEFT JOIN workspace.analysis_run_dispatch AS dispatch
        ON dispatch.analysis_run_pk = ar.analysis_run_pk
     WHERE ar.analysis_run_pk = p_analysis_run_pk;

    -- Parent deletion may cascade the dispatch row. No active workspace state
    -- remains in that case, so there is nothing for this helper to fence.
    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF v_run_status = 'running' THEN
        IF v_processing_run_pk IS NULL
           OR v_claimed_by IS NULL
           OR v_claimed_at IS NULL
           OR v_heartbeat_at IS NULL
           OR v_lease_expires_at IS NULL
           OR v_lease_expires_at <= clock_timestamp()
           OR NOT EXISTS (
                SELECT 1
                  FROM ops.processing_run AS processing_attempt
                 WHERE processing_attempt.processing_run_pk = v_processing_run_pk
                   AND processing_attempt.source_analysis_run_id = p_analysis_run_pk
                   AND processing_attempt.run_type = 'analysis'
                   AND processing_attempt.status = 'running'
           )
           OR EXISTS (
                SELECT 1
                  FROM ops.processing_run AS processing_attempt
                 WHERE processing_attempt.source_analysis_run_id = p_analysis_run_pk
                   AND processing_attempt.status IN ('queued', 'running')
                   AND processing_attempt.processing_run_pk <> v_processing_run_pk
           ) THEN
            RAISE EXCEPTION 'LIVE_PROCESSING_ATTEMPT_FENCE_INVARIANT_VIOLATION'
                USING ERRCODE = '23514';
        END IF;
    ELSIF v_processing_run_pk IS NOT NULL
          OR v_claimed_by IS NOT NULL
          OR v_claimed_at IS NOT NULL
          OR v_heartbeat_at IS NOT NULL
          OR v_lease_expires_at IS NOT NULL
          OR EXISTS (
                SELECT 1
                  FROM ops.processing_run AS processing_attempt
                 WHERE processing_attempt.source_analysis_run_id = p_analysis_run_pk
                   AND processing_attempt.status IN ('queued', 'running')
          ) THEN
        RAISE EXCEPTION 'NON_RUNNING_ANALYSIS_HAS_LIVE_FENCE'
            USING ERRCODE = '23514';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION workspace.assert_analysis_run_fence(UUID)
FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION workspace.enforce_dispatch_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM workspace.assert_analysis_run_fence(NEW.analysis_run_pk);
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION workspace.enforce_dispatch_fence()
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_workspace_enforce_dispatch_fence
ON workspace.analysis_run_dispatch;
CREATE CONSTRAINT TRIGGER trg_workspace_enforce_dispatch_fence
AFTER INSERT OR UPDATE
ON workspace.analysis_run_dispatch
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION workspace.enforce_dispatch_fence();

-- The source size participates in the queued invariant but is stored on the
-- analysis row.  Protect it alongside both source metadata records.
CREATE OR REPLACE FUNCTION workspace.protect_active_source_size()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.declared_size_bytes IS DISTINCT FROM NEW.declared_size_bytes THEN
        -- The row being updated is already locked. Declared source metadata is
        -- immutable from reservation onward; cleanup creates no in-place edit.
        RAISE EXCEPTION 'SOURCE_SIZE_IMMUTABLE'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION workspace.protect_active_source_size()
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_workspace_protect_active_source_size
ON workspace.analysis_run;
CREATE TRIGGER trg_workspace_protect_active_source_size
BEFORE UPDATE OF declared_size_bytes
ON workspace.analysis_run
FOR EACH ROW
EXECUTE FUNCTION workspace.protect_active_source_size();

-- A live ops attempt also participates in the queued invariant. Serialize its
-- lifecycle against the parent analysis row, then verify at transaction end
-- that a live attempt is the exact fence held by one running dispatch.
CREATE OR REPLACE FUNCTION workspace.serialize_analysis_processing_attempt()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_analysis_run_pk UUID;
    v_run_status TEXT;
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.source_analysis_run_id IS DISTINCT FROM NEW.source_analysis_run_id
       AND OLD.source_analysis_run_id IS NOT NULL THEN
        RAISE EXCEPTION 'PROCESSING_ATTEMPT_RUN_IMMUTABLE'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.source_analysis_run_id IS NULL
           OR NEW.status NOT IN ('queued', 'running') THEN
            RETURN NEW;
        END IF;
        v_analysis_run_pk := NEW.source_analysis_run_id;
    ELSE
        IF NEW.source_analysis_run_id IS NULL
           OR (
                OLD.status NOT IN ('queued', 'running')
                AND NEW.status NOT IN ('queued', 'running')
           ) THEN
            RETURN NEW;
        END IF;
        v_analysis_run_pk := NEW.source_analysis_run_id;
    END IF;

    SELECT ar.status
      INTO v_run_status
      FROM workspace.analysis_run AS ar
     WHERE ar.analysis_run_pk = v_analysis_run_pk
     FOR UPDATE;

    IF NEW.status IN ('queued', 'running')
       AND (
            v_run_status IS NULL
            OR v_run_status NOT IN ('queued', 'running')
       ) THEN
        RAISE EXCEPTION 'LIVE_PROCESSING_ATTEMPT_REQUIRES_ACTIVE_RUN'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION workspace.serialize_analysis_processing_attempt()
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_ops_serialize_analysis_processing_attempt
ON ops.processing_run;
CREATE TRIGGER trg_ops_serialize_analysis_processing_attempt
BEFORE INSERT OR UPDATE OF source_analysis_run_id, status
ON ops.processing_run
FOR EACH ROW
EXECUTE FUNCTION workspace.serialize_analysis_processing_attempt();

CREATE OR REPLACE FUNCTION workspace.enforce_live_processing_attempt_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_source_analysis_run_id UUID;
    v_status TEXT;
BEGIN
    -- Read the final row, not the transition's OLD/NEW image: a processing
    -- attempt may be inserted and failed within the same transaction during
    -- migration quarantine or fenced recovery.
    SELECT processing_attempt.source_analysis_run_id,
           processing_attempt.status
      INTO v_source_analysis_run_id, v_status
      FROM ops.processing_run AS processing_attempt
     WHERE processing_attempt.processing_run_pk = NEW.processing_run_pk;

    IF NOT FOUND
       OR v_source_analysis_run_id IS NULL THEN
        RETURN NULL;
    END IF;

    -- Terminal audit/stage rows do not participate in the live fence. A row
    -- which was or is live does, even when its final image is already terminal.
    IF NEW.status IN ('queued', 'running')
       OR (TG_OP = 'UPDATE' AND OLD.status IN ('queued', 'running')) THEN
        PERFORM workspace.assert_analysis_run_fence(v_source_analysis_run_id);
    END IF;

    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION workspace.enforce_live_processing_attempt_fence()
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_ops_enforce_live_processing_attempt_fence
ON ops.processing_run;
CREATE CONSTRAINT TRIGGER trg_ops_enforce_live_processing_attempt_fence
AFTER INSERT OR UPDATE OF source_analysis_run_id, status
ON ops.processing_run
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION workspace.enforce_live_processing_attempt_fence();

-- Run the one-time reconciliation only after all permanent guards are in
-- place.  This ordering is also safe on a reviewed re-run: quarantine-created
-- deferred trigger events are never followed by DROP TRIGGER in this file.
SELECT workspace.quarantine_invalid_queued_analysis_runs();

COMMIT;
