import { expect, test } from '@playwright/test'

test('pins skills, shows continuation evidence, exports and confirms undo', async ({ page }) => {
  const skill = {
    id: '0'.repeat(64), sha256: '1'.repeat(64), name: 'project-checks', source: '项目',
    path: 'project-checks/SKILL.md', description: 'Fixture', shadowed_paths: [], content: 'Check fixture',
  }
  const original = {
    run_id: 'original', session_id: 'thread', workspace_id: 'project',
    status: 'completed', mode: 'auto', isolated: true, has_report: true, prompt: '修复夹具',
  }
  const followup = { ...original, run_id: 'followup', prompt: '结合证据复核', skills: [skill] }
  const undo = { ...followup, run_id: 'undo', is_undo: true, final_text: '已撤销改动' }
  let latest = original
  let trusted = false
  let sentUndo = false
  await page.route('**/api/v1/**', route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.slice('/api/v1/'.length)
    const [, id, action] = path.split('/')
    if (path === 'auth/config') return route.fulfill({ json: { password: true } })
    if (path === 'workspaces') return route.fulfill({ json: {
      provider: 'fake', repair_enabled: true, can_submit: true, isolated: true,
      account: { name: 'Owner', role: 'admin' },
      context_window_tokens: 100000,
      workspaces: [{ id: 'project', kind: 'project', label: 'Repository', path: 'D:/test' }],
    } })
    if (path === 'workspaces/project/skill-trust') {
      trusted = request.postDataJSON().enabled
      return route.fulfill({ json: { project_trusted: trusted } })
    }
    if (path === 'skills') {
      expect(url.searchParams.get('workspace_id')).toBe('project')
      return route.fulfill({ json: {
        sources: ['项目'], skills: trusted ? [skill] : [], errors: [],
        project_trusted: trusted, can_manage_project: true,
      } })
    }
    if (path === 'skills/' + skill.id) return route.fulfill({ json: skill })
    if (path === 'threads') return route.fulfill({ json: [] })
    if (path === 'admin/audit') return route.fulfill({ json: [] })
    if (path === 'admin/usage/overview') return route.fulfill({ json: {
      read_at: '2026-11-22T04:00:00Z', day: '2026-11-22',
      quota_week_start: '2026-11-16', quota_resets_at: '2026-11-23T00:00:00Z',
      today: { tokens: 17, requests: 1, estimated_requests: 0, unsettled_requests: 0 },
      quota: { limit_tokens: 10000000, used_tokens: 310 }, history: [],
    } })
    if (path === 'runs') {
      if (request.method() === 'POST') {
        expect(request.postDataJSON().skills).toEqual([{ id: skill.id, sha256: skill.sha256 }])
        expect(request.postDataJSON().continuation).toBe('history')
        latest = followup
        return route.fulfill({ json: followup })
      }
      return route.fulfill({ json: latest === original ? [original] : [latest, original] })
    }
    if (!action) return route.fulfill({ json: id === 'original' ? original : id === 'followup' ? followup : undo })
    if (action === 'report') return route.fulfill({ json: {
      run_id: id, verification: 'not_run', baseline_comparable: true, changed_paths: [], patches: [], checks: [],
    } })
    if (action === 'events') {
      const event = (seq: number, type: string, payload: object) => 'data: ' + JSON.stringify({
        run_id: id, session_id: 'thread', schema_version: 1, type, payload, seq,
      }) + '\n\n'
      return route.fulfill({ contentType: 'text/event-stream', body: id === 'followup'
        ? event(1, 'skill.selected', { skill_name: skill.name, skill_source: skill.source }) +
          event(2, 'context.prior_evidence', { files: 1, checks: 1, skills: 0 })
        : event(1, 'run.started', {}) })
    }
    if (action === 'export') return route.fulfill({ contentType: 'text/html', body: '<p>Offline report</p>' })
    if (action === 'undo') {
      if (request.method() === 'POST') {
        expect(request.postDataJSON().operation_id).toBe('edit-1')
        sentUndo = true
        latest = undo
        return route.fulfill({ json: undo })
      }
      return route.fulfill({ json: { target: id === 'followup'
        ? { operation_id: 'edit-1', path: 'note.txt', tool_name: 'edit_file' } : null } })
    }
    return route.abort()
  })

  await page.goto('/?run=original')
  const skillButton = page.getByRole('button', { name: '技能', exact: true })
  const menu = page.getByRole('region', { name: '技能', exact: true })
  await skillButton.hover()
  await expect(menu).toBeVisible()
  await menu.getByRole('button', { name: '打开 Skills 库' }).click()
  const catalog = page.getByRole('dialog', { name: 'Skills 库' })
  const trust = catalog.getByRole('checkbox', { name: /信任此工作区/ })
  await trust.click()
  await expect(trust).toBeChecked()
  await catalog.getByRole('button', { name: '关闭', exact: true }).click()
  await skillButton.hover()
  await menu.getByRole('searchbox', { name: '搜索技能' }).fill('project')
  await menu.getByRole('button', { name: /project-checks/ }).click()
  await expect(menu.getByRole('button', { name: '移除技能 project-checks' })).toBeVisible()
  await menu.getByRole('searchbox', { name: '搜索技能' }).press('Escape')
  await expect(menu).toBeHidden()
  await page.getByRole('textbox', { name: '任务说明' }).fill('结合证据复核')
  await page.getByRole('button', { name: '发送追问' }).click()
  await page.getByRole('button', { name: /执行过程/ }).click()
  await expect(page.getByText('复核续接证据', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '运行详情' }).click()
  await page.getByRole('button', { name: '日志', exact: true }).click()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载离线回放' }).click()
  expect((await download).suggestedFilename()).toBe('stellarweaver-followup.html')
  await page.getByRole('button', { name: '改动', exact: true }).click()
  await page.getByRole('button', { name: '撤销最近一次文件修改' }).click()
  await expect(page.getByText('note.txt', { exact: true })).toBeVisible()
  expect(sentUndo).toBe(false)
  await page.getByRole('button', { name: '确认撤销' }).click()
  await expect(page.getByText('已撤销改动')).toBeVisible()
  expect(sentUndo).toBe(true)
  await page.getByRole('button', { name: '审计与用量' }).click()
  const quota = page.getByRole('region', { name: '我的周额度' })
  await expect(quota).toContainText('310 / 10,000,000 tokens')
  await expect(quota).toContainText('2026-11-16')
})
