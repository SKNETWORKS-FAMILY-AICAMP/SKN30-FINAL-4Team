import { useState } from 'react'
import PasswordChangeView from './PasswordChangeView'

interface PasswordChangeProps {
    layoutType?: 'main' | 'public' // 'main' (SCR-006) vs 'public' (SCR-003-P1)
    onSuccess?: () => void         // 변경 성공 시 처리할 라우팅 콜백 함수 등
}

export default function PasswordChange({
    layoutType = 'main',
    onSuccess,
}: PasswordChangeProps) {
    const [currentPassword, setCurrentPassword] = useState('')
    const [newPassword, setNewPassword] = useState('')
    const [confirmPassword, setConfirmPassword] = useState('')

    const handleSubmit = () => {
        // 1. 간단한 프론트엔드 유효성 검증
        if (layoutType === 'main' && !currentPassword.trim()) {
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

        // 2. API 호출 로직 (상황에 맞게 연동)
        // 예시 비즈니스 분기 처리
        if (layoutType === 'public') {
            alert('비밀번호가 성공적으로 변경되었습니다. 로그인 페이지로 이동합니다.')
            onSuccess?.() // 예: router.push('/login')
        } else {
            alert('비밀번호가 성공적으로 변경되었습니다.')
            // 초기화 또는 현재 페이지 유지
            setCurrentPassword('')
            setNewPassword('')
            setConfirmPassword('')
        }
    }

    return (
        <PasswordChangeView
            layoutType={layoutType}
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