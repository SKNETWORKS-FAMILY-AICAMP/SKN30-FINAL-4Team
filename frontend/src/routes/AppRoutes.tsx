import { useState, useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import ProtectedRoute from './ProtectedRoute'

// Layouts
import PublicLayout from '../components/layout/PublicLayout'
import AppLayout from '../components/layout/AppLayout'

// Pages
import LandingPage from '../pages/LandingPage'
import LoginPage from '../pages/LoginPage'
import PasswordResetPage from '../pages/PasswordResetPage'
import MainPage from '../pages/MainPage'
import PasswordChangePage from '../pages/PasswordChangePage'

export default function AppRoutes() {
    // sessionStorage의 토큰을 리액트 state로 관리
    const [token, setToken] = useState<string | null>(() => sessionStorage.getItem('access_token'))

    useEffect(() => {
        // storage 이벤트나 커스텀벤트를 통해 토큰 변경 감지
        const handleStorageChange = () => {
            setToken(sessionStorage.getItem('access_token'))
        }

        window.addEventListener('storage', handleStorageChange)
        // 로그인 성공 시 강제로 발생시킬 커스텀 이벤트 대비
        window.addEventListener('auth-change', handleStorageChange)

        return () => {
            window.removeEventListener('storage', handleStorageChange)
            window.removeEventListener('auth-change', handleStorageChange)
        }
    }, [])

    return (
        <Routes>
            {token ? (
                // --- 로그인 상태 ---
                // 루트('/') 자체가 업로드 화면(MainPage)이며 AppLayout을 사용
                <Route element={<ProtectedRoute />}>
                    <Route element={<AppLayout />}>
                        <Route path="/" element={<MainPage />} />
                        <Route path="/mypage" element={<PasswordChangePage />} />
                    </Route>
                </Route>
            ) : (
                // --- 미로그인 상태 ---
                // 루트('/')가 랜딩 페이지이며 PublicLayout을 사용
                <Route element={<PublicLayout />}>
                    <Route path="/" element={<LandingPage />} />
                    <Route path="/login" element={<LoginPage />} />
                    <Route path="/password-reset" element={<PasswordResetPage />} />
                </Route>
            )}

            {/* --- 잘못된 경로는 루트로 리다이렉트 --- */}
            <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
    )
}