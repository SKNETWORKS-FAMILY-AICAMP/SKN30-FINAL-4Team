import { useState } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import Sidebar from './shared/Sidebar'
import HistoryDetailLayer from '../../features/history/HistoryDetailLayer'
import AlertModal from '../../components/common/AlertModal'
import { authService } from '../../services/authService'

export default function AppLayout() {
    const navigate = useNavigate()
    const [selectedHistoryId, setSelectedHistoryId] = useState<string | null>(null)

    const [expireTime, setExpireTime] = useState<number>(() => {
        const saved = sessionStorage.getItem('expire_time')
        if (saved) return Number(saved)
        const defaultExpire = Date.now() + 60 * 60 * 1000
        sessionStorage.setItem('expire_time', String(defaultExpire))
        return defaultExpire
    })

    const [remainingMinutes, setRemainingMinutes] = useState<number>(5)
    const [showImminentAlert, setShowImminentAlert] = useState(false)

    const handleRefreshSession = async () => {
        try {
            const response = await authService.refreshToken()
            if (response && response.access_token) {
                sessionStorage.setItem('access_token', response.access_token)
            }
            
            const newExpire = Date.now() + 60 * 60 * 1000
            sessionStorage.setItem('expire_time', String(newExpire))
            setExpireTime(newExpire)
            setShowImminentAlert(false)
        } catch (error) {
            handleLogout()
        }
    }

    const handleLogout = () => {
        sessionStorage.removeItem('access_token')
        sessionStorage.removeItem('expire_time')
        navigate('/')
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
        <div className="min-h-screen flex bg-background text-on-background overflow-hidden relative">
            <Sidebar
                onNewAnalysis={handleNewAnalysis}
                onHistoryClick={handleHistoryClick}
                onLogout={handleLogout}
                onRefreshSession={handleRefreshSession}
                expireTime={expireTime}
                onImminent={handleImminent}
            />

            <main className="ml-[320px] min-h-screen flex flex-col flex-1">
                <Outlet />
            </main>

            <HistoryDetailLayer 
                historyId={selectedHistoryId} 
                onClose={handleCloseHistory} 
            />

            {showImminentAlert && (
                <AlertModal
                    title="로그인 유효시간 만료 임박"
                    description={`로그인 유효시간이 ${remainingMinutes}분 남았습니다<br>연장하시겠습니까?`}
                    type="confirm"
                    confirmText="연장하기"
                    cancelText="나중에 하기"
                    onConfirm={handleRefreshSession}
                    onClose={() => setShowImminentAlert(false)}
                />
            )}
        </div>
    )
}