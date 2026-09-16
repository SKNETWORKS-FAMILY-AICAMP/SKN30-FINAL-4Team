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
        <div className="fixed inset-0 flex items-center justify-center p-md bg-black/50 z-50">
            <div className="relative flex flex-col max-w-[840px] w-full max-h-[90vh] my-auto bg-surface-container-lowest border border-outline-variant rounded-xl shadow-2xl">
                {/* 상단 타이틀 및 X 버튼 */}
                <div className="flex justify-between items-center px-lg pt-lg pb-md border-b border-outline-variant">
                    {title && <h3 className="font-title-sm text-title-sm text-on-surface">{title}</h3>}
                    <button
                        onClick={onClose}
                        className="text-on-surface-variant hover:text-on-surface text-lg font-bold ml-auto"
                        aria-label="닫기"
                    >
                        ✕
                    </button>
                </div>

                {/* 본문 컨텐츠 영역 (유연한 확장) */}
                <div className="p-lg group/scroll overflow-y-auto" style={{ scrollbarWidth: 'thin', scrollbarColor: 'rgba(67, 71, 78, 0.3) transparent' }}>
                    {children}
                </div>
            </div>
        </div>
    )
}