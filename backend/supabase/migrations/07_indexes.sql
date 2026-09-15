-- ============================================================================
-- Migration 07: Database Indexes
-- Date: 2026-08-31
--
-- PostgreSQL does not automatically index FK columns.
-- Strategic indexes are created for common access patterns.
-- ============================================================================

BEGIN;

-- app
CREATE INDEX IF NOT EXISTS ix_app_user_profile_user
    ON app.user_profile(user_id);

-- ops
CREATE INDEX IF NOT EXISTS ix_ops_processing_source_run
    ON ops.processing_run(source_analysis_run_id, run_type, status);

CREATE INDEX IF NOT EXISTS ix_ops_model_processing
    ON ops.model_invocation(processing_run_pk);

CREATE INDEX IF NOT EXISTS ix_ops_cleanup_source_run
    ON ops.cleanup_event(source_analysis_run_id, status);

-- kb
CREATE INDEX IF NOT EXISTS ix_kb_source_profile_notice
    ON kb.source_profile(notice_pk);

CREATE INDEX IF NOT EXISTS ix_kb_source_version_profile
    ON kb.source_version(source_profile_pk);

CREATE INDEX IF NOT EXISTS ix_kb_artifact_source_version
    ON kb.artifact(source_version_pk);

CREATE INDEX IF NOT EXISTS ix_kb_profile_source_version
    ON kb.profile_version(source_version_pk);

CREATE INDEX IF NOT EXISTS ix_kb_component_profile
    ON kb.support_component(profile_version_pk);

CREATE INDEX IF NOT EXISTS ix_kb_fact_profile_field
    ON kb.fact_occurrence(profile_version_pk, field_name);

CREATE INDEX IF NOT EXISTS ix_kb_fact_component
    ON kb.fact_occurrence(support_component_pk)
    WHERE support_component_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_kb_fact_evidence_fact
    ON kb.fact_evidence(fact_pk);

CREATE INDEX IF NOT EXISTS ix_kb_fact_context_fact
    ON kb.fact_context(fact_pk);

CREATE INDEX IF NOT EXISTS ix_kb_fact_relation_source
    ON kb.fact_relation(source_fact_pk);

CREATE INDEX IF NOT EXISTS ix_kb_fact_relation_target
    ON kb.fact_relation(target_fact_pk);

CREATE INDEX IF NOT EXISTS ix_kb_fact_component_link_fact
    ON kb.fact_component_link(fact_pk);

CREATE INDEX IF NOT EXISTS ix_kb_fact_component_link_component
    ON kb.fact_component_link(component_pk);

CREATE INDEX IF NOT EXISTS ix_kb_delivery_org_fact
    ON kb.delivery_role_organization(fact_pk);

CREATE INDEX IF NOT EXISTS ix_kb_target_source_fact
    ON kb.target_constraint_source(fact_pk);

CREATE INDEX IF NOT EXISTS ix_kb_target_dimension_lookup
    ON kb.target_constraint_dimension(dimension_key, value_text);

CREATE INDEX IF NOT EXISTS ix_kb_facet_source_fact
    ON kb.support_facet_source(fact_pk);

CREATE INDEX IF NOT EXISTS ix_kb_facet_value_lookup
    ON kb.support_facet_value(facet_type, value_text);

CREATE INDEX IF NOT EXISTS ix_kb_scale_measure_source
    ON kb.support_scale_measure(source_fact_pk);

-- workspace
CREATE INDEX IF NOT EXISTS ix_workspace_analysis_user
    ON workspace.analysis_run(user_id);

CREATE INDEX IF NOT EXISTS ix_workspace_analysis_orphan
    ON workspace.analysis_run(status, created_at);

CREATE INDEX IF NOT EXISTS ix_workspace_analysis_expires
    ON workspace.analysis_run(expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_workspace_artifact_run
    ON workspace.source_artifact(analysis_run_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_request_profile_run
    ON workspace.request_profile(analysis_run_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_component_profile
    ON workspace.support_component(request_profile_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_program_profile
    ON workspace.program_node(request_profile_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_program_parent
    ON workspace.program_node(parent_program_node_pk)
    WHERE parent_program_node_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_workspace_fact_profile_field
    ON workspace.fact_occurrence(request_profile_pk, field_name);

CREATE INDEX IF NOT EXISTS ix_workspace_fact_program
    ON workspace.fact_occurrence(program_node_pk)
    WHERE program_node_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_workspace_fact_component
    ON workspace.fact_occurrence(support_component_pk)
    WHERE support_component_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_workspace_fact_evidence_fact
    ON workspace.fact_evidence(fact_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_fact_context_fact
    ON workspace.fact_context(fact_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_target_source_fact
    ON workspace.target_constraint_source(fact_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_target_dimension_lookup
    ON workspace.target_constraint_dimension(dimension_key, value_text);

CREATE INDEX IF NOT EXISTS ix_workspace_facet_source_fact
    ON workspace.support_facet_source(fact_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_facet_value_lookup
    ON workspace.support_facet_value(facet_type, value_text);

CREATE INDEX IF NOT EXISTS ix_workspace_scale_measure_source
    ON workspace.support_scale_measure(source_fact_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_delivery_profile
    ON workspace.delivery_relation(request_profile_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_delivery_action_relation
    ON workspace.delivery_action(delivery_relation_pk);

CREATE INDEX IF NOT EXISTS ix_workspace_field_state_profile
    ON workspace.field_state(request_profile_pk, field_name);

CREATE INDEX IF NOT EXISTS ix_workspace_field_state_ref_state
    ON workspace.field_state_ref(field_state_pk);

-- result
CREATE INDEX IF NOT EXISTS ix_result_case_user
    ON result.analysis_case(user_id);

CREATE INDEX IF NOT EXISTS ix_result_case_retention_expires
    ON result.analysis_case(retention_expires_at);

CREATE INDEX IF NOT EXISTS ix_result_axis_analysis
    ON result.axis_result(analysis_case_pk, axis_type, axis_code);

CREATE INDEX IF NOT EXISTS ix_result_sim_analysis_rank
    ON result.sim_candidate(analysis_case_pk, rank_no);

CREATE INDEX IF NOT EXISTS ix_result_sim_existing_profile
    ON result.sim_candidate(existing_profile_version_pk);

CREATE INDEX IF NOT EXISTS ix_result_evidence_analysis
    ON result.evidence_snapshot(analysis_case_pk);

CREATE INDEX IF NOT EXISTS ix_result_evidence_existing_profile
    ON result.evidence_snapshot(existing_profile_version_pk)
    WHERE existing_profile_version_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_result_report_analysis
    ON result.report_artifact(analysis_case_pk);

CREATE INDEX IF NOT EXISTS ix_result_session_status_expiry
    ON result.analysis_session(status, expires_at);

CREATE INDEX IF NOT EXISTS ix_result_message_session_sequence
    ON result.conversation_message(analysis_session_pk, sequence_no);

CREATE INDEX IF NOT EXISTS ix_result_conversation_reference_message
    ON result.conversation_reference(message_pk);

CREATE INDEX IF NOT EXISTS ix_result_conversation_reference_axis
    ON result.conversation_reference(axis_result_pk)
    WHERE axis_result_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_result_conversation_reference_sim
    ON result.conversation_reference(sim_candidate_pk)
    WHERE sim_candidate_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_result_conversation_reference_evidence
    ON result.conversation_reference(evidence_snapshot_pk)
    WHERE evidence_snapshot_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_result_conversation_reference_existing_fact
    ON result.conversation_reference(existing_fact_pk)
    WHERE existing_fact_pk IS NOT NULL;

COMMIT;
