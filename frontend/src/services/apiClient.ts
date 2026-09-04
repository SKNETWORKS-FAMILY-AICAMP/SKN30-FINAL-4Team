import axios, { AxiosError } from 'axios'

export const apiClient = axios.create({
    baseURL: import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000',
    headers: {
        'Content-Type': 'application/json',
    },
})

// 요청 인터셉터: 토큰 주입
apiClient.interceptors.request.use(
    (config) => {
        const token = sessionStorage.getItem('access_token')
        if (token) {
            config.headers.Authorization = `Bearer ${token}`
        }
        return config
    },
    (error) => Promise.reject(error)
)

// 응답 인터셉터: 공통 에러 및 PDF 바이너리 분기
apiClient.interceptors.response.use(
    (response) => {
        const contentType = String(response.headers['content-type'] || '')
        if (contentType.indexOf('application/pdf') !== -1) {
            return response
        }
        return response
    },
    (error: AxiosError<any>) => {
        if (error.response) {
            const { status, data } = error.response

            switch (status) {
                case 401:
                    console.warn('인증 오류 발생:', data?.message)
                    break
                case 429:
                    const retryAfter = error.response.headers['retry-after']
                    console.warn(`요청 제한 초과. ${retryAfter ? retryAfter + '초 후' : '잠시 후'} 다시 시도해주세요.`)
                    break
                case 422:
                    console.warn('입력값 검증 오류:', data?.errors)
                    break
                default:
                    console.error(`API 오류 [${status}]:`, data?.message || error.message)
            }
        } else {
            console.error('네트워크 연결을 확인해주세요.', error.message)
        }

        return Promise.reject(error)
    }
)