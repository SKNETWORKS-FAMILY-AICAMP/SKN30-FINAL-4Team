import { useState, useEffect, useRef } from 'react'

export interface UseSessionTimerOptions {
    expireTime: number
    onLogout: () => void
    onImminent?: (minutes: number) => void
    imminentThresholdSeconds?: number
}

export interface UseSessionTimerReturn {
    timeLeft: number
    formattedTime: string
    isImminent: boolean
    minutes: number
    seconds: number
}

export function useSessionTimer({
    expireTime,
    onLogout,
    onImminent,
    imminentThresholdSeconds = 300,
}: UseSessionTimerOptions): UseSessionTimerReturn {
    const [timeLeft, setTimeLeft] = useState<number>(() =>
        Math.max(0, Math.floor((expireTime - Date.now()) / 1000))
    )

    const onLogoutRef = useRef(onLogout)
    const onImminentRef = useRef(onImminent)

    useEffect(() => {
        onLogoutRef.current = onLogout
    }, [onLogout])

    useEffect(() => {
        onImminentRef.current = onImminent
    }, [onImminent])

    useEffect(() => {
        let alertTriggered = false

        const updateTimer = () => {
            const remaining = Math.max(0, Math.floor((expireTime - Date.now()) / 1000))
            setTimeLeft(remaining)

            // 5분(imminentThresholdSeconds) 이하 남았을 때 부모의 얼럿 트리거 함수 호출
            if (
                remaining <= imminentThresholdSeconds &&
                remaining > 0 &&
                !alertTriggered &&
                remaining % 60 === 0
            ) {
                const currentMinutes = Math.ceil(remaining / 60)
                alertTriggered = true
                onImminentRef.current?.(currentMinutes)
            }

            // 시간이 만료된 경우 로그아웃 처리
            if (remaining <= 0) {
                onLogoutRef.current?.()
            }
        }

        updateTimer()
        const timer = setInterval(updateTimer, 1000)
        return () => clearInterval(timer)
    }, [expireTime, imminentThresholdSeconds])

    const minutes = Math.floor(timeLeft / 60)
    const seconds = timeLeft % 60
    const formattedTime = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    const isImminent = timeLeft <= imminentThresholdSeconds && timeLeft > 0

    return {
        timeLeft,
        formattedTime,
        isImminent,
        minutes,
        seconds,
    }
}

export default useSessionTimer
