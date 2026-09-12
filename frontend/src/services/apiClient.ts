const API_BASE = '/api/v1'

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
        const options: RequestInit = {
            method: 'POST',
            credentials: 'include',
            headers,
        }
        console.log(import.meta.env.VITE_API_BASE_URL)
        if (body instanceof FormData) {
            options.body = body
        } else if (body) {
            options.headers = { 'Content-Type': 'application/json', ...headers }
            options.body = JSON.stringify(body)
        }
        const res = await fetch(`${API_BASE}${path}`, options)
        return handleResponse(res)
    }
}