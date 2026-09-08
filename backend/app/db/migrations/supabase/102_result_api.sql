-- ============================================================================
-- Migration 102: Result API RPCs
-- Date: 2026-09-08
--
-- The result screen reads an owner-scoped JSON projection instead of querying
-- result.* tables directly.  These functions intentionally expose no ranking
-- or similarity score columns.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS api;

-- SIM axis payloads are stored as JSON so the worker can preserve its result
-- contract.  Remove score-shaped keys recursively at this API boundary in case
-- a legacy payload contains one below the axis root.
CREATE OR REPLACE FUNCTION api._strip_sim_score_keys(p_value pg_catalog.jsonb)
RETURNS pg_catalog.jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, api
AS $function$
    SELECT CASE pg_catalog.jsonb_typeof(p_value)
        WHEN 'object' THEN COALESCE(
            (
                SELECT pg_catalog.jsonb_object_agg(
                    item.key,
                    api._strip_sim_score_keys(item.value)
                )
                  FROM pg_catalog.jsonb_each(p_value) AS item
                 WHERE item.key NOT IN (
                     'similarity_score',
                     'priority_score',
                     'weighted_score',
                     'percent',
                     'score'
                 )
            ),
            '{}'::pg_catalog.jsonb
        )
        WHEN 'array' THEN COALESCE(
            (
                SELECT pg_catalog.jsonb_agg(
                    api._strip_sim_score_keys(item.value)
                )
                  FROM pg_catalog.jsonb_array_elements(p_value) AS item
            ),
            '[]'::pg_catalog.jsonb
        )
        ELSE p_value
    END
$function$;

CREATE OR REPLACE FUNCTION api.rpc_get_analysis_result(
    p_analysis_case_id uuid
)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, auth, result, api
AS $function$
DECLARE
    v_owner_id uuid;
