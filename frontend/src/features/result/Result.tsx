import { useState, useEffect } from 'react'
import ResultView from './ResultView'
import { resultService } from '../../services/resultService'
import { usePolling } from '../../hooks/usePolling'
import { api } from '../../services/apiClient'

interface ResultProps {
    caseId: string | null
    isHistoryDetail?: boolean // 💡 기존 readOnlyChat 역할을 포함하는 통합 플래그
    onBackToUpload?: () => void
}

export default function Result({ caseId, isHistoryDetail = false, onBackToUpload }: ResultProps) {
    const [reportData, setReportData] = useState<any>(null)
    const [reportStatus, setReportStatus] = useState<any>(null)
    const [isLoading, setIsLoading] = useState<boolean>(true)
    const [isDownloading, setIsDownloading] = useState<boolean>(false)
    const [error, setError] = useState<string | null>(null)

    // 모달 상태 및 데이터 관리
    const [selectedItem, setSelectedItem] = useState<any>(null)
    const [isModalOpen, setIsModalOpen] = useState<boolean>(false)
    const [isFetchingDetail, setIsFetchingDetail] = useState<boolean>(false)

    // 최초 분석 결과 및 초기 상태 세팅
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
                if (data?.report) {
                    setReportStatus(data.report)
                }
            } catch (err: any) {
                console.error('분석 결과 조회 실패:', err)
                setError(err.message || '분석 결과를 불러오는 데 실패했습니다.')
            } finally {
                setIsLoading(false)
            }
        }

        fetchResult()
    }, [caseId])

    // 💡 generating 상태일 때만 2~3초 간격 폴링 실행 (과거 분석 상세가 아닐 때만)
    const isGenerating = reportStatus?.status === 'generating'

    usePolling(
        async () => {
            if (!caseId) return false
            try {
                const statusData = await resultService.getReportStatus(caseId)
                setReportStatus(statusData)

                // ready && can_download 이거나 failed 이면 폴링 중단
                if ((statusData?.status === 'ready' && statusData?.can_download) || statusData?.status === 'failed') {
                    return false
                }
            } catch (err) {
                console.error('상태 폴링 실패:', err)
                return false
            }
        },
        {
            interval: 3000,
            enabled: isGenerating && !isHistoryDetail,
            immediate: false,
        }
    )

    const handleExportPDF = async () => {
        if (!caseId || !reportStatus?.can_download) return

        try {
            setIsDownloading(true)

            // 💡 커스텀 api.get을 통해 안전하게 blob 데이터 획득
            const blobData = await api.get(`/analysis-cases/${caseId}/report.pdf`, {
                responseType: 'blob',
            })

            const blob = new Blob([blobData], { type: 'application/pdf' })
            const url = window.URL.createObjectURL(blob)
            const link = document.createElement('a')
            link.href = url
            link.setAttribute('download', `analysis-report-${caseId}.pdf`)
            document.body.appendChild(link)
            link.click()
            link.remove()
            window.URL.revokeObjectURL(url)
        } catch (err) {
            console.error('PDF 다운로드 실패:', err)
            alert('PDF 다운로드에 실패했습니다.')
        } finally {
            setIsDownloading(false)
        }
    }

    const handleOpenCandidateDetail = async (simCandidateId: string) => {
        if (isFetchingDetail) return
        setSelectedItem(null)

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

    if (error && onBackToUpload) {
        onBackToUpload()
    }

    return (
        <ResultView
            reportData={reportData}
            reportStatus={reportStatus}
            onExportPDF={handleExportPDF}
            isDownloading={isDownloading}
            isHistoryDetail={isHistoryDetail}
            onOpenCandidateDetail={handleOpenCandidateDetail}
            selectedItem={selectedItem}
            isModalOpen={isModalOpen}
            onCloseModal={handleCloseModal}
            isFetchingDetail={isFetchingDetail}
        />
    )
}