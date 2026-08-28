import { useState } from 'react'
import HistoryListView from './HistoryListView'

interface HistoryListProps {
    onHistoryClick: (id: string) => void
}

export default function HistoryList({ onHistoryClick }: HistoryListProps) {
    // 전체 더미 데이터 (추후 API 연동 시 페이징 처리)
    const allHistories = [
        { id: '1', title: '정부 지원사업 사전검토를 위한 기획안 및 기술 명세서 상세 분석 리포트_2023_최종', date: '2023.10.24 14:30' },
        { id: '2', title: 'AI 의료 진단 도구', date: '2023.10.22 09:15' },
        { id: '3', title: '스마트 팜 IoT 확장', date: '2023.10.20 16:45' },
        { id: '4', title: '차세대 배터리 제조', date: '2023.10.18 11:20' },
        { id: '5', title: '탄소중립 기술 검토', date: '2023.10.15 10:00' },
        { id: '6', title: '신재생 에너지 최적화', date: '2023.10.12 13:20' },
        { id: '7', title: '스마트 시티 인프라', date: '2023.10.10 15:45' },
        { id: '8', title: '자율주행 제어 시스템', date: '2023.10.08 09:30' },
        { id: '9', title: '바이오 헬스 케어 플랫폼', date: '2023.10.05 11:15' },
        { id: '10', title: '디지털 트윈 공정 관리', date: '2023.10.03 14:00' },
    ]

    // 예시로 5개씩 끊어서 보여주기 위한 상태 관리
    const [visibleCount, setVisibleCount] = useState(5)

    const displayedHistories = allHistories.slice(0, visibleCount)
    const hasMore = visibleCount < allHistories.length

    const handleLoadMore = () => {
        setVisibleCount((prev) => Math.min(prev + 5, allHistories.length))
    }

    return (
        <HistoryListView 
            histories={displayedHistories} 
            hasMore={hasMore}
            onHistoryClick={onHistoryClick} 
            onLoadMore={handleLoadMore}
        />
    )
}