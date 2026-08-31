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
            text: "분석 결과에 대해 궁금한 점을 물어보세요. '원가율 산정 방식' 또는 '유사 사례 기준' 등을 물어보시면 답변해 드립니다."
        },
        {
            id: '2',
            sender: 'user',
            text: "'수혜자 기준 누락' 항목에 대해 더 자세히 설명해줘."
        },
        {
            id: '3',
            sender: 'ai',
            text: "해당 항목은 사업 공고문 4절에 명시된 '중소기업기본법'에 따른 규모 요건이 현재 제출된 사업계획서 본문에 누락되어 있음을 의미합니다. 4.1.2절에 해당 정의를 추가하시길 권장합니다."
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