import type { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../providers/AuthProvider'

interface ProtectedRouteProps {
    children: ReactNode
}

export function ProtectedRoute({ children }: ProtectedRouteProps) {
    const { isAuthenticated } = useAuth()

    // 세션 확인 중일 때
    if (isAuthenticated === null) {
        return null
    }

    // 미인증 시 로그인으로 리다이렉트
    if (!isAuthenticated) {
        return <Navigate to="/" replace />
    }

    return <>{children}</>
}