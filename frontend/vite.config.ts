import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import fs from 'node:fs'
import path from 'node:path'

const USE_MOCK = false 

export default defineConfig({
    plugins: [
        react(),
        tailwindcss(),
        {
            name: 'local-json-mock-server',
            configureServer(server) {
                if (!USE_MOCK) return

                server.middlewares.use((req, res, next) => {
                    if (req.url?.startsWith('/api/v1')) {
                        const pathname = req.url.split('?')[0]
                        const subPath = pathname.replace('/api/v1', '')
                        
                        let targetFile = path.resolve(__dirname, `public/mock-api${subPath}.json`)
                        
                        if (!fs.existsSync(targetFile) && req.method === 'POST') {
                            targetFile = path.resolve(__dirname, `public/mock-api/success.json`)
                        }

                        if (fs.existsSync(targetFile)) {
                            res.setHeader('Content-Type', 'application/json')
                            const data = fs.readFileSync(targetFile, 'utf-8')
                            res.end(data)
                            return
                        }
                    }
                    next()
                })
            }
        }
    ],
    server: {
        port: 3000,
        proxy: {
            '/api/v1': {
                target: 'http://localhost:8001',
                changeOrigin: true,
                secure: false,
            }
        }
    }
})