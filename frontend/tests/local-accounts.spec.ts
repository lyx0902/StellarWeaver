import { expect, test, type Page } from '@playwright/test'

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
