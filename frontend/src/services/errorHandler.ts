export function getFriendlyErrorMessage(error: any, defaultMsg = '오류가 발생했습니다', action?: string): string {
    const status = error?.status || error?.response?.status

    switch (status) {
        case 401:
            // 로그인 시도 중 발생한 401은 계정 정보 불일치로 안내
            if (action === 'login') {
                return '이메일 또는 비밀번호가 올바르지 않습니다<br>다시 확인해주세요'
            }
            // 그 외의 API 호출 중 발생한 401은 세션 만료로 안내
            return '로그인 세션이 만료되었습니다<br>다시 로그인해주세요'
            
        case 403:
            return '해당 요청에 대한 접근 권한이 없습니다'
        case 404:
            return '요청한 리소스를 찾을 수 없거나<br>접근할 수 없습니다'
        case 422:
            return '입력한 형식이 올바르지 않습니다<br>다시 확인해주세요'
        case 429:
            return '요청이 너무 많습니다<br>잠시 후 다시 시도해주세요'

        // --- 분석 도메인 전용 에러 ---
        case 409:
            return '이미 진행 중인 분석 작업이 존재합니다'
        case 413:
            return '업로드한 파일의 용량이 너무 큽니다<br>허용된 용량을 확인해주세요'
        case 415:
            return '지원하지 않는 파일 형식입니다<br>파일 확장자를 확인해주세요'

        // --- 서버 공통 에러 통합 ---
        case 500:
        case 502:
        case 503:
            return '서버 내부 오류가 발생했습니다<br>잠시 후 다시 시도해주세요'
            
        default:
            return error?.message || defaultMsg
    }
}