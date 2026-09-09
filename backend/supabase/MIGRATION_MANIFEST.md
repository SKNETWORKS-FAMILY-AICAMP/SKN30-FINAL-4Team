# Supabase Migration Manifest
Date: 2026-09-08 | Version: v0.6

## Migration Summary

This manifest documents the self-hosted Supabase database migrations for the Pre-Review platform.

**Total Migrations:** 17 sequential SQL migrations  
**Total Tables:** 57 tables across 5 data-bearing schemas  
**Total Lines of SQL:** 2,447 lines  
**Authorization:** Trusted workers/Edge Functions write business data; authenticated users have curated reads and one reservation-bound Storage upload path  

## Migration Files

| # | File | Lines | Tables | Indexes | Purpose |
|---|------|-------|--------|---------|---------|
| 1 | `01_core_schemas.sql` | 27 | 0 | 0 | Create app, ops, kb, workspace, result, retrieval schemas |
| 2 | `02_core_ddl.sql` | 553 | 29 | 0 | App user profile, ops audit, kb existing knowledge base |
| 3 | `03_workspace_ddl.sql` | 97 | 4 | 0 | Workspace analysis_run root and artifacts |
| 4 | `04_workspace_components.sql` | 114 | 6 | 0 | Workspace support components, program hierarchy, facts |
| 5 | `05_workspace_projections.sql` | 233 | 14 | 0 | Workspace projections, delivery, field state |
| 6 | `06_result_ddl.sql` | 166 | 8 | 0 | Result analysis cases, similarity, conversations, reports |
| 7 | `07_indexes.sql` | 206 | 0 | 60 | Strategic indexes for common access patterns |
| 8 | `08_rls_policies.sql` | 250 | 0 | 0 | RLS enable + read-only policies for authenticated |
| 9 | `09_kb_notice_metadata.sql` | 9 | 0 | 0 | Deterministic Bizinfo portal metadata on KB notice roots |
| 10 | `10_api_contract_foundation.sql` | 70 | 0 | 2 | `api` schema, direct-client run state, worker job link, chat state |
| 11 | `11_storage_policies.sql` | 41 | 0 | 0 | Private buckets and authenticated source upload policy |
| 12 | `12_realtime_analysis_run.sql` | 21 | 0 | 0 | Publish analysis-run state to Realtime |
| 13 | `13_api_contract_state_hardening.sql` | 151 | 1 | 3 | Private dispatch metadata, durable result snapshots, retry/state hardening |
| 14 | `14_storage_upload_hardening.sql` | 66 | 0 | 1 | Reservation-bound 50 MiB browser upload policy |
| 15 | `15_api_views_and_result_rpcs.sql` | 298 | 0 | 0 | Frontend Views and result/session RPC read models |
| 16 | `16_conversation_command_rpcs.sql` | 126 | 0 | 0 | Atomic Edge-only chat creation and retry commands |
| 17 | `17_request_profile_ingest_core.sql` | 137 | 0 | 0 | Trusted Edge RPC for Request Profile core materialisation |

## Schema Breakdown

### app (Application)
- **Tables:** 1
- **Purpose:** User profiles and application settings
- **Lifecycle:** Persistent
- **RLS:** Own row only

### ops (Operations & Audit)
- **Tables:** 3 (processing_run, model_invocation, cleanup_event)
- **Purpose:** Processing runs, model invocations, cleanup audit trails
- **Lifecycle:** Persistent (audit log)
- **RLS:** No authenticated policies (backend only)

### kb (Existing Knowledge Base)
- **Tables:** 22
- **Purpose:** Existing program/policy data with version lineage
- **Key:** source → source_version → artifact → profile_version → facts/components/projections
- **Lifecycle:** Persistent
- **RLS:** Authenticated read-only

### workspace (Request Analysis)
- **Tables:** 23
- **Purpose:** Temporary workspace during user analysis (ephemeral)
- **Key:** analysis_run → request_profile → facts/components/projections
- **Lifecycle:** Deleted after analysis_session close/expiry
- **RLS:** Own analysis_run only

### result (Analysis Results)
- **Tables:** 8
- **Purpose:** Analysis results, similarity candidates, conversations
- **Key:** analysis_case → sim_candidate/evidence/session → messages
- **Lifecycle:** 90-day retention (retention_expires_at)
- **RLS:** Own analysis_case only

### api (Frontend Contract)
- **Tables:** 0
- **Purpose:** Curated Views and RPCs used by the React client
- **Lifecycle:** Contract layer; base tables remain internal
- **RLS:** Every View/RPC must enforce `auth.uid()` ownership before exposure

## Table Statistics

### Table Count by Schema
- app: 1
- ops: 3
- kb: 22
- workspace: 23
- result: 8
- **Total: 57**

