import { expect, test } from '@playwright/test'

test('reply tables render while streaming and after reloading the conversation', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const runId = 'markdown-table-run'
  const sessionId = 'markdown-table-session'
  const header = '| 方法 | 奖励信号 | 使用工具 | 评估方式 | score |\n'
  const rows = '| --- | --- | --- | --- | ---: |\n'
    + '| 策略梯度 | 每轮任务是否完成 | `read_file` | 验证工具结果与最终回复 | 42 |\n'
  const answer = header + rows
  const run = {
    run_id: runId, session_id: sessionId, workspace_id: 'chat', mode: 'read-only',
    prompt: '介绍 Agentic RL，并给出表格',
  }
  function frame(seq: number, type: string, content: string) {
    return 'data: ' + JSON.stringify({
      seq, type, run_id: runId, session_id: sessionId, schema_version: 1,
      payload: { content, has_tools: false },
    }) + '\n\n'
  }

  let settled = false
  let finish!: () => void
  const allowCompletion = new Promise<void>(resolve => { finish = resolve })
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url())
    switch (url.pathname.slice('/api/v1/'.length)) {
      case 'auth/config':
        return route.fulfill({ json: { oidc: false, bearer: false, password: true } })
      case 'workspaces':
        return route.fulfill({
          json: {
            provider: 'fake', repair_enabled: false, context_window_tokens: 100000,
            workspaces: [{ id: 'chat', kind: 'chat', label: '普通对话' }],
          },
        })
      case 'threads':
        return route.fulfill({ json: [] })
      case 'runs':
        return route.fulfill({ json: [] })
      case 'runs/' + runId:
        return route.fulfill({
          json: { ...run, status: settled ? 'completed' : 'active', final_text: settled ? answer : null },
        })
      case 'runs/' + runId + '/events': {
        const after = Number(url.searchParams.get('after'))
        if (!settled && after > 0) await allowCompletion
        return route.fulfill({
          contentType: 'text/event-stream',
          body: settled
            ? frame(3, 'assistant.completed', answer)
            : frame(1, 'assistant.delta', header) + frame(2, 'assistant.delta', rows),
        })
      }
      default:
        return route.abort()
    }
  })

  try {
    await page.goto('/?run=' + runId)
    const live = page.getByLabel('实时回复')
    const table = live.getByRole('table')
    await expect(table.getByRole('columnheader')).toHaveCount(5)
    await expect(table.getByRole('cell', { name: 'read_file' }).locator('code')).toHaveText('read_file')
    await expect(table.getByRole('cell', { name: '42' })).toHaveCSS('text-align', 'right')
    const wrapper = live.getByRole('region', { name: '回复表格' })
    expect(await wrapper.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
    expect(await page.locator('.chat-scroll').evaluate(element => element.scrollWidth))
      .toBe(await page.locator('.chat-scroll').evaluate(element => element.clientWidth))
    await wrapper.focus()
    await wrapper.press('ArrowRight')
    await expect.poll(() => wrapper.evaluate(element => element.scrollLeft)).toBeGreaterThan(0)
  } finally {
    settled = true
    finish()
  }

  await expect(page.getByLabel('实时回复')).toHaveCount(0)
  await expect(page.getByRole('table').getByRole('cell', { name: '策略梯度' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('table').getByRole('columnheader', { name: '评估方式' })).toBeVisible()
})
