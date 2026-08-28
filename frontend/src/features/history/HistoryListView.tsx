interface HistoryItem {
    id: string
    title: string
    date: string
}

interface HistoryListViewProps {
    histories: HistoryItem[]
    hasMore: boolean
    onHistoryClick: (id: string) => void
    onLoadMore: () => void
}

export default function HistoryListView({ histories, hasMore, onHistoryClick, onLoadMore }: HistoryListViewProps) {
    return (
        <div className="flex flex-col flex-1 overflow-hidden">
            <div className="flex justify-between items-center mb-md px-xs">
                <h3 className="font-title-sm text-title-sm text-on-surface">최근 분석 이력</h3>
            </div>

            <ul
                className="flex-1 overflow-y-auto group/scroll pr-xs"
                style={{ scrollbarWidth: 'thin', scrollbarColor: 'rgba(67, 71, 78, 0.3) transparent' }}
            >
                {histories.map((item, index) => (
                    <li
                        key={item.id}
                        className={`min-w-0 ${index === 0 ? 'border-t border-b border-outline-variant/30' : 'border-b border-outline-variant/30'
                            }`}
                    >
                        <button
                            type="button"
                            onClick={() => onHistoryClick(item.id)}
                            className="w-full flex flex-col min-w-0 gap-xs py-md px-md hover:bg-surface-container-low transition-all cursor-pointer group text-left"
                        >
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

                {/* 조건부 렌더링: 더 불러올 항목이 있으면 [더보기], 없으면 마지막 안내 문구 */}
                {hasMore ? (
                    <button
                        onClick={onLoadMore}
                        className="w-full text-body-sm text-on-surface-variant hover:text-primary hover:bg-surface-container-low rounded-lg transition-colors flex items-center justify-center gap-xs py-md mt-md"
                    >
                        더보기 <span className="material-symbols-outlined text-sm">expand_more</span>
                    </button>
                ) : (
                    <div className="py-xl text-center">
                        <span className="text-body-sm text-on-surface-variant/70">마지막 항목입니다</span>
                    </div>
                )}
            </ul>
        </div>
    )
}