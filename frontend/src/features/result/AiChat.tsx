import { useState, useEffect } from 'react'
import AiChatView from './AiChatView'
import { chatService, type ChatMessageModel } from '../../services/chatService'

export type ChatMessage = {
    id: string
    sender: 'user' | 'ai'
    text: string
    status?: 'generating' | 'completed' | 'failed'
    assistantMessageId?: string
    retryCount?: number
    createdAt?: string
}

interface AiChatProps {
    caseId?: string | null
    readOnly?: boolean
}

export default function AiChat({ caseId, readOnly = false }: AiChatProps) {
    const [isChatOpen, setIsChatOpen] = useState(false)
    const [inputText, setInputText] = useState('')
    const [messages, setMessages] = useState<ChatMessage[]>([])
    const [hasLoaded, setHasLoaded] = useState<boolean>(false)
    const [cursor, setCursor] = useState<string | null>(null)
    const [hasMore, setHasMore] = useState<boolean>(false)
    const [isLoadingMore, setIsLoadingMore] = useState<boolean>(false)

    const fetchMessages = async (targetCursor?: string, isAppend = false) => {
        if (!caseId) return
        try {
            if (isAppend) {
                setIsLoadingMore(true)
            }
            
            const response = await chatService.listMessages(caseId, targetCursor)
            const items = response.items || []

            const mapped: ChatMessage[] = items.map((item: ChatMessageModel) => ({
                id: item.message_id,
                sender: item.role === 'user' ? 'user' : 'ai',
                text: item.content || (item.status === 'generating' ? '답변을 생성 중입니다...' : '내용이 없습니다.'),
                status: item.status as any,
                assistantMessageId: item.role === 'assistant' ? item.message_id : undefined,
                retryCount: item.retry_count || 0,
                createdAt: item.created_at || new Date().toISOString()
            }))

            setMessages((prev) => {
                const map = new Map(prev.map(m => [m.id, m]))
                mapped.forEach(m => map.set(m.id, m))
                
                return Array.from(map.values()).sort((a, b) => {
                    const timeA = new Date(a.createdAt || 0).getTime()
                    const timeB = new Date(b.createdAt || 0).getTime()
                    return timeA - timeB
                })
            })

            setCursor(response.next_cursor)
            setHasMore(!!response.next_cursor)
        } catch (error) {
            console.error('메시지 조회 실패:', error)
        } finally {
            setHasLoaded(true)
            setIsLoadingMore(false)
        }
    }

    useEffect(() => {
        if (caseId) {
            setMessages([])
            setCursor(null)
            setHasMore(false)
            setHasLoaded(false)
            fetchMessages()
        }
    }, [caseId])

    useEffect(() => {
        const hasGeneratingMessage = messages.some((msg) => msg.status === 'generating')

        if (!hasGeneratingMessage || !caseId || !isChatOpen) {
            return
        }

        const interval = setInterval(() => {
            fetchMessages()
        }, 3000)

        return () => clearInterval(interval)
    }, [messages, caseId, isChatOpen])

    if (!hasLoaded || (readOnly && messages.length === 0)) {
        return null
    }

    const handleToggleChat = () => {
        setIsChatOpen((prev) => !prev)
    }

    const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        if (readOnly) return
        setInputText(e.target.value)
    }

    const handleLoadMore = () => {
        if (cursor && !isLoadingMore) {
            fetchMessages(cursor, true)
        }
    }

    const handleSendMessage = async () => {
        if (readOnly || !inputText.trim() || !caseId) return

        const userText = inputText.trim()
        setInputText('') 

        const nowIso = new Date().toISOString()

        try {
            const res = await chatService.sendMessage(caseId, userText)
            
            const newUserMsg: ChatMessage = {
                id: res?.user_message_id || `user-${Date.now()}`,
                sender: 'user',
                text: userText,
                status: 'completed',
                createdAt: nowIso
            }
            const newAiMsg: ChatMessage = {
                id: res?.assistant_message_id || `ai-${Date.now()}`,
                sender: 'ai',
                text: '답변을 생성 중입니다...',
                status: 'generating',
                assistantMessageId: res?.assistant_message_id,
                createdAt: new Date(Date.now() + 10).toISOString()
            }

            setMessages((prev) => [...prev, newUserMsg, newAiMsg])
        } catch (error: any) {
            console.error('메시지 전송 실패:', error)
        }
    }

    const handleRetryMessage = async (assistantMessageId?: string) => {
        if (readOnly || !caseId || !assistantMessageId) return

        try {
            await chatService.retryMessage(caseId, assistantMessageId)
            setMessages((prev) =>
                prev.map((msg) =>
                    msg.assistantMessageId === assistantMessageId
                        ? { ...msg, text: '답변을 다시 생성 중입니다...', status: 'generating' }
                        : msg
                )
            )
        } catch (error) {
            console.error('답변 재시도 실패:', error)
        }
    }

    return (
        <AiChatView 
            isChatOpen={isChatOpen}
            inputText={inputText}
            messages={messages}
            readOnly={readOnly}
            hasMore={hasMore}
            isLoadingMore={isLoadingMore}
            onToggleChat={handleToggleChat}
            onInputChange={handleInputChange}
            onLoadMore={handleLoadMore}
            onSendMessage={handleSendMessage}
            onRetryMessage={handleRetryMessage}
        />
    )
}