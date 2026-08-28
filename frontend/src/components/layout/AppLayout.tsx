import Sidebar from './shared/Sidebar'
import { Outlet, useNavigate } from 'react-router-dom'

export default function AppLayout() {
    const navigate = useNavigate()

    // 설계서 라우팅 흐름에 따른 핸들러 예시
    const handleNewAnalysis = () => {
        navigate('/main') // SCR-004 업로드 화면
    }

    const handleHistoryClick = (id: string) => {
        navigate(`/main/history/${id}`) // SCR-007-P1 과거 분석 이력 상세
    }

    const handlePasswordChangeClick = () => {
        navigate('/main/password') // SCR-006 비밀번호 변경
    }

    const handleLogout = () => {
        // 로그아웃 후 랜딩(SCR-001)으로 이동
        navigate('/')
    }

    return (
        <div className="min-h-screen bg-background text-on-background flex">
            {/* 좌측 사이드바 */}
            <Sidebar
                onNewAnalysis={handleNewAnalysis}
                onHistoryClick={handleHistoryClick}
                onPasswordChangeClick={handlePasswordChangeClick}
                onLogout={handleLogout}
            />

            {/* 우측 본문 컨테이너 영역 (사이드바 너비 280px 만큼 여백 부여) */}
            <main className="flex-grow ml-[320px] min-h-screen flex flex-col bg-surface">
                <Outlet />
            </main>
        </div>
    )
}