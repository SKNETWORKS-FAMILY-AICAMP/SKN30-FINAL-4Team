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
                login_id: email,
                password,
            })

            // 성공 시 sessionStorage에 access_token 저장
            if (response && response.data && response.data.access_token) {
                sessionStorage.setItem('access_token', response.data.access_token)
                window.dispatchEvent(new Event('auth-change'))
                navigate('/')
            }
        } catch (error: any) {
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