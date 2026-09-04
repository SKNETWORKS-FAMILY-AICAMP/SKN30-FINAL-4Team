import { useState } from 'react'
import AiChatView from './AiChatView'

export type ChatMessage = {
    id: string
    sender: 'user' | 'ai'
    text: string
}

interface AiChatProps {
    readOnly?: boolean // 과거 이력 상세 등에서 읽기 전용으로 제어하기 위한 프롭
}

export default function AiChat({ readOnly = false }: AiChatProps) {
    const [isChatOpen, setIsChatOpen] = useState(false)
    const [inputText, setInputText] = useState('')

    const [messages, setMessages] = useState<ChatMessage[]>([
        {
            id: '1',
            sender: 'ai',
            text: "분석 결과에 대해 궁금한 점을 물어보세요."
        }
    ])

    const handleToggleChat = () => {
        setIsChatOpen((prev) => !prev)
    }

    const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        if (readOnly) return
        setInputText(e.target.value)
    }

    const handleSendMessage = () => {
        if (readOnly || !inputText.trim()) return

        const newUserMessage: ChatMessage = {
            id: Date.now().toString(),
            sender: 'user',
            text: inputText.trim()
        }

        setMessages((prev) => [...prev, newUserMessage])
        setInputText('')

        setTimeout(() => {
            const newAiMessage: ChatMessage = {
                id: (Date.now() + 1).toString(),
                sender: 'ai',
                text: "문의하신 내용에 대한 추가 분석 결과입니다."
            }
            setMessages((prev) => [...prev, newAiMessage])
        }, 600)
    }

    return (
        <AiChatView 
            isChatOpen={isChatOpen}
            inputText={inputText}
            messages={messages}
            readOnly={readOnly}
            onToggleChat={handleToggleChat}
            onInputChange={handleInputChange}
            onSendMessage={handleSendMessage}
        />
    )
}