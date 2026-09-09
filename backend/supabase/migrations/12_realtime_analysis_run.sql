-- ============================================================================
-- Migration 12: Realtime Analysis Run Status
-- Date: 2026-09-08
--
-- RLS already limits workspace.analysis_run SELECT to its owner. Adding this
-- table to the publication lets that owner receive status UPDATE events.
-- ============================================================================

BEGIN;

DO $$
BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE workspace.analysis_run;
EXCEPTION
    -- Fresh PostgreSQL instances may not have the Storage/Realtime service
    -- publication yet. The full Supabase stack creates it during startup;
    -- avoid failing the entire schema bootstrap before that point.
    WHEN duplicate_object OR undefined_object THEN NULL;
END $$;

COMMIT;
