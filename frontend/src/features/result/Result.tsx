import { useState, useEffect } from 'react'
import ResultView from './ResultView'
import { resultService } from '../../services/resultService'

interface ResultProps {
    caseId: string | null
    onBackToUpload?: () => void
    readOnlyChat?: boolean
}

export default function Result({ caseId, onBackToUpload, readOnlyChat = false }: ResultProps) {
    const [reportData, setReportData] = useState<any>(null)
    const [isLoading, setIsLoading] = useState<boolean>(true)
    const [error, setError] = useState<string | null>(null)

    useEffect(() => {
        const fetchResult = async () => {
            if (!caseId) {
                setError('분석 케이스 ID가 존재하지 않습니다.')
                setIsLoading(false)
                return
            }

            try {
                setIsLoading(true)
                // RESULT-01 · 분석 결과 상세 조회 RPC 또는 서비스 호출
                const data = await resultService.getAnalysisResult(caseId)
                setReportData(data)
            } catch (err: any) {
                console.error('분석 결과 조회 실패:', err)
                setError(err.message || '분석 결과를 불러오는 데 실패했습니다.')
            } finally {
                setIsLoading(false)
            }
        }

        fetchResult()
    }, [caseId])

    const handleExportPDF = () => {
        window.print()
    }

    if (isLoading) {
        return (
            <div className="flex-1 flex items-center justify-center h-screen">
                <p className="text-body-md text-on-surface-variant">분석 결과를 불러오는 중입니다...</p>
            </div>
        )
    }

    if (error) {
        return (
            <div className="flex-1 flex flex-col items-center justify-center h-screen gap-md">
                <p className="text-body-md text-error">{error}</p>
                {onBackToUpload && (
                    <button 
                        onClick={onBackToUpload}
                        className="px-md py-sm bg-primary text-on-primary rounded font-label-caps"
                    >
                        돌가기
                    </button>
                )}
            </div>
        )
    }

    return (
        <ResultView 
            reportData={reportData}
            onExportPDF={handleExportPDF}
            onBackToUpload={onBackToUpload}
            readOnlyChat={readOnlyChat}
        />
    )
}