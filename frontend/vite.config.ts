import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    host: '127.0.0.1',
    port: Number(process.env.SW_WEB_FRONTEND_PORT ?? 5173),
    strictPort: true,
    proxy: { '/api': 'http://127.0.0.1:' + (process.env.SW_WEB_API_PORT ?? '8765') },
  },
})
