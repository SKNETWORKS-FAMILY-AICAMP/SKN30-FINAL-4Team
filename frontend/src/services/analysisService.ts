import { supabase } from './supabase'

export const analysisService = {
    // SES-01 · 활성 분석 세션 조회
    getActiveSession: async () => {
        const { data, error } = await supabase
            .schema('api')
            .from('v_active_analysis_session')
            .select()
            .maybeSingle()
        if (error) throw error
        return data
    },

    // SCR-004 · 업로드 및 분석 통합 프로세스
    uploadAndAnalyze: async (file: File) => {
        // RUN-01 · 분석 작업 생성 (Edge Function)
        const { data: run, error: createError } = await supabase.functions.invoke(
            'edge-analysis-run-create', {
                body: {
                    original_filename: file.name,
                    declared_mime_type: file.type || 'application/octet-stream',
                    declared_size_bytes: file.size,
                }
            }
        )
        if (createError) throw createError

        // RUN-02 · Storage 원본 파일 업로드
        const { error: uploadError } = await supabase.storage
            .from(run.bucket)
            .upload(run.object_key, file, { contentType: file.type || undefined, upsert: false })
        if (uploadError) throw uploadError

        // RUN-03 · 업로드 완료와 큐 등록 (Edge Function)
        const { data, error: completeError } = await supabase.functions.invoke(
            'edge-analysis-run-complete-upload', {
                body: { analysis_run_id: run.analysis_run_id }
            }
        )
        if (completeError) throw completeError
        return data
    },

    // RUN-04 · 분석 상태 Realtime 수신 구독
    subscribeRun: (runId: string, onUpdate: (row: any) => void) => {
        return supabase.channel(`analysis-run:${runId}`)
            .on('postgres_changes', {
                event: 'UPDATE', 
                schema: 'workspace', 
                table: 'analysis_run',
                filter: `analysis_run_pk=eq.${runId}`,
            }, ({ new: row }) => onUpdate(row))
            .subscribe()
    },
}