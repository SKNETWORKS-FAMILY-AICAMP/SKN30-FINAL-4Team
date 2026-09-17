"""Static contracts for the private PDF report queue migration."""

from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "41_report_pdf_worker_queue.sql"
)


def test_pdf_queue_is_private_and_one_pdf_artifact_is_owned_by_each_case() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "RENAME TO public_analysis_projection_v2_base" in sql
    assert "report.report_type = 'pdf'" in sql
    assert "PDF_REPORT_DUPLICATES_REQUIRE_CONSOLIDATION" in sql
    assert "SET LOCAL ROLE supabase_storage_admin;" in sql
    assert "file_size_limit = 26214400" in sql
    assert sql.index("SET LOCAL ROLE supabase_storage_admin;") < sql.index("UPDATE storage.buckets") < sql.index("SET LOCAL ROLE postgres;", sql.index("UPDATE storage.buckets"))
    assert "uq_result_report_artifact_one_pdf_per_case" in sql
    assert "WHERE report_type = 'pdf'" in sql
    assert "CREATE TABLE IF NOT EXISTS workspace.report_pdf_dispatch" in sql
    assert "report_artifact_pk UUID PRIMARY KEY" in sql
    assert "attempt_count BETWEEN 0 AND 3" in sql
    assert "ALTER TABLE workspace.report_pdf_dispatch ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON TABLE workspace.report_pdf_dispatch" in sql


def test_ready_case_trigger_enqueues_only_future_completions() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "workspace.enqueue_pdf_report_for_ready_case" in sql
    assert "AFTER INSERT OR UPDATE OF case_status, analysis_completed_at" in sql
    assert "NEW.case_status <> 'ready'" in sql
    assert "OLD.case_status = 'ready'" in sql
    assert "OLD.analysis_completed_at IS NOT DISTINCT FROM NEW.analysis_completed_at" in sql
    assert "ON CONFLICT (analysis_case_pk) WHERE (report_type = 'pdf')" in sql
    assert "INSERT INTO workspace.report_pdf_dispatch" in sql
    assert "Do not enqueue pre-migration completed cases" in sql
    after_trigger = sql[
        sql.index("CREATE TRIGGER trg_result_analysis_case_enqueue_pdf_report") :
        sql.index("-- Worker queue RPCs")
    ]
    assert "INSERT INTO result.report_artifact" not in after_trigger
    assert "INSERT INTO workspace.report_pdf_dispatch" not in after_trigger
    assert sql.count("INSERT INTO result.report_artifact") == 1
    assert sql.count("INSERT INTO workspace.report_pdf_dispatch") == 1
    assert "'status', 'failed', 'can_download', false" in sql


def test_projection_base_declares_a_full_replay_refresh_guard() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "procedure.oid = to_regprocedure(" in sql
    assert "v_projection_source NOT LIKE '%public_analysis_projection_v2_base%'" in sql
    assert "DROP FUNCTION IF EXISTS api.public_analysis_projection_v2_base" in sql


def test_claim_exposes_only_public_projection_all_sim_details_and_source_run() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    claim = sql[sql.index("CREATE OR REPLACE FUNCTION workspace.claim_next_pdf_report_v1") :]
    for field in (
        "report_artifact_id UUID",
        "analysis_case_id UUID",
        "owner_id UUID",
        "source_analysis_run_id UUID",
        "result_payload JSONB",
        "sim_details JSONB",
        "processing_run_pk UUID",
        "storage_object_key TEXT",
    ):
        assert field in claim
    assert "api.public_analysis_projection_v2" in claim
    assert "api.rpc_get_sim_candidate_detail_v2" in claim
    assert "ORDER BY candidate.rank_no, candidate.sim_candidate_pk" in claim
    assert "dispatch.attempt_count < 3" in claim
    assert "FOR UPDATE OF dispatch, report SKIP LOCKED" in claim
    assert "NULL, \047report_pdf\047, \047running\047" in claim
    assert "\047source_analysis_run_id\047, v_dispatch.source_analysis_run_id" in claim


def test_every_transition_is_fenced_and_ready_persists_immutable_metadata() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for function_name in (
        "heartbeat_pdf_report_v1",
        "complete_pdf_report_v1",
        "fail_pdf_report_v1",
    ):
        assert f"workspace.{function_name}" in sql
    assert "dispatch.processing_run_pk = p_processing_run_pk" in sql
    assert "dispatch.lease_expires_at > v_now" in sql
    assert "p_storage_bucket <> 'analysis-reports'" in sql
    assert "p_mime_type <> 'application/pdf'" in sql
    assert "p_size_bytes > 26214400" in sql
    assert "status = 'ready'" in sql
    assert "storage_object_key = p_storage_object_key" in sql
    assert "content_sha256 = p_content_sha256" in sql
    assert "v_user_id::text || '/' || v_analysis_case_pk::text || '/pdf/' || p_processing_run_pk::text || '.pdf'" in sql
    assert "PDF_REPORT_FAILED" in sql
    assert "PDF_REPORT_MAX_ATTEMPTS_EXCEEDED" in sql
    assert sql.count("AND (dispatch.lease_expires_at IS NULL OR dispatch.lease_expires_at <= v_now)") >= 3
    assert "CREATE TABLE IF NOT EXISTS workspace.report_pdf_object_cleanup" in sql
    # These functions return columns named storage_bucket/storage_object_key.
    # A conflict target using those bare names is ambiguous in PL/pgSQL.
    assert sql.count("ON CONFLICT DO NOTHING") >= 3
    assert "ON CONFLICT (storage_bucket, storage_object_key)" not in sql
    assert "workspace.claim_next_pdf_report_cleanup_v1" in sql
    assert "report.expires_at <= v_now" in sql
    assert "dispatch.lease_expires_at > v_now" in sql
    assert "next_attempt_at = CASE WHEN delete_attempt_count < 1" in sql
    assert "FOR UPDATE OF dispatch, owned_report SKIP LOCKED" in sql
    orphan_start = sql.index("orphan_candidate AS (")
    orphan = sql[
        orphan_start : sql.index("\n    candidate AS (", orphan_start)
    ]
    assert "FROM workspace.report_pdf_dispatch" in orphan
    assert "FROM result.report_artifact" in orphan
    assert "report.expires_at > v_now" in orphan
    assert "NOT EXISTS (SELECT 1 FROM linked_candidate)" in orphan
    assert "FOR UPDATE OF cleanup SKIP LOCKED" in orphan
    assert "RETURN QUERY\n    WITH linked_candidate AS (" in sql
    assert "candidate.expired_processing_run_pk" in sql
    assert "failed_processing AS (" in sql
    assert "fenced_dispatch AS (" in sql
    assert "processing_run_pk = NULL" in sql
    assert "PDF_REPORT_LEASE_EXPIRED" in sql


def test_worker_rpcs_are_service_role_only() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    grants = sql[sql.index("REVOKE ALL ON FUNCTION workspace.enqueue_pdf_report_for_ready_case") :]
    assert "TO service_role" in grants
    assert "TO authenticated" not in grants
