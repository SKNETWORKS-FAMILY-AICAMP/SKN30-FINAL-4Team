import { api } from './apiClient'
import { getFriendlyErrorMessage } from './errorHandler'

// UUID 또는 고유한 Idempotency-Key 생성 함수
const generateIdempotencyKey = () => {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return crypto.randomUUID()
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0
        const v = c === 'x' ? r : (r & 0x3) | 0x8
        return v.toString(16)
    })
}

// 누락되었던 ChatMessageModel 인터페이스 추가
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
    /**
     * 1. 과거 대화 목록 조회 (Keyset Pagination)
     * GET /api/v1/analysis-cases/{analysis_case_id}/messages
     */
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

    /**
     * 2. 분석 결과에 질문 등록
     * POST /api/v1/analysis-cases/{analysis_case_id}/messages
     */
    sendMessage: async (caseId: string, content: string) => {
        try {
            return await api.post(
                `/analysis-cases/${caseId}/messages`, 
                { content },
                { 'Idempotency-Key': generateIdempotencyKey() }
            )
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '메시지 전송 중 오류가 발생했습니다'))
        }
    },

    /**
     * 3. 단건 메시지 상태 폴링
     * GET /api/v1/analysis-cases/{analysis_case_id}/messages/{message_id}
     */
    getMessageStatus: async (caseId: string, messageId: string): Promise<ChatMessageModel> => {
        try {
            return await api.get(`/analysis-cases/${caseId}/messages/${messageId}`)
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '메시지 상태를 조회하는 중 오류가 발생했습니다'))
        }
    },

    /**
     * 4. 실패한 AI 답변 재시도 등록
     * POST /api/v1/analysis-cases/{analysis_case_id}/messages/{assistant_message_id}/retry
     */
    retryMessage: async (caseId: string, assistantMessageId: string) => {
        try {
            return await api.post(
                `/analysis-cases/${caseId}/messages/${assistantMessageId}/retry`,
                {},
                { 'Idempotency-Key': generateIdempotencyKey() }
            )
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '답변 재시도 중 오류가 발생했습니다'))
        }
    },
}