import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import LoginView from './LoginView'

export default function Login() {
    const navigate = useNavigate()
    const [email, setEmail] = useState('')
    const [password, setPassword] = useState('')

    const handleLogin = () => {
        if (!email || !password) {
            alert('이메일과 비밀번호를 모두 입력해주세요.')
            return
        }

        // TODO: 실제 로그인 인증 로직 및 에러 처리
        console.log('로그인 시도:', email)
        
        // 성공 시 Pre-review 메인(SCR-004 또는 대시보드)으로 이동
        navigate('/')
    }

    return (
        <LoginView
            email={email}
            password={password}
            onEmailChange={(e) => setEmail(e.target.value)}
            onPasswordChange={(e) => setPassword(e.target.value)}
            onSubmit={handleLogin}
        />
    )
}