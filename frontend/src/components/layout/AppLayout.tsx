import { useState, useCallback, useEffect } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../../providers/AuthProvider'
import { analysisService } from '../../services/analysisService'
import AppLayoutView from './AppLayoutView'

interface AppLayoutProps {
    displayName: string
}

export default function AppLayout({ displayName }: AppLayoutProps) {
    const navigate = useNavigate()
    const location = useLocation() // URL 변경 감지를 위해 추가
    const { expireTime, refreshSession, logout } = useAuth()
    
    const [selectedHistoryId, setSelectedHistoryId] = useState<string | null>(null)
    const [sessionId, setSessionId] = useState<string | null>(null)

    const [remainingMinutes, setRemainingMinutes] = useState<number>(5)
    const [showImminentAlert, setShowImminentAlert] = useState(false)

    // [핵심] URL이 바뀔 때마다 화면에 떠 있던 모달이나 사이드 패널 등 UI 상태를 일괄 초기화
    useEffect(() => {
        setSelectedHistoryId(null)
        setShowImminentAlert(false)
    }, [location.pathname])

    // [중복 제거] 열린 세션이 있다면 안전하게 닫는 공통 함수
    const closeActiveSession = useCallback(async (targetSessionId: string | null) => {
        if (!targetSessionId) return
        try {
            await analysisService.closeSession(targetSessionId)
        } catch (error) {
            console.error('세션 종료 실패:', error)
        }
    }, [])

    const handleRefreshSession = async () => {
        try {
            await refreshSession()
            setShowImminentAlert(false)
        } catch (error) {
            handleLogout()
        }
    }

    const handleLogout = async () => {
        try {
            await closeActiveSession(sessionId)
            await logout()
        } catch (e) {
            // 에러 무시 후 정리
        }
    }

    const handleNewAnalysis = async () => {
        await closeActiveSession(sessionId)
        setSessionId(null)
        setSelectedHistoryId(null)
        setShowImminentAlert(false)

        if (location.pathname !== '/') {
            navigate('/')
        }
    }

    const handleHistoryClick = (id: string) => {
        setSelectedHistoryId(id)
    }

    const handleCloseHistory = () => {
        setSelectedHistoryId(null)
    }

    const handleImminent = (minutes: number) => {
        setRemainingMinutes(minutes)
        setShowImminentAlert(true)
    }

    return (
        <AppLayoutView
            selectedHistoryId={selectedHistoryId}
            displayName={displayName}
            expireTime={expireTime}
            showImminentAlert={showImminentAlert}
            remainingMinutes={remainingMinutes}
            onNewAnalysis={handleNewAnalysis}
            onHistoryClick={handleHistoryClick}
            onLogout={handleLogout}
            onRefreshSession={handleRefreshSession}
            onCloseHistory={handleCloseHistory}
            onImminent={handleImminent}
            onCloseImminentAlert={() => setShowImminentAlert(false)}
            setSessionId={setSessionId}
        />
    )
}