BEGIN
    -- Do not rely on RLS here.  The owner check is explicit inside the
    -- SECURITY DEFINER function so a guessed UUID cannot reveal a case.
    SELECT ac.user_id
      INTO v_owner_id
      FROM result.analysis_case AS ac
     WHERE ac.analysis_case_pk = p_analysis_case_id
       AND ac.user_id = auth.uid();

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN (
        SELECT pg_catalog.jsonb_build_object(
            'case', pg_catalog.jsonb_build_object(
                'analysis_case_id', ac.analysis_case_pk,
                'program_name', ac.program_name,
                'original_filename', ac.original_filename,
                'completed_at', ac.analysis_completed_at
            ),
            'cpl', pg_catalog.jsonb_build_object(
                'items', COALESCE(
                    (
                        SELECT pg_catalog.jsonb_agg(
                            pg_catalog.jsonb_build_object(
                                'code', ar.axis_code,
                                'status', ar.status,
                                'summary', ar.summary_text,
                                'detail',
                                    COALESCE(ar.result_data, '{}'::pg_catalog.jsonb)
                                    || pg_catalog.jsonb_build_object(
                                        'evidence_ids',
                                        COALESCE(
                                            (
                                                SELECT pg_catalog.jsonb_agg(
                                                    pg_catalog.to_jsonb(es.evidence_snapshot_pk)
                                                    ORDER BY es.created_at,
                                                             es.evidence_snapshot_pk
                                                )
                                                  FROM result.evidence_snapshot AS es
                                                 WHERE es.analysis_case_pk = ac.analysis_case_pk
                                                   AND es.axis_result_pk = ar.axis_result_pk
                                                   AND es.usage_scope = 'RESULT'
                                            ),
                                            '[]'::pg_catalog.jsonb
                                        )
                                    )
                            )
                            ORDER BY ar.ordinal, ar.axis_result_pk
                        )
                          FROM result.axis_result AS ar
                         WHERE ar.analysis_case_pk = ac.analysis_case_pk
                           AND ar.axis_type = 'CPL'
                    ),
                    '[]'::pg_catalog.jsonb
                )
            ),
            'fit', pg_catalog.jsonb_build_object(
                'items', COALESCE(
                    (
                        SELECT pg_catalog.jsonb_agg(
                            pg_catalog.jsonb_build_object(
                                'code', ar.axis_code,
                                'status', ar.status,
                                'summary', ar.summary_text,
                                'detail',
                                    detail.data
                                    || pg_catalog.jsonb_build_object(
                                        'evidence_ids',
                                        COALESCE(
                                            (
                                                SELECT pg_catalog.jsonb_agg(
                                                    pg_catalog.to_jsonb(es.evidence_snapshot_pk)
                                                    ORDER BY es.created_at,
                                                             es.evidence_snapshot_pk
                                                )
                                                  FROM result.evidence_snapshot AS es
                                                 WHERE es.analysis_case_pk = ac.analysis_case_pk
                                                   AND es.axis_result_pk = ar.axis_result_pk
                                                   AND es.usage_scope = 'RESULT'
                                            ),
                                            '[]'::pg_catalog.jsonb
                                        ),
                                        'left',
                                            COALESCE(
                                                detail.data -> 'left',
                                                '{}'::pg_catalog.jsonb
                                            )
                                            || pg_catalog.jsonb_build_object(
                                                'evidence_ids',
                                                COALESCE(
                                                    (
                                                        SELECT pg_catalog.jsonb_agg(
                                                            pg_catalog.to_jsonb(es.evidence_snapshot_pk)
                                                            ORDER BY es.created_at,
                                                                     es.evidence_snapshot_pk
                                                        )
                                                          FROM result.evidence_snapshot AS es
                                                         WHERE es.analysis_case_pk = ac.analysis_case_pk
                                                           AND es.axis_result_pk = ar.axis_result_pk
                                                           AND es.usage_scope = 'RESULT'
                                                           AND es.comparison_side = 'LEFT'
                                                    ),
                                                    '[]'::pg_catalog.jsonb
                                                )
                                            ),
                                        'right',
                                            COALESCE(
                                                detail.data -> 'right',
                                                '{}'::pg_catalog.jsonb
                                            )
                                            || pg_catalog.jsonb_build_object(
                                                'evidence_ids',
                                                COALESCE(
                                                    (
                                                        SELECT pg_catalog.jsonb_agg(
                                                            pg_catalog.to_jsonb(es.evidence_snapshot_pk)
                                                            ORDER BY es.created_at,
                                                                     es.evidence_snapshot_pk
                                                        )
                                                          FROM result.evidence_snapshot AS es
                                                         WHERE es.analysis_case_pk = ac.analysis_case_pk
                                                           AND es.axis_result_pk = ar.axis_result_pk
                                                           AND es.usage_scope = 'RESULT'
                                                           AND es.comparison_side = 'RIGHT'
                                                    ),
                                                    '[]'::pg_catalog.jsonb
                                                )
                                            )
                                    )
                            )
                            ORDER BY ar.ordinal, ar.axis_result_pk
                        )
                          FROM result.axis_result AS ar
                          CROSS JOIN LATERAL (
                              SELECT CASE
                                  WHEN ar.result_data IS NULL
                                       OR pg_catalog.jsonb_typeof(ar.result_data) <> 'object'
                                  THEN '{}'::pg_catalog.jsonb
                                  ELSE ar.result_data
                              END AS data
                          ) AS detail
                         WHERE ar.analysis_case_pk = ac.analysis_case_pk
                           AND ar.axis_type = 'FIT'
                    ),
                    '[]'::pg_catalog.jsonb
                )
            ),
            'sim', pg_catalog.jsonb_build_object(
                'candidates', COALESCE(
                    (
                        SELECT pg_catalog.jsonb_agg(
                            pg_catalog.jsonb_build_object(
                                'sim_candidate_id', sc.sim_candidate_pk,
                                'rank', sc.rank_no,
                                'title', sc.title
                            )
                            ORDER BY sc.rank_no, sc.sim_candidate_pk
                        )
                          FROM result.sim_candidate AS sc
                         WHERE sc.analysis_case_pk = ac.analysis_case_pk
                    ),
                    '[]'::pg_catalog.jsonb
                )
            ),
            'report', pg_catalog.jsonb_build_object(
                'status', ac.report_status,
                'can_download', (
                    ac.report_status = 'ready'
                    AND EXISTS (
                        SELECT 1
                          FROM result.report_artifact AS ra
                         WHERE ra.analysis_case_pk = ac.analysis_case_pk
                           AND ra.expires_at > pg_catalog.now()
                    )
                )
            ),
            'session', COALESCE(
                (
                    SELECT CASE
                        WHEN s.expires_at > pg_catalog.now()
                             AND s.status = 'active'
                        THEN pg_catalog.jsonb_build_object(
                            'can_chat', TRUE,
                            'expires_at', s.expires_at
                        )
                        ELSE pg_catalog.jsonb_build_object(
                            'can_chat', FALSE,
                            'expires_at', NULL
                        )
                    END
                      FROM result.analysis_session AS s
                     WHERE s.analysis_case_pk = ac.analysis_case_pk
                ),
                pg_catalog.jsonb_build_object(
                    'can_chat', FALSE,
                    'expires_at', NULL
                )
            ),
            'evidences', COALESCE(
                (
                    SELECT pg_catalog.jsonb_agg(
                        pg_catalog.jsonb_build_object(
                            'evidence_id', es.evidence_snapshot_pk,
                            'raw_value', es.raw_value,
                            'excerpt', es.context_excerpt
                        )
                        ORDER BY es.created_at, es.evidence_snapshot_pk
                    )
                      FROM result.evidence_snapshot AS es
                     WHERE es.analysis_case_pk = ac.analysis_case_pk
                       AND es.usage_scope = 'RESULT'
                ),
                '[]'::pg_catalog.jsonb
            )
        )
          FROM result.analysis_case AS ac
         WHERE ac.analysis_case_pk = p_analysis_case_id
           AND ac.user_id = auth.uid()
    );
