import { supabase } from './supabase'

export const reportService = {
    // REPORT-01 · PDF Signed URL 발급 (Edge Function)
    getDownloadUrl: async (caseId: string) => {
        const { data, error } = await supabase.functions.invoke(
            'edge-report-create-download-url', {
                body: { analysis_case_id: caseId },
            }
        )
        if (error) throw error
        return data
    },
}