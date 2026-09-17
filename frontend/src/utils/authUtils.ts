export const authUtils = {
    // 응답 객체에서 access_token_expires_at을 찾아 세션 스토리지에 저장
    saveTokenExpiry(res: any) {
        const expiresAt = res?.access_token_expires_at
        if (expiresAt) {
            const expireTimestamp = new Date(expiresAt).getTime()
            sessionStorage.setItem('expire_time', String(expireTimestamp))
        }
    },

    // 저장된 만료 타임스탬프 가져오기 (없으면 기본 1시간 후)
    getExpireTime(): number {
        const saved = sessionStorage.getItem('expire_time')
        return saved ? Number(saved) : Date.now() + 60 * 60 * 1000
    },

    clearTokenExpiry() {
        sessionStorage.removeItem('expire_time')
    }
}