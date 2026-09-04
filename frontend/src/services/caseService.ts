import { apiClient } from './apiClient'

export const caseService = {
    // 1. 파일 업로드 API (/api/v1/cases)
    uploadCase: async (formData: FormData) => {
        const response = await apiClient.post('/api/v1/cases', formData, {
            headers: {
                'Content-Type': 'multipart/form-data',
            },
        })
        return response.data
    },

    // 2. 분석 시작 API (/api/v1/cases/{case_id}/analyze)
    analyzeCase: async (caseId: string | number) => {
        const response = await apiClient.post(`/api/v1/cases/${caseId}/analyze`)
        return response.status // 상태코드 확인을 위해 response 전체 또는 status 반환
    },

    // 3. 분석 리포트 조회 API (/api/v1/cases/{case_id}/report)
    getReport: async (caseId: string | number) => {
        const response = await apiClient.get(`/api/v1/cases/${caseId}/report`)
        return response.data
    },

    // caseService 예시 추가
    getStatus: async (caseId: string) => {
        const response = await apiClient.get(`/api/v1/cases/${caseId}/status`); // 혹은 fetch 사용
        return response.data;
    }
}