import Modal from '../../../components/common/DetailModal'
import {
    getSimStatusBadge,
    getAxisLabel,
    getEvidenceText
} from '../../../utils/resultData'

interface SimDetailModalProps {
    isOpen: boolean
    onClose: () => void
    selectedItem: any
}

export default function SimDetailModal({
    isOpen,
    onClose,
    selectedItem
}: SimDetailModalProps) {
    const metadata = selectedItem?.metadata || {}
    const comparison = selectedItem?.comparison || {}
    const axes = selectedItem?.axes || {}
    const evidencesMap = selectedItem?.evidences || {}

    return (
        <Modal
            isOpen={isOpen}
            onClose={onClose}
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
    )
}