### Table Count by Type
- **Core tables:** 46 (entity data)
- **Relationship tables:** 20 (fact_evidence, fact_context, lineage, etc.)
- **Metadata tables:** 12 (user_profile, processing_run, cleanup_event, etc.)

## Key Design Principles

### 1. Immutable Audit Trail
- Processing runs and model invocations are immutable once created
- Cleanup events track all deletion operations
- No UPDATE/DELETE on core entities (only INSERTs)

### 2. Multi-Version Lineage
- Knowledge base sources track all versions (is_current flag)
- Profiles have version history with SHA256 integrity
- Artifacts track transformation pipeline (artifact_lineage)

### 3. User-Scoped Isolation
- All user data is scoped to auth.users(id)
- RLS ensures users can only see their own data
- Batch operations use service role for efficiency

### 4. Ephemeral Workspaces
- Workspace analysis_run has expires_at field
- Session retention drives workspace cleanup
- Failed analysis_runs cleaned immediately

### 5. Result Retention
- Results retained for 90 days (configurable)
- retention_expires_at gates automatic cleanup
- Stable correlation IDs survive workspace deletion

## FK Hierarchy

```
auth.users
├── app.user_profile
├── workspace.analysis_run
│   ├── workspace.source_artifact
│   ├── workspace.request_profile
│   │   ├── workspace.support_component
│   │   ├── workspace.program_node
│   │   ├── workspace.fact_occurrence
│   │   ├── workspace.target_constraint
│   │   ├── workspace.support_facet
│   │   ├── workspace.support_scale_projection
│   │   ├── workspace.request_type
│   │   ├── workspace.delivery_relation
│   │   └── workspace.field_state
│   └── ops.processing_run (optional)
│       ├── ops.model_invocation
│       └── ops.cleanup_event (audit)
└── result.analysis_case
    ├── result.axis_result
    ├── result.sim_candidate
    │   └── kb.profile_version (link to existing)
    ├── result.evidence_snapshot
    ├── result.analysis_session
    │   ├── result.conversation_message
    │   └── result.conversation_reference
    └── result.report_artifact

kb.notice (Existing KB root)
├── kb.source_profile
│   └── kb.source_version
│       ├── kb.artifact
│       │   └── kb.artifact_lineage
│       └── kb.profile_version
│           ├── kb.support_component
│           ├── kb.fact_occurrence
│           ├── kb.target_constraint
│           ├── kb.support_facet
│           └── kb.support_scale_projection
└── kb.delivery_role (fact subtype)
    └── kb.delivery_role_organization
```

## Constraints & Validation

### Foreign Keys
- 30+ foreign key constraints
- ON DELETE CASCADE for ownership hierarchies
- ON DELETE RESTRICT for critical references (auth.users, notices)
- ON DELETE SET NULL for optional references (processing_run)

### CHECK Constraints
- **status enums:** processing_run, model_invocation, analysis_case, analysis_session
- **fact_scope enums:** comparison, existing_specific, request_context, request_delivery
- **value_kind enums:** categorical, numeric
- **comparator enums:** eq, lt, lte, gt, gte, range, approx
- **side enums:** REQUEST, EXISTING
- **role enums:** lead_agency, operating_agency, etc.
- **action enums:** announce, recruit, receive, etc.
- **method enums:** direct, subsidy, contribution, commissioned
- **facet_type enums:** activity, method, item
- **measure_type/role:** count, amount, rate with correct role pairs
- **text basis validation:** text_basis = 'common_ir_v1_candidate_pack'
- **character span validation:** start_char >= 0, end_char > start_char
- **numeric ranges:** comparator/lower_value/upper_value consistency
- **SHA256 validation:** 64-char hex pattern

### UNIQUE Constraints
- 40+ UNIQUE constraints on identity fields
- Single-current-version indexes: kb.source_version, kb.profile_version
- Composite uniqueness: (profile_version, fact_id), (analysis_case, rank_no), etc.
- Exact-span uniqueness: (profile, source_block, start_char, end_char) for comparison facts

### Indexes
- 60 indexes created
- FK indexes on all foreign key columns
- Composite indexes for common query patterns
- Partial indexes for sparse data (expires_at, parent_program_node)

## RLS Policy Summary

| Schema | Tables | Policy Type | Authenticated Access |
|--------|--------|-------------|---------------------|
| app | user_profile | Self | Own row only |
| kb | 22 tables | Public | All readable |
| workspace | 24 tables | API-only | No browser grants; service-role backend reads them |
| result | 8 tables | API-only | No browser grants; service-role backend reads them |
| ops | 3 tables | None | Denied (no policy = deny) |

## Storage Buckets

Three object storage buckets (documentation only):

