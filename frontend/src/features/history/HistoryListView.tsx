interface HistoryItem {
    id: string
    title: string
    date: string
}

interface HistoryListViewProps {
    histories: HistoryItem[]
    hasMore: boolean
    totalCount: number
    onHistoryClick: (id: string) => void
    onLoadMore: () => void
}

export default function HistoryListView({ histories, hasMore, totalCount, onHistoryClick, onLoadMore }: HistoryListViewProps) {
    return (
        <div className="flex flex-col flex-1 overflow-hidden">
            <div className="flex justify-between items-center mb-md px-xs">
                <h3 className="font-title-sm text-title-sm text-on-surface">최근 분석 이력</h3>
            </div>

            {/* 스크롤 영역을 감싸는 컨테이너 */}
            <div
                className="flex-1 overflow-y-auto group/scroll flex flex-col"
                style={{ scrollbarWidth: 'thin', scrollbarColor: 'rgba(67, 71, 78, 0.3) transparent' }}
            >
                {/* <ul> 내부에는 오직 <li>만 존재하도록 수정 */}
                <ul className="flex flex-col">
                    {histories.map((item, index) => (
                        <li
                            key={item.id}
                            className={`min-w-0 ${index === 0 ? 'border-t border-b border-outline-variant/30' : 'border-b border-outline-variant/30'}`}
                        >
                            <button
                                type="button"
                                onClick={() => onHistoryClick(item.id)}
                                className="relative w-full flex flex-col min-w-0 gap-xs py-md px-md hover:bg-surface-container-low transition-all group text-left"
                            >
                                <div className="absolute left-0 top-0 bottom-0 w-1 bg-primary opacity-0 group-hover:opacity-100 transition-opacity"></div>

                                <h4 className="font-semibold text-sm text-on-surface truncate group-hover:text-primary transition-colors">
                                    {item.title}
                                </h4>
                                <div className="flex items-center gap-xs">
                                    <span
                                        className="material-symbols-outlined text-on-surface-variant/70"
                                        style={{ fontSize: '14px' }}
                                    >
                                        schedule
                                    </span>
                                    <span className="font-data-mono text-[12px] text-on-surface-variant/70">{item.date}</span>
                                </div>
                            </button>
                        </li>
                    ))}
                </ul>

                {/* 리스트 외부(스크롤 영역 내)에 제어 영역 배치 */}
                {hasMore ? (
                    <div className="p-md">
                        <button
                            type="button"
                            onClick={onLoadMore}
                            className="w-full py-sm border border-outline-variant rounded-lg text-body-sm text-on-surface-variant hover:bg-surface-container-low hover:text-primary transition-all flex items-center justify-center gap-xs cursor-pointer"
                        >
                            더보기 <span className="material-symbols-outlined" style={{ fontSize: '16px' }}>expand_more</span>
                        </button>
                    </div>
                ) : (
                    /* 💡 전체 개수가 5개를 초과하면서 더 이상 불러올 게 없을 때만 노출 */
                    totalCount > 5 && (
                        <div className="py-md text-center">
                            <span className="text-body-sm text-on-surface-variant/70">마지막 항목입니다</span>
                        </div>
                    )
                )}
            </div>
        </div>
    )
}