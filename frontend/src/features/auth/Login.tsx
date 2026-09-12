import { useState } from 'react'
import { authService } from '../../services/authService'
import LoginView from './LoginView'
import AlertModal from '../../components/common/AlertModal'

export default function Login() {
    const [email, setEmail] = useState('')
    const [password, setPassword] = useState('')
    const [alertMessage, setAlertMessage] = useState<string | null>(null)

    const handleLogin = async () => {
        if (!email || !password) {
            setAlertMessage('이메일과 비밀번호를 모두 입력해주세요')
            return
        }

        try {
            await authService.login({ email, password })
            window.location.href = '/'
        } catch (error: any) {
            console.error('로그인 에러:', error)
            setAlertMessage(error.message)
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