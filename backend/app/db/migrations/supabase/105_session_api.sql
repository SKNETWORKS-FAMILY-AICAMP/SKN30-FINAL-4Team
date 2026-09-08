-- ============================================================================
-- Migration 105: 세션·이력 API 표면 (SES-01, SES-02, HISTORY-01)
-- Date: 2026-09-09
--
-- 프론트 명세가 부르는 12개 표면 중 남아 있던 네 개다.
--
--     api.v_active_analysis_session              SES-01  앱 진입 분기
--     api.v_my_analysis_history                  HISTORY-01
--     api.rpc_touch_active_analysis_session      SES-02
--     api.rpc_close_active_analysis_session      SES-02  [새 분석]
--
-- 소유권은 RLS 가 아니라 auth.uid() 로 **명시적으로** 건다. 102 와 같은 규율이다
-- ("Do not rely on RLS here"). 뷰도 auth.uid() 로 직접 거르므로 정의자 권한으로
-- 돌아도 남의 행이 새지 않는다.
--
-- 세션 수명 30분은 worker/dispatcher.py 의 _WRITE_ANALYSIS_SESSION 과 같은
-- 값이다. 한쪽만 바꾸면 워커가 연 세션과 touch 가 연장한 세션의 수명이
-- 달라진다.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS api;

-- ----------------------------------------------------------------------------
-- SES-01 활성 분석 세션
-- ----------------------------------------------------------------------------
-- 프론트는 `.maybeSingle()` 로 읽는다. 두 행이 오면 그쪽이 오류가 된다.
-- 한 사용자가 여러 케이스를 열어 둘 수 있으므로(=[새 분석]으로 닫지 않고
-- 다시 올린 경우) 가장 최근 활동 하나로 좁힌다. 이름 그대로 "그 사용자의
-- 활성 분석" 하나다.
--
-- status='active' 인데 시간이 지난 행은 여기서 제외한다. 상태 컬럼을 고치는
-- 것은 touch RPC 의 일이고, 읽기 뷰는 시간을 기준으로 판단한다.
CREATE OR REPLACE VIEW api.v_active_analysis_session AS
    SELECT s.analysis_session_pk AS analysis_session_id,
           ac.analysis_case_pk   AS analysis_case_id,
           ac.program_name,
           ac.original_filename,
           s.expires_at          AS session_expires_at
      FROM result.analysis_session AS s
      JOIN result.analysis_case AS ac
        ON ac.analysis_case_pk = s.analysis_case_pk
     WHERE ac.user_id = auth.uid()
       AND s.status = 'active'
       AND s.expires_at > pg_catalog.now()
     ORDER BY s.last_activity_at DESC
     LIMIT 1;

-- ----------------------------------------------------------------------------
-- HISTORY-01 분석 이력
-- ----------------------------------------------------------------------------
-- **ORDER BY 도 LIMIT 도 두지 않는다.** 프론트가 `.order('completed_at')` 과
-- `.range(from, to)` 로 정렬·페이지를 정한다. 뷰가 먼저 자르면 그 호출이
-- 잘린 집합 위에서 다시 자르게 된다.
--
-- 명세: "보관 기간이 끝난 결과는 목록 View가 반환하지 않는다."
-- retention_expires_at 이 NULL 이면 보관 기한을 아직 정하지 않은 것이므로
-- 만료로 보지 않는다.
CREATE OR REPLACE VIEW api.v_my_analysis_history AS
    SELECT ac.analysis_case_pk       AS analysis_case_id,
           ac.program_name,
           ac.original_filename,
           ac.analysis_completed_at  AS completed_at,
           ac.report_status
      FROM result.analysis_case AS ac
     WHERE ac.user_id = auth.uid()
       AND ac.case_status = 'ready'
       AND (
            ac.retention_expires_at IS NULL
            OR ac.retention_expires_at > pg_catalog.now()
       );

