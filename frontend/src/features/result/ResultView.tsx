import AiChat from './AiChat'

interface ResultViewProps {
    onExportPDF: () => void
    onBackToUpload?: () => void
    onClose?: () => void          // 💡 SCR-007-P1용 닫기 버튼 핸들러
    readOnlyChat?: boolean       // 💡 AI 챗봇 읽기 전용 여부
}

export default function ResultView({ 
    onExportPDF, 
    onClose, 
    readOnlyChat = false 
}: ResultViewProps) {
    return (
        <>
            <div className="flex-1 flex flex-col min-w-0 overflow-y-auto h-screen pb-xl px-md md:px-lg xl:px-xl w-full relative">
                
                {/* 상단 헤더 영역 */}
                <div className="mb-lg flex flex-col sm:flex-row sm:items-end justify-between gap-md pt-lg relative">
                    <div>
                        <h2 className="font-headline-md text-headline-md text-on-surface">분석 결과</h2>
                        <p className="font-body-md text-body-md text-on-surface-variant mt-xs">
                            문서 ID: PRG-2024-883A • 처리일시: 오늘, 오전 09:42
                        </p>
                    </div>
                    <div className="flex items-center gap-sm">
                        <button 
                            type="button"
                            onClick={onExportPDF}
                            className="px-md py-sm border border-outline-variant rounded bg-surface hover:bg-surface-container-low font-label-caps text-label-caps text-on-surface flex items-center gap-xs transition-colors cursor-pointer"
                        >
                            <span className="material-symbols-outlined text-[16px]">download</span> 
                            보고서 내보내기
                        </button>

                        {/* 💡 SCR-007-P1 우측 상단 고정 닫기(X) 버튼[cite: 1, 2] */}
                        {onClose && (
                            <button 
                                type="button"
                                onClick={onClose}
                                className="w-12 h-12 flex items-center justify-center bg-primary-container text-on-tertiary hover:bg-primary transition-all rounded-none cursor-pointer"
                                title="닫기"
                            >
                                <span className="material-symbols-outlined text-[24px]">close</span>
                            </button>
                        )}
                    </div>
                </div>

                {/* 대시보드 그리드 본문 (기존 분석 결과 피처 컴포넌트 재사용) */}
                <div className="grid grid-cols-12 gap-lg">
                    {/* 자체 점검도, 구조적 정합성, 검토 쟁점 테이블 등 기존 피처 그대로 렌더링[cite: 1] */}
                </div>
            </div>

            {/* AI 질의응답 (readOnly 상태를 props로 전달) */}
            <AiChat readOnly={readOnlyChat} />
        </>
    )
}