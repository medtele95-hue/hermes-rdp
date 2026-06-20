import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/local-api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
        configure(proxy) {
          // Suppress ECONNREFUSED terminal spam when backend is not yet running.
          proxy.on('error', (err, _req, res) => {
            const code = (err as NodeJS.ErrnoException).code
            if (code === 'ECONNREFUSED') {
              // Let the frontend's BackendHealthContext handle reconnect logic.
              if ('writeHead' in res && typeof res.writeHead === 'function') {
                res.writeHead(503, { 'Content-Type': 'application/json' })
                res.end(JSON.stringify({ ok: false, status: 'PROXY_ECONNREFUSED' }))
              }
              return
            }
            // Non-ECONNREFUSED errors still surface to the console.
            console.error('[VITE_PROXY_ERROR]', err.message)
          })
        },
      },
    },
  },
})
