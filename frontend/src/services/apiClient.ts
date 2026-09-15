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

async function handleResponse(res: Response) {
    if (!res.ok) {
        const errBody = await res.json().catch(() => ({ message: res.statusText }))
        throw { status: res.status, ...errBody }
    }
    if (res.status === 204) return null
    return res.json()
}

export const api = {
    async get(path: string) {
        const res = await fetch(`${API_BASE}${path}`, {
            method: 'GET',
            credentials: 'include',
        })
        return handleResponse(res)
    },
    async post(path: string, body?: any, headers: Record<string, string> = {}) {
        const reqHeaders: Record<string, string> = {
            ...headers,
        }

        // POST 요청이고 Idempotency-Key가 없다면 자동으로 생성해서 주입
        if (!Object.keys(reqHeaders).some(h => h.toLowerCase() === 'idempotency-key')) {
            reqHeaders['Idempotency-Key'] = generateIdempotencyKey()
        }

        const options: RequestInit = {
            method: 'POST',
            credentials: 'include',
            headers: reqHeaders,
        }

        if (body instanceof FormData) {
            options.body = body
            // FormData 사용 시 Content-Type은 브라우저가 boundary와 함께 자동 설정하도록 제거
            delete reqHeaders['Content-Type']
        } else if (body) {
            reqHeaders['Content-Type'] = 'application/json'
            options.body = JSON.stringify(body)
        }

        const res = await fetch(`${API_BASE}${path}`, options)
        return handleResponse(res)
    }
}