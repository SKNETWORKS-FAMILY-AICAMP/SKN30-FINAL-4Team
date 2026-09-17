import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import PasswordChangeView from './PasswordChangeView'
import { authService } from '../../services/authService'

export default function PasswordResetUpdate() {
    const navigate = useNavigate()
    const [newPassword, setNewPassword] = useState('')
    const [confirmPassword, setConfirmPassword] = useState('')
    const [recoveryVerified, setRecoveryVerified] = useState(false)
    const exchangeStarted = useRef(false)

    useEffect(() => {
        if (exchangeStarted.current) return
        exchangeStarted.current = true

        const fragment = new URLSearchParams(window.location.hash.slice(1))
        const tokenHash = fragment.get('token_hash')
        // Remove the one-time proof before any later navigation, screenshot,
        // extension, or error report can retain it in the visible URL.
        window.history.replaceState(
            null,
            '',
            `${window.location.pathname}${window.location.search}`,
        )
        if (!tokenHash) {
            alert('비밀번호 재설정 링크가 유효하지 않습니다.')
            navigate('/password-reset', { replace: true })
            return
        }

        authService.verifyPasswordRecovery(tokenHash)
            .then(() => setRecoveryVerified(true))
            .catch((error: unknown) => {
                const message = error instanceof Error
                    ? error.message
                    : '비밀번호 재설정 링크가 유효하지 않습니다.'
                alert(message)
                navigate('/password-reset', { replace: true })
            })
    }, [navigate])

    const handleSubmit = async () => {
        if (!recoveryVerified) {
            alert('비밀번호 재설정 링크를 확인하는 중입니다.')
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
            alert('비밀번호가 성공적으로 변경되었습니다. 로그인 페이지로 이동합니다.')
            navigate('/login')
        } catch (error: unknown) {
            const message = error instanceof Error
                ? error.message
                : '비밀번호 재설정 중 오류가 발생했습니다.'
            alert(message)
        }
    }

    if (!recoveryVerified) return null

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
