import { useState, useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import ProtectedRoute from './ProtectedRoute'
import { supabase } from '../services/supabase'

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
    const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false)

    useEffect(() => {
        // 초기 세션 동기화
        supabase.auth.getSession().then(({ data: { session } }) => {
            setIsAuthenticated(!!session?.user)
        }).catch(() => {
            setIsAuthenticated(false)
        })

        // 인증 상태 변경 실시간 감지
        const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
            setIsAuthenticated(!!session?.user)
        })

        return () => {
            subscription.unsubscribe()
        }
    }, [])

    return (
        <Routes>
            {isAuthenticated ? (
                // --- 로그인 상태 ---
                <Route element={<ProtectedRoute />}>
                    <Route element={<AppLayout />}>
                        <Route path="/" element={<MainPage />} />
                        <Route path="/mypage" element={<MyPage />} />
                    </Route>
                </Route>
            ) : (
                // --- 미로그인 상태 (퍼블릭 레이아웃) ---
                <Route element={<PublicLayout />}>
                    <Route path="/" element={<LandingPage />} />
                    <Route path="/login" element={<LoginPage />} />
                    <Route path="/password-reset" element={<PasswordResetPage />} />
                    <Route path="/password-reset/update" element={<PasswordChangePage />} />
                </Route>
            )}

            {/* --- 잘못된 경로는 루트로 리다이렉트 --- */}
            <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
    )
}