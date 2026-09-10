from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "25_ml_result_contract.sql"
)


def test_ml_result_migration_preserves_fenced_writer_and_owned_read() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ADD COLUMN ml_result JSONB NOT NULL" in sql
    assert "persist_analysis_result_core_without_ml" in sql
    assert "v_case_pk := workspace.persist_analysis_result_core_without_ml" in sql
    assert "IF v_case_pk IS NULL" in sql
    assert "rpc_get_analysis_result_without_ml" in sql
    assert "c.user_id = (SELECT auth.uid())" in sql
    assert "c.retention_expires_at > now()" in sql
    assert "v_ml - ARRAY['model_1', 'model_2', 'model_3']" in sql
    assert "GRANT EXECUTE ON FUNCTION api.rpc_get_analysis_result(UUID) TO authenticated" in sql
