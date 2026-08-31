import Result from '../result/Result'

interface HistoryDetailLayerProps {
    historyId: string | null
    onClose: () => void
}

export default function HistoryDetailLayer({ historyId, onClose }: HistoryDetailLayerProps) {
    if (!historyId) return null

    return (
        <div className="fixed inset-0 left-[320px] z-50 flex justify-end pointer-events-none overflow-hidden">
            {/* 꼬임 없는 깔끔한 클래스 방식 적용 */}
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

                {/* 결과 화면 컴포넌트 호출 (AI 챗봇 읽기 전용 모드 활성화) */}
                <div className="flex-1 overflow-y-auto">
                    <Result readOnlyChat={true} />
                </div>
            </div>
        </div>
    )
}