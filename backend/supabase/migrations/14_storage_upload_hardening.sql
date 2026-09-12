-- ============================================================================
-- Migration 14: Browser Upload Reservation Hardening
-- Date: 2026-09-08
--
-- A browser can upload only the exact Storage object reserved by its own Edge
-- Function-created analysis run. HWP/HWPX MIME values vary by browser, so
-- server-side extension/content validation remains the worker's responsibility.
-- ============================================================================

BEGIN;

-- Bucket rows are official Storage-owned objects.
SET LOCAL ROLE supabase_storage_admin;

UPDATE storage.buckets
   SET public = FALSE,
       file_size_limit = 52428800
 WHERE id = 'request-temp';

-- SECURITY DEFINER function ownership must remain postgres, never Storage.
SET LOCAL ROLE postgres;

CREATE OR REPLACE FUNCTION workspace.can_manage_own_reserved_source(
    p_object_key TEXT,
    p_allowed_statuses TEXT[]
)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, workspace
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM workspace.analysis_run ar
        JOIN workspace.analysis_run_dispatch dispatch
          ON dispatch.analysis_run_pk = ar.analysis_run_pk
        WHERE ar.user_id = auth.uid()
          AND dispatch.source_bucket = 'request-temp'
          AND dispatch.source_object_key = p_object_key
          AND ar.status = ANY (p_allowed_statuses)
    );
$$;

REVOKE ALL ON FUNCTION workspace.can_manage_own_reserved_source(TEXT, TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION workspace.can_manage_own_reserved_source(TEXT, TEXT[]) TO authenticated;

-- CREATE POLICY resolves auth.uid/workspace.can_manage_own_reserved_source as
-- its current role (the Storage table owner), not as the eventual
-- authenticated caller. Keep those parser-only schema permissions
-- transactional and avoid revoking a privilege an operator already supplied.
-- auth is not postgres-owned, so use the local peer admin for both grants.
SET LOCAL ROLE supabase_admin;
DO $$
DECLARE
    storage_role_had_auth_usage BOOLEAN;
    storage_role_had_workspace_usage BOOLEAN;
BEGIN
    storage_role_had_auth_usage := has_schema_privilege(
        'supabase_storage_admin', 'auth', 'USAGE'
    );
    storage_role_had_workspace_usage := has_schema_privilege(
        'supabase_storage_admin', 'workspace', 'USAGE'
    );
    PERFORM set_config(
        'pre_review.m14_storage_auth_usage_preexisting',
        CASE WHEN storage_role_had_auth_usage THEN 'true' ELSE 'false' END,
        TRUE
    );
    PERFORM set_config(
        'pre_review.m14_storage_workspace_usage_preexisting',
        CASE WHEN storage_role_had_workspace_usage THEN 'true' ELSE 'false' END,
        TRUE
    );

    IF NOT storage_role_had_auth_usage THEN
        GRANT USAGE ON SCHEMA auth TO supabase_storage_admin;
    END IF;

    IF NOT storage_role_had_workspace_usage THEN
        GRANT USAGE ON SCHEMA workspace TO supabase_storage_admin;
    END IF;
END;
$$;

-- Return to the Storage owner for policy DDL on storage.objects.
SET LOCAL ROLE supabase_storage_admin;

DROP POLICY IF EXISTS request_temp_insert_own_prefix ON storage.objects;
DROP POLICY IF EXISTS request_temp_insert_reserved_source ON storage.objects;
CREATE POLICY request_temp_insert_reserved_source
ON storage.objects
FOR INSERT
TO authenticated
WITH CHECK (
    bucket_id = 'request-temp'
    AND (storage.foldername(name))[1] = 'request-source'
    AND (storage.foldername(name))[2] = (SELECT auth.uid()::text)
    AND workspace.can_manage_own_reserved_source(name, ARRAY['uploading'])
);

DROP POLICY IF EXISTS request_temp_delete_own_prefix ON storage.objects;
DROP POLICY IF EXISTS request_temp_delete_unprocessed_source ON storage.objects;
CREATE POLICY request_temp_delete_unprocessed_source
ON storage.objects
FOR DELETE
TO authenticated
USING (
    bucket_id = 'request-temp'
    AND workspace.can_manage_own_reserved_source(
        name, ARRAY['uploading','failed','cancelled']
    )
);

-- Revoke only the transaction-scoped parser aids granted above. The policy is
-- evaluated by authenticated callers, which already receive auth/workspace
-- access and EXECUTE on the helper through the normal runtime grants.
SET LOCAL ROLE supabase_admin;
DO $$
BEGIN
    IF current_setting(
        'pre_review.m14_storage_auth_usage_preexisting', TRUE
    ) = 'false' THEN
        REVOKE USAGE ON SCHEMA auth FROM supabase_storage_admin;
    END IF;

    IF current_setting(
        'pre_review.m14_storage_workspace_usage_preexisting', TRUE
    ) = 'false' THEN
        REVOKE USAGE ON SCHEMA workspace FROM supabase_storage_admin;
    END IF;
END;
$$;

COMMIT;
