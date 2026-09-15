import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuth } from '../providers/AuthProvider'

// Layouts
import PublicLayout from '../components/layout/PublicLayout'
import AppLayout from '../components/layout/AppLayout'

// Pages
import LandingPage from '../pages/LandingPage'
import LoginPage from '../pages/LoginPage'
import PasswordResetPage from '../pages/PasswordResetPage'
import PasswordChangePage from '../pages/PasswordChangePage'
import MainPage from '../pages/MainPage'
import MyPage from '../pages/MyPage'

export default function AppRoutes() {
    const { displayName, isAuthenticated } = useAuth()

    // 세션 확인 중일 때 로딩 처리
    if (isAuthenticated === null) {
        return null
    }

    return (
        <Routes>
            {isAuthenticated ? (
                // --- 로그인 상태 ---
                <Route element={<AppLayout displayName={displayName} />}>
                    <Route path="/" element={<MainPage />} />
                    <Route path="/mypage" element={<MyPage />} />
                    <Route path="*" element={<Navigate to="/" replace />} />
                </Route>
            ) : (
                // --- 미로그인 상태 ---
                <Route element={<PublicLayout />}>
                    <Route path="/" element={<LandingPage />} />
                    <Route path="/login" element={<LoginPage />} />
                    <Route path="/password-reset" element={<PasswordResetPage />} />
                    <Route path="/password-reset/update" element={<PasswordChangePage />} />
                    <Route path="*" element={<Navigate to="/" replace />} />
                </Route>
            )}
        </Routes>
    )
}