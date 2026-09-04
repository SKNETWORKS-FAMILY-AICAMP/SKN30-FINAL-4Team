interface AlertModalProps {
    title: string
    description: string
    type?: 'alert' | 'confirm'
    confirmText?: string
    cancelText?: string
    onConfirm: () => void
    onClose?: () => void
}

export default function AlertModal({
    title,
    description,
    type = 'alert',
    confirmText = '확인',
    cancelText = '취소',
    onConfirm,
    onClose,
}: AlertModalProps) {
    return (
        <div 
            aria-modal="true" 
            role="dialog" 
            className="fixed inset-0 bg-slate-900/50 backdrop-blur-sm z-50 flex items-center justify-center p-4"
        >
            <div className="bg-white rounded-xl shadow-2xl border border-slate-200 w-full max-w-[440px] p-6 relative flex flex-col items-center text-center animate-in fade-in zoom-in-95 duration-150">
                <h3 className="font-bold text-lg text-slate-900 pt-md">
                    {title}
                </h3>
                <p className="text-sm text-slate-600 mt-2 leading-relaxed whitespace-pre-line">
                    {description}
                </p>

                {type === 'confirm' ? (
                    <div className="grid grid-cols-2 gap-3 mt-6 w-full">
                        <button
                            type="button"
                            onClick={onClose}
                            className="bg-slate-100 hover:bg-slate-200 text-slate-700 font-medium py-2.5 px-4 rounded-lg transition-colors text-center text-sm cursor-pointer"
                        >
                            {cancelText}
                        </button>
                        <button
                            type="button"
                            onClick={onConfirm}
                            className="bg-[#1a365d] hover:bg-[#0f2442] text-white font-medium py-2.5 px-4 rounded-lg transition-colors text-center text-sm shadow-sm cursor-pointer"
                        >
                            {confirmText}
                        </button>
                    </div>
                ) : (
                    <div className="w-full mt-6">
                        <button
                            type="button"
                            onClick={onConfirm}
                            className="bg-[#1a365d] hover:bg-[#0f2442] text-white font-medium py-2.5 px-4 rounded-lg w-full transition-colors flex items-center justify-center shadow-sm cursor-pointer"
                        >
                            {confirmText}
                        </button>
                    </div>
                )}
            </div>
        </div>
    )
}