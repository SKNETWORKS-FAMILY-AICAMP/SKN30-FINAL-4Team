interface LogoProps {
    href?: string
    size?: 'sm' | 'md' | 'lg'
    className?: string
}

export default function Logo({ href = '/', size = 'md', className = '' }: LogoProps) {
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
                className={`${sizeClasses[size]} w-auto object-contain`}
                alt="Pre-review Logo"
            />
        </a>
    )
}