import { api } from './apiClient'
import { getFriendlyErrorMessage } from './errorHandler'

export interface HistoryItemModel {
    analysis_case_id: string
    program_name: string | null
    original_filename: string | null
    completed_at: string | null
}

export interface HistoryListResponse {
    items: HistoryItemModel[]
    next_cursor: string | null
}

export const historyService = {
    listHistory: async (cursor?: string): Promise<HistoryListResponse> => {
        try {
            const url = cursor ? `/analysis-history?cursor=${encodeURIComponent(cursor)}` : '/analysis-history'
            const data = await api.get(url)
            return {
                items: data?.items || [],
                next_cursor: data?.next_cursor || null,
            }
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '분석 이력 목록을 조회하는 중 오류가 발생했습니다'))
        }
    },
}