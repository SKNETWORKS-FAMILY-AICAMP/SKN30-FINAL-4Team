import { useState, useEffect } from 'react'
import AiChat from './AiChat'
import Modal from '../../components/common/Modal'
import { getCplLabel, getFitLabel, getCplBadge, getFitBadge } from '../../utils/resultData'

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

                {/* 분석 본문 */}
                <div className="flex flex-col gap-lg pb-xl">
                    {/* Category 1: 요청자료 완전성·기초구조 점검 */}
                    <div className="flex flex-col bg-surface-container-lowest border border-outline-variant rounded p-lg">
                        <div className="flex justify-between items-start mb-md">
                            <div>
                                <h3 className="font-title-sm text-title-sm text-on-surface">1. 요청자료 완전성·기초구조 점검</h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant">현재 요청서에서 13개 주요 항목들을 확인합니다</p>
                            </div>
                        </div>

                        <ul className="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-3 gap-sm">
                            {cplItems.map((item: any, index: number) => (
                                <li key={item.code || index} className="flex justify-between items-center bg-surface border border-outline-variant rounded-xs">
                                    <div className="p-md font-medium text-[14px] text-on-surface">
                                        {getCplLabel(item.detail.field_code)}
                                    </div>
                                    {getCplBadge(item.status)}
                                </li>
                            ))}
                        </ul>
                    </div>

                    {/* Category 2: 내부 정합성 점검 */}
                    <div className="flex flex-col bg-surface-container-lowest border border-outline-variant rounded p-lg">
                        <div className="flex justify-between items-start mb-md">
                            <div>
                                <h3 className="font-title-sm text-title-sm text-on-surface">2. 내부 정합성 점검</h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant">현재 요청서에서 확인되는 항목 간 연결성·범위 차이·충돌 여부를 점검합니다</p>
                            </div>
                        </div>

                        <div className="grid grid-cols-1 xl:grid-cols-2 gap-sm">
                            {fitItems.map((item: any, index: number) => (
                                <div key={item.code || index} className="flex flex-col justify-between border border-outline-variant p-md rounded-lg bg-surface hover:bg-surface-container-low text-left">
                                    <div className="flex justify-between items-center w-full mb-xs">
                                        <span className="font-label-caps text-on-surface-variant font-semibold">{getFitLabel(item.code)}</span>
                                        {getFitBadge(item.status)}
                                    </div>
                                    <div className="bg-surface-container-lowest p-2 rounded mt-2 text-xs text-on-surface-variant w-full">{item.summary}</div>
                                </div>
                            ))}
                        </div>
                    </div>

                    {/* Category 3: 기존 사업과의 유사·중복성 검토 */}
                    <div className="flex flex-col bg-surface-container-lowest border border-outline-variant rounded p-lg">
                        <div className="flex justify-between items-start mb-md">
                            <div>
                                <h3 className="font-title-sm text-title-sm text-on-surface">3. 기존 사업과의 유사·중복성 검토</h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant">기존 지원사업 데이터에서 비교가 필요한 후보를 검색합니다</p>
                            </div>
                        </div>

                        <table className="w-full border-collapse border-t-2 border-b-2 text-left zebra-table" style={{ tableLayout: 'fixed' }}>
                            <thead>
                                <tr className="border-b border-outline-variant bg-surface-container-low">
                                    <th className="p-md font-semibold text-body-sm text-on-surface">사업명</th>
                                    <th className="w-[108px]"></th>
                                </tr>
                            </thead>
                            <tbody className="text-body-sm text-on-surface-variant">
                                {similarCandidates.map((candidate: any, index: number) => (
                                    <tr key={candidate.sim_candidate_id || index} className="border-b border-outline-variant hover:bg-surface-container-low transition-colors">
                                        <td className="p-md font-medium text-on-surface">
                                            {candidate.title}
                                        </td>
                                        <td className="p-md text-center">
                                            <button 
                                                onClick={() => onOpenCandidateDetail(candidate.sim_candidate_id)}
                                                disabled={isFetchingDetail}
                                                className={`w-[74px] px-3 py-1.5 border border-outline-variant rounded bg-surface hover:bg-white text-[12px] font-medium transition-colors cursor-pointer ${isFetchingDetail ? 'opacity-50 cursor-not-allowed' : ''}`}
                                            >
                                                {isFetchingDetail ? '조회중' : '상세보기'}
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                        
                        {mlMessages.length > 0 && (
                            <div className="mt-md p-md bg-surface-container-low border border-outline-variant rounded-xs flex flex-col gap-xs">
                                <div className="flex items-center gap-xs text-primary font-semibold text-body-sm mb-xs">
                                    <span className="material-symbols-outlined" style={{ fontSize: '18px' }}>info</span>
                                    <span>참고 사항</span>
                                </div>
                                <ul className="flex flex-col gap-xs text-body-sm text-on-surface-variant">
                                    {mlMessages.map((msg: string, idx: number) => (
                                        <li key={idx} className="flex items-start gap-xs">
                                            <span className="text-primary font-bold select-none">•</span>
                                            <span>{msg}</span>
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        )}
                    </div>
                </div>
            </div>

            {/* AI 질의응답 컴포넌트 */}
            <AiChat caseId={caseInfo?.analysis_case_id || caseInfo?.id} readOnly={readOnlyChat} />

            {/* 💡 기존 공통 Modal 컴포넌트 사용 */}
            <Modal 
                isOpen={isModalOpen}
                onClose={onCloseModal}
                title="유사 공고 후보 상세"
            >
                {selectedItem ? (
                    <div className="flex flex-col gap-sm text-body-sm text-on-surface">
                        <div><strong>사업명:</strong> {selectedItem.title}</div>
                        <div><strong>발행기관:</strong> {selectedItem.issuing_organization || '-'}</div>
                        <div><strong>상태:</strong> {selectedItem.notice_status || '-'}</div>
                        <div><strong>요약:</strong> {selectedItem.summary || '-'}</div>
                        {selectedItem.source_url && (
                            <div>
                                <strong>원문 링크:</strong>{' '}
                                <a href={selectedItem.source_url} target="_blank" rel="noreferrer" className="text-primary underline">
                                    바로가기
                                </a>
                            </div>
                        )}
                    </div>
                ) : (
                    <div className="py-xl text-center text-on-surface-variant">정보가 없습니다.</div>
                )}
            </Modal>
        </>
    )
}