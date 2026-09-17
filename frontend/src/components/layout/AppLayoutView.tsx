import { Outlet } from 'react-router-dom'
import Sidebar from './shared/Sidebar'
import HistoryDetail from '../../features/history/HistoryDetail'
import AlertModal from '../../components/common/AlertModal'

interface AppLayoutViewProps {
    selectedHistoryId: string | null
    displayName: string
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
    setSessionId: (id: string | null) => void
}

export default function AppLayoutView({
    selectedHistoryId,
    displayName,
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
    setSessionId,
}: AppLayoutViewProps) {
    return (
        <div className="min-h-screen flex bg-background text-on-background overflow-hidden relative">
            <Sidebar
                displayName={displayName}
                expireTime={expireTime}
                onNewAnalysis={onNewAnalysis}
                onHistoryClick={onHistoryClick}
                onLogout={onLogout}
                onRefreshSession={onRefreshSession}
                onImminent={onImminent}
            />

            <main className="ml-[320px] min-h-screen flex flex-col flex-1">
                {/* Outlet을 통해 자식(MainPage)에게 세션 ID 설정 함수 전달 */}
                <Outlet context={{ setSessionId }} />
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