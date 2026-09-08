import MainPageTemplate from '../../components/common/MainPageTemplate'
import InputField from '../../components/common/InputField'

interface PasswordChangeViewProps {
    currentPassword: string
    newPassword: string
    confirmPassword: string
    onCurrentPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onNewPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onConfirmPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onSubmit: () => void
}

export default function PasswordChangeView({
    currentPassword,
    newPassword,
    confirmPassword,
    onCurrentPasswordChange,
    onNewPasswordChange,
    onConfirmPasswordChange,
    onSubmit,
}: PasswordChangeViewProps) {
    return (
        <MainPageTemplate
            title="비밀번호 변경"
            subtitle="안전한 서비스 이용을 위해 비밀번호를 변경해 주세요"
        >
            <div className="flex flex-col gap-xl">
                {/* 1. 현재 비밀번호 */}
                <InputField
                    label="현재 비밀번호"
                    id="current-password"
                    className="pb-xl border-b border-outline-variant"
                    type="password"
                    iconName="lock"
                    placeholder="현재 비밀번호 입력"
                    value={currentPassword}
                    onChange={onCurrentPasswordChange}
                    layout="horizontal"
                />

                {/* 2. 새 비밀번호 */}
                <InputField
                    label="새 비밀번호"
                    id="new-password"
                    type="password"
                    iconName="lock"
                    placeholder="새 비밀번호 입력"
                    value={newPassword}
                    onChange={onNewPasswordChange}
                    layout="horizontal"
                >
                    <div className="flex items-start gap-sm pt-sm">
                        <span className="material-symbols-outlined text-on-surface-variant text-[20px]">info</span>
                        <div className="flex flex-col gap-1">
                            <span className="font-semibold text-body-sm text-on-surface-variant">보안 가이드</span>
                            <span className="text-body-sm text-on-surface-variant">영문, 숫자, 특수문자 조합 8자리 이상으로 설정해 주세요.</span>
                        </div>
                    </div>
                </InputField>

                {/* 3. 비밀번호 확인 */}
                <InputField
                    label="비밀번호 확인"
                    id="confirm-password"
                    type="password"
                    iconName="lock_reset"
                    placeholder="새 비밀번호 재입력"
                    value={confirmPassword}
                    onChange={onConfirmPasswordChange}
                    layout="horizontal"
                />

                {/* 제출 버튼 영역 */}
                <div className="grid grid-cols-1 md:grid-cols-3 gap-md pt-xl border-t border-outline-variant">
                    <div className="hidden md:block"></div>
                    <div className="md:col-span-2">
                        <button
                            type="button"
                            onClick={onSubmit}
                            className="w-full md:w-auto px-xl bg-primary-container text-on-primary font-title-sm text-[16px] py-3 rounded-lg hover:bg-primary-container/90 transition-colors cursor-pointer"
                        >
                            비밀번호 변경
                        </button>
                    </div>
                </div>
            </div>
        </MainPageTemplate>
    )
}