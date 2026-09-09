import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authService } from '../../services/authService'
import AppLayoutView from './AppLayoutView'

export default function AppLayout() {
    const navigate = useNavigate()
    const [selectedHistoryId, setSelectedHistoryId] = useState<string | null>(null)

    // 세션 만료 시간 관리 (기본 1시간)
    const [expireTime, setExpireTime] = useState<number>(() => {
        const saved = sessionStorage.getItem('expire_time')
        if (saved) return Number(saved)
        const defaultExpire = Date.now() + 60 * 60 * 1000
        sessionStorage.setItem('expire_time', String(defaultExpire))
        return defaultExpire
    })

    const [remainingMinutes, setRemainingMinutes] = useState<number>(5)
    const [showImminentAlert, setShowImminentAlert] = useState(false)

    // 세션 연장 함수 (이미 구현된 authService 활용)
    const handleRefreshSession = async () => {
        try {
            await authService.extendSession()
            
            const newExpire = Date.now() + 60 * 60 * 1000
            sessionStorage.setItem('expire_time', String(newExpire))
            setExpireTime(newExpire)
            setShowImminentAlert(false)
        } catch (error) {
            handleLogout()
        }
    }

    // 로그아웃 함수 (이미 구현된 authService 활용)
    const handleLogout = async () => {
        try {
            await authService.logout()
        } catch (e) {
            // 에러가 나도 로컬 세션 정보는 정리 후 이동
        } finally {
            sessionStorage.removeItem('expire_time')
            navigate('/')
        }
    }

    const handleNewAnalysis = () => {
        setSelectedHistoryId(null)
        navigate('/')
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
        />
    )
}