import { useState } from 'react'
import Upload from '../features/upload/Upload'
import Result from '../features/result/Result'

export default function MainPage() {
    // 현재 화면 상태 관리 ('upload': 업로드 화면, 'result': 결과 화면)
    const [viewState, setViewState] = useState<'upload' | 'result'>('upload')
    
    // 💡 1. 케이스 ID와 최종 리포트 데이터를 담을 부모 상태 선언
    const [caseId, setCaseId] = useState<string | number | null>(null)
    const [reportData, setReportData] = useState<any>(null)

    // 💡 2. 업로드 완료 시 Upload 컴포넌트에서 caseId와 reportData를 받아올 핸들러
    const handleAnalysisComplete = (completedCaseId: string | number, data: any) => {
        setCaseId(completedCaseId)
        setReportData(data)
        setViewState('result')
    }

    return (
        <>
            {/* 상태에 따라 피처 컴포넌트 렌더링 스위칭 */}
            {viewState === 'upload' ? (
                <Upload 
                    onAnalysisComplete={handleAnalysisComplete}
                />
            ) : (
                <Result
                    reportData={reportData}
                    onBackToUpload={() => setViewState('upload')}
                />
            )}
        </>
    )
}