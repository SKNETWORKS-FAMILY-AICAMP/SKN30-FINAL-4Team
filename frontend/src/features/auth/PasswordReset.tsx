import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import PasswordResetView from './PasswordResetView'

export default function PasswordReset() {
    const navigate = useNavigate()
    const [email, setEmail] = useState('')

    const handleEmailSend = () => {
        if (!email) {
            alert('이메일 주소를 입력해주세요.')
            return
        }

        // TODO: 이메일 발송 API 연동
        console.log('비밀번호 재설정 링크 발송 요청:', email)

        // 성공 시뮬레이션
        alert('비밀번호 재설정 링크가 발송되었습니다.')
        navigate('/login')
    }

    return (
        <PasswordResetView
            email={email}
            onEmailChange={(e) => setEmail(e.target.value)}
            onSubmit={handleEmailSend}
        />
    )
}