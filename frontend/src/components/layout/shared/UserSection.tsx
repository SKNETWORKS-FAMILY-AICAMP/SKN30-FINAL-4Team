import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'

interface UserSectionProps {
    onLogout: () => void
    onRefreshSession: () => void
    expireTime: number
    onImminent: (minutes: number) => void
}

export default function UserSection({
    onLogout,
    onRefreshSession,
    expireTime,
    onImminent,
}: UserSectionProps) {
    const [timeLeft, setTimeLeft] = useState<number>(0)

    useEffect(() => {
        let alertTriggered = false

        const updateTimer = () => {
            const remaining = Math.max(0, Math.floor((expireTime - Date.now()) / 1000))
            setTimeLeft(remaining)

            // 5분(300초) 이하 남았을 때 부모의 얼럿 트리거 함수 호출
            if (remaining <= 300 && remaining > 0 && !alertTriggered && remaining % 60 == 0) {
                const currentMinutes = Math.ceil(remaining / 60)

                alertTriggered = true
                onImminent(currentMinutes)
            }

            // 시간이 만료된 경우 로그아웃 처리
            if (remaining <= 0) {
                onLogout()
            }
        }

        updateTimer()
        const timer = setInterval(updateTimer, 1000)
        return () => clearInterval(timer)
    }, [expireTime, onImminent, onLogout])

    const minutes = Math.floor(timeLeft / 60)
    const seconds = timeLeft % 60
    const formattedTime = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    const isImminent = timeLeft <= 300 && timeLeft > 0

    return (
        <div className="flex flex-col gap-sm px-xs py-sm mt-auto border-t border-outline-variant pt-md">
            <div className="flex items-center gap-sm">
                <div className="w-8 h-8 rounded-full flex items-center justify-center bg-surface-container-low text-on-surface-variant">
                    <span className="material-symbols-outlined text-[24px]">badge</span>
                </div>
                <div className="flex flex-col min-w-0">
                    <Link to="/mypage" className="font-title-sm text-[15px] font-semibold text-on-surface truncate no-underline hover:text-primary transition-colors">
                        분석가
                    </Link>
                </div>
            </div>

            <div className={`flex items-center justify-between gap-xs px-2 py-1.5 rounded-lg text-body-sm w-full border ${
                isImminent 
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
                        className={`text-[11px] font-medium px-2 py-0.5 rounded-xs transition-all border ${
                            isImminent 
                                ? 'text-white bg-error border-error hover:opacity-90 font-bold' 
                                : 'text-primary border-outline-variant hover:bg-surface'
                        }`}
                    >
                        연장
                    </button>
                    <button
                        type="button"
                        onClick={onLogout}
                        className={`text-[11px] font-medium px-1.5 py-0.5 rounded-xs border flex items-center gap-0.5 ${
                            isImminent 
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