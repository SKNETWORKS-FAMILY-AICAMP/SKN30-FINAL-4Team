import { supabase } from './supabase'

export const resultService = {
    // RESULT-01 · 분석 결과 전체 조회 (RPC)
    getAnalysisResult: async (caseId: string) => {
        const { data, error } = await supabase
            .schema('api')
            .rpc('rpc_get_analysis_result', { p_analysis_case_id: caseId })
        if (error) throw error
        return data
    },

    // RESULT-02 · 유사 공고 상세 팝업 조회 (RPC)
    getSimCandidateDetail: async (simCandidateId: string) => {
        const { data, error } = await supabase
            .schema('api')
            .rpc('rpc_get_sim_candidate_detail', { p_sim_candidate_id: simCandidateId })
        if (error) throw error
        return data
    },

    // SES-02 · 분석 활동 시간 30분 갱신 (touch)
    touchSession: async (caseId: string) => {
        const { data, error } = await supabase
            .schema('api')
            .rpc('rpc_touch_active_analysis_session', { p_analysis_case_id: caseId })
        if (error) throw error
        return data
    },

    // SES-02 · 새 분석 전 기존 세션 종료 (close)
    closeSession: async (caseId: string) => {
        const { data, error } = await supabase
            .schema('api')
            .rpc('rpc_close_active_analysis_session', { p_analysis_case_id: caseId })
        if (error) throw error
        return data
    },
}