-- ============================================================================
-- Migration 08: Row Level Security (RLS) Policies
-- Date: 2026-08-31
-- Version: v0.3
--
-- Assumptions:
-- - Backend/Worker writes with service role
-- - Frontend authenticated role is read-only
-- - retrieval.* and ops.* are NOT exposed to frontend in MVP
-- - No browser direct writes allowed
-- ============================================================================

BEGIN;

-- ============================================================================
-- Enable RLS on all tables
-- ============================================================================

-- app
ALTER TABLE app.user_profile ENABLE ROW LEVEL SECURITY;

-- kb
ALTER TABLE kb.notice ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.source_profile ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.source_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.artifact ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.artifact_lineage ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.profile_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.support_component ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.fact_occurrence ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.fact_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.fact_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.fact_relation ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.fact_component_link ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.delivery_role ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.delivery_role_organization ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.target_constraint ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.target_constraint_source ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.target_constraint_dimension ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.support_facet ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.support_facet_source ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.support_facet_value ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.support_scale_projection ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.support_scale_measure ENABLE ROW LEVEL SECURITY;

-- workspace
ALTER TABLE workspace.analysis_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.source_artifact ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.artifact_lineage ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.request_profile ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.support_component ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.program_node ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.fact_occurrence ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.fact_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.fact_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.target_constraint ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.target_constraint_source ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.target_constraint_dimension ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.support_facet ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.support_facet_source ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.support_facet_value ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.support_scale_projection ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.support_scale_measure ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.request_type ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.delivery_relation ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.delivery_action ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.delivery_method ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.field_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.field_state_ref ENABLE ROW LEVEL SECURITY;

-- result
ALTER TABLE result.analysis_case ENABLE ROW LEVEL SECURITY;
ALTER TABLE result.axis_result ENABLE ROW LEVEL SECURITY;
ALTER TABLE result.sim_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE result.evidence_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE result.report_artifact ENABLE ROW LEVEL SECURITY;
ALTER TABLE result.analysis_session ENABLE ROW LEVEL SECURITY;
ALTER TABLE result.conversation_message ENABLE ROW LEVEL SECURITY;
ALTER TABLE result.conversation_reference ENABLE ROW LEVEL SECURITY;

-- ops (no authenticated policies - denied by default)
ALTER TABLE ops.processing_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE ops.model_invocation ENABLE ROW LEVEL SECURITY;
ALTER TABLE ops.cleanup_event ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- Grants: browser clients may only read the deliberately exposed app/kb data.
--
-- workspace and result rows are served through the API.  Granting SELECT on
-- every internal projection table while only a subset has a row policy is
-- both misleading and fragile: a later policy change could expose an
-- intermediate artifact unintentionally.  The service-role backend/worker
-- is the sole runtime reader for those schemas.
-- ============================================================================

GRANT USAGE ON SCHEMA app, kb, workspace, result TO authenticated;

GRANT SELECT ON app.user_profile TO authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA kb TO authenticated;

-- Deliberately no authenticated grants on ops/retrieval

-- ============================================================================
-- app.user_profile: own row only
-- ============================================================================

DROP POLICY IF EXISTS user_profile_select_own ON app.user_profile;
CREATE POLICY user_profile_select_own
ON app.user_profile
FOR SELECT
TO authenticated
USING (user_id = (SELECT auth.uid()));

-- ============================================================================
-- KB: authenticated read-only
-- ============================================================================

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'notice','source_profile','source_version','artifact','artifact_lineage',
        'profile_version','support_component','fact_occurrence','fact_evidence',
        'fact_context','fact_relation','fact_component_link','delivery_role',
        'delivery_role_organization','target_constraint','target_constraint_source',
        'target_constraint_dimension','support_facet','support_facet_source',
        'support_facet_value','support_scale_projection','support_scale_measure'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS kb_read_authenticated ON kb.%I', t);
        EXECUTE format(
            'CREATE POLICY kb_read_authenticated ON kb.%I FOR SELECT TO authenticated USING (true)',
            t
        );
    END LOOP;
END $$;

-- ============================================================================
-- Workspace: own analysis_run only (reads are service-role writes)
-- ============================================================================

DROP POLICY IF EXISTS analysis_run_select_own ON workspace.analysis_run;
CREATE POLICY analysis_run_select_own
ON workspace.analysis_run
FOR SELECT
TO authenticated
USING (user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS request_profile_select_own ON workspace.request_profile;
CREATE POLICY request_profile_select_own
ON workspace.request_profile
FOR SELECT
TO authenticated
USING (
    EXISTS (
        SELECT 1
        FROM workspace.analysis_run ar
        WHERE ar.analysis_run_pk = request_profile.analysis_run_pk
          AND ar.user_id = (SELECT auth.uid())
    )
);

DROP POLICY IF EXISTS source_artifact_select_own ON workspace.source_artifact;
CREATE POLICY source_artifact_select_own
ON workspace.source_artifact
FOR SELECT
TO authenticated
USING (
    EXISTS (
        SELECT 1
        FROM workspace.analysis_run ar
        WHERE ar.analysis_run_pk = source_artifact.analysis_run_pk
          AND ar.user_id = (SELECT auth.uid())
    )
);

-- ============================================================================
-- Result: own analysis_case only
-- ============================================================================

DROP POLICY IF EXISTS analysis_case_select_own ON result.analysis_case;
CREATE POLICY analysis_case_select_own
ON result.analysis_case
FOR SELECT
TO authenticated
USING (user_id = (SELECT auth.uid()));

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'axis_result','sim_candidate','evidence_snapshot','report_artifact','analysis_session'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS result_owner_read ON result.%I', t);
        EXECUTE format(
            'CREATE POLICY result_owner_read ON result.%I
             FOR SELECT TO authenticated
             USING (
                EXISTS (
                    SELECT 1
                    FROM result.analysis_case ar
                    WHERE ar.analysis_case_pk = %I.analysis_case_pk
                      AND ar.user_id = (SELECT auth.uid())
                )
             )',
            t, t
        );
    END LOOP;
END $$;

-- Conversation message: session -> analysis_case owner
DROP POLICY IF EXISTS conversation_message_select_own ON result.conversation_message;
CREATE POLICY conversation_message_select_own
ON result.conversation_message
FOR SELECT
TO authenticated
USING (
    EXISTS (
        SELECT 1
        FROM result.analysis_session s
        JOIN result.analysis_case c
          ON c.analysis_case_pk = s.analysis_case_pk
        WHERE s.analysis_session_pk = conversation_message.analysis_session_pk
          AND c.user_id = (SELECT auth.uid())
    )
);

-- Conversation reference: message -> session -> analysis_case owner
DROP POLICY IF EXISTS conversation_reference_select_own ON result.conversation_reference;
CREATE POLICY conversation_reference_select_own
ON result.conversation_reference
FOR SELECT
TO authenticated
USING (
    EXISTS (
        SELECT 1
        FROM result.conversation_message m
        JOIN result.analysis_session s
          ON s.analysis_session_pk = m.analysis_session_pk
        JOIN result.analysis_case c
          ON c.analysis_case_pk = s.analysis_case_pk
        WHERE m.message_pk = conversation_reference.message_pk
          AND c.user_id = (SELECT auth.uid())
    )
);

COMMIT;
