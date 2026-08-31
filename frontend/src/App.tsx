import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'

// Layouts
import PublicLayout from './components/layout/PublicLayout'
import AppLayout from './components/layout/AppLayout'

// Pages
import LandingPage from './pages/LandingPage'
import LoginPage from './pages/LoginPage'
import PasswordResetPage from './pages/PasswordResetPage'

import MainPage from './pages/MainPage'
import PasswordChangePage from './pages/PasswordChangePage'

export default function App() {
    // TODO: 실제 인증 상태 연동 (현재는 로그인 상태 테스트를 위해 true/false 변경 가능)
    // const isAuthenticated = true
    const isAuthenticated = false

    return (
        <BrowserRouter>
            <Routes>
                {/* --- 루트('/') 경로 분기 --- */}
                {isAuthenticated ? (
                    // 로그인 상태: 루트('/')로 접속 시 메인 앱 레이아웃 및 업로드 화면(SCR-004) 노출
                    <Route element={<AppLayout />}>
                        <Route path="/" element={<MainPage />} />
                        <Route path="/mypage" element={<PasswordChangePage />} />
                    </Route>
                ) : (
                    // 미로그인 상태: 퍼블릭 레이아웃 (랜딩, 로그인, 비밀번호 재설정)
                    <Route element={<PublicLayout />}>
                        <Route path="/" element={<LandingPage />} /> {/* SCR-001 */}
                        <Route path="/login" element={<LoginPage />} /> {/* SCR-002 */}
                        <Route path="/password-reset" element={<PasswordResetPage />} /> {/* SCR-003 */}
                    </Route>
                )}

                {/* --- 미로그인 상태에서 보호된 경로 접근 시 로그인으로 리다이렉트 --- */}
                {!isAuthenticated && (
                    <Route path="/history/*" element={<Navigate to="/login" replace />} />
                )}

                {/* --- 잘못된 경로는 루트로 리다이렉트 --- */}
                <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
        </BrowserRouter>
    )
}