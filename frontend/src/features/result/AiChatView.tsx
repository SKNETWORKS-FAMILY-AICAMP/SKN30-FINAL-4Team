import { useRef, useEffect } from 'react'
import type { ChatMessage } from './AiChat'

interface AiChatViewProps {
    isChatOpen: boolean
    inputText: string
    messages: ChatMessage[]
    readOnly?: boolean
    onToggleChat: () => void
    onInputChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void
    onSendMessage: () => void
}

export default function AiChatView({
    isChatOpen,
    inputText,
    messages,
    readOnly = false,
    onToggleChat,
    onInputChange,
    onSendMessage,
}: AiChatViewProps) {
    const textareaRef = useRef<HTMLTextAreaElement | null>(null)

    useEffect(() => {
        const textarea = textareaRef.current
        if (textarea) {
            textarea.style.height = 'auto'
            textarea.style.height = `${Math.min(textarea.scrollHeight, 74)}px`
        }
    }, [inputText])

    return (
        <aside className={`fixed bottom-0 right-md w-[400px] bg-surface border border-outline-variant flex flex-col z-50 shadow-md rounded-t-xl overflow-hidden transition-all duration-300 ${isChatOpen ? 'h-[600px]' : 'h-16'}`}>

            {/* 팝업 헤더 (클릭 시 토글) */}
            <div
                onClick={onToggleChat}
                className="h-16 bg-primary-container text-on-tertiary px-md flex items-center justify-between shrink-0 cursor-pointer select-none"
            >
                <div className="flex items-center gap-sm">
                    <span className="material-symbols-outlined text-[20px]">forum</span>
                    <span className="font-title-sm text-[16px]">AI 질의응답</span>
                </div>
                <button
                    type="button"
                    className="hover:bg-white/10 p-xs rounded transition-colors"
                    aria-label="챗봇 창 토글"
                >
                    <span className="material-symbols-outlined text-[20px] transition-transform duration-300">
                        {isChatOpen ? 'keyboard_arrow_down' : 'keyboard_arrow_up'}
                    </span>
                </button>
            </div>

            {/* 팝업 바디 (열렸을 때만 표시) */}
            {isChatOpen && (
                <div className="flex-1 p-md bg-surface-bright flex flex-col overflow-hidden animate-fadeIn">

                    {/* 대화 말풍선 리스트 영역 (스크롤 가능, 단어 끊김 방지 적용) */}
                    <div className="flex-1 overflow-y-auto flex flex-col gap-md pr-xs group/scroll">
                        {messages.map((msg) => {
                            const isUser = msg.sender === 'user'
                            return (
                                <div
                                    key={msg.id}
                                    className={`p-md rounded-lg max-w-[90%] font-body-sm text-[14px] shadow-sm break-words whitespace-pre-wrap ${isUser
                                        ? 'bg-primary-container text-on-primary rounded-tr-none self-end'
                                        : 'bg-secondary-container text-on-secondary-container rounded-tl-none self-start'
                                        }`}
                                >
                                    {msg.text}
                                </div>
                            )
                        })}
                    </div>

                    {/* 💡 [핵심] readOnly 여부에 따라 입력창 vs 안내 문구 깔끔하게 분기 */}
                    <div className="relative mt-md pt-md border-t border-outline-variant">
                        {readOnly && (
                            <div className="absolute flex items-center justify-center gap-xs pt-5 inset-0 bg-surface/80 backdrop-blur-[2px] rounded-xl z-10">
                                <span className="material-symbols-outlined text-on-surface-variant" style={{ fontSize: '20px' }}>lock</span>
                                <span className="text-on-surface font-semibold text-[12px] text-center">과거 대화 이력은 열람만 가능합니다</span>
                            </div>
                        )}

                        <div className="flex items-center gap-sm bg-surface-container-low p-xs rounded-xl border border-outline-variant focus-within:border-primary transition-all">
                            <textarea
                                ref={textareaRef}
                                value={inputText}
                                onChange={onInputChange}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' && !e.shiftKey) {
                                        e.preventDefault()
                                        onSendMessage()
                                    }
                                }}
                                className="flex-1 bg-transparent border-none focus:ring-0 p-sm font-body-sm text-[14px] resize-none outline-none leading-normal group/scroll"
                                rows={1}
                                placeholder="질문을 입력하세요..."
                                style={{ lineHeight: '1.5' }}
                            />
                            <button
                                type="button"
                                onClick={onSendMessage}
                                className="bg-primary text-on-primary rounded-lg hover:opacity-90 transition-opacity shadow-sm flex items-center justify-center w-10 h-10 shrink-0 cursor-pointer self-end mb-0.5"
                            >
                                <span className="material-symbols-outlined text-[20px]">send</span>
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </aside>
    )
}