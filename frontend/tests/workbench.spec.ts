import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('real repository listing survives refresh without a new POST', async ({ page }) => {
  const token = process.env.STELLARWEAVER_WEB_TOKEN
  if (!token) throw new Error('Provide the test web token in the environment')
  let submissions = 0
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith('/api/v1/runs')) submissions++
  })
  await page.goto('/')
  await page.getByLabel('访问令牌').fill(token)
  await page.getByRole('button', { name: '连接', exact: true }).click()
  await page.getByLabel('任务说明').fill('浏览真实测试仓库')
  await page.getByRole('button', { name: '运行只读任务' }).click()
  await expect(page.getByTestId('run-status')).toHaveText('已完成')
  await page.getByRole('button', { name: '运行详情', exact: true }).click()
  const timeline = page.getByRole('list', { name: '运行事件' })
  await expect(timeline).toContainText('g1-browser-fixture.txt')
  const session = await page.getByTestId('session-id').textContent()
  const last = await timeline.locator('li').last().getAttribute('data-seq')
  await page.reload()
  await expect(page.getByTestId('session-id')).toHaveText(session!)
  await page.getByRole('button', { name: '运行详情', exact: true }).click()
  await expect(timeline).toContainText('g1-browser-fixture.txt')
  await expect(timeline.locator('li').last()).toHaveAttribute('data-seq', last!)
  expect(submissions).toBe(1)
})

test('approve edits and real checks, resume pending approval, download report, and cancel another run', async ({ page }) => {
  test.skip(process.env.SW_WEB_E2E_REPAIR !== '1', 'Requires a fresh calc.py Git fixture')
  const token = process.env.STELLARWEAVER_WEB_TOKEN
  const workspace = process.env.SW_WEB_E2E_WORKSPACE
  if (!token || !workspace) throw new Error('Set the test token and workspace')
  const source = resolve('..', workspace, 'calc.py')
  let submissions = 0
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith('/api/v1/runs')) submissions++
  })
  await page.goto('/')
  await page.getByLabel('访问令牌').fill(token)
  await page.getByRole('button', { name: '连接', exact: true }).click()
  const mode = page.getByLabel('运行模式')
  await expect(mode).toHaveValue('read-only')
  await mode.selectOption('repair')
  const panel = page.getByRole('region', { name: '待审批操作' })
  const approve = panel.getByRole('button', { name: '批准一次' })
  const submit = page.getByRole('button', { name: '运行修复任务' })
  await page.getByLabel('任务说明').fill('取消未审批的修复')
  await submit.click()
  await expect(panel).toContainText('return a + b + 1')
  await page.getByRole('button', { name: '取消运行' }).click()
  await expect(page.getByTestId('run-status')).toHaveText('已取消')
  await expect(panel).toHaveCount(0)
  expect(readFileSync(source, 'utf8')).toContain('return a - b')

  await page.getByRole('button', { name: '新任务', exact: true }).click()
  await mode.selectOption('repair')
  await page.getByLabel('任务说明').fill('批准两次编辑并运行两次测试')
  await submit.click()
  await expect(panel).toContainText('return a + b + 1')
  const session = await page.getByTestId('session-id').textContent()
  await page.reload()
  await expect(panel).toContainText('return a + b + 1')
  await expect(page.getByTestId('session-id')).toHaveText(session!)
  expect(submissions).toBe(2)
  await approve.click()
  await expect(panel).toContainText('unittest')
  await approve.click()
  await expect(panel).toContainText('+    return a + b')
  await approve.click()
  await expect(panel).toContainText('unittest')
  await approve.click()
  await expect(page.getByTestId('run-status')).toHaveText('已完成')
  await page.getByRole('button', { name: '查看验证', exact: true }).click()
  await expect(page.getByTestId('verification')).toHaveText('passed')
  const report = page.getByRole('region', { name: '交付报告' })
  await expect(report).toContainText('退出码 1')
  await expect(report).toContainText('退出码 0')
  const [download] = await Promise.all([
    page.waitForEvent('download'), report.getByRole('button', { name: '下载 JSON 报告' }).click(),
  ])
  const evidence = JSON.parse(readFileSync((await download.path())!, 'utf8'))
  expect(evidence.checks.map((check: { exit_code: number }) => check.exit_code)).toEqual([1, 0])
  expect(evidence.changed_paths).toEqual(['calc.py'])
  expect(readFileSync(source, 'utf8')).toMatch(/return a \+ b\r?\n/)
})
