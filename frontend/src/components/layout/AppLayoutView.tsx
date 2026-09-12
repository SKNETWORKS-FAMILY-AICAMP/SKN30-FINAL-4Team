import { Outlet } from 'react-router-dom'
import Sidebar from './shared/Sidebar'
import HistoryDetail from '../../features/history/HistoryDetail'
import AlertModal from '../../components/common/AlertModal'

interface AppLayoutViewProps {
    selectedHistoryId: string | null
    expireTime: number
    showImminentAlert: boolean
    remainingMinutes: number
    onNewAnalysis: () => void
    onHistoryClick: (id: string) => void
    onLogout: () => void
    onRefreshSession: () => void
    onCloseHistory: () => void
    onImminent: (minutes: number) => void
    onCloseImminentAlert: () => void
}

export default function AppLayoutView({
    selectedHistoryId,
    expireTime,
    showImminentAlert,
    remainingMinutes,
    onNewAnalysis,
    onHistoryClick,
    onLogout,
    onRefreshSession,
    onCloseHistory,
    onImminent,
    onCloseImminentAlert,
}: AppLayoutViewProps) {
    return (
        <div className="min-h-screen flex bg-background text-on-background overflow-hidden relative">
            <Sidebar
                onNewAnalysis={onNewAnalysis}
                onHistoryClick={onHistoryClick}
                onLogout={onLogout}
                onRefreshSession={onRefreshSession}
                expireTime={expireTime}
                onImminent={onImminent}
            />

            <main className="ml-[320px] min-h-screen flex flex-col flex-1">
                <Outlet />
            </main>

            <HistoryDetail 
                historyId={selectedHistoryId} 
                onClose={onCloseHistory} 
            />

            {showImminentAlert && (
                <AlertModal
                    title="로그인 유효시간 만료 임박"
                    description={`로그인 유효시간이 ${remainingMinutes}분 남았습니다<br>연장하시겠습니까?`}
                    type="confirm"
                    confirmText="연장하기"
                    cancelText="나중에 하기"
                    onConfirm={onRefreshSession}
                    onClose={onCloseImminentAlert}
                />
            )}
        </div>
    )
}