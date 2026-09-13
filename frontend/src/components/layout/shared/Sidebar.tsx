import Logo from '../../../components/common/Logo'
import HistoryList from '../../../features/history/HistoryList'
import UserSection from './UserSection'

interface SidebarProps {
    onNewAnalysis: () => void
    onHistoryClick: (id: string) => void
    onLogout: () => void
    onRefreshSession: () => void
    expireTime: number
    onImminent: (minutes: number) => void
}

export default function Sidebar({
    onNewAnalysis,
    onHistoryClick,
    onLogout,
    onRefreshSession,
    expireTime,
    onImminent
}: SidebarProps) {
    return (
        <nav className="flex flex-col gap-lg fixed left-0 top-0 h-full w-[320px] py-xl px-lg bg-surface border-r border-outline-variant z-50">
            <div className="mb-md text-center">
                <Logo size='lg' />
            </div>

            <button
                type="button"
                onClick={onNewAnalysis}
                className="w-full bg-primary-container text-on-primary font-title-sm text-title-sm py-md pr-2 rounded-lg flex items-center justify-center gap-sm hover:opacity-90 transition-opacity mb-md cursor-pointer"
            >
                <span className="material-symbols-outlined">add</span>
                새 분석
            </button>

            <HistoryList onHistoryClick={onHistoryClick} />

            <UserSection
                onLogout={onLogout}
                onRefreshSession={onRefreshSession}
                expireTime={expireTime}
                onImminent={onImminent}
            />
        </nav>
    )
}