-- Request Profile v0.1 core materialisation. Called only by a trusted Edge
-- Function; the worker never receives database credentials.
BEGIN;

CREATE OR REPLACE FUNCTION workspace.ingest_request_profile_core(
    p_analysis_run_id UUID,
    p_profile JSONB
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, workspace
AS $$
DECLARE
  v_profile_pk UUID; v_item JSONB; v_field TEXT; v_fact_pk UUID;
  v_node_map JSONB := '{}'::jsonb; v_component_map JSONB := '{}'::jsonb;
  v_fact_map JSONB := '{}'::jsonb; v_parent_id TEXT; v_source JSONB; v_text TEXT; v_relation_pk UUID; v_projection_pk UUID; v_ordinal INTEGER;
BEGIN
  IF p_profile->>'schema_version' <> 'pre_review_request_profile/v0.1' THEN
    RAISE EXCEPTION 'REQUEST_PROFILE_SCHEMA_UNSUPPORTED' USING ERRCODE = '22023';
  END IF;
  IF COALESCE(p_profile->>'profile_id','') = '' THEN
    RAISE EXCEPTION 'REQUEST_PROFILE_ID_REQUIRED' USING ERRCODE = '22023';
  END IF;

  INSERT INTO workspace.request_profile (
    analysis_run_pk, profile_id, schema_version, program_name,
    requesting_organization, source_document_id, common_ir_document_id,
    candidate_pack_id, candidate_pack_generator, candidate_pack_generator_version
  ) VALUES (
    p_analysis_run_id, p_profile->>'profile_id', p_profile->>'schema_version',
    COALESCE(p_profile#>>'{identity,program_name}', p_profile#>>'{identity,title_raw}'),
    p_profile#>>'{identity,requesting_organization}', p_profile#>>'{identity,source_document_id}',
    p_profile#>>'{processing_metadata,common_ir_document_id}',
    p_profile#>>'{processing_metadata,candidate_pack,id}',
    p_profile#>>'{processing_metadata,candidate_pack,generator}',
    p_profile#>>'{processing_metadata,candidate_pack,generator_version}'
  ) ON CONFLICT (analysis_run_pk, profile_id) DO UPDATE
    SET schema_version = EXCLUDED.schema_version
  RETURNING request_profile_pk INTO v_profile_pk;

  INSERT INTO workspace.request_type (
    request_profile_pk, selected_code, value_raw, label_source_block_id,
    label_start_char, label_end_char, label_text_basis, glyph_raw,
    glyph_source_block_id, glyph_start_char, glyph_end_char, glyph_text_basis, evidence
  ) VALUES (
    v_profile_pk, p_profile#>>'{request_type,selected_code}', p_profile#>>'{request_type,value_raw}',
    p_profile#>>'{request_type,value_source,source_block_id}',
    NULLIF(p_profile#>>'{request_type,value_source,start_char}','')::integer,
    NULLIF(p_profile#>>'{request_type,value_source,end_char}','')::integer,
    p_profile#>>'{request_type,value_source,text_basis}', p_profile#>>'{request_type,selection_source,glyph_raw}',
    p_profile#>>'{request_type,selection_source,source_block_id}',
    NULLIF(p_profile#>>'{request_type,selection_source,start_char}','')::integer,
    NULLIF(p_profile#>>'{request_type,selection_source,end_char}','')::integer,
    p_profile#>>'{request_type,selection_source,text_basis}', COALESCE(p_profile#>'{request_type,evidence}','[]'::jsonb)
  ) ON CONFLICT (request_profile_pk) DO NOTHING;

  FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_profile#>'{program_hierarchy,nodes}','[]'::jsonb)) LOOP
    v_source := v_item->'value_source';
    INSERT INTO workspace.program_node (request_profile_pk, program_node_id, level, name_raw, source_block_id, start_char, end_char, text_basis, evidence, ordinal)
    VALUES (v_profile_pk, v_item->>'program_node_id', v_item->>'level', v_item->>'name_raw',
      v_source->>'source_block_id', (v_source->>'start_char')::integer, (v_source->>'end_char')::integer,
      v_source->>'text_basis', COALESCE(v_item->'evidence','[]'::jsonb), COALESCE((v_item->>'ordinal')::integer,0))
    RETURNING program_node_pk INTO v_fact_pk;
    v_node_map := v_node_map || jsonb_build_object(v_item->>'program_node_id', v_fact_pk);
  END LOOP;
  FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_profile#>'{program_hierarchy,nodes}','[]'::jsonb)) LOOP
    v_parent_id := v_item->>'parent_node_id';
    IF v_parent_id IS NOT NULL THEN
      UPDATE workspace.program_node SET parent_program_node_pk = (v_node_map->>v_parent_id)::uuid
       WHERE program_node_pk = (v_node_map->>(v_item->>'program_node_id'))::uuid;
    END IF;
  END LOOP;

  FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_profile->'support_components','[]'::jsonb)) LOOP
    v_source := v_item->'value_source';
    INSERT INTO workspace.support_component (request_profile_pk, support_component_id, component_kind, name_raw, name_source_block_id, name_start_char, name_end_char, name_text_basis, evidence, ordinal)
    VALUES (v_profile_pk, v_item->>'support_component_id', v_item->>'component_kind', v_item->>'name_raw',
      v_source->>'source_block_id', (v_source->>'start_char')::integer, (v_source->>'end_char')::integer,
      v_source->>'text_basis', COALESCE(v_item->'evidence','[]'::jsonb), COALESCE((v_item->>'ordinal')::integer,0))
    RETURNING component_pk INTO v_fact_pk;
    v_component_map := v_component_map || jsonb_build_object(v_item->>'support_component_id', v_fact_pk);
  END LOOP;

  -- Comparison and request-context facts share the same exact-span model.
  FOR v_field, v_item IN
    SELECT e.key, a.value FROM jsonb_each(COALESCE(p_profile->'comparison_profile','{}'::jsonb)) e
    CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(e.value)='array' THEN e.value ELSE '[]'::jsonb END) a
    UNION ALL
    SELECT e.key, a.value FROM jsonb_each(COALESCE(p_profile->'request_context','{}'::jsonb)) e
    CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(e.value)='array' THEN e.value ELSE '[]'::jsonb END) a
  LOOP
    IF v_field = 'delivery_relations' THEN CONTINUE; END IF;
    v_source := v_item->'value_source';
    INSERT INTO workspace.fact_occurrence (request_profile_pk, fact_id, fact_scope, field_name, value_raw, status, program_node_pk, support_component_pk, source_block_id, start_char, end_char, text_basis, ordinal)
    VALUES (v_profile_pk, v_item->>'fact_id', CASE WHEN p_profile->'request_context' ? v_field THEN 'request_context' ELSE 'comparison' END,
      COALESCE(v_item->>'field_name',v_field), v_item->>'value_raw', COALESCE(v_item->>'status','identified'),
      NULLIF(v_node_map->>(v_item->>'program_node_id'),'')::uuid, NULLIF(v_component_map->>(v_item->>'primary_component_id'),'')::uuid,
      v_source->>'source_block_id', (v_source->>'start_char')::integer, (v_source->>'end_char')::integer, v_source->>'text_basis', COALESCE((v_item->>'ordinal')::integer,0))
    RETURNING fact_pk INTO v_fact_pk;
    v_fact_map := v_fact_map || jsonb_build_object(v_item->>'fact_id', v_fact_pk);
    FOR v_source IN SELECT value FROM jsonb_array_elements(COALESCE(v_item->'evidence','[]'::jsonb)) LOOP
      INSERT INTO workspace.fact_evidence (fact_pk, source_block_id, section_id, common_ir_document_id, common_ir_block_id, common_ir_cell_id, common_ir_occurrence_ids, ordinal)
      VALUES (v_fact_pk, v_source->>'source_block_id', v_source->>'section_id', v_source->>'common_ir_document_id', v_source->>'common_ir_block_id', v_source->>'common_ir_cell_id',
        ARRAY(SELECT jsonb_array_elements_text(COALESCE(v_source->'common_ir_occurrence_ids','[]'::jsonb))), 0);
    END LOOP;
  END LOOP;

  FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_profile#>'{comparison_profile,delivery_relations}','[]'::jsonb)) LOOP
    INSERT INTO workspace.delivery_relation (
      request_profile_pk, delivery_relation_id, actor_raw, canonical_actor_type,
      actor_source_block_id, actor_start_char, actor_end_char, actor_text_basis,
      actor_evidence, role_raw, canonical_role, role_source_block_id, role_start_char,
      role_end_char, role_text_basis, role_evidence, relation_container_type,
      relation_container_data, ordinal
    ) VALUES (
      v_profile_pk, v_item->>'delivery_relation_id', v_item#>>'{actor,value_raw}',
      v_item#>>'{actor,canonical_actor_type}', v_item#>>'{actor,value_source,source_block_id}',
      (v_item#>>'{actor,value_source,start_char}')::integer, (v_item#>>'{actor,value_source,end_char}')::integer,
      v_item#>>'{actor,value_source,text_basis}', COALESCE(v_item#>'{actor,evidence}','[]'::jsonb),
      v_item#>>'{role,value_raw}', v_item#>>'{role,canonical_role}', v_item#>>'{role,value_source,source_block_id}',
      NULLIF(v_item#>>'{role,value_source,start_char}','')::integer, NULLIF(v_item#>>'{role,value_source,end_char}','')::integer,
      v_item#>>'{role,value_source,text_basis}', COALESCE(v_item#>'{role,evidence}','[]'::jsonb),
      v_item#>>'{relation_container,container_type}', COALESCE(v_item->'relation_container','{}'::jsonb),
      COALESCE((v_item->>'ordinal')::integer,0)
    ) RETURNING delivery_relation_pk INTO v_relation_pk;
    FOR v_source IN SELECT value FROM jsonb_array_elements(COALESCE(v_item->'actions','[]'::jsonb)) LOOP
      INSERT INTO workspace.delivery_action (delivery_relation_pk, action_raw, canonical_action, source_block_id, start_char, end_char, text_basis, evidence, ordinal)
      VALUES (v_relation_pk, v_source->>'value_raw', v_source->>'canonical_action', v_source#>>'{value_source,source_block_id}',
        (v_source#>>'{value_source,start_char}')::integer, (v_source#>>'{value_source,end_char}')::integer,
        v_source#>>'{value_source,text_basis}', COALESCE(v_source->'evidence','[]'::jsonb), COALESCE((v_source->>'ordinal')::integer,0));
    END LOOP;
  END LOOP;

  FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_profile->'derived_projections','[]'::jsonb)) LOOP
    IF v_item->>'projection_type' = 'target_constraints' THEN
      INSERT INTO workspace.target_constraint (request_profile_pk,status)
      VALUES (v_profile_pk, COALESCE(v_item->>'status','identified')) RETURNING target_constraint_pk INTO v_projection_pk;
      v_ordinal := 0;
      FOR v_text IN SELECT value FROM jsonb_array_elements_text(COALESCE(v_item->'positive_source_fact_ids','[]'::jsonb)) LOOP
        INSERT INTO workspace.target_constraint_source (target_constraint_pk,fact_pk,source_role,ordinal)
        VALUES (v_projection_pk,(v_fact_map->>v_text)::uuid,'positive',v_ordinal); v_ordinal := v_ordinal + 1;
      END LOOP;
      FOR v_text IN SELECT value FROM jsonb_array_elements_text(COALESCE(v_item->'exclusion_source_fact_ids','[]'::jsonb)) LOOP
        INSERT INTO workspace.target_constraint_source (target_constraint_pk,fact_pk,source_role,ordinal)
        VALUES (v_projection_pk,(v_fact_map->>v_text)::uuid,'exclusion',v_ordinal); v_ordinal := v_ordinal + 1;
      END LOOP;
      v_ordinal := 0;
      FOR v_field, v_text IN SELECT key,value FROM jsonb_each_text(COALESCE(v_item - 'projection_type' - 'positive_source_fact_ids' - 'exclusion_source_fact_ids' - 'status','{}'::jsonb)) LOOP
        IF v_field IN ('entity_types','regions','industries') THEN
          INSERT INTO workspace.target_constraint_dimension (target_constraint_pk,dimension_key,value_kind,value_text,ordinal)
          VALUES (v_projection_pk,v_field,'categorical',v_text,v_ordinal); v_ordinal := v_ordinal + 1;
        END IF;
      END LOOP;
    ELSIF v_item->>'projection_type' = 'support_facets' THEN
      INSERT INTO workspace.support_facet (request_profile_pk,status,ordinal)
      VALUES (v_profile_pk,COALESCE(v_item->>'status','identified'),COALESCE((v_item->>'ordinal')::integer,0)) RETURNING support_facet_pk INTO v_projection_pk;
      v_ordinal := 0;
      FOR v_text IN SELECT value FROM jsonb_array_elements_text(COALESCE(v_item->'source_fact_ids','[]'::jsonb)) LOOP
        INSERT INTO workspace.support_facet_source (support_facet_pk,fact_pk,ordinal) VALUES (v_projection_pk,(v_fact_map->>v_text)::uuid,v_ordinal); v_ordinal := v_ordinal + 1;
      END LOOP;
      FOR v_field, v_text IN SELECT key,value FROM jsonb_each_text(COALESCE(v_item - 'projection_type' - 'source_fact_ids' - 'status','{}'::jsonb)) LOOP
        IF v_field IN ('activities','methods','items') THEN
          INSERT INTO workspace.support_facet_value (support_facet_pk,facet_type,value_text,ordinal)
          VALUES (v_projection_pk,CASE v_field WHEN 'activities' THEN 'activity' WHEN 'methods' THEN 'method' ELSE 'item' END,v_text,0);
        END IF;
      END LOOP;
    ELSIF v_item->>'projection_type' = 'support_scale_measures' THEN
      INSERT INTO workspace.support_scale_projection (request_profile_pk,status)
      VALUES (v_profile_pk,COALESCE(v_item->>'status','identified')) RETURNING support_scale_projection_pk INTO v_projection_pk;
      v_ordinal := 0;
      FOR v_source IN SELECT value FROM jsonb_array_elements(COALESCE(v_item->'measures','[]'::jsonb)) LOOP
        INSERT INTO workspace.support_scale_measure (support_scale_projection_pk,source_fact_pk,source_numeric_candidate_id,measure_type,measure_role,lower_value,upper_value,unit,comparator,applies_per,calculation_basis,frequency,aggregation_scope,ordinal)
        VALUES (v_projection_pk,(v_fact_map->>(v_source->>'source_fact_id'))::uuid,v_source->>'source_numeric_candidate_id',v_source->>'measure_type',v_source->>'measure_role',
          NULLIF(v_source->>'lower_value','')::numeric,NULLIF(v_source->>'upper_value','')::numeric,v_source->>'unit',v_source->>'comparator',v_source->>'applies_per',v_source->>'calculation_basis',v_source->>'frequency',v_source->>'aggregation_scope',v_ordinal); v_ordinal := v_ordinal + 1;
      END LOOP;
    END IF;
  END LOOP;

  FOR v_item IN SELECT value FROM jsonb_array_elements(COALESCE(p_profile->'field_states','[]'::jsonb)) LOOP
    INSERT INTO workspace.field_state (request_profile_pk, field_name, status, reason_codes, ordinal)
    VALUES (v_profile_pk, v_item->>'field_name', v_item->>'status', ARRAY(SELECT jsonb_array_elements_text(COALESCE(v_item->'reason_codes','[]'::jsonb))), COALESCE((v_item->>'ordinal')::integer,0))
    RETURNING field_state_pk INTO v_fact_pk;
    FOR v_text IN SELECT value FROM jsonb_array_elements_text(COALESCE(v_item->'fact_ids','[]'::jsonb)) LOOP
      INSERT INTO workspace.field_state_ref (field_state_pk, fact_pk, ordinal)
      VALUES (v_fact_pk, (v_fact_map->>v_text)::uuid, 0);
    END LOOP;
  END LOOP;
  RETURN v_profile_pk;
END;
$$;

REVOKE ALL ON FUNCTION workspace.ingest_request_profile_core(UUID, JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION workspace.ingest_request_profile_core(UUID, JSONB) TO service_role;
COMMIT;
