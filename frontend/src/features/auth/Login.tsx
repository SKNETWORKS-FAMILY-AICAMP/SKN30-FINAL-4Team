import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authService } from '../../services/authService'
import LoginView from './LoginView'
import AlertModal from '../../components/common/AlertModal'

export default function Login() {
    const navigate = useNavigate()
    const [email, setEmail] = useState('')
    const [password, setPassword] = useState('')
    const [alertMessage, setAlertMessage] = useState<string | null>(null)

    const handleLogin = async () => {
        if (!email || !password) {
            setAlertMessage('이메일과 비밀번호를 모두 입력해주세요.')
            return
        }

        try {
            const response = await authService.login({
                email: email,
                password,
            })

            // 성공 시 sessionStorage에 access_token 및 expire_time 저장
            if (response && response.access_token) {
                sessionStorage.setItem('access_token', response.access_token)
                
                // 1시간(60분 * 60초 * 1000ms) 뒤의 만료 시간 저장
                const expireTime = Date.now() + 60 * 60 * 1000
                sessionStorage.setItem('expire_time', String(expireTime))

                window.dispatchEvent(new Event('auth-change'))
                navigate('/')
            }
        } catch (error: any) {
            // 🔍 어떤 에러 때문에 catch로 빠지는지 상세히 출력
            console.error('로그인 try 내부 에러 상세:', error)

            const errorData = error.response?.data
            if (errorData && errorData.message) {
                setAlertMessage(errorData.message)
            } else {
                setAlertMessage('로그인 중 오류가 발생했습니다.')
            }
        }
    }

    return (
        <>
            <LoginView
                email={email}
                password={password}
                onEmailChange={(e) => setEmail(e.target.value)}
                onPasswordChange={(e) => setPassword(e.target.value)}
                onSubmit={handleLogin}
            />

            {/* 공통 얼럿 컴포넌트 적용 */}
            {alertMessage && (
                <AlertModal
                    title="알림"
                    description={alertMessage}
                    type="alert"
                    onConfirm={() => setAlertMessage(null)}
                />
            )}
        </>
    )
}