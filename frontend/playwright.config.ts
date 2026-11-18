import { defineConfig } from '@playwright/test'

const apiPort = Number(process.env.SW_WEB_API_PORT ?? 8765)
const frontendPort = Number(process.env.SW_WEB_FRONTEND_PORT ?? 5173)
const workspace = process.env.SW_WEB_E2E_WORKSPACE ?? '.validation/gui-g1-fixture'
const repair = process.env.SW_WEB_E2E_REPAIR === '1' ? ' --enable-repair' : ''
const deployment = process.env.SW_WEB_E2E_BASE_URL

export default defineConfig({
  testDir: './tests',
  use: { baseURL: deployment ?? 'http://127.0.0.1:' + frontendPort, channel: 'msedge' },
  webServer: deployment ? undefined : [
    {
      command: `uv run --offline --locked --extra web python -m stellarweaver.web serve --provider fake --workspace "${workspace}" --port ${apiPort}${repair}`,
      cwd: '..',
      port: apiPort,
      reuseExistingServer: false,
    },
    {
      command: 'npm run dev',
      port: frontendPort,
      reuseExistingServer: false,
    },
  ],
})
