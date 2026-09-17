import { useState, useEffect } from 'react'
import AiChat from '../chat/AiChat'
import CplSection from './components/CplSection'
import FitSection from './components/FitSection'
import SimSection from './components/SimSection'
import SimDetailModal from './components/SimDetailModal'

interface ResultViewProps {
    reportData: any
    onExportPDF: () => void
    onBackToUpload?: () => void
    onClose?: () => void
    readOnlyChat?: boolean
    onOpenCandidateDetail: (simCandidateId: string) => void
    selectedItem: any
    isModalOpen: boolean
    onCloseModal: () => void
    isFetchingDetail: boolean
}

export default function ResultView({
    reportData,
    onExportPDF,
    onClose,
    readOnlyChat = false,
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

                    <div className="flex items-end">
                        <button onClick={onExportPDF} className="px-md py-sm border border-outline-variant rounded bg-surface hover:bg-surface-container-low font-label-caps text-label-caps text-on-surface flex items-center gap-xs transition-colors cursor-pointer">
                            <span className="material-symbols-outlined" style={{ fontSize: '16px' }}>download</span>
                            보고서 내보내기
                        </button>

                        {onClose && (
                            <button type="button" onClick={onClose} className="px-md py-sm border border-outline-variant rounded bg-primary-container text-on-primary font-label-caps text-label-caps flex items-center gap-xs transition-colors cursor-pointer">
                                닫기
                            </button>
                        )}
                    </div>
                </div>

                {/* 분석 본문 영역 */}
                <div className="flex flex-col gap-lg pb-xl">
                    {/* Category 1: 요청자료 완전성·기초구조 점검 */}
                    <CplSection items={cplItems} />

                    {/* Category 2: 내부 정합성 점검 */}
                    <FitSection items={fitItems} />

                    {/* Category 3: 기존 사업과의 유사·중복성 검토 */}
                    <SimSection
                        candidates={similarCandidates}
                        mlMessages={mlMessages}
                        onOpenCandidateDetail={onOpenCandidateDetail}
                        isFetchingDetail={isFetchingDetail}
                    />
                </div>
            </div>

            {/* AI 질의응답 컴포넌트 */}
            <AiChat caseId={caseInfo?.analysis_case_id || caseInfo?.id} readOnly={readOnlyChat} />

            {/* 유사 공고 후보 상세 모달 */}
            <SimDetailModal
                isOpen={isModalOpen}
                onClose={onCloseModal}
                selectedItem={selectedItem}
            />
        </>
    )
}