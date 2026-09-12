import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import PasswordChangeView from './PasswordChangeView'
import { authService } from '../../services/authService'

export default function PasswordResetUpdate() {
    const navigate = useNavigate()
    const [newPassword, setNewPassword] = useState('')
    const [confirmPassword, setConfirmPassword] = useState('')

    const handleSubmit = async () => {
        if (!newPassword.trim()) {
            alert('새 비밀번호를 입력해 주세요.')
            return
        }

        if (newPassword !== confirmPassword) {
            alert('새 비밀번호와 비밀번호 확인 값이 일치하지 않습니다.')
            return
        }

        try {
            await authService.updatePassword(newPassword)
            alert('비밀번호가 성공적으로 변경되었습니다. 로그인 페이지로 이동합니다.')
            navigate('/login')
        } catch (error: any) {
            alert(error.message || '비밀번호 재설정 중 오류가 발생했습니다.')
        }
    }

    return (
        <PasswordChangeView
            newPassword={newPassword}
            confirmPassword={confirmPassword}
            onNewPasswordChange={(e) => setNewPassword(e.target.value)}
            onConfirmPasswordChange={(e) => setConfirmPassword(e.target.value)}
            onSubmit={handleSubmit}
        />
    )
}