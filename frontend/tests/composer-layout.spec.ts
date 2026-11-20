import { expect, test } from '@playwright/test'

test('project alias editing and composer controls at desktop and mobile widths', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  const path = 'D:/Projects/' + 'a-long-project-segment/'.repeat(12) + 'research'
  const id = 'project'
  const label = 'research'
  const renamed = '研究工作区'
  const run = {
    run_id: 'layout-run', session_id: 'layout-session', workspace_id: id,
    status: 'completed', mode: 'read-only', prompt: '布局测试', final_text: '已完成。',
  }
  let alias: string | null = null
  await page.route('**/api/v1/**', route => {
    const resource = new URL(route.request().url()).pathname.slice('/api/v1/'.length)
    switch (resource) {
      case 'auth/config':
        return route.fulfill({ json: { oidc: false, bearer: false, password: true } })
      case 'workspaces':
        return route.fulfill({ json: {
          provider: 'deepseek', repair_enabled: true, can_submit: true, manage_workspaces: true,
          context_window_tokens: 100000, workspaces: [
            { id, path, label: alias ?? label, alias, default_label: label, kind: 'project' },
            { id: 'chat', kind: 'chat', label: '普通对话' },
          ],
        } })
      case 'workspaces/' + id:
        expect(route.request().method()).toBe('PATCH')
        alias = route.request().postDataJSON().alias || null
        return route.fulfill({ json: { alias, label: alias ?? label } })
      case 'threads':
        return route.fulfill({ json: [] })
      case 'runs':
        return route.fulfill({ json: [run] })
      case 'runs/' + run.run_id:
        return route.fulfill({ json: run })
      case 'runs/' + run.run_id + '/events': {
        const frame = (seq: number, type: string, payload: object) => 'data: ' + JSON.stringify({
          seq, type, payload, schema_version: 1, run_id: run.run_id, session_id: run.session_id,
        }) + '\n\n'
        return route.fulfill({ contentType: 'text/event-stream', body:
          frame(1, 'model.started', { request_index: 1, input_tokens: 1000 }) +
          frame(2, 'model.completed', { request_index: 1, input_tokens: 1234, input_estimated: false }),
        })
      }
      default:
        return route.abort()
    }
  })

  await page.goto('/?run=' + run.run_id)
  const edit = page.getByRole('button', { name: '修改 research 的别名' })
  const add = page.getByRole('button', { name: '在 research 新建任务' })
  await expect(edit).toBeVisible()
  expect((await edit.boundingBox())!.x).toBeLessThan((await add.boundingBox())!.x)
  await expect(page.locator('.project-edit')).toHaveCount(1)
  await edit.click()
  const dialog = page.getByRole('dialog', { name: '修改工作区别名' })
  await dialog.getByLabel('工作区别名', { exact: true }).fill('  ' + renamed + '  ')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.locator('.project-label').first()).toHaveText(renamed)
  expect(alias).toBe(renamed)
  await page.reload()
  await expect(page.locator('.project-label').first()).toHaveText(renamed)
  await page.getByRole('button', { name: '修改 ' + renamed + ' 的别名' }).click()
  await expect(dialog.getByLabel('工作区别名', { exact: true })).toHaveValue(renamed)
  await dialog.getByLabel('工作区别名', { exact: true }).fill('')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.locator('.project-label').first()).toHaveText(label)

  const trigger = page.getByTestId('context-usage').getByRole('button')
  const tooltip = page.getByRole('tooltip')
  await expect(trigger).toHaveAttribute('aria-label', /1,234 \/ 100,000/)
  await expect(tooltip).toHaveCount(0)
  await trigger.hover()
  await expect(tooltip).toHaveText(/上下文\s*1,234 \/ 100,000/)
  await page.mouse.move(0, 0)
  await expect(tooltip).toHaveCount(0)
  await trigger.focus()
  await expect(tooltip).toBeVisible()
  await trigger.press('Escape')
  await expect(tooltip).toHaveCount(0)

  await page.setViewportSize({ width: 320, height: 720 })
  const mode = page.getByLabel('运行模式')
  const send = page.getByRole('button', { name: '发送追问' })
  const [circle, selector, submit, toolbar, projectPath] = await Promise.all([
    trigger.boundingBox(), mode.boundingBox(), send.boundingBox(), page.locator('.composer-toolbar').boundingBox(),
    page.locator('.composer-path').boundingBox(),
  ])
  expect(circle!.x + circle!.width).toBeLessThan(selector!.x)
  expect(selector!.x + selector!.width).toBeLessThan(submit!.x)
  expect(Math.abs(selector!.y - submit!.y)).toBeLessThan(5)
  expect(submit!.x + submit!.width).toBeLessThan(320)
  expect(projectPath!.y).toBeGreaterThanOrEqual(toolbar!.y + toolbar!.height)
  await expect(page.locator('.composer-path')).toHaveAttribute('title', path)
  await expect(page.locator('.model-label')).toBeVisible()
})
