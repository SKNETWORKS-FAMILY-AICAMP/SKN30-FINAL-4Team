import { api } from './apiClient'
import { getFriendlyErrorMessage } from './errorHandler'

export const analysisService = {
    getActiveSession: async () => {
        try {
            return await api.get('/analysis-sessions/active')
        } catch (err: any) {
            if (err.status === 204 || err.status === 404) return null
            throw new Error(getFriendlyErrorMessage(err, '활성 분석 세션을 조회하는 중 오류가 발생했습니다'))
        }
    },

    uploadAndAnalyze: async (file: File) => {
        try {
            const idempotencyKey = crypto.randomUUID()
            const formData = new FormData()
            formData.append('file', file)

            return await api.post('/analysis-runs', formData, {
                'Idempotency-Key': idempotencyKey,
            })
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '파일 업로드 및 분석 중 오류가 발생했습니다'))
        }
    },

    getRunStatus: async (runId: string) => {
        try {
            return await api.get(`/analysis-runs/${runId}`)
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '분석 상태를 조회하는 중 오류가 발생했습니다'))
        }
    },
}