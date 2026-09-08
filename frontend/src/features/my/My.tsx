import { useState } from 'react'
import My from './MyView'
import { authService } from '../../services/authService'

export default function PasswordChange() {
    const [currentPassword, setCurrentPassword] = useState('')
    const [newPassword, setNewPassword] = useState('')
    const [confirmPassword, setConfirmPassword] = useState('')

    const handleSubmit = async () => {
        if (!currentPassword.trim()) {
            alert('현재 비밀번호를 입력해 주세요.')
            return
        }

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
            alert('비밀번호가 성공적으로 변경되었습니다.')
            setCurrentPassword('')
            setNewPassword('')
            setConfirmPassword('')
        } catch (error: any) {
            alert(error.message || '비밀번호 변경 중 오류가 발생했습니다.')
        }
    }

    return (
        <My
            currentPassword={currentPassword}
            newPassword={newPassword}
            confirmPassword={confirmPassword}
            onCurrentPasswordChange={(e) => setCurrentPassword(e.target.value)}
            onNewPasswordChange={(e) => setNewPassword(e.target.value)}
            onConfirmPasswordChange={(e) => setConfirmPassword(e.target.value)}
            onSubmit={handleSubmit}
        />
    )
}