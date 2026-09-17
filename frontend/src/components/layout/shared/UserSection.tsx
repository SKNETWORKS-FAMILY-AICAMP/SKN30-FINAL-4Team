import { Link } from 'react-router-dom'
import { useSessionTimer } from '../../../hooks/useSessionTimer'

interface UserSectionProps {
    displayName: string
    onLogout: () => void
    onRefreshSession: () => void
    expireTime: number
    onImminent: (minutes: number) => void
}

export default function UserSection({
    displayName,
    onLogout,
    onRefreshSession,
    expireTime,
    onImminent,
}: UserSectionProps) {
    const { formattedTime, isImminent } = useSessionTimer({
        expireTime,
        onLogout,
        onImminent,
    })

    return (
        <div className="flex flex-col gap-sm px-xs py-sm mt-auto border-t border-outline-variant pt-md">
            <div className="flex items-center gap-sm">
                <div className="w-8 h-8 rounded-full flex items-center justify-center bg-surface-container-low text-on-surface-variant">
                    <span className="material-symbols-outlined text-[24px]">badge</span>
                </div>
                <div className="flex flex-col min-w-0">
                    <Link to="/mypage" className="font-title-sm text-[15px] font-semibold text-on-surface truncate no-underline hover:text-primary transition-colors">
                        {displayName}
                    </Link>
                </div>
            </div>

            <div className={`flex items-center justify-between gap-xs px-2 py-1.5 rounded-lg text-body-sm w-full border ${isImminent
                    ? 'bg-error-container/40 border-error/30 text-error'
                    : 'bg-surface-container-low border-outline-variant/60 text-on-surface'
                }`}>
                <div className="flex items-center gap-xs min-w-0">
                    <span className={`material-symbols-outlined ${isImminent ? 'text-error' : 'text-primary'}`} style={{ fontSize: '16px' }}>
                        timer
                    </span>
                    <span className={`font-data-mono text-[12px] font-bold ${isImminent ? 'text-error' : 'text-primary'}`}>
                        {formattedTime}
                    </span>
                </div>
                <div className="flex items-center gap-xs shrink-0">
                    <button
                        type="button"
                        onClick={onRefreshSession}
                        className={`text-[11px] font-medium px-2 py-0.5 rounded-xs transition-all border ${isImminent
                                ? 'text-white bg-error border-error hover:opacity-90 font-bold'
                                : 'text-primary border-outline-variant hover:bg-surface'
                            }`}
                    >
                        연장
                    </button>
                    <button
                        type="button"
                        onClick={onLogout}
                        className={`text-[11px] font-medium px-1.5 py-0.5 rounded-xs border flex items-center gap-0.5 ${isImminent
                                ? 'text-error border-error hover:opacity-90 font-bold hover:bg-surface'
                                : 'text-primary border-outline-variant hover:bg-surface'
                            }`}
                    >
                        <span className="material-symbols-outlined" style={{ fontSize: '13px' }}>logout</span>
                        로그아웃
                    </button>
                </div>
            </div>
        </div>
    )
}