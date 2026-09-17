const API_BASE = '/api/v1'

// 고유한 Idempotency-Key(UUID) 생성 공통 함수
const generateIdempotencyKey = () => {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return crypto.randomUUID()
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0
        const v = c === 'x' ? r : (r & 0x3) | 0x8
        return v.toString(16)
    })
}

async function handleResponse(res: Response, responseType: 'json' | 'blob' = 'json') {
    if (!res.ok) {
        const errBody = await res.json().catch(() => ({ message: res.statusText }))
        throw { status: res.status, ...errBody }
    }
    if (res.status === 204) return null

    if (responseType === 'blob') {
        return res.blob()
    }

    return res.json()
}

export const api = {
    async get(path: string, options: { responseType?: 'json' | 'blob' } = {}) {
        const res = await fetch(`${API_BASE}${path}`, {
            method: 'GET',
            credentials: 'include',
        })
        return handleResponse(res, options.responseType || 'json')
    },

    async post(path: string, body?: any, headers: Record<string, string> = {}) {
        const reqHeaders: Record<string, string> = { ...headers }

        if (!Object.keys(reqHeaders).some(h => h.toLowerCase() === 'idempotency-key')) {
            reqHeaders['Idempotency-Key'] = generateIdempotencyKey()
        }

        const requestOptions: RequestInit = {
            method: 'POST',
            credentials: 'include',
            headers: reqHeaders,
        }

        if (body instanceof FormData) {
            requestOptions.body = body
            delete reqHeaders['Content-Type']
        } else if (body) {
            reqHeaders['Content-Type'] = 'application/json'
            requestOptions.body = JSON.stringify(body)
        }

        const res = await fetch(`${API_BASE}${path}`, requestOptions)
        return handleResponse(res, 'json')
    }
}