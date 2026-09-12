import { useState, useEffect } from 'react'
import Upload from '../features/upload/Upload'
import Result from '../features/result/Result'
import { analysisService } from '../services/analysisService'

export default function MainPage() {
    const [viewState, setViewState] = useState<'loading' | 'upload' | 'result'>('loading')
    const [caseId, setCaseId] = useState<string | null>(null)

    useEffect(() => {
        const checkActiveSession = async () => {
            try {
                const activeSession = await analysisService.getActiveSession()
                
                if (activeSession && activeSession.analysis_case_id) {
                    setCaseId(activeSession.analysis_case_id)
                    setViewState('result')
                } else {
                    setViewState('upload')
                }
            } catch (error) {
                console.error('활성 분석 세션 조회 실패:', error)
                setViewState('upload')
            }
        }

        checkActiveSession()
    }, [])

    const handleAnalysisComplete = (completedCaseId: string) => {
        setCaseId(completedCaseId)
        setViewState('result')
    }

    if (viewState === 'loading') {
        return null
    }

    return (
        <>
            {viewState === 'result' ? (
                <Result
                    caseId={caseId}
                    onBackToUpload={() => setViewState('upload')}
                />
            ) : (
                <Upload 
                    onAnalysisComplete={handleAnalysisComplete}
                />
            )}
        </>
    )
}