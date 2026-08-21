import React from 'react'

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
    label?: string
    icon?: string
}

export default function Input({ label, icon, className = '', ...props }: InputProps) {
    return (
        <div className="flex flex-col gap-xs w-full">
            {label && <label className="font-body-sm text-on-surface font-medium">{label}</label>}
            <div className="relative flex items-center w-full">
                {icon && (
                    <span className="material-symbols-outlined absolute left-md text-on-surface-variant text-[20px]">
                        {icon}
                    </span>
                )}
                <input
                    className={`w-full bg-surface border border-outline-variant rounded-xl py-md text-on-surface font-body-md placeholder:text-on-surface-variant/60 focus:outline-none focus:border-primary transition-colors ${
                        icon ? 'pl-12 pr-md' : 'px-md'
                    } ${className}`}
                    {...props}
                />
            </div>
        </div>
    )
}