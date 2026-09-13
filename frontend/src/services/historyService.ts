import { api } from './apiClient'
import { getFriendlyErrorMessage } from './errorHandler'

export const historyService = {
    listHistory: async () => {
        try {
            const data = await api.get('/analysis-history')
            const list = Array.isArray(data) ? data : (data?.data || [])
            return { data: list, count: list.length }
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '분석 이력 목록을 조회하는 중 오류가 발생했습니다'))
        }
    },
}