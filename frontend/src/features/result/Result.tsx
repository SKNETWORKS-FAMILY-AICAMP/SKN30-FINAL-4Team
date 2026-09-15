import { useState, useEffect } from 'react'
import ResultView from './ResultView'
import { resultService } from '../../services/resultService'

interface ResultProps {
    caseId: string | null
    readOnlyChat?: boolean
    onBackToUpload?: () => void
}

export default function Result({ caseId, readOnlyChat = false, onBackToUpload }: ResultProps) {
    const [reportData, setReportData] = useState<any>(null)
    const [isLoading, setIsLoading] = useState<boolean>(true)
    const [error, setError] = useState<string | null>(null)

    // 모달 상태 및 데이터 관리
    const [selectedItem, setSelectedItem] = useState<any>(null)
    const [isModalOpen, setIsModalOpen] = useState<boolean>(false)
    
    // 💡 상세보기 중복 클릭 방지용 로딩 상태
    const [isFetchingDetail, setIsFetchingDetail] = useState<boolean>(false)

    useEffect(() => {
        const fetchResult = async () => {
            if (!caseId) {
                setError('분석 케이스 ID가 존재하지 않습니다.')
                setIsLoading(false)
                return
            }

            try {
                setIsLoading(true)
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

    // 보고서 PDF 다운로드 핸들러
    const handleExportPDF = async () => {
        
    }

    const handleOpenCandidateDetail = async (simCandidateId: string) => {
        if (isFetchingDetail) return

        try {
            setIsFetchingDetail(true)
            const data = await resultService.getSimCandidateDetail(simCandidateId)
            setSelectedItem(data)
            setIsModalOpen(true)
        } catch (err) {
            console.error('상세 조회 실패:', err)
        } finally {
            setIsFetchingDetail(false)
        }
    }

    const handleCloseModal = () => {
        setIsModalOpen(false)
        setSelectedItem(null)
    }

    if (isLoading) {
        return null
    }

    if (error) {
        onBackToUpload
    }

    return (
        <ResultView 
            reportData={reportData}
            onExportPDF={handleExportPDF}
            readOnlyChat={readOnlyChat}
            onOpenCandidateDetail={handleOpenCandidateDetail}
            selectedItem={selectedItem}
            isModalOpen={isModalOpen}
            onCloseModal={handleCloseModal}
            isFetchingDetail={isFetchingDetail}
        />
    )
}