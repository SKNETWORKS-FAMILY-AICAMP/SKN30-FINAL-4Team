import { api } from './apiClient'
import { getFriendlyErrorMessage } from './errorHandler'

export interface ChatMessageModel {
    message_id: string
    role: string
    content: string | null
    status: string
    retry_count?: number
    created_at?: string
    updated_at?: string
}

export interface ChatMessageListResponse {
    items: ChatMessageModel[]
    next_cursor: string | null
}

export const chatService = {
    listMessages: async (caseId: string, cursor?: string, limit: number = 50): Promise<ChatMessageListResponse> => {
        try {
            let query = `/analysis-cases/${caseId}/messages?limit=${limit}`
            if (cursor) {
                query += `&cursor=${encodeURIComponent(cursor)}`
            }
            const response = await api.get(query)
            return {
                items: response?.items || [],
                next_cursor: response?.next_cursor || null,
            }
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

    getMessageStatus: async (caseId: string, messageId: string): Promise<ChatMessageModel> => {
        try {
            return await api.get(`/analysis-cases/${caseId}/messages/${messageId}`)
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '메시지 상태를 조회하는 중 오류가 발생했습니다'))
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