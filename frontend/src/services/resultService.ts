import { api } from './apiClient'
import { getFriendlyErrorMessage } from './errorHandler'

export const resultService = {
    getAnalysisResult: async (caseId: string) => {
        try {
            return await api.get(`/analysis-cases/${caseId}`)
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '분석 결과를 조회하는 중 오류가 발생했습니다'))
        }
    },

    getSimCandidateDetail: async (simCandidateId: string) => {
        try {
            return await api.get(`/sim-candidates/${simCandidateId}`)
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '유사 공고 상세 정보를 조회하는 중 오류가 발생했습니다'))
        }
    },
}