| Bucket | Purpose | Retention | Key Pattern |
|--------|---------|-----------|------------|
| `existing-kb` | KB artifacts | Indefinite | `{notice_id}/{source_profile_id}/{source_sha256}/{artifact_type}/{content_sha256}.{ext}` |
| `request-temp` | Workspace artifacts | On cleanup | Browser uploads: `request-source/{user_id}/{analysis_run_pk}/source.{ext}`; exact key must match a reserved run |
| `analysis-reports` | Generated reports | 90 days | `{user_id}/{analysis_case_pk}/{report_type}/{content_sha256}.{ext}` |

## Testing

### Contract Tests
File: `backend/tests/test_migration_contract.py`

**18 static tests** verify:
1. All migration files exist in order
2. All schemas created
3. All tables created
4. All critical foreign keys exist
5. auth.users constraints use ON DELETE RESTRICT
6. RLS enabled on protected tables
7. All indexes created
8. Authenticated role has no write grants
9. Storage buckets documented
10. .env.example provided
11. UNIQUE constraints on identity fields
12. Retention columns exist
13. Lifecycle tracking columns exist

Run: `pytest backend/tests/test_migration_contract.py -v`

### Runtime Tests
Database runtime tests are the responsibility of the application layer and integration tests.

## Migration Checklist

- [x] Schemas created (5 required)
- [x] Core DDL tables created (78 total)
- [x] Foreign key hierarchy established
- [x] CHECK constraints for all enums
- [x] UNIQUE constraints on identity fields
- [x] Indexes on FK columns and common queries
- [x] RLS enabled and configured
- [x] Auth.users ownership preserved
- [x] Authenticated role is read-only
- [x] Service role is the only writer
- [x] Storage bucket documentation
- [x] Environment variables documented
- [x] Contract tests provided

## Deployment Instructions

### Prerequisites
- PostgreSQL 14+ (via self-hosted Supabase)
- Supabase instance configured
- Service role key available

### Apply Migrations
```bash
# Option 1: Supabase CLI
supabase migration up

# Option 2: psql
for f in backend/supabase/migrations/*.sql; do
  psql -U postgres -d postgres -f "$f"
done

# Option 3: Manual in Supabase SQL Editor
# Copy each migration file content and execute in sequence
```

### Initialize Buckets
```bash
# Via Supabase dashboard or API
curl -X POST https://your-instance.supabase.co/storage/v1/bucket \
  -H "Authorization: Bearer $SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"existing-kb","public":false}'
```

### Verify Schema
```bash
python backend/tests/test_migration_contract.py
```

## Maintenance

### Monitor Workspace Cleanup
```sql
-- Find orphaned analysis runs (expired but not cleaned)
SELECT ar.analysis_run_pk, ar.created_at, ar.expires_at, ar.status
FROM workspace.analysis_run ar
WHERE ar.expires_at < now()
  AND ar.status != 'cleanup_pending'
ORDER BY ar.created_at DESC;
```

### Monitor Result Retention
```sql
-- Find analysis cases about to expire
SELECT ac.analysis_case_pk, ac.retention_expires_at, ac.user_id
FROM result.analysis_case ac
WHERE ac.retention_expires_at BETWEEN now() AND now() + interval '7 days'
ORDER BY ac.retention_expires_at ASC;
```

### Check Index Health
```sql
-- Find unused or bloated indexes
SELECT schemaname, tablename, indexname, idx_scan, idx_tup_read, idx_tup_fetch
FROM pg_stat_user_indexes
ORDER BY idx_scan ASC;
```

## Known Limitations & Future Work

### Excluded from v0.3
- pgvector embedding tables (dimension TBD)
- Request embedding persistence (ephemeral by design)
- Graph DB projections
- unresolved_relations RDB projection

### Future Enhancements
- Vector similarity search index (pgvector)
- Materialized views for common queries
- Partitioning on workspace.analysis_run.expires_at
- Audit logging views
- Analytical summary tables (star schema)

## References

- Schema Handoff: `backend/handover/PreReview_DB_Implementation_Handoff_v1.1_20260831/`
- Core DDL: `backend/handover/.../02_ddl/PreReview_PostgreSQL_Core_DDL_v0.3_20260831.sql`
- RLS Spec: `backend/handover/.../02_ddl/PreReview_PostgreSQL_RLS_v0.3_20260831.sql`
- Request Profile Contract: `backend/handover/.../99_reference/contracts/PreReview_Request_Profile_Structured_JSON_Contract_v0.1.2_20260830.md`

---

**Migration Generated:** 2026-08-31  
**Source:** PreReview DB Implementation Handoff v1.1  
**Author:** Claude Code / PreReview Team  
**Status:** Ready for self-hosted Supabase deployment
