import { defineConfig } from '@playwright/test'
import { resolve } from 'node:path'
const python = resolve('..', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const apiPort = process.env.PLAYWRIGHT_API_PORT || '8000'
const apiURL = `http://127.0.0.1:${apiPort}`
export default defineConfig({
  testDir: './e2e', timeout: 30000, workers: 1, use: { baseURL: 'http://127.0.0.1:5173', screenshot: 'only-on-failure' },
  webServer: [
    { command: `"${python}" -m uvicorn web_fixture:app --app-dir ../tests --host 127.0.0.1 --port ${apiPort}`, url: `${apiURL}/health`, reuseExistingServer: false, timeout: 30000 },
    { command: 'npm run dev', url: 'http://127.0.0.1:5173', env: { DEVCONTEXT_API_URL: apiURL }, reuseExistingServer: false, timeout: 30000 },
  ],
})
