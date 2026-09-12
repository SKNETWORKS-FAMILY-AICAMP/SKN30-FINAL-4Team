import InputField from '../../components/common/InputField'

interface PasswordResetUpdateViewProps {
    newPassword: string
    confirmPassword: string
    onNewPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onConfirmPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onSubmit: () => void
}

export default function PasswordResetUpdateView({
    newPassword,
    confirmPassword,
    onNewPasswordChange,
    onConfirmPasswordChange,
    onSubmit,
}: PasswordResetUpdateViewProps) {
    return (
        <main className="flex-1 flex flex-col items-center justify-center w-full max-w-xl py-xl mx-auto px-6">
            <div className="w-full bg-surface-container-lowest rounded-xl border border-outline-variant p-xl shadow-sm flex flex-col gap-xl">
                <div className="text-center">
                    <h1 className="font-display-lg text-display-lg text-on-surface mb-sm">새 비밀번호 설정</h1>
                    <p className="font-body-md text-body-md text-on-surface-variant">
                        새로운 비밀번호를 입력해 주세요.<br />보안을 위해 영문, 숫자, 특수문자를 조합하여 8자 이상으로 설정해 주세요.
                    </p>
                </div>

                <div className="flex flex-col gap-xl">
                    <InputField
                        label="새 비밀번호"
                        id="new-password"
                        type="password"
                        iconName="lock"
                        placeholder="영문, 숫자, 특수문자 조합 8자 이상"
                        value={newPassword}
                        onChange={onNewPasswordChange}
                        layout="vertical"
                    />

                    <InputField
                        label="비밀번호 확인"
                        id="confirm-password"
                        type="password"
                        iconName="lock_reset"
                        placeholder="비밀번호를 한번 더 입력해 주세요"
                        value={confirmPassword}
                        onChange={onConfirmPasswordChange}
                        layout="vertical"
                    />

                    <button
                        type="button"
                        onClick={onSubmit}
                        className="w-full bg-primary-container text-on-primary font-title-sm text-[16px] py-3 rounded-lg hover:bg-primary-container/90 transition-colors cursor-pointer mt-md"
                    >
                        변경 완료
                    </button>
                </div>
            </div>
        </main>
    )
}