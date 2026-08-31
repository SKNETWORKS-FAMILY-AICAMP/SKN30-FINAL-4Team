interface LogoProps {
    href?: string
    size?: 'sm' | 'md' | 'lg'
    className?: string
}

export default function Logo({ href = '/', size = 'md', className = '' }: LogoProps) {
    // 사이즈별 이미지 높이 매핑 (사이드바 h=86 공간 등을 고려한 sm 포함)
    const sizeClasses = {
        sm: 'h-[32px]',
        md: 'h-[40px]',
        lg: 'h-[82px]',
    }

    return (
        <a href={href} className={`flex items-center gap-xs text-title-sm font-title-sm font-bold text-primary dark:text-primary-fixed select-none no-underline ${className}`}
        >
            <img
                src="/images/pre-review.png"
                alt="Pre-review Logo"
                className={`${sizeClasses[size]} w-auto object-contain`}
            />
        </a>
    )
}