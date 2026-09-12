import { supabase } from './supabase'

export const chatService = {
    // CHAT-01 · 대화 목록 조회 (View)
    listMessages: async (caseId: string) => {
        const { data, error } = await supabase
            .schema('api')
            .from('v_conversation_messages')
            .select()
            .eq('analysis_case_id', caseId)
            .order('created_at', { ascending: true })
        if (error) throw error
        return data
    },

    // CHAT-02 · 질문 전송 (Edge Function)
    sendMessage: async (caseId: string, content: string) => {
        const { data, error } = await supabase.functions.invoke(
            'edge-conversation-create-message', {
                body: { analysis_case_id: caseId, content },
            }
        )
        if (error) throw error
        return data
    },

    // CHAT-03 · 답변 재시도 (Edge Function)
    retryMessage: async (assistantMessageId: string) => {
        const { data, error } = await supabase.functions.invoke(
            'edge-conversation-retry-message', {
                body: { assistant_message_id: assistantMessageId },
            }
        )
        if (error) throw error
        return data
    },
}