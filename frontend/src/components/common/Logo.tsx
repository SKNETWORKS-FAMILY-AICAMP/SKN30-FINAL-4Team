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
        <a href={href} className={`no-underline ${className}`}
        >
            <img
                src="/images/pre-review.png"
                className={`${sizeClasses[size]} inline w-auto m-auto`}
                alt="Pre-review Logo"
            />
        </a>
    )
}