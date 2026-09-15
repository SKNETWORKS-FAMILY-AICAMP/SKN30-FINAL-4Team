import type { ReactNode } from 'react'

interface PageTemplateProps {
    title: string
    subtitle?: string
    children: ReactNode
}

export default function PageTemplate({ title, subtitle, children }: PageTemplateProps) {
    return (
        <div className="p-lg md:p-xl flex-1 flex flex-col items-center max-w-container-max mx-auto w-full pb-xl">
            <div className="w-full max-w-4xl flex flex-col gap-lg mt-md">
                
                {/* 상단 타이틀 영역 */}
                <div className="text-center mb-xl pt-lg">
                    <h1 className="font-display-lg text-display-lg text-on-surface mb-sm">{title}</h1>
                    {subtitle && <p className="font-body-md text-body-md text-on-surface-variant">{subtitle}</p>}
                </div>

                {/* 메인 컨텐츠 카드 영역 */}
                <div className="bg-surface-container-lowest rounded-2xl border border-outline-variant p-xl flex flex-col shadow-sm relative overflow-hidden">
                    {children}
                </div>

            </div>
        </div>
    )
}