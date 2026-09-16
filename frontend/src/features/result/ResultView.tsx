import { useState, useEffect } from 'react'
import AiChat from '../chat/AiChat'
import Modal from '../../components/common/DetailModal'
import {
    getCplLabel,
    getFitLabel,
    getCplBadge,
    getFitBadge,
    getSimStatusBadge,
    getAxisLabel,
    getEvidenceText
} from '../../utils/resultData'

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

    const metadata = selectedItem?.metadata || {}
    const comparison = selectedItem?.comparison || {}
    const axes = selectedItem?.axes || {}
    const evidencesMap = selectedItem?.evidences || {}

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
                                <h3 className="font-title-sm text-title-sm text-on-surface">1. 요청자료 완전성 · 기초구조 점검</h3>
                                <p className="font-body-sm text-body-sm text-on-surface-variant">현재 요청서에서 13개 주요 항목들을 확인합니다</p>
                            </div>
                        </div>

                        <ul className="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-3 gap-sm">
                            {cplItems.map((item: any, index: number) => (
                                <li key={item.code || index} className="flex justify-between items-center bg-surface border border-outline-variant rounded-xs">
                                    <div className="p-md font-medium text-[14px] text-on-surface">
                                        {getCplLabel(item.code)}
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
                                <h3 className="font-title-sm text-title-sm text-on-surface">3. 기존 사업과의 유사 · 중복성 검토</h3>
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

            {/* 유사 공고 후보 상세 모달 */}
            <Modal
                isOpen={isModalOpen}
                onClose={onCloseModal}
                title="유사 공고 후보 상세"
            >
                {selectedItem ? (
                    <div className="flex flex-col gap-5 p-2">
                        {/* 메타데이터 영역 */}
                        <div className="flex flex-col">
                            <div className="flex items-start justify-between">
                                <span className="text-[13px] font-bold text-[#4B7A75] mb-1.5">유사 후보</span>{getSimStatusBadge(comparison.status)}
                            </div>
                            <h2 className="text-[22px] font-bold text-[#1A1C1E] mb-2">{metadata.title || '공고명 없음'}</h2>
                            <p className="text-[14px] text-[#43474E]">{comparison.summary || '요약 정보가 없습니다.'}</p>
                        </div>

                        <ul className='grid grid-cols-1 sm:grid-cols-3 gap-3'>
                            {[
                                { label: '지원 분야', value: metadata.support_field },
                                { label: '소관 부처', value: metadata.ministry },
                                { label: '수행 기관', value: metadata.executing_agency },
                                { label: '신청 기간', value: metadata.apply_period },
                                { label: '등록일', value: metadata.registered_at },
                                {
                                    label: '원문 링크',
                                    value: metadata.source_url ? (
                                        <a href={metadata.source_url} target="_blank" rel="noreferrer" className="text-primary flex items-center gap-xs text-[14px] font-bold">
                                            <span className="material-symbols-outlined" style={{ fontSize: '16px' }}>open_in_new</span> 바로가기
                                        </a>
                                    ) : '-'
                                }
                            ].map((item, idx) => (
                                <li key={idx} className="bg-[#F8F9FA] p-3.5 rounded-xl flex flex-col gap-1">
                                    <span className="text-[12px] font-medium text-[#74777F]">{item.label}</span>
                                    <span className="text-[14px] font-bold text-[#1A1C1E]">{item.value || '-'}</span>
                                </li>
                            ))}
                        </ul>

                        {/* 축별 비교 상세 카드 영역 */}
                        {Object.entries(axes).map(([axisKey, axisVal]: [string, any]) => {
                            const axisTitle = getAxisLabel(axisKey)
                            const commonPoints = axisVal.common_points || []
                            const differences = axisVal.differences || []
                            const requestIds = axisVal.request_evidence_ids || []
                            const existingIds = axisVal.existing_evidence_ids || []

                            return (
                                <div key={axisKey} className="border border-[#D1D5DB] rounded-2xl p-6 bg-white flex flex-col gap-5">
                                    <div>
                                        <div className="flex items-start justify-between">
                                            <h3 className="text-[18px] font-bold text-[#1A1C1E] mb-1">{axisTitle}</h3>
                                            {getSimStatusBadge(axisVal.status)}
                                        </div>
                                        <p className="text-[14px] text-[#43474E]">{axisVal.summary}</p>
                                    </div>

                                    {commonPoints.length > 0 && (
                                        <div className="bg-[#F8F9FA] p-4 rounded-xl flex flex-col gap-2">
                                            <span className="text-[13px] font-bold text-[#1A1C1E]">공통점</span>
                                            {commonPoints.map((cp: string, idx: number) => (
                                                <div key={idx} className="flex items-start gap-1 text-[13px] text-[#374151]">
                                                    <span className="text-[16px] leading-none select-none">•</span>
                                                    <span>{cp}</span>
                                                </div>
                                            ))}
                                        </div>
                                    )}

                                    {differences.length > 0 && (
                                        <div className="bg-[#F8F9FA] p-4 rounded-xl flex flex-col gap-2">
                                            <span className="text-[13px] font-bold text-[#1A1C1E]">차이점</span>
                                            {differences.map((diff: string, idx: number) => (
                                                <div key={idx} className='flex items-start gap-1 text-[13px] text-[#374151]'>
                                                    <span className="text-[16px] leading-none select-none">•</span>
                                                    <span>{diff}</span>
                                                </div>
                                            ))}
                                        </div>
                                    )}

                                    {requestIds.length > 0 && (
                                        <div className="flex flex-col gap-1.5">
                                            <span className="text-[13px] font-bold text-[#1A1C1E]">요청서 근거</span>
                                            <div className="p-3.5 bg-[#F8F9FA] rounded-r-lg border-l-4 border-[#8292A1] text-[13px] text-[#374151] leading-relaxed">
                                                {requestIds.map((id: string) => getEvidenceText(id, evidencesMap)).join('\n')}
                                            </div>
                                        </div>
                                    )}

                                    {existingIds.length > 0 && (
                                        <div className="flex flex-col gap-1.5">
                                            <span className="text-[13px] font-bold text-[#1A1C1E]">기존 공고 근거</span>
                                            <div className="p-3.5 bg-[#F8F9FA] rounded-r-lg border-l-4 border-[#8292A1] text-[13px] text-[#374151] leading-relaxed">
                                                {existingIds.map((id: string) => getEvidenceText(id, evidencesMap)).join('\n')}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )
                        })}
                    </div>
                ) : (
                    <div className="py-xl text-center text-on-surface-variant">정보가 없습니다.</div>
                )}
            </Modal>
        </>
    )
}