END;
$function$;

CREATE OR REPLACE FUNCTION api.rpc_get_sim_candidate_detail(
    p_sim_candidate_id uuid
)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, auth, result, api
AS $function$
DECLARE
    v_analysis_case_id uuid;
BEGIN
    -- The candidate is owner-scoped through its parent analysis case.  Keep the
    -- auth.uid() comparison in this function rather than trusting the caller or
    -- a policy on the underlying tables.
    SELECT ac.analysis_case_pk
      INTO v_analysis_case_id
      FROM result.sim_candidate AS sc
      JOIN result.analysis_case AS ac
        ON ac.analysis_case_pk = sc.analysis_case_pk
     WHERE sc.sim_candidate_pk = p_sim_candidate_id
       AND ac.user_id = auth.uid();

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN (
        SELECT pg_catalog.jsonb_build_object(
            'sim_candidate_id', sc.sim_candidate_pk,
            'rank', sc.rank_no,
            'title', sc.title,
            'result_summary', sc.result_summary,
            'comparable_axes', COALESCE(
                (
                    SELECT pg_catalog.jsonb_agg(
                        axis.axis_name ORDER BY axis.ordinal
                    )
                      FROM (
                          VALUES
                              ('purpose'::text, sc.purpose_result, 1),
                              ('target'::text, sc.target_result, 2),
                              ('support'::text, sc.support_result, 3),
                              ('delivery'::text, sc.delivery_result, 4)
                      ) AS axis(axis_name, payload, ordinal)
                     WHERE axis.payload IS NOT NULL
                       AND pg_catalog.lower(axis.payload ->> 'status') <> 'insufficient'
                ),
                '[]'::pg_catalog.jsonb
            ),
            'axes', pg_catalog.jsonb_build_object(
                'purpose', api._strip_sim_score_keys(sc.purpose_result),
                'target', api._strip_sim_score_keys(sc.target_result),
                'support', api._strip_sim_score_keys(sc.support_result),
                'delivery', api._strip_sim_score_keys(sc.delivery_result)
            ),
            'observations', '[]'::pg_catalog.jsonb,
            'evidences', COALESCE(
                (
                    SELECT pg_catalog.jsonb_agg(
                        pg_catalog.jsonb_build_object(
                            'evidence_id', es.evidence_snapshot_pk,
                            'raw_value', es.raw_value,
                            'excerpt', es.context_excerpt
                        )
                        ORDER BY es.created_at, es.evidence_snapshot_pk
                    )
                     FROM result.evidence_snapshot AS es
                     WHERE es.analysis_case_pk = sc.analysis_case_pk
                       AND es.usage_scope = 'RESULT'
                       AND es.axis_type = 'SIM'
                       AND (
                           es.sim_candidate_pk = sc.sim_candidate_pk
                           OR (
                               es.sim_candidate_pk IS NULL
                               AND es.axis_type = 'SIM'
                           )
                       )
                ),
                '[]'::pg_catalog.jsonb
            )
        )
          FROM result.sim_candidate AS sc
          JOIN result.analysis_case AS ac
            ON ac.analysis_case_pk = sc.analysis_case_pk
         WHERE sc.sim_candidate_pk = p_sim_candidate_id
           AND ac.analysis_case_pk = v_analysis_case_id
           AND ac.user_id = auth.uid()
    );
END;
$function$;

REVOKE ALL ON SCHEMA api FROM PUBLIC;
GRANT USAGE ON SCHEMA api TO authenticated;

REVOKE ALL ON FUNCTION api._strip_sim_score_keys(pg_catalog.jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION api.rpc_get_analysis_result(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION api.rpc_get_sim_candidate_detail(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api.rpc_get_analysis_result(uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION api.rpc_get_sim_candidate_detail(uuid) TO authenticated;

COMMIT;
