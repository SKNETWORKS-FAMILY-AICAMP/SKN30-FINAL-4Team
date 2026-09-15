import { createContext, useContext, useState, useEffect, type ReactNode } from 'react'
import { authService } from '../services/authService'
import { authUtils } from '../utils/authUtils'

interface AuthContextType {
    isAuthenticated: boolean | null
    displayName: string
    expireTime: number
    refreshSession: () => Promise<void>
    logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextType | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
    const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null)
    const [displayName, setDisplayName] = useState<string>('')
    const [expireTime, setExpireTime] = useState<number>(() => authUtils.getExpireTime())

    useEffect(() => {
        authService.getCurrentUser()
            .then((res) => {
                setIsAuthenticated(!!res?.user?.id)
                if (res?.user?.display_name) {
                    setDisplayName(res.user.display_name)
                }
                // 서버가 응답한 만료 시각 동기화
                authUtils.saveTokenExpiry(res)
                setExpireTime(authUtils.getExpireTime())
            })
            .catch(() => {
                setIsAuthenticated(false)
            })
    }, [])

    const refreshSession = async () => {
        const res = await authService.extendSession()
        authUtils.saveTokenExpiry(res)
        setExpireTime(authUtils.getExpireTime())
    }

    const logout = async () => {
        try {
            await authService.logout()
        } finally {
            authUtils.clearTokenExpiry()
            window.location.href = '/'
        }
    }

    return (
        <AuthContext.Provider value={{ isAuthenticated, displayName, expireTime, refreshSession, logout }}>
            {children}
        </AuthContext.Provider>
    )
}

export const useAuth = () => {
    const context = useContext(AuthContext)
    if (!context) throw new Error('useAuth must be used within an AuthProvider')
    return context
}