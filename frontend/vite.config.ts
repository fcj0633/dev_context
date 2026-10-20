import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
export default defineConfig({
  plugins: [vue()],
  server: { port: 5173, strictPort: true, proxy: { '/api': process.env.DEVCONTEXT_API_URL || 'http://127.0.0.1:8000', '/health': process.env.DEVCONTEXT_API_URL || 'http://127.0.0.1:8000' } },
  test: { environment: 'jsdom', include: ['tests/**/*.test.ts'], restoreMocks: true },
})
