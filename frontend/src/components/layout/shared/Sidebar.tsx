import { Link } from 'react-router-dom'

import Logo from '../../../components/common/Logo'
import HistoryList from '../../../features/history/HistoryList'

interface SidebarProps {
    onNewAnalysis: () => void
    onHistoryClick: (id: string) => void
    onLogout: () => void
}

export default function Sidebar({
    onNewAnalysis,
    onHistoryClick,
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
                className="w-full pr-2 bg-primary-container text-on-primary font-title-sm text-title-sm py-md rounded-lg flex items-center justify-center gap-sm hover:opacity-90 transition-opacity mb-md shadow-sm"
            >
                <span className="material-symbols-outlined">add</span>
                새 분석
            </button>

            {/* 3. 최근 분석 이력 컨테이너 */}
            <HistoryList onHistoryClick={onHistoryClick} />

            {/* 4. 하단 사용자 프로필 및 로그아웃 영역 */}
            <div className="flex items-center gap-md px-xs py-sm mt-auto border-t border-outline-variant pt-md">
                <div className="w-8 h-8 rounded-full">
                    <span className="material-symbols-outlined text-on-surface-variant" style={{ fontSize: '32px' }}>badge</span>
                </div>

                <div className="">
                    <Link to="/mypage"
                        className="font-title-sm text-title-sm text-on-surface text-[15px] font-semibold"
                    >
                        분석가
                    </Link>

                    <button type='button' onClick={onLogout}
                        className="font-body-sm text-body-sm text-on-surface-variant hover:text-error transition-colors flex items-center gap-xs text-left cursor-pointer"
                    >
                        <span className="material-symbols-outlined" style={{ fontSize: '14px' }}>logout</span> 로그아웃
                    </button>
                </div>
            </div>
        </nav>
    )
}