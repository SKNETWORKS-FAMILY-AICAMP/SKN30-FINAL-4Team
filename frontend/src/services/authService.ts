import { supabase } from './supabase'

export interface LoginRequest {
    email: string
    password: string
}

export const authService = {
    // AUTH-01 · 로그인
    login: async ({ email, password }: LoginRequest) => {
        const { data, error } = await supabase.auth.signInWithPassword({ email, password })
        if (error) throw error
        return data
    },

    // AUTH-02 · 비밀번호 재설정 요청
    requestPasswordReset: async (email: string) => {
        const { data, error } = await supabase.auth.resetPasswordForEmail(email, {
            redirectTo: `${window.location.origin}/auth/reset-password`,
        })
        if (error) throw error
        return data
    },

    // AUTH-02 · 새 비밀번호 저장 (recovery session)
    updatePassword: async (password: string) => {
        const { data, error } = await supabase.auth.updateUser({ password })
        if (error) throw error
        return data
    },

    // AUTH-03 · 로그아웃
    logout: async () => {
        const { error } = await supabase.auth.signOut()
        if (error) throw error
    },

    // AUTH-03 · 1시간 세션 연장
    extendSession: async () => {
        const { data, error } = await supabase.auth.refreshSession()
        if (error) throw error
        return data
    },
}