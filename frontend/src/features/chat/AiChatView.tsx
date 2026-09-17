import { useRef, useEffect } from 'react'
import type { ChatMessage } from './AiChat'

interface AiChatViewProps {
    caseId?: string | null
    isChatOpen: boolean
    inputText: string
    messages: ChatMessage[]
    readOnly?: boolean
    hasMore?: boolean
    isLoadingMore?: boolean
    onToggleChat: () => void
    onInputChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void
    onLoadMore: () => void
    onSendMessage: () => void
    onRetryMessage: (assistantMessageId?: string) => void
}

export default function AiChatView({
    caseId,
    isChatOpen,
    inputText,
    messages,
    readOnly = false,
    hasMore = false,
    isLoadingMore = false,
    onToggleChat,
    onInputChange,
    onLoadMore,
    onSendMessage,
    onRetryMessage,
}: AiChatViewProps) {
    const textareaRef = useRef<HTMLTextAreaElement | null>(null)
    const scrollContainerRef = useRef<HTMLDivElement | null>(null)
    const lastMessageRef = useRef<HTMLDivElement | null>(null)

    // 스크롤 위치 보존 및 첫 진입 관리용 ref
    const savedScrollTopRef = useRef<number | null>(null)
    const hasOpenedThisCaseRef = useRef<boolean>(false)
    const prevMessagesRef = useRef<ChatMessage[]>([])
    const prevCaseIdRef = useRef<string | null | undefined>(caseId)

    // caseId 변경 시 상태 초기화
    useEffect(() => {
        if (prevCaseIdRef.current !== caseId) {
            prevCaseIdRef.current = caseId
            hasOpenedThisCaseRef.current = false
            savedScrollTopRef.current = null
            prevMessagesRef.current = []
        }
    }, [caseId])

    // 스크롤 이벤트 핸들러: 레이어를 닫거나 리렌더링되기 전 스크롤 위치 실시간 보존
    const handleScroll = () => {
        if (scrollContainerRef.current) {
            savedScrollTopRef.current = scrollContainerRef.current.scrollTop
        }
    }

    // 💡 레이어 열림/닫힘(isChatOpen) 시 스크롤 처리
    useEffect(() => {
        if (!isChatOpen) return

        const timer = requestAnimationFrame(() => {
            if (!scrollContainerRef.current) return
            const container = scrollContainerRef.current

            // 3. 해당 케이스에서 레이어를 처음 열었을 경우: 무조건 최하단으로 이동
            if (!hasOpenedThisCaseRef.current) {
                hasOpenedThisCaseRef.current = true
                container.scrollTop = container.scrollHeight
                savedScrollTopRef.current = container.scrollTop
            } else if (savedScrollTopRef.current !== null) {
                // 2. 레이어를 닫았다가 다시 열었을 때: 이전 스크롤 위치 유지
                container.scrollTop = savedScrollTopRef.current
            }
        })

        return () => cancelAnimationFrame(timer)
    }, [isChatOpen])

    // 💡 메시지 변경 시 스크롤 처리
    useEffect(() => {
        if (!isChatOpen || !scrollContainerRef.current) {
            prevMessagesRef.current = messages
            return
        }

        if (isLoadingMore) {
            prevMessagesRef.current = messages
            return
        }

        const prevMessages = prevMessagesRef.current
        prevMessagesRef.current = messages

        if (messages.length === 0) return

        // 이전 상태에서 generating 중이던 AI 메시지가 있었는지 확인
        const prevHadGenerating = prevMessages.some(
            (m) => m.sender === 'ai' && m.status === 'generating'
        )
        const lastMsg = messages[messages.length - 1]

        // 1. 대화 진행 중: 답변 바로 받은 직후 (generating -> 답변 완료/텍스트 수신)
        const isAnswerJustReceived =
            prevHadGenerating &&
            lastMsg?.sender === 'ai' &&
            lastMsg?.status !== 'generating'

        // 사용자가 질문을 방금 전송한 직후
        const isQuestionJustSent =
            prevMessages.length < messages.length &&
            lastMsg?.sender === 'ai' &&
            lastMsg?.status === 'generating'

        if (isAnswerJustReceived) {
            // 답변 바로 받은 직후 -> 마지막 말풍선 시작 지점으로 이동
            const timer = requestAnimationFrame(() => {
                if (scrollContainerRef.current && lastMessageRef.current) {
                    const container = scrollContainerRef.current
                    const target = lastMessageRef.current
                    const containerRect = container.getBoundingClientRect()
                    const targetRect = target.getBoundingClientRect()

                    const offset = targetRect.top - containerRect.top
                    container.scrollTop = container.scrollTop + offset - 8
                    savedScrollTopRef.current = container.scrollTop
                }
            })
            return () => cancelAnimationFrame(timer)
        } else if (isQuestionJustSent) {
            // 질문 전송 직후 -> 최하단(답변 생성 중 로딩 표시)으로 이동
            const timer = requestAnimationFrame(() => {
                if (scrollContainerRef.current) {
                    scrollContainerRef.current.scrollTop = scrollContainerRef.current.scrollHeight
                    savedScrollTopRef.current = scrollContainerRef.current.scrollTop
                }
            })
            return () => cancelAnimationFrame(timer)
        }
    }, [messages, isChatOpen, isLoadingMore])

    useEffect(() => {
        const textarea = textareaRef.current
        if (textarea) {
            textarea.style.height = 'auto'
            textarea.style.height = `${Math.min(textarea.scrollHeight, 74)}px`
        }
    }, [inputText])

    return (
        <aside className={`fixed bottom-0 right-md w-[400px] bg-surface border border-outline-variant flex flex-col z-50 shadow-md rounded-t-xl overflow-hidden transition-all duration-300 ${isChatOpen ? 'h-[600px]' : 'h-16'}`}>

            {/* 팝업 헤더 */}
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

            {/* 팝업 바디 */}
            {isChatOpen && (
                <div className="flex-1 p-md bg-surface-bright flex flex-col overflow-hidden animate-fadeIn">

                    {/* 대화 말풍선 리스트 영역 */}
                    <div 
                        ref={scrollContainerRef}
                        onScroll={handleScroll}
                        className="flex-1 overflow-y-auto flex flex-col gap-md pr-xs group/scroll"
                    >
                        {/* 과거 메시지 더보기 버튼 */}
                        {hasMore && (
                            <div className="flex justify-center my-2">
                                <button
                                    type="button"
                                    onClick={onLoadMore}
                                    disabled={isLoadingMore}
                                    className="px-3 py-1 bg-surface-container-high text-on-surface-variant rounded-full text-xs shadow-sm hover:bg-surface-container-highest transition-colors disabled:opacity-50"
                                >
                                    {isLoadingMore ? '불러오는 중...' : '이전 대화 더보기'}
                                </button>
                            </div>
                        )}

                        {messages.map((msg, index) => {
                            const isUser = msg.sender === 'user'
                            const isGenerating = msg.status === 'generating'
                            const isFailed = msg.status === 'failed'
                            const isLast = index === messages.length - 1

                            return (
                                <div
                                    key={msg.id}
                                    ref={isLast ? lastMessageRef : null}
                                    className={`p-md rounded-lg max-w-[90%] font-body-sm text-[14px] shadow-sm break-words whitespace-pre-wrap flex flex-col gap-xs ${
                                        isUser
                                            ? 'bg-primary-container text-on-primary rounded-tr-none self-end'
                                            : 'bg-secondary-container text-on-secondary-container rounded-tl-none self-start'
                                    }`}
                                >
                                    <div>{msg.text}</div>

                                    {isGenerating && (
                                        <div className="flex items-center gap-xs text-xs opacity-70">
                                            <span className="w-2 h-2 rounded-full bg-current animate-ping" />
                                            <span>답변 생성 중...</span>
                                        </div>
                                    )}

                                    {isFailed && !readOnly && (
                                        <button
                                            type="button"
                                            onClick={() => onRetryMessage(msg.assistantMessageId)}
                                            className="self-start mt-1 px-2 py-1 bg-error text-on-error rounded text-xs flex items-center gap-1 cursor-pointer hover:opacity-90"
                                        >
                                            <span className="material-symbols-outlined text-[14px]">refresh</span>
                                            재시도
                                        </button>
                                    )}
                                </div>
                            )
                        })}
                    </div>

                    {/* 입력 영역 */}
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