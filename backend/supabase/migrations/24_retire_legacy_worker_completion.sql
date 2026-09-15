BEGIN;

-- Migration 21 exposed a fenced state-only success transition before result
-- materialisation was implemented.  Migrations 22-23 replaced it with
-- persist_analysis_result_core(), which writes the result and marks the run
-- succeeded in one transaction.  Keeping this older function would allow a
-- trusted legacy caller to publish a succeeded run without any result rows.
DROP FUNCTION IF EXISTS workspace.complete_analysis_run(UUID, UUID, UUID);

COMMIT;
