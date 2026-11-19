import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'

const secret = 'local-browser-fixture-20261118'

async function register(page: Page, name: string) {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible()
  await expect(page.getByLabel('访问令牌')).toHaveCount(0)
  await page.getByRole('button', { name: '注册账号', exact: true }).click()
  await page.getByLabel('账号名').fill(name)
  await page.getByLabel('密码').fill(secret)
  await page.getByRole('button', { name: '注册并登录' }).click()
  await expect(page.locator('.sidebar-footer')).toContainText(name)
}

async function createWorkspace(page: Page, name: string) {
  await page.getByRole('button', { name: '新建工作区', exact: true }).click()
  const picker = page.getByRole('dialog', { name: '新建工作区' })
  await picker.getByRole('button', { name, exact: true }).click()
  await picker.getByRole('button', { name: '选择此文件夹' }).click()
  await expect(picker).toHaveCount(0)
  await expect(page.locator('.composer-path')).toContainText(name)
}

async function write(page: Page, message: string) {
  const prompt = page.getByLabel('任务说明')
  await prompt.fill(message)
  await expect(page.getByRole('button', { name: /^(发送|发送追问|运行只读任务)$/ })).toBeEnabled()
  const request = page.waitForResponse(response =>
    response.url().endsWith('/api/v1/runs') && response.request().method() === 'POST')
  await prompt.press('Enter')
  expect((await request).status()).toBe(202)
  await expect(page.getByTestId('run-status')).toHaveText('已完成', { timeout: 25_000 })
}

test('register and log in, group project threads, archive, and chat without a project', async ({ browser, page, baseURL }) => {
  const firstProject = process.env.SW_WEB_LOCAL_PROJECT_A
  const secondProject = process.env.SW_WEB_LOCAL_PROJECT_B
  test.skip(!firstProject || !secondProject, 'Requires local account mode and two committed fixtures')
  test.setTimeout(100_000)
  if (!firstProject || !secondProject) throw new Error('Missing project fixtures')
  const handle = 'browser-' + Date.now()
  await register(page, handle)
  const nav = page.getByRole('navigation', { name: '项目与线程' })
  await expect(nav.locator('.project-label')).toHaveText(['普通对话'])
  await page.getByRole('button', { name: '新任务', exact: true }).click()
  await expect(page.getByLabel('运行模式')).toHaveCount(0)
  await write(page, '无项目浏览器对话')
  await expect(page.getByText('这是离线普通对话的演示回复。')).toBeVisible()
  await createWorkspace(page, firstProject)
  await write(page, '第一个工作区任务')
  await createWorkspace(page, secondProject)
  await write(page, '第二个工作区任务')
  await expect(nav.locator('.project-label').last()).toHaveText('普通对话')

  const project = nav.locator('.project').filter({ has: page.getByText(firstProject, { exact: true }) })
  await project.getByRole('button', { name: '第一个工作区任务', exact: true }).click()
  await expect(page.locator('.composer-path')).toContainText(firstProject)
  await project.getByRole('button', { name: '归档 第一个工作区任务' }).click()
  await expect(page.getByText('此会话已归档。')).toBeVisible()
  await project.getByText('已归档 · 1').click()
  await project.getByRole('button', { name: '恢复 第一个工作区任务' }).click()
  await expect(page.getByText('此会话已归档。')).toHaveCount(0)
  await nav.getByRole('button', { name: '无项目浏览器对话', exact: true }).click()
  await expect(page.locator('.composer-path')).toHaveCount(0)
  await write(page, '浏览器追问')
  await expect(page.getByRole('article', { name: '对话轮次' })).toHaveCount(2)

  const other = await browser.newPage({ baseURL })
  try {
    await register(other, handle + '-other')
    await expect(other.getByRole('navigation').getByRole('button', { name: '第一个工作区任务' })).toHaveCount(0)
  } finally {
    await other.context().close()
  }
  await page.getByRole('button', { name: '退出登录' }).click()
  await page.getByLabel('账号名').fill(handle)
  await page.getByLabel('密码').fill(secret)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.locator('.sidebar-footer')).toContainText(handle)
  await expect(nav.getByRole('button', { name: '第一个工作区任务', exact: true })).toBeVisible()
})

