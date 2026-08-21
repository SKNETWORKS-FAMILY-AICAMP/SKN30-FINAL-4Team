import React from 'react'

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
    variant?: 'primary' | 'secondary' | 'outline'
    fullWidth?: boolean
    children: React.ReactNode
}

export default function Button({
    variant = 'primary',
    fullWidth = false,
    children,
    className = '',
    ...props
}: ButtonProps) {
    const baseStyle = "font-title-sm py-md px-lg rounded-xl transition-all flex items-center justify-center gap-xs font-bold cursor-pointer"
    
    const variantStyles = {
        primary: "bg-[#172e53] text-white hover:bg-[#11223f] shadow-sm", // Primary Navy
        secondary: "bg-surface-container text-on-surface hover:bg-surface-container-high",
        outline: "border border-outline-variant bg-transparent text-on-surface hover:bg-surface-container-low"
    }

    const widthStyle = fullWidth ? "w-full" : ""

    return (
        <button
            className={`${baseStyle} ${variantStyles[variant]} ${widthStyle} ${className}`}
            {...props}
        >
            {children}
        </button>
    )
}