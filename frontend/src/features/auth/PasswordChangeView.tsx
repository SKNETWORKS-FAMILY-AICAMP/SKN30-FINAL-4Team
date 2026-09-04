import MainPageTemplate from '../../components/common/MainPageTemplate'
import InputField from '../../components/common/InputField'

interface PasswordChangeViewProps {
    layoutType?: 'main' | 'public' // 'main' (SCR-006) vs 'public' (SCR-003-P1)
    currentPassword: string
    newPassword: string
    confirmPassword: string
    onCurrentPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onNewPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onConfirmPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onSubmit: () => void
}

export default function PasswordChangeView({
    layoutType = 'main',
    currentPassword,
    newPassword,
    confirmPassword,
    onCurrentPasswordChange,
    onNewPasswordChange,
    onConfirmPasswordChange,
    onSubmit,
}: PasswordChangeViewProps) {
    const isPublic = layoutType === 'public'

    // 공통 폼 콘텐츠 (CSS 및 조건부 노출은 props로 깔끔하게 제어)
    const content = (
        <div className="flex flex-col gap-xl">
            {/* 1. 현재 비밀번호 (SCR-006 메인에서만 노출) */}
            {!isPublic && (
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
            )}

            {/* 2. 새 비밀번호 */}
            <InputField
                label="새 비밀번호"
                id="new-password"
                type="password"
                iconName="lock"
                placeholder={isPublic ? "영문, 숫자, 특수문자 조합 8자 이상" : "새 비밀번호 입력"}
                value={newPassword}
                onChange={onNewPasswordChange}
                layout={isPublic ? 'vertical' : 'horizontal'}
            >
                {/* 메인 화면용 보안 가이드 (퍼블릭엔 상단 안내문구가 있으므로 생략 혹은 필요시 조건부 처리) */}
                {!isPublic && (
                    <div className="flex items-start gap-sm pt-sm">
                        <span className="material-symbols-outlined text-on-surface-variant text-[20px]">info</span>
                        <div className="flex flex-col gap-1">
                            <span className="font-semibold text-body-sm text-on-surface-variant">보안 가이드</span>
                            <span className="text-body-sm text-on-surface-variant">영문, 숫자, 특수문자 조합 8자리 이상으로 설정해 주세요.</span>
                        </div>
                    </div>
                )}
            </InputField>

            {/* 3. 비밀번호 확인 */}
            <InputField
                label="비밀번호 확인"
                id="confirm-password"
                type="password"
                iconName="lock_reset"
                placeholder={isPublic ? "비밀번호를 한번 더 입력해 주세요" : "새 비밀번호 재입력"}
                value={confirmPassword}
                onChange={onConfirmPasswordChange}
                layout={isPublic ? 'vertical' : 'horizontal'}
            />

            {/* 제출 버튼 영역 */}
            {isPublic ? (
                <button
                    type="button"
                    onClick={onSubmit}
                    className="w-full bg-primary-container text-on-primary font-title-sm text-[16px] py-3 rounded-lg hover:bg-primary-container/90 transition-colors cursor-pointer mt-md"
                >
                    변경 완료
                </button>
            ) : (
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
            )}
        </div>
    )

    // 퍼블릭 화면 (SCR-003-P1) 렌더링 구조
    if (isPublic) {
        return (
            <main className="flex-1 flex flex-col items-center justify-center w-full max-w-xl py-xl">
                <div className="w-full bg-surface-container-lowest rounded-xl border border-outline-variant p-xl shadow-sm flex flex-col gap-xl">
                    <div className="text-center">
                        <h1 className="font-display-lg text-display-lg text-on-surface mb-sm">비밀번호 변경</h1>
                        <p className="font-body-md text-body-md text-on-surface-variant">
                            새로운 비밀번호를 입력해 주세요.<br />보안을 위해 영문, 숫자, 특수문자를 조합하여 8자 이상으로 설정해 주세요.
                        </p>
                    </div>
                    {content}
                </div>
            </main>
        )
    }

    // 메인 화면 (SCR-006) 렌더링 구조
    return (
        <MainPageTemplate
            title="비밀번호 변경"
            subtitle="안전한 서비스 이용을 위해 비밀번호를 변경해 주세요"
        >
            {content}
        </MainPageTemplate>
    )
}