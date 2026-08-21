import React from 'react'

interface CardProps {
    children: React.ReactNode
    className?: string
}

export default function Card({ children, className = '' }: CardProps) {
    return (
        <div className={`bg-surface border border-outline-variant rounded-2xl shadow-sm p-xl ${className}`}>
            {children}
        </div>
    )
}