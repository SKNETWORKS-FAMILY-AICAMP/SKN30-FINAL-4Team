import { api } from './apiClient'
import { getFriendlyErrorMessage } from './errorHandler'

export interface LoginRequest {
    email: string
    password: string
}

export const authService = {
    login: async ({ email, password }: LoginRequest) => {
        try {
            return await api.post('/auth/sign-in', { email, password })
        } catch (error: any) {
            // 'login' 액션명을 넘겨주어 401 에러를 맞춤형으로 분기
            throw new Error(getFriendlyErrorMessage(error, '로그인 중 오류가 발생했습니다', 'login'))
        }
    },

    signUp: async ({ email, password }: LoginRequest) => {
        try {
            return await api.post('/auth/sign-up', { email, password })
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '회원가입 중 오류가 발생했습니다', 'signUp'))
        }
    },

    // 다른 메서드들도 필요에 따라 액션명을 지정할 수 있습니다
    requestPasswordReset: async (email: string) => {
        try {
            return await api.post('/auth/password-reset', { email })
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '비밀번호 재설정 요청 중 오류가 발생했습니다', 'requestPasswordReset'))
        }
    },

    updatePassword: async (password: string) => {
        try {
            return await api.post('/auth/update-password', { password })
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '비밀번호 변경 중 오류가 발생했습니다', 'updatePassword'))
        }
    },

    logout: async () => {
        try {
            return await api.post('/auth/sign-out')
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '로그아웃 중 오류가 발생했습니다', 'logout'))
        }
    },

    extendSession: async () => {
        try {
            return await api.post('/auth/refresh')
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '세션 연장 중 오류가 발생했습니다', 'extendSession'))
        }
    },

    getCurrentUser: async () => {
        try {
            return await api.get('/auth/me')
        } catch (error: any) {
            throw new Error(getFriendlyErrorMessage(error, '사용자 정보를 불러오는 중 오류가 발생했습니다', 'getCurrentUser'))
        }
    },
}