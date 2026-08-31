import { useState } from 'react'
import Upload from '../features/upload/Upload'
import Result from '../features/result/Result'

export default function MainPage() {
    // 현재 화면 상태 관리 ('upload': 업로드 화면, 'result': 결과 화면)
    const [viewState, setViewState] = useState<'upload' | 'result'>('upload')

    // 파일 업로드 완료 시 실행될 핸들러
    const handleAnalysisComplete = () => {
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
                    onBackToUpload={() => setViewState('upload')}
                />
            )}
        </>
    )
}