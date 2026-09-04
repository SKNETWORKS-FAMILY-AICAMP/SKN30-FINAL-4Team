import { useEffect, useState } from 'react'
import { Navigate, Outlet } from 'react-router-dom'
import { authService } from '../services/authService'

export default function ProtectedRoute() {
    const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null)

    useEffect(() => {
        const verifyToken = async () => {
            const token = sessionStorage.getItem('access_token')
            if (!token) {
                setIsAuthenticated(false)
                return
            }

            try {
                await authService.getMe()
                setIsAuthenticated(true)
            } catch (error) {
                sessionStorage.removeItem('access_token')
                setIsAuthenticated(false)
            }
        }

        verifyToken()
    }, [])

    if (isAuthenticated === null) {
        return <div className="flex items-center justify-center min-h-screen">인증 확인 중...</div>
    }

    if (!isAuthenticated) {
        return <Navigate to="/login" replace />
    }

    return <Outlet />
}