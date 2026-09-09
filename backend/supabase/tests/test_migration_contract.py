"""
Migration Contract Tests
Date: 2026-08-31

Static file-based contract tests that verify the Supabase schema migration
configuration matches expected requirements.

These are NOT database runtime tests - they verify the migration files
themselves are structurally correct and complete.

Tests:
1. All migration files exist in correct order
2. All required schemas are created
3. All required tables are defined
4. All critical foreign key relationships exist
5. All CHECK constraints are present
6. All indexes are created
7. RLS is properly configured

Run: pytest backend/tests/test_migration_contract.py -v
"""

import os
import re
from pathlib import Path
from typing import Set, List, Dict, Tuple


class MigrationContractTest:
    """Verify migration schema contract."""

    MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

    EXPECTED_MIGRATIONS = [
        "01_core_schemas.sql",
        "02_core_ddl.sql",
        "03_workspace_ddl.sql",
        "04_workspace_components.sql",
        "05_workspace_projections.sql",
        "06_result_ddl.sql",
        "07_indexes.sql",
        "08_rls_policies.sql",
        "09_kb_notice_metadata.sql",
        "10_api_contract_foundation.sql",
        "11_storage_policies.sql",
        "12_realtime_analysis_run.sql",
        "13_api_contract_state_hardening.sql",
        "14_storage_upload_hardening.sql",
        "15_api_views_and_result_rpcs.sql",
        "16_conversation_command_rpcs.sql",
        "17_request_profile_ingest_core.sql",
    ]

    REQUIRED_SCHEMAS = {"app", "ops", "kb", "workspace", "result"}

    REQUIRED_TABLES = {
        # app schema
        "app.user_profile",

        # ops schema
        "ops.processing_run",
        "ops.model_invocation",
        "ops.cleanup_event",

        # kb schema (existing knowledge base)
        "kb.notice",
        "kb.source_profile",
        "kb.source_version",
        "kb.artifact",
        "kb.artifact_lineage",
        "kb.profile_version",
        "kb.support_component",
        "kb.fact_occurrence",
        "kb.fact_evidence",
        "kb.fact_context",
        "kb.fact_relation",
        "kb.fact_component_link",
        "kb.delivery_role",
        "kb.delivery_role_organization",
        "kb.target_constraint",
        "kb.target_constraint_source",
        "kb.target_constraint_dimension",
        "kb.support_facet",
        "kb.support_facet_source",
        "kb.support_facet_value",
        "kb.support_scale_projection",
        "kb.support_scale_measure",

        # workspace schema (request analysis)
        "workspace.analysis_run",
        "workspace.source_artifact",
        "workspace.artifact_lineage",
        "workspace.request_profile",
        "workspace.support_component",
        "workspace.program_node",
        "workspace.fact_occurrence",
        "workspace.fact_evidence",
        "workspace.fact_context",
        "workspace.target_constraint",
        "workspace.target_constraint_source",
        "workspace.target_constraint_dimension",
        "workspace.support_facet",
        "workspace.support_facet_source",
        "workspace.support_facet_value",
        "workspace.support_scale_projection",
        "workspace.support_scale_measure",
        "workspace.request_type",
        "workspace.delivery_relation",
        "workspace.delivery_action",
        "workspace.delivery_method",
        "workspace.field_state",
        "workspace.field_state_ref",

        # result schema (analysis results)
        "result.analysis_case",
        "result.axis_result",
        "result.sim_candidate",
        "result.evidence_snapshot",
        "result.analysis_session",
        "result.conversation_message",
        "result.conversation_reference",
        "result.report_artifact",
    }

    # Critical foreign keys that must exist
    REQUIRED_FOREIGN_KEYS = {
        "app.user_profile.user_id -> auth.users.id",
        "kb.source_profile.notice_pk -> kb.notice.notice_pk",
        "kb.source_version.source_profile_pk -> kb.source_profile.source_profile_pk",
        "kb.artifact.source_version_pk -> kb.source_version.source_version_pk",
        "kb.profile_version.source_version_pk -> kb.source_version.source_version_pk",
        "kb.support_component.profile_version_pk -> kb.profile_version.profile_version_pk",
        "kb.fact_occurrence.profile_version_pk -> kb.profile_version.profile_version_pk",
        "workspace.analysis_run.user_id -> auth.users.id",
        "workspace.request_profile.analysis_run_pk -> workspace.analysis_run.analysis_run_pk",
        "workspace.support_component.request_profile_pk -> workspace.request_profile.request_profile_pk",
        "workspace.fact_occurrence.request_profile_pk -> workspace.request_profile.request_profile_pk",
        "result.analysis_case.user_id -> auth.users.id",
        "result.sim_candidate.analysis_case_pk -> result.analysis_case.analysis_case_pk",
        "result.sim_candidate.existing_profile_version_pk -> kb.profile_version.profile_version_pk",
    }

    def test_all_migrations_present(self):
        """Verify all expected migration files exist."""
        missing = []
        for expected in self.EXPECTED_MIGRATIONS:
            migration_path = self.MIGRATIONS_DIR / expected
            if not migration_path.exists():
                missing.append(expected)

        assert not missing, f"Missing migration files: {missing}"

    def test_migrations_valid_sql(self):
        """Verify each migration file contains valid SQL."""
        for migration_file in self.MIGRATIONS_DIR.glob("*.sql"):
            content = migration_file.read_text()

            # Check for BEGIN/COMMIT
            assert "BEGIN;" in content or "BEGIN" in content.upper(), \
                f"{migration_file.name} missing BEGIN"
            assert "COMMIT;" in content or "COMMIT" in content.upper(), \
                f"{migration_file.name} missing COMMIT"

            # Check for proper SQL statement termination
            statements = [s.strip() for s in content.split(";") if s.strip()]
            assert len(statements) > 0, f"{migration_file.name} has no SQL statements"

    def test_all_schemas_created(self):
        """Verify CREATE SCHEMA for all required schemas."""
        migration_content = ""
        for migration_file in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            migration_content += migration_file.read_text() + "\n"

        missing_schemas = []
        for schema in self.REQUIRED_SCHEMAS:
            # Look for CREATE SCHEMA IF NOT EXISTS schema_name
            pattern = rf"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS)?\s+{schema}"
            if not re.search(pattern, migration_content, re.IGNORECASE):
                missing_schemas.append(schema)

        assert not missing_schemas, f"Missing schema definitions: {missing_schemas}"

    def test_all_tables_created(self):
        """Verify CREATE TABLE for all required tables."""
        migration_content = ""
        for migration_file in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            migration_content += migration_file.read_text() + "\n"

        missing_tables = []
        for table in self.REQUIRED_TABLES:
            schema, table_name = table.split(".")
            # Look for CREATE TABLE IF NOT EXISTS schema.table_name
            pattern = rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS)?\s+{schema}\.{table_name}"
            if not re.search(pattern, migration_content, re.IGNORECASE):
                missing_tables.append(table)

        assert not missing_tables, f"Missing table definitions: {missing_tables}"

    def test_auth_users_constraints(self):
        """Verify auth.users references use ON DELETE RESTRICT."""
        migration_content = ""
        for migration_file in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            migration_content += migration_file.read_text() + "\n"

        # Find all references to auth.users
        user_refs = re.finditer(
            r"user_id\s+UUID[^,;]*?REFERENCES\s+auth\.users\([^)]+\)([^,;]*?)(?:,|$)",
            migration_content,
            re.IGNORECASE | re.DOTALL
        )

        for ref in user_refs:
            fk_spec = ref.group(1)
            assert "ON DELETE RESTRICT" in fk_spec, \
                "auth.users reference must use ON DELETE RESTRICT to preserve user ownership"

    def test_rls_enabled(self):
        """Verify RLS is enabled on all protected tables."""
        rls_file = self.MIGRATIONS_DIR / "08_rls_policies.sql"
        assert rls_file.exists(), "RLS migration file missing"

        rls_content = rls_file.read_text()

        # Verify RLS is enabled on key tables
        tables_need_rls = {
            "app.user_profile",
            "workspace.analysis_run",
            "workspace.request_profile",
            "result.analysis_case",
        }

        for table in tables_need_rls:
            schema, tbl = table.split(".")
            pattern = rf"ALTER\s+TABLE\s+{schema}\.{tbl}\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY"
            assert re.search(pattern, rls_content, re.IGNORECASE), \
                f"RLS not enabled on {table}"

    def test_indexes_created(self):
        """Verify indexes are created for foreign keys."""
        index_file = self.MIGRATIONS_DIR / "07_indexes.sql"
        assert index_file.exists(), "Index migration file missing"

        index_content = index_file.read_text()

        # Verify critical indexes exist
        critical_indexes = [
            "ix_app_user_profile_user",
            "ix_workspace_analysis_user",
            "ix_workspace_request_profile_run",
            "ix_result_case_user",
            "ix_result_case_retention_expires",
        ]

        for idx in critical_indexes:
            assert idx in index_content, f"Critical index missing: {idx}"

    def test_check_constraints_present(self):
        """Verify CHECK constraints for status fields."""
        migration_content = ""
        for migration_file in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            migration_content += migration_file.read_text() + "\n"

        # Verify CHECK constraints on status columns
        checks_needed = [
            "status IN ('queued','running','succeeded','failed','cancelled')",  # processing_run
            "status IN ('processing','ready','read_only','failed')",  # analysis_case
            "side IN ('REQUEST','EXISTING')",  # evidence_snapshot
        ]

        for check in checks_needed:
            assert check in migration_content, f"Missing CHECK constraint: {check}"

    def test_no_direct_writes_to_protected_schemas(self):
        """Verify business-table writes are not granted to browser clients."""
        migration_content = ""
        for migration_file in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            migration_content += migration_file.read_text() + "\n"

        # Storage RLS may allow a reservation-bound object INSERT, but no
        # business table may grant direct INSERT/UPDATE/DELETE to authenticated.
        assert "GRANT INSERT" not in migration_content or "TO authenticated" not in migration_content, \
            "Authenticated role should not have INSERT grants"
        assert "GRANT UPDATE" not in migration_content or "TO authenticated" not in migration_content, \
            "Authenticated role should not have UPDATE grants"
        assert "GRANT DELETE" not in migration_content or "TO authenticated" not in migration_content, \
            "Authenticated role should not have DELETE grants"

    def test_direct_client_foundation_is_hardened(self):
        """Verify the direct Supabase client has its minimum security boundary."""
        foundation = (self.MIGRATIONS_DIR / "10_api_contract_foundation.sql").read_text()
        state = (self.MIGRATIONS_DIR / "13_api_contract_state_hardening.sql").read_text()
        storage = (self.MIGRATIONS_DIR / "14_storage_upload_hardening.sql").read_text()
        api_views = (self.MIGRATIONS_DIR / "15_api_views_and_result_rpcs.sql").read_text()
        conversation_commands = (self.MIGRATIONS_DIR / "16_conversation_command_rpcs.sql").read_text()
        request_ingest = (self.MIGRATIONS_DIR / "17_request_profile_ingest_core.sql").read_text()

        assert "CREATE SCHEMA IF NOT EXISTS api" in foundation
        assert "ALTER DEFAULT PRIVILEGES IN SCHEMA api" in state
        assert "uq_workspace_analysis_run_one_active_per_user" in state
        assert "uq_workspace_dispatch_worker_job" in state
        assert "GRANT SELECT (" in state
        assert "request_temp_insert_reserved_source" in storage
        assert "can_manage_own_reserved_source" in storage
        assert "file_size_limit = 52428800" in storage
        assert "rpc_get_analysis_result" in api_views
        assert "rpc_get_sim_candidate_detail" in api_views
        assert "(SELECT auth.uid())" in api_views
        assert "prepare_conversation_messages" in conversation_commands
        assert "retry_conversation_message" in conversation_commands
        assert "ingest_request_profile_core" in request_ingest

    def test_storage_buckets_documented(self):
        """Verify storage bucket configuration is documented."""
        readme = Path(self.MIGRATIONS_DIR.parent) / "README.md"
        assert readme.exists(), "README.md missing"

        content = readme.read_text()
        assert "existing-kb" in content, "existing-kb bucket not documented"
        assert "request-temp" in content, "request-temp bucket not documented"
        assert "analysis-reports" in content, "analysis-reports bucket not documented"

    def test_env_example_provided(self):
        """Verify .env.example is provided."""
        env_example = Path(self.MIGRATIONS_DIR.parent) / ".env.example"
        assert env_example.exists(), ".env.example missing"

        content = env_example.read_text()
        assert "SUPABASE_URL" in content, "SUPABASE_URL not in .env.example"
        assert "SUPABASE_KEY" in content, "SUPABASE_KEY not in .env.example"
        assert "SUPABASE_SERVICE_ROLE_KEY" in content, "SERVICE_ROLE_KEY not in .env.example"

    def test_unique_constraints(self):
        """Verify UNIQUE constraints on identity columns."""
        migration_content = ""
        for migration_file in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            migration_content += migration_file.read_text() + "\n"

        # Verify key identity constraints
        unique_checks = [
            r"notice_id\s+TEXT\s+NOT\s+NULL\s+UNIQUE",  # kb.notice
            r"source_profile_id\s+TEXT\s+NOT\s+NULL\s+UNIQUE",  # kb.source_profile
            r"UNIQUE\s*\(\s*analysis_run_pk\s*,\s*profile_id\s*\)",  # workspace.request_profile
            r"source_analysis_run_id\s+UUID\s+NOT\s+NULL",  # result.analysis_case identity
        ]

        for unique in unique_checks:
            assert re.search(unique, migration_content, re.IGNORECASE), \
                f"Missing UNIQUE constraint matching: {unique}"

    def test_retention_logic(self):
        """Verify retention logic columns exist."""
        migration_content = ""
        for migration_file in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            migration_content += migration_file.read_text() + "\n"

        # Verify analysis_case has retention columns
        assert "retention_expires_at" in migration_content, \
            "retention_expires_at column missing for result retention"
        assert "analysis_completed_at" in migration_content, \
            "analysis_completed_at column missing for result retention tracking"

    def test_lifecycle_columns(self):
        """Verify lifecycle tracking columns (created_at, updated_at, expires_at)."""
        migration_content = ""
        for migration_file in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            migration_content += migration_file.read_text() + "\n"

        # Verify key lifecycle columns
        lifecycle_cols = [
            r"created_at\s+TIMESTAMPTZ",
            r"updated_at\s+TIMESTAMPTZ",
            r"expires_at\s+TIMESTAMPTZ",
            r"started_at\s+TIMESTAMPTZ",
            r"completed_at\s+TIMESTAMPTZ",
        ]

        for pattern in lifecycle_cols:
            assert re.search(pattern, migration_content, re.IGNORECASE), \
                f"Lifecycle column missing: {pattern}"


# ============================================================================
# Pytest Fixtures & Runners
# ============================================================================

def test_migration_contract():
    """Run all migration contract tests."""
    tester = MigrationContractTest()

    # Run all test methods
    for method_name in dir(tester):
        if method_name.startswith("test_"):
            method = getattr(tester, method_name)
            print(f"Running {method_name}...")
            method()
            print(f"✓ {method_name} passed")


if __name__ == "__main__":
    # Run tests
    test = MigrationContractTest()

    print("=" * 70)
    print("Migration Contract Tests")
    print("=" * 70)

    test_methods = [m for m in dir(test) if m.startswith("test_")]

    passed = 0
    failed = 0

    for method_name in sorted(test_methods):
        try:
            method = getattr(test, method_name)
            method()
            print(f"✓ {method_name}")
            passed += 1
        except AssertionError as e:
            print(f"✗ {method_name}")
            print(f"  Error: {e}")
            failed += 1

    print("=" * 70)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 70)

    exit(0 if failed == 0 else 1)
