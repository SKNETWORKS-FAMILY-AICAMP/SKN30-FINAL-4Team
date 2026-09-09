import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import PasswordResetView from './PasswordResetView'
import { authService } from '../../services/authService'

export default function PasswordReset() {
    const navigate = useNavigate()
    const [email, setEmail] = useState('')

    const handleEmailSend = async () => {
        if (!email) {
            alert('이메일 주소를 입력해주세요.')
            return
        }

        try {
            await authService.requestPasswordReset(email)
            alert('비밀번호 재설정 링크가 발송되었습니다.')
            navigate('/login')
        } catch (error: any) {
            alert(error.message || '이메일 발송 중 오류가 발생했습니다.')
        }
    }

    return (
        <PasswordResetView
            email={email}
            onEmailChange={(e) => setEmail(e.target.value)}
            onSubmit={handleEmailSend}
        />
    )
}