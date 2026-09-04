import type { InputHTMLAttributes, ReactNode } from 'react'

interface InputFieldProps extends InputHTMLAttributes<HTMLInputElement> {
    label: string
    iconName: string
    className?: string
    layout?: 'horizontal' | 'vertical'
    children?: ReactNode
}

export default function InputField({
    label,
    iconName,
    id,
    className = '',
    layout = 'horizontal',
    children,
    ...props
}: InputFieldProps) {
    // 💡 아이콘과 인풋이 포함된 핵심 입력 박스는 하나로 공통화
    const inputWithIcon = (
        <div className="relative">
            <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-outline" style={{ fontSize: '22px' }}>
                {iconName}
            </span>
            <input id={id}
                className="w-full pl-10 pr-4 h-[40px] bg-surface border border-outline-variant rounded-lg text-body-md text-on-surface focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent transition-all"
                {...props}
            />
        </div>
    )

    // 레이아웃(배치 구조)에 따른 아웃라인만 분기
    if (layout === 'vertical') {
        return (
            <div className="flex flex-col gap-sm">
                <label className="font-label-caps text-label-caps text-on-surface-variant mb-xs" htmlFor={id}>
                    {label}
                </label>
                {inputWithIcon}
                {children}
            </div>
        )
    }

    return (
        <div className={`grid grid-cols-1 md:grid-cols-3 gap-md items-start ${className}`}>
            <label className="font-label font-semibold text-on-surface pt-2 text-body-md text-[16px]" htmlFor={id}>
                {label}
            </label>
            <div className="md:col-span-2 flex flex-col gap-md">
                {inputWithIcon}
                {children}
            </div>
        </div>
    )
}