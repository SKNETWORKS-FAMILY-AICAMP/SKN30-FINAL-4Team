import { useEffect, useState } from 'react'
import { Navigate, Outlet } from 'react-router-dom'
import { supabase } from '../services/supabase'

export default function ProtectedRoute() {
    const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false)

    useEffect(() => {
        const verifySession = async () => {
            const { data: { session }, error } = await supabase.auth.getSession()
            
            if (error || !session) {
                setIsAuthenticated(false)
            } else {
                setIsAuthenticated(true)
            }
        }

        verifySession()
    }, [])

    if (!isAuthenticated) {
        return <Navigate to="/" replace />
    }

    return <Outlet />
}