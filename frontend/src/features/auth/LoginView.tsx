import PublicFormPageTemplate from '../../components/common/PublicFormPageTemplate'
import InputField from '../../components/common/InputField'

interface LoginViewProps {
    email: string
    password: string
    onEmailChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onPasswordChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onSubmit: () => void
}

export default function LoginView({
    email,
    password,
    onEmailChange,
    onPasswordChange,
    onSubmit,
}: LoginViewProps) {
    return (
        <PublicFormPageTemplate
            title="로그인"
            description="서비스 이용을 위해 로그인해 주세요"
            actionButtonText="로그인"
            onActionClick={onSubmit}
            footerLink={{
                text: '비밀번호 재설정',
                to: '/password-reset',
            }}
        >
            <div className="flex flex-col gap-lg">
                <InputField
                    label="이메일 주소"
                    iconName="mail"
                    id="email"
                    type="email"
                    placeholder="이메일을 입력하세요"
                    value={email}
                    onChange={onEmailChange}
                    layout="vertical"
                />

                <InputField
                    label="비밀번호"
                    iconName="lock"
                    id="password"
                    type="password"
                    placeholder="비밀번호를 입력하세요"
                    value={password}
                    onChange={onPasswordChange}
                    layout="vertical"
                />
            </div>
        </PublicFormPageTemplate>
    )
}