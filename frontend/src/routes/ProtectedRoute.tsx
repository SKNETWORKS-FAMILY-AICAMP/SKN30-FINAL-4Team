import { useEffect, useState } from 'react'
import { Navigate, Outlet } from 'react-router-dom'
import { authService } from '../services/authService'

export default function ProtectedRoute() {
    const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null)

    useEffect(() => {
        const verifySession = async () => {
            try {
                const res = await authService.getCurrentUser()
                if (res?.user) {
                    setIsAuthenticated(true)
                } else {
                    setIsAuthenticated(false)
                }
            } catch {
                setIsAuthenticated(false)
            }
        }

        verifySession()
    }, [])

    if (isAuthenticated === null) {
        return null // 검증 대기 중
    }

    if (!isAuthenticated) {
        return <Navigate to="/" replace />
    }

    return <Outlet />
}