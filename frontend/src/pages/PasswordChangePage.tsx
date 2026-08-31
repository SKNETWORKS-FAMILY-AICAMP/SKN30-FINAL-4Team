import { useLocation, useNavigate } from 'react-router-dom'
import PasswordChange from '../features/auth/PasswordChange'

export default function PasswordChangePage() {
    const location = useLocation()
    const navigate = useNavigate()

    // 예: 주소 경로가 '/reset-password' 이거나 퍼블릭 진입 경로일 때 'public'으로 판단
    const isPublic = location.pathname.includes('reset') 
        ? 'public' 
        : 'main'

    const handleSuccess = () => {
        if (isPublic === 'public') {
            navigate('/login') // 퍼블릭 재설정 완료 시 로그인 화면으로 이동
        }
    }

    return (
        <PasswordChange 
            layoutType={isPublic} 
            onSuccess={handleSuccess} 
        />
    )
}