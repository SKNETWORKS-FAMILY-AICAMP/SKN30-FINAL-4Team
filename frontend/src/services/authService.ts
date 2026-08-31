import { apiClient } from './apiClient'

export interface LoginRequest {
    login_id: string
    password: string
}

export interface PasswordResetRequestDto {
    email: string
}

export interface PasswordResetConfirmDto {
    token: string
    new_password: string
}

export interface ChangePasswordDto {
    current_password: string
    new_password: string
}

export const authService = {
    // 1. 로그인 (`POST /api/v1/auth/login`)
    login: async (credentials: LoginRequest) => {
        const response = await apiClient.post('/api/v1/auth/login', credentials)
        return response.data
    },

    // 2. 내 정보 조회 (`GET /api/v1/auth/me`)
    getMe: async () => {
        const response = await apiClient.get('/api/v1/auth/me')
        return response.data
    },

    // 3. 비밀번호 변경 (`POST /api/v1/auth/change-password`)
    changePassword: async (data: ChangePasswordDto) => {
        const response = await apiClient.post('/api/v1/auth/change-password', data)
        return response.data
    },

    // 4. 로그아웃 (`POST /api/v1/auth/logout`)
    logout: async () => {
        const response = await apiClient.post('/api/v1/auth/logout')
        return response.data
    },

    // 5. 비밀번호 재설정 요청 (`POST /api/v1/auth/password-reset/request`)
    requestPasswordReset: async (data: PasswordResetRequestDto) => {
        const response = await apiClient.post('/api/v1/auth/password-reset/request', data)
        return response.data
    },

    // 6. 비밀번호 재설정 확인 (`POST /api/v1/auth/password-reset/confirm`)
    confirmPasswordReset: async (data: PasswordResetConfirmDto) => {
        const response = await apiClient.post('/api/v1/auth/password-reset/confirm', data)
        return response.data
    },
}