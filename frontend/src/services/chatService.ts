import { api } from './apiClient'
import { getFriendlyErrorMessage } from './errorHandler'

export const chatService = {
    listMessages: async (caseId: string, updatedSince?: string) => {
        try {
            let query = `/analysis-cases/${caseId}/messages?limit=50`
            if (updatedSince) {
                query += `&updated_since=${encodeURIComponent(updatedSince)}`
            }
            return await api.get(query)
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '대화 목록을 불러오는 중 오류가 발생했습니다'))
        }
    },

    sendMessage: async (caseId: string, content: string) => {
        try {
            return await api.post(`/analysis-cases/${caseId}/messages`, { content })
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '메시지 전송 중 오류가 발생했습니다'))
        }
    },

    retryMessage: async (caseId: string, assistantMessageId: string) => {
        try {
            return await api.post(`/analysis-cases/${caseId}/messages/${assistantMessageId}/retry`)
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '답변 재시도 중 오류가 발생했습니다'))
        }
    },
}