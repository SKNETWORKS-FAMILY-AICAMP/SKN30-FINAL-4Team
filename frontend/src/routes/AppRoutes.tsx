import { useState, useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import ProtectedRoute from './ProtectedRoute'
import { authService } from '../services/authService'

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
    const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null)

    useEffect(() => {
        authService.getCurrentUser()
            .then((res) => {
                setIsAuthenticated(!!res?.user)
            })
            .catch(() => {
                setIsAuthenticated(false)
            })
    }, [])

    if (isAuthenticated === null) {
        return null
    }

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
                // --- 미로그인 상태 ---
                <Route element={<PublicLayout />}>
                    <Route path="/" element={<LandingPage />} />
                    <Route path="/login" element={<LoginPage />} />
                    <Route path="/password-reset" element={<PasswordResetPage />} />
                    <Route path="/password-reset/update" element={<PasswordChangePage />} />
                </Route>
            )}

            <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
    )
}