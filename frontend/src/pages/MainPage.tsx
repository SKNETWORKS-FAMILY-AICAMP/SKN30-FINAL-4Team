import { useState, useEffect } from 'react'
import { useOutletContext } from 'react-router-dom'
import Upload from '../features/upload/Upload'
import Result from '../features/result/Result'
import { analysisService } from '../services/analysisService'

interface OutletContextType {
    setSessionId: (id: string | null) => void
}

export default function MainPage() {
    const { setSessionId } = useOutletContext<OutletContextType>()
    
    const [viewState, setViewState] = useState<'loading' | 'upload' | 'analyzing' | 'result'>('loading')
    const [caseId, setCaseId] = useState<string | null>(null)
    const [runId, setRunId] = useState<string | null>(null)

    useEffect(() => {
        const checkCurrentAnalysis = async () => {
            try {
                const currentData = await analysisService.getCurrentAnalysis()
                const currentState = currentData.state || currentData.status

                if (currentState === 'processing') {
                    setRunId(currentData.run?.analysis_run_id || null)
                    setViewState('analyzing')
                } else if (currentState === 'ready') {
                    setCaseId(currentData.session?.analysis_case_id || null)
                    setSessionId(currentData.session?.analysis_session_id || null)
                    setViewState('result')
                } else {
                    setViewState('upload')
                }
            } catch (error) {
                console.error('현재 분석 상태 조회 실패:', error)
                setViewState('upload')
            }
        }

        checkCurrentAnalysis()
    }, [])

    const handleAnalysisComplete = (completedId: string) => {
        setCaseId(completedId)
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
                    initialViewState={viewState}
                    runId={runId}
                    onAnalysisComplete={handleAnalysisComplete}
                />
            )}
        </>
    )
}