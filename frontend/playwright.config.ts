import { defineConfig } from '@playwright/test'
import { resolve } from 'node:path'
const python = resolve('..', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
export default defineConfig({
  testDir: './e2e', timeout: 30000, workers: 1, use: { baseURL: 'http://127.0.0.1:5173', screenshot: 'only-on-failure' },
  webServer: [
    { command: `"${python}" -m uvicorn web_fixture:app --app-dir ../tests --host 127.0.0.1 --port 8000`, url: 'http://127.0.0.1:8000/health', reuseExistingServer: false, timeout: 30000 },
    { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: false, timeout: 30000 },
  ],
})
