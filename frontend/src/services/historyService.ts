import { supabase } from './supabase'

export const historyService = {
    // HISTORY-01 · 목록 조회 (View + Pagination)
    listHistory: async (page: number = 0, limit: number = 5) => {
        const from = page * limit
        const to = from + limit - 1

        const { data, error, count } = await supabase
            .schema('api')
            .from('v_my_analysis_history')
            .select('*', { count: 'exact' })
            .order('completed_at', { ascending: false })
            .range(from, to)

        if (error) throw error
        return { data: data || [], count: count || 0 }
    },
}