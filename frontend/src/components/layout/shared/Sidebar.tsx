import Logo from '../../../components/common/Logo'
import HistoryList from '../../../features/history/HistoryList'

interface SidebarProps {
    onNewAnalysis: () => void
    onHistoryClick: (id: string) => void
    onPasswordChangeClick: () => void
    onLogout: () => void
}

export default function Sidebar({
    onNewAnalysis,
    onHistoryClick,
    onPasswordChangeClick,
    onLogout,
}: SidebarProps) {
    return (
        <nav className="fixed left-0 top-0 h-full w-[320px] border-r border-outline-variant flex flex-col py-xl px-lg gap-lg z-50">
            {/* 1. 상단 로고 */}
            <div className="mb-md px-md">
                <Logo size='lg' />
            </div>

            {/* 2. 새 분석 버튼 */}
            <button
                onClick={onNewAnalysis}
                className="w-full bg-primary-container text-on-primary font-title-sm text-title-sm py-md rounded-lg flex items-center justify-center gap-sm hover:opacity-90 transition-opacity mb-md shadow-sm"
            >
                <span className="material-symbols-outlined">add</span>
                새 분석
            </button>

            {/* 3. 최근 분석 이력 컨테이너 */}
            <HistoryList onHistoryClick={onHistoryClick} />

            {/* 4. 하단 사용자 프로필 및 로그아웃 영역 */}
            <div className="mt-auto border-t border-outline-variant pt-md">
                <div className="flex items-center gap-md px-xs py-sm">
                    <div className="w-10 h-10 rounded-full bg-surface-container-high overflow-hidden border border-outline-variant shrink-0">
                        <img
                            alt="Analyst Profile"
                            className="w-full h-full object-cover"
                            src="https://lh3.googleusercontent.com/aida-public/AB6AXuBfflctLCBH9MzYdw4r2HgL508lb_Ure-RRnv9cGBwj_IoY0dSpucbeU4tYUJtFKeT74XY2fLXgKFUSC7Tzfn_p4vlVFVcgVMuRnXQyevwqQtkH6JsjL-V-SaWoiuZ9mYA9R46CydKhycFC9V5b6x93foQWiH-jHXSUnSOz45SW6WzUjDhMkuH0_AQK398W0fo7Pi8ywe20TMj-EFkJZA5isv5IPv1xOBgTuGHa1QSEufOBIVOEv44e0A"
                        />
                    </div>
                    <div className="flex flex-col flex-1 min-w-0">
                        <button
                            onClick={onPasswordChangeClick}
                            className="text-left font-title-sm text-title-sm text-on-surface truncate hover:text-primary transition-colors"
                        >
                            분석가
                        </button>
                        <button
                            onClick={onLogout}
                            className="font-body-sm text-body-sm text-on-surface-variant hover:text-error transition-colors flex items-center gap-xs text-left"
                        >
                            <span
                                className="material-symbols-outlined text-sm"
                                style={{ fontSize: '14px' }}
                            >
                                logout
                            </span> 로그아웃
                        </button>
                    </div>
                </div>
            </div>
        </nav>
    )
}