-- ----------------------------------------------------------------------------
-- SES-02 활동 갱신
-- ----------------------------------------------------------------------------
-- 결과 진입·상세 보기·PDF 클릭·채팅에서 부른다. 클릭 시점부터 30분이지
-- 남은 시간에 더하는 것이 아니다 (명세 3절의 세션 연장 규칙과 같다).
--
-- 만료된 세션을 되살리지 않는다. 시간이 지난 active 행은 여기서 expired 로
-- 정정하고 그 상태를 그대로 돌려준다 — 상태 전이는 결정적이므로 규칙이 맡는다.
CREATE OR REPLACE FUNCTION api.rpc_touch_active_analysis_session(
    p_analysis_case_id uuid
)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, auth, result, api
AS $function$
DECLARE
    v_session result.analysis_session%ROWTYPE;
BEGIN
    -- 소유권을 함수 안에서 명시적으로 본다. 남의 UUID 를 찍어도 NULL 이다.
    SELECT s.*
      INTO v_session
      FROM result.analysis_session AS s
      JOIN result.analysis_case AS ac
        ON ac.analysis_case_pk = s.analysis_case_pk
     WHERE s.analysis_case_pk = p_analysis_case_id
       AND ac.user_id = auth.uid();

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF v_session.status = 'active' AND v_session.expires_at <= pg_catalog.now() THEN
        UPDATE result.analysis_session
           SET status = 'expired', updated_at = pg_catalog.now()
         WHERE analysis_session_pk = v_session.analysis_session_pk
        RETURNING * INTO v_session;
    ELSIF v_session.status = 'active' THEN
        UPDATE result.analysis_session
           SET last_activity_at = pg_catalog.now(),
               expires_at       = pg_catalog.now() + interval '30 minutes',
               updated_at       = pg_catalog.now()
         WHERE analysis_session_pk = v_session.analysis_session_pk
        RETURNING * INTO v_session;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'analysis_session_id', v_session.analysis_session_pk,
        'status', v_session.status,
        'session_expires_at', v_session.expires_at
    );
END;
$function$;

-- ----------------------------------------------------------------------------
-- SES-02 [새 분석]
-- ----------------------------------------------------------------------------
-- close_reason 컬럼은 스키마에 없다. 이 RPC 를 부르는 곳이 [새 분석] 하나뿐이라
-- 사유가 하나로 정해져 있기 때문이다 — 값을 지어내는 것이 아니라 이 진입점의
-- 뜻을 그대로 적는다. 다른 사유가 생기면 그때 컬럼을 만든다.
--
-- 이미 닫힌 세션을 다시 닫아도 같은 응답이다. [새 분석]을 두 번 눌러도
-- 화면이 오류를 받지 않는다.
CREATE OR REPLACE FUNCTION api.rpc_close_active_analysis_session(
    p_analysis_case_id uuid
)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, auth, result, api
AS $function$
DECLARE
    v_session result.analysis_session%ROWTYPE;
BEGIN
    SELECT s.*
      INTO v_session
      FROM result.analysis_session AS s
      JOIN result.analysis_case AS ac
        ON ac.analysis_case_pk = s.analysis_case_pk
     WHERE s.analysis_case_pk = p_analysis_case_id
       AND ac.user_id = auth.uid();

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF v_session.status <> 'closed' THEN
        UPDATE result.analysis_session
           SET status     = 'closed',
               closed_at  = pg_catalog.now(),
               updated_at = pg_catalog.now()
         WHERE analysis_session_pk = v_session.analysis_session_pk
        RETURNING * INTO v_session;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'analysis_session_id', v_session.analysis_session_pk,
        'status', v_session.status,
        'close_reason', 'new_analysis'
    );
END;
$function$;

REVOKE ALL ON SCHEMA api FROM PUBLIC;
GRANT USAGE ON SCHEMA api TO authenticated;

REVOKE ALL ON api.v_active_analysis_session FROM PUBLIC;
REVOKE ALL ON api.v_my_analysis_history FROM PUBLIC;
GRANT SELECT ON api.v_active_analysis_session TO authenticated;
GRANT SELECT ON api.v_my_analysis_history TO authenticated;

REVOKE ALL ON FUNCTION api.rpc_touch_active_analysis_session(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION api.rpc_close_active_analysis_session(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api.rpc_touch_active_analysis_session(uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION api.rpc_close_active_analysis_session(uuid) TO authenticated;

COMMIT;
