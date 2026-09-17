import { useState, useEffect } from 'react'
import AiChat from '../chat/AiChat'
import CplSection from './components/CplSection'
import FitSection from './components/FitSection'
import SimSection from './components/SimSection'
import SimDetailModal from './components/SimDetailModal'

interface ResultViewProps {
    reportData: any
    reportStatus: any
    onExportPDF: () => void
    isDownloading: boolean
    isHistoryDetail: boolean
    onBackToUpload?: () => void
    onClose?: () => void
    onOpenCandidateDetail: (simCandidateId: string) => void
    selectedItem: any
    isModalOpen: boolean
    onCloseModal: () => void
    isFetchingDetail: boolean
}

export default function ResultView({
    reportData,
    reportStatus,
    onExportPDF,
    isDownloading,
    isHistoryDetail,
    onClose,
    onOpenCandidateDetail,
    selectedItem,
    isModalOpen,
    onCloseModal,
    isFetchingDetail
}: ResultViewProps) {
    const [report, setReport] = useState<any>(reportData || {})

    useEffect(() => {
        if (reportData) {
            setReport(reportData)
        }
    }, [reportData])

    const caseInfo = report?.case || {}
    const cplData = report?.cpl || {}
    const fitData = report?.fit || {}
    const simData = report?.sim || {}
    const mlData = report?.ml || null

    const cplItems = cplData?.items || []
    const fitItems = fitData?.items || []
    const similarCandidates = simData?.candidates || []
    const mlMessages = mlData ? Object.values(mlData).map((m: any) => m?.message).filter(Boolean) : []

    const isGenerating = reportStatus?.status === 'generating'
    const canDownload = reportStatus?.can_download === true

    return (
        <>
            <div className="flex-1 flex flex-col min-w-0 overflow-y-auto h-screen pb-xl px-md md:px-lg xl:px-xl w-full relative">
                {/* 상단 헤더 영역 */}
                <div className="mb-lg flex flex-col sm:flex-row sm:items-end justify-between gap-md pt-lg">
                    <div className="flex-1">
                        <h2 className="font-headline-md text-headline-md text-on-surface">분석 결과</h2>
                        <div className="mt-xs">
                            <p className="font-semibold text-body-md text-on-surface">
                                {caseInfo?.program_name || caseInfo?.original_filename || '분석 리포트'}
                            </p>
                            <p className="font-body-sm text-body-sm text-on-surface-variant mt-xs">
                                분석완료일시: {caseInfo?.completed_at ? new Date(caseInfo.completed_at).toLocaleString() : '-'}
                            </p>
                        </div>
                    </div>

                    <div className="flex items-end gap-sm">
                        {/* 💡 1. 과거 분석 상세가 아닐 때만 PDF 내보내기 버튼 노출 및 상태/스피너 제어 */}
                        {!isHistoryDetail && (
                            <button 
                                onClick={onExportPDF} 
                                disabled={!canDownload || isDownloading || isGenerating}
                                className={`px-md py-sm border border-outline-variant rounded bg-surface hover:bg-surface-container-low font-label-caps text-label-caps text-on-surface flex items-center gap-xs transition-colors ${
                                    (!canDownload || isDownloading || isGenerating) ? 'opacity-50 cursor-not-allowed' : ''
                                }`}
                            >
                                {(isDownloading || isGenerating) ? (
                                    <>
                                        <span className="material-symbols-outlined animate-spin" style={{ fontSize: '16px' }}>progress_activity</span>
                                        {isGenerating ? '보고서 생성 중...' : '다운로드 중...'}
                                    </>
                                ) : (
                                    <>
                                        <span className="material-symbols-outlined" style={{ fontSize: '16px' }}>download</span>
                                        보고서 내보내기
                                    </>
                                )}
                            </button>
                        )}

                        {onClose && (
                            <button type="button" onClick={onClose} className="px-md py-sm border border-outline-variant rounded bg-primary-container text-on-primary font-label-caps text-label-caps flex items-center gap-xs transition-colors">
                                닫기
                            </button>
                        )}
                    </div>
                </div>

                {/* 분석 본문 영역 */}
                <div className="flex flex-col gap-lg pb-xl">
                    <CplSection items={cplItems} />
                    <FitSection items={fitItems} />
                    <SimSection
                        candidates={similarCandidates}
                        mlMessages={mlMessages}
                        onOpenCandidateDetail={onOpenCandidateDetail}
                        isFetchingDetail={isFetchingDetail}
                    />
                </div>
            </div>

            {/* 💡 2 & 3. AI 질의응답 컴포넌트: isHistoryDetail을 readOnly 플래그로 전달 */}
            <AiChat 
                caseId={caseInfo?.analysis_case_id || caseInfo?.id} 
                readOnly={isHistoryDetail} 
            />

            {/* 유사 공고 후보 상세 모달 */}
            <SimDetailModal
                isOpen={isModalOpen}
                onClose={onCloseModal}
                selectedItem={selectedItem}
            />
        </>
    )
}