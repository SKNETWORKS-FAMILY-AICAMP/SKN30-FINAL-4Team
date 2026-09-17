import { useEffect, useRef, useState, useCallback } from 'react'

export interface UsePollingOptions {
    interval?: number
    enabled?: boolean
    immediate?: boolean
    onError?: (error: any) => void
}

export function usePolling(
    callback: () => Promise<boolean | void> | boolean | void,
    options: UsePollingOptions = {}
) {
    const { interval = 3000, enabled = true, immediate = false, onError } = options
    const [isPolling, setIsPolling] = useState(enabled)
    const savedCallback = useRef(callback)
    const savedOnError = useRef(onError)
    const isExecutingRef = useRef(false)

    useEffect(() => {
        savedCallback.current = callback
    }, [callback])

    useEffect(() => {
        savedOnError.current = onError
    }, [onError])

    useEffect(() => {
        setIsPolling(enabled)
    }, [enabled])

    const stop = useCallback(() => {
        setIsPolling(false)
    }, [])

    const start = useCallback(() => {
        setIsPolling(true)
    }, [])

    useEffect(() => {
        if (!isPolling) return

        let timerId: ReturnType<typeof setInterval> | null = null
        let isCancelled = false

        const tick = async () => {
            if (isCancelled || isExecutingRef.current) return
            isExecutingRef.current = true

            try {
                const result = await savedCallback.current()
                if (result === false && !isCancelled) {
                    setIsPolling(false)
                }
            } catch (error) {
                if (!isCancelled) {
                    if (savedOnError.current) {
                        savedOnError.current(error)
                    }
                    setIsPolling(false)
                }
            } finally {
                isExecutingRef.current = false
            }
        }

        if (immediate) {
            tick()
        }

        timerId = setInterval(tick, interval)

        return () => {
            isCancelled = true
            if (timerId) clearInterval(timerId)
        }
    }, [isPolling, interval, immediate])

    return { isPolling, start, stop }
}

export default usePolling
