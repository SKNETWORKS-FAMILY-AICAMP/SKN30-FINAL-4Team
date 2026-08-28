import Upload from '../features/upload/Upload'

export default function UploadPage() {
    // 업로드 완료 시 분석 결과 화면(SCR-005)으로 전환 핸들러
    const handleAnalysisComplete = () => {
        console.log('업로드 완료: 분석 결과 화면(SCR-005)으로 이동')
        // TODO: 네비게이트를 통한 SCR-005 경로 이동 처리
    }

    return (
        <Upload onAnalysisComplete={handleAnalysisComplete} />
    )
}