test('local account can send anchored feedback and hand off a verified patch', async ({ page }) => {
  const project = process.env.SW_WEB_LOCAL_REVIEW_PROJECT
  test.skip(!project, 'Requires a local account workbench with a committed calc repair fixture')
  test.setTimeout(90_000)
  if (!project) throw new Error('Missing review project')
  await register(page, 'review-' + Date.now())
  await createWorkspace(page, project)
  await page.getByLabel('运行模式').selectOption('repair')
  const title = '浏览器审阅与交接验证'
  await page.getByLabel('任务说明').fill(title)
  await page.getByRole('button', { name: '运行修复任务' }).click()
  const approval = page.getByRole('region', { name: '待审批操作' })
  for (const hint of ['return a + b + 1', 'unittest', '+    return a + b', 'unittest']) {
    await expect(approval).toContainText(hint, { timeout: 20_000 })
    await approval.getByRole('button', { name: '批准一次' }).click()
  }
  await expect(page.getByTestId('run-status')).toHaveText('已完成', { timeout: 20_000 })
  const session = await page.getByTestId('session-id').textContent()
  await page.getByRole('button', { name: '查看改动', exact: true }).click()
  const review = page.getByRole('region', { name: '补丁审阅' })
  await review.getByText('calc.py · 1 个改动块').click()
  await review.getByRole('checkbox').check()
  await review.getByRole('button', { name: '预检目标分支' }).click()
  const result = page.getByRole('region', { name: '补丁预检结果' })
  await expect(result).toContainText('目标预检通过')
  await expect(result.getByRole('button', { name: '下载已复验补丁' })).toHaveCount(0)
  await review.getByRole('button', { name: '在独立工作区应用所选并验证' }).click()
  await expect(page.getByTestId('session-id')).not.toHaveText(session!)
  await expect(page.getByTestId('verification')).toHaveText('passed', { timeout: 25_000 })
  await page.getByRole('button', { name: '预检并交接到 IDE' }).click()
  await review.getByRole('button', { name: '预检目标分支' }).click()
  const download = result.getByRole('button', { name: '下载已复验补丁' })
  await expect(download).toBeVisible()
  const [patch] = await Promise.all([page.waitForEvent('download'), download.click()])
  expect(readFileSync((await patch.path())!, 'utf8')).toContain('+    return a + b')
  await page.getByRole('navigation', { name: '项目与线程' }).getByRole('button', { name: title, exact: true }).click()
  await page.getByRole('button', { name: '查看改动', exact: true }).click()
  await review.getByText('calc.py · 1 个改动块').click()
  await review.getByRole('button', { name: '反馈此改动块' }).click()
  await review.getByLabel('审阅意见').fill('请再检查这个加法实现。')
  await review.getByRole('button', { name: '发送到原线程并修订' }).click()
  await expect(page.getByText(/请再检查这个加法实现/)).toBeVisible()
  await expect(page.getByTestId('session-id')).toHaveText(session!)
  await expect(page.getByRole('article', { name: '对话轮次' })).toHaveCount(2)
})

test('installed skill browsing and 30-second usage and audit refresh', async ({ page }) => {
  test.skip(!process.env.SW_WEB_PANELS_E2E, 'Requires a local account workbench with the panel skill fixture')
  await register(page, 'panels-' + Date.now())
  await page.getByRole('button', { name: 'Skills 库', exact: true }).click()
  const library = page.getByRole('dialog', { name: 'Skills 库' })
  await library.getByLabel('搜索 Skills').fill('readme')
  await library.getByRole('button', { name: /readme-helper/ }).click()
  await expect(library.getByRole('region', { name: '技能详情' })).toContainText('Fixed skill fixture body.')
  await library.getByRole('button', { name: '刷新目录' }).click()
  await expect(library.getByRole('region', { name: '技能详情' })).toContainText('Fixed skill fixture body.')
  await library.getByRole('button', { name: '关闭' }).click()

  let reads = 0
  let audits = 0
  page.on('request', request => { if (request.url().endsWith('/api/v1/admin/audit')) audits++ })
  await page.route('**/api/v1/admin/usage/overview', route => {
    const previous = ++reads === 1
    return route.fulfill({ json: {
      read_at: previous ? '2026-11-18T15:59:45Z' : '2026-11-18T16:00:15Z',
      day: previous ? '2026-11-18' : '2026-11-19', quota_day: '2026-11-18',
      quota_resets_at: '2026-11-19T00:00:00Z',
      today: { tokens: previous ? 58 : 0, requests: previous ? 1 : 0, estimated_requests: 0, unsettled_requests: 0 },
      quota: { limit_tokens: 500000, used_tokens: 58 }, history: [],
    } })
  })
  await page.clock.install({ time: new Date('2026-11-18T15:59:45Z') })
  await page.getByRole('button', { name: '审计与用量' }).click()
  const panel = page.getByRole('dialog', { name: '审计与用量' })
  const today = panel.getByRole('region', { name: '今日模型用量' })
  await expect(today).toContainText('2026-11-18')
  await expect(panel.getByTestId('usage-read-at')).toContainText('23:59:45')
  await panel.getByRole('button', { name: '刷新', exact: true }).waitFor()
  await page.clock.fastForward(30_000)
  await expect(today).toContainText('2026-11-19')
  await expect(today).toContainText('0 tokens')
  await expect(panel.getByTestId('usage-read-at')).toContainText('00:00:15')
  expect(audits).toBe(2)
  await panel.getByRole('button', { name: '关闭' }).click()
  await page.clock.fastForward(30_000)
  expect(reads).toBe(2)
  expect(audits).toBe(2)
})
