import PublicFormPageTemplate from '../../components/common/PublicFormPageTemplate'
import InputField from '../../components/common/InputField'

interface PasswordResetViewProps {
    email: string
    onEmailChange: (e: React.ChangeEvent<HTMLInputElement>) => void
    onSubmit: () => void
}

export default function PasswordResetView({
    email,
    onEmailChange,
    onSubmit,
}: PasswordResetViewProps) {
    return (
        <PublicFormPageTemplate
            title="비밀번호 재설정"
            description={`가입 시 등록한 이메일 주소를 입력해 주세요\n비밀번호 재설정 링크를 보내드립니다`}
            actionButtonText="이메일 발송"
            onActionClick={onSubmit}
            footerLink={{
                text: '← 로그인 화면으로 돌아가기',
                to: '/login',
            }}
        >
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
        </PublicFormPageTemplate>
    )
}