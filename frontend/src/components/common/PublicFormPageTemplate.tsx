import type { ReactNode, FormEvent } from 'react'
import { Link } from 'react-router-dom'

interface PublicFormPageTemplateProps {
    title: string
    description?: string
    children: ReactNode
    actionButtonText: string
    onActionClick: () => void
    footerLink?: {
        text: string
        to: string
    }
}

export default function PublicFormPageTemplate({
    title,
    description,
    children,
    actionButtonText,
    onActionClick,
    footerLink,
}: PublicFormPageTemplateProps) {
    const handleSubmit = (e: FormEvent) => {
        e.preventDefault()
        onActionClick()
    }

    return (
        <form 
            onSubmit={handleSubmit}
            className="w-full max-w-[400px] mx-auto my-20 p-xl bg-surface-container-lowest border border-surface-variant rounded-lg"
        >
            <h2 className="font-headline-md text-headline-md text-primary-container text-center mb-md">
                {title}
            </h2>
            
            {description && (
                <p className="font-body-sm text-body-sm text-secondary mt-xs text-center mb-xl whitespace-pre-line">
                    {description}
                </p>
            )}

            {children}

            <div className="mt-xl">
                <button
                    type="submit"
                    className="w-full py-md rounded-lg bg-primary text-on-primary font-title-sm text-title-sm hover:opacity-90 transition-opacity shadow-sm cursor-pointer"
                >
                    {actionButtonText}
                </button>
            </div>

            {footerLink && (
                <div className="mt-lg flex items-center justify-center gap-md text-body-sm text-on-surface-variant">
                    <Link 
                        to={footerLink.to} 
                        className="hover:text-primary hover:underline transition-colors"
                    >
                        {footerLink.text}
                    </Link>
                </div>
            )}
        </form>
    )
}