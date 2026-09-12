import type { ReactNode } from 'react'

interface ModalProps {
    isOpen: boolean
    onClose: () => void
    title?: string
    children: ReactNode
}

export default function Modal({ isOpen, onClose, title, children }: ModalProps) {
    if (!isOpen) return null

    return (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-md">
            <div className="bg-surface border border-outline-variant rounded-xl max-w-[560px] w-full p-lg shadow-xl flex flex-col gap-md max-h-[90vh] overflow-y-auto">
                {/* 상단 타이틀 및 X 버튼 */}
                <div className="flex justify-between items-center border-b border-outline-variant pb-sm">
                    {title && <h3 className="font-title-sm text-title-sm text-on-surface">{title}</h3>}
                    <button 
                        onClick={onClose}
                        className="text-on-surface-variant hover:text-on-surface cursor-pointer text-lg font-bold ml-auto"
                        aria-label="닫기"
                    >
                        ✕
                    </button>
                </div>

                {/* 본문 컨텐츠 영역 (유연한 확장) */}
                <div className="flex flex-col gap-sm text-body-sm text-on-surface">
                    {children}
                </div>
            </div>
        </div>
    )
}