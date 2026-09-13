export function getUploadStatusMessage(status: string) {
    switch (status) {
        case 'uploading':
            return {
                title: '파일을 업로드하는 중입니다',
                description: '안전하게 파일을 전송하고 있습니다<br>잠시만 기다려주세요',
            }
        case 'queued':
            return {
                title: '분석 대기 중입니다',
                description: '대기열에 등록되었습니다<br>순차적으로 분석을 시작합니다',
            }
        case 'running':
            return {
                title: 'AI가 문서를 분석하고 있습니다',
                description: '내용을 정밀하게 검토하는 중입니다<br>완료되는 대로 결과를 보여드립니다',
            }
        default:
            return {
                title: '파일을 업로드하고 분석을 진행 중입니다',
                description: '업로드가 완료되는 대로 분석 결과를 확인하실 수 있습니다',
            }
    }
}

export const UPLOAD_GUIDES = [
    {
        icon: 'description',
        title: '지원 형식 안내',
        description: 'HWP, HWPX 포맷의 문서를 업로드하여 정확하고 빠른 사전검토를 진행하실 수 있습니다',
    },
    {
        icon: 'security',
        title: '보안 정책 안내',
        description: '업로드된 문서는 엄격하게 암호화되어 처리되며, 분석 완료 후 즉시 파기됩니다',
    },
    {
        icon: 'info',
        title: '분석 가이드',
        description: '사업 목적, 추진 일정, 예산 계획 등이 포함된 정식 본문 형태의 문서일수록 분석 정확도가 높아집니다',
    },
]