import { execFileSync } from 'node:child_process'
import { expect, test, type Page } from '@playwright/test'

async function connect(page: Page, token: string, account: string) {
  await page.goto('/')
  await page.getByLabel('访问令牌').fill(token)
  await page.getByRole('button', { name: '连接', exact: true }).click()
  await expect(page.getByText(account, { exact: true })).toBeVisible()
}

async function startRepair(page: Page, title: string) {
  await page.getByLabel('运行模式').selectOption('repair')
  await page.getByLabel('任务说明').fill(title)
  await page.getByRole('button', { name: '运行修复任务' }).click()
}

async function approve(page: Page, hint: string) {
  const approval = page.getByRole('region', { name: '待审批操作' })
  await expect(approval).toContainText(hint)
  await approval.getByRole('button', { name: '批准一次' }).click()
}

function runners(project: string, all = false) {
  return execFileSync('docker', ['ps', ...(all ? ['-a'] : []), '-q', '--filter', `label=io.stellarweaver.runner=${project}`],
    { encoding: 'utf8' }).trim().split(/\s+/).filter(Boolean)
}

test('two accounts run isolated repairs concurrently and scope audit and patches', async ({ browser, baseURL }) => {
  const project = process.env.SW_G5_E2E_PROJECT
  const a = process.env.SW_G5_E2E_ACCOUNT_A
  const b = process.env.SW_G5_E2E_ACCOUNT_B
  const admin = process.env.SW_G5_E2E_ACCOUNT_ADMIN
  test.skip(!project || !a || !b || !admin, 'Requires the dedicated G5 Compose fixture and three fixture accounts')
  test.setTimeout(120_000)
  if (!project || !a || !b || !admin || !baseURL) throw new Error('Missing G5 fixture configuration')

  const pages = await Promise.all([a, b, admin].map(() => browser.newPage({ baseURL })))
  const [first, second, auditor] = pages
  const titles = ['G5 Account A ' + Date.now(), 'G5 Account B ' + Date.now()]
  try {
    await Promise.all([
      connect(first, a, 'Account A'), connect(second, b, 'Account B'), connect(auditor, admin, 'Account Admin'),
    ])
    await Promise.all([startRepair(first, titles[0]), startRepair(second, titles[1])])
    await expect(first.getByRole('button', { name: titles[1], exact: true })).toHaveCount(0)
    await expect(second.getByRole('button', { name: titles[0], exact: true })).toHaveCount(0)

    await Promise.all([approve(first, 'return a + b + 1'), approve(second, 'return a + b + 1')])
    await Promise.all([approve(first, 'unittest'), approve(second, 'unittest')])
    await expect.poll(() => runners(project).length, { timeout: 20_000, intervals: [125, 200] }).toBeGreaterThanOrEqual(2)
    for (const hint of ['+    return a + b', 'unittest']) {
      await Promise.all([approve(first, hint), approve(second, hint)])
    }
    const patches = await Promise.all([first, second].map(async (page) => {
      await expect(page.getByTestId('run-status')).toHaveText('已完成')
      await page.getByRole('button', { name: '查看改动', exact: true }).click()
      await expect(page.getByTestId('verification')).toHaveText('passed')
      const result = await page.context().request.get('/api/v1/runs', {
        headers: { Authorization: 'Bearer ' + (page === first ? a : b) },
      })
      expect(result.ok()).toBeTruthy()
      const runs = await result.json()
      return runs[0].run_id
    }))
    for (const [i, page] of [first, second].entries()) {
      const headers = { Authorization: 'Bearer ' + [a, b][i] }
      const patch = await page.request.get('/api/v1/runs/' + patches[i] + '/patch', { headers })
      expect(patch.ok()).toBeTruthy()
      expect(await patch.text()).toContain('+    return a + b')
      const forbidden = await page.request.get('/api/v1/runs/' + patches[1 - i] + '/patch', { headers })
      expect(forbidden.status()).toBe(404)
    }
    await auditor.getByRole('button', { name: '审计与用量' }).click()
    const audit = auditor.getByRole('dialog', { name: '审计与用量' })
    await expect(audit).toContainText('Account A')
    await expect(audit).not.toContainText('Account B')
    await expect.poll(() => runners(project, true)).toEqual([])
  } finally {
    await Promise.all(pages.map(page => page.context().close()))
  }
})

test('executor crash preserves unconfirmed verification and removes its abandoned container', async ({ page }) => {
  const project = process.env.SW_G5_E2E_PROJECT
  const token = process.env.SW_G5_E2E_ACCOUNT_A
  test.skip(project !== 'stellarweaver-g5-acceptance' || !token || process.env.SW_G5_E2E_CRASH !== '1',
    'Only run the fault test against the dedicated G5 acceptance project')
  test.setTimeout(120_000)
  if (!project || !token) throw new Error('Missing G5 fixture configuration')

  await connect(page, token, 'Account A')
  await startRepair(page, 'G5 executor recovery ' + Date.now())
  for (const hint of ['return a + b + 1', 'unittest', '+    return a + b', 'unittest']) await approve(page, hint)
  await expect(page.getByTestId('run-status')).toHaveText('已完成')
  await page.getByRole('button', { name: '查看改动', exact: true }).click()
  const review = page.getByRole('region', { name: '补丁审阅' })
  await review.getByText('calc.py · 1 个改动块').click()
  await review.getByRole('checkbox').check()
  await review.getByRole('button', { name: '在独立工作区应用所选并验证' }).click()
  const executor = execFileSync('docker', ['ps', '-q', '--filter', `label=com.docker.compose.project=${project}`,
    '--filter', 'label=com.docker.compose.service=executor'], { encoding: 'utf8' }).trim()
  expect(executor).toMatch(/^[a-f0-9]{12,64}$/)
  await expect.poll(() => runners(project).length, { timeout: 20_000, intervals: [100] }).toBe(1)
  try {
    execFileSync('docker', ['kill', '--signal', 'KILL', executor])
    const headers = { Authorization: 'Bearer ' + token }
    const runs = await (await page.request.get('/api/v1/runs', { headers })).json()
    const id = runs[0].run_id
    await expect.poll(async () => {
      const response = await page.request.get('/api/v1/runs/' + id, { headers })
      const run = await response.json()
      return run.outcome?.error_code
    }, { timeout: 20_000 }).toBe('RUNNER_CLEANUP_UNCONFIRMED')
    await expect(page.getByTestId('run-status')).toHaveText('失败')
    await expect(page.getByText('执行服务未确认验证结果及清理状态。')).toBeVisible()
  } finally {
    execFileSync('docker', ['start', executor])
    await expect.poll(() => execFileSync('docker', ['inspect', '--format', '{{.State.Health.Status}}', executor],
      { encoding: 'utf8' }).trim(), { timeout: 20_000 }).toBe('healthy')
    await expect.poll(() => runners(project, true)).toEqual([])
  }
})
