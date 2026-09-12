import Result from '../result/Result'

interface HistoryDetailViewProps {
    historyId: string
    onClose: () => void
}

export default function HistoryDetailView({ historyId, onClose }: HistoryDetailViewProps) {
    return (
        <div className="fixed inset-0 left-[320px] z-50 flex justify-end pointer-events-none overflow-hidden">
            <div className="w-full h-full bg-surface shadow-2xl flex flex-col relative pointer-events-auto animate-slide-right-in">
                
                {/* 우측 상단 X 닫기 버튼 */}
                <button 
                    type="button"
                    onClick={onClose}
                    className="absolute top-0 right-0 w-16 h-16 flex items-center justify-center bg-primary-container text-on-tertiary hover:bg-primary transition-all z-50 cursor-pointer"
                    title="닫기"
                >
                    <span className="material-symbols-outlined text-[24px]">close</span>
                </button>

                {/* Result 컴포넌트에 caseId만 넘겨주어 알아서 데이터 조회 및 렌더링 수행 */}
                <div className="flex-1 overflow-y-auto flex flex-col">
                    <Result 
                        caseId={historyId} 
                        readOnlyChat={true} 
                    />
                </div>
            </div>
        </div>
    )
}