interface SimSectionProps {
    candidates: any[]
    mlMessages?: string[]
    onOpenCandidateDetail: (simCandidateId: string) => void
    isFetchingDetail: boolean
}

export default function SimSection({
    candidates,
    mlMessages = [],
    onOpenCandidateDetail,
    isFetchingDetail
}: SimSectionProps) {
    return (
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
                    {candidates.map((candidate: any, index: number) => (
                        <tr key={candidate.sim_candidate_id || index} className="border-b border-outline-variant hover:bg-surface-container-low transition-colors">
                            <td className="p-md font-medium text-on-surface">
                                {candidate.title}
                            </td>
                            <td className="p-md text-center">
                                <button
                                    onClick={() => onOpenCandidateDetail(candidate.sim_candidate_id)}
                                    disabled={isFetchingDetail}
                                    className={`w-[74px] px-3 py-1.5 border border-outline-variant rounded bg-surface hover:bg-white text-[12px] font-medium transition-colors ${isFetchingDetail ? 'opacity-50 cursor-not-allowed' : ''}`}
                                >
                                    {isFetchingDetail ? <span className="material-symbols-outlined animate-spin" style={{ fontSize: '12px' }}>progress_activity</span> : '상세보기'}
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
    )
}
