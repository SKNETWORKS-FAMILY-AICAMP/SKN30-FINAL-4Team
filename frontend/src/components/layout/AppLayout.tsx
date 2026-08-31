import { useState } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import Sidebar from './shared/Sidebar'
import HistoryDetailLayer from '../../features/history/HistoryDetailLayer'

export default function AppLayout() {
    const navigate = useNavigate()
    const [selectedHistoryId, setSelectedHistoryId] = useState<string | null>(null)

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

    const handleLogout = () => {
        navigate('/')
    }

    return (
        <div className="min-h-screen flex bg-background text-on-background overflow-hidden relative">
            <Sidebar
                onNewAnalysis={handleNewAnalysis}
                onHistoryClick={handleHistoryClick}
                onLogout={handleLogout}
            />

            <main className="ml-[320px] min-h-screen flex flex-col flex-1">
                <Outlet />
            </main>

            {/* 💡 껍데기(컨테이너) 역할만 하는 분리된 레이어 팝업 컴포넌트 호출 */}
            <HistoryDetailLayer 
                historyId={selectedHistoryId} 
                onClose={handleCloseHistory} 
            />
        </div>
    )
}