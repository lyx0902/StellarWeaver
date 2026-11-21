import { expect, test } from '@playwright/test'

test('experiment dashboard keeps planned denominators and explains excluded pairs', async ({ page }) => {
  const batch = {
    manifest: { split: 'dev', grader_version: 'fixture', revision_verified: true },
    aggregate: { graded: 1, resolved_rate: 0.5, by_evaluation_status: { resolved: 1 } },
    tasks: [
      {
        instance_id: 'fixture__a-1', latest_attempt_id: 'first', latest_score_id: 'grade',
        scores: [{ evaluation_run_id: 'grade', attempt_id: 'first', evaluation_status: 'resolved' }],
        attempts: [{ attempt_id: 'first', run_status: 'completed', duration_ms: 1234, input_tokens: 1234, output_tokens: 50, usage_source: 'reported' }],
      },
      {
        instance_id: 'fixture__b-2', latest_attempt_id: 'retry', latest_score_id: 'old-grade',
        scores: [{ evaluation_run_id: 'old-grade', attempt_id: 'old', evaluation_status: 'resolved' }],
        attempts: [{ attempt_id: 'old' }, { attempt_id: 'retry', run_status: 'budget_exceeded', failure_category: 'tool_budget' }],
      },
    ],
  }
  const rows = [
    { id: 'baseline', title: '基线', kind: 'swebench', planned: 2 },
    { id: 'candidate', title: '候选', kind: 'swebench', planned: 2 },
    { id: 'safety', title: '安全验证', kind: 'security', planned: 1 },
  ]
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1/', '')
    if (path === 'auth/config') return route.fulfill({ json: { bearer: true, oidc: false } })
    if (!route.request().headers().authorization) return route.fulfill({ status: 401, json: {} })
    if (path === 'workspaces') return route.fulfill({ json: {
      provider: 'fake', repair_enabled: false, context_window_tokens: 100000,
      workspaces: [], experiment_available: true,
    } })
    if (path === 'threads') return route.fulfill({ json: [] })
    if (path === 'experiments') return route.fulfill({ json: { reports: rows, unavailable: [] } })
    if (path === 'experiments/compare') return route.fulfill({ json: {
      paired_graded: 0, common_planned: 2, pairs: { left_only_resolved: 0, right_only_resolved: 0 },
      excluded: { 'fixture__a-1': 'different_manifest_controls' }, control_differences: ['limits'],
      left_plan_only: [], right_plan_only: [],
    } })
    const entry = rows.find(row => path === 'experiments/' + row.id)
    if (!entry) return route.fulfill({ status: 404, json: {} })
    return route.fulfill({ json: {
      ...entry, sha256: 'a'.repeat(64),
      report: entry.kind === 'security' ? {
        manifest: { model: 'FakeModel' },
        security: { planned: 1, boundary_checks_passed: 1, unauthorized_effects: 0 },
        utility: { planned: 0, passed: 0 },
        tool_efficiency: { tool_calls: 1, model_calls: 2, approvals: 0 },
        cases: [{ instance_id: 'guard-fixture', kind: 'protected', passed: true, unsafe_effect: false }],
      } : batch,
    } })
  })
  await page.goto('/')
  await page.getByLabel('访问令牌').fill('fixture-token')
  await page.getByRole('button', { name: '连接', exact: true }).click()
  await page.getByRole('button', { name: '实验记录', exact: true }).click()
  const panel = page.getByRole('dialog', { name: '实验记录' })
  await expect(panel).toContainText('50.0%')
  await expect(panel).toContainText('1.2 秒')
  await expect(panel).toContainText('1,234 / 50')
  await expect(panel).toContainText('服务端统计')
  await panel.getByLabel('筛选评分状态').selectOption('not_evaluated')
  await expect(panel.getByRole('region', { name: '样本结果' })).toContainText('fixture__b-2')
  await expect(panel.getByRole('region', { name: '样本结果' })).toContainText('tool_budget')
  await expect(panel.getByRole('region', { name: '样本结果' })).not.toContainText('fixture__a-1')
  await panel.getByLabel('选择对照实验').selectOption('candidate')
  await panel.getByRole('button', { name: '按题号对照' }).click()
  await expect(panel).toContainText('没有达到配对条件')
  await panel.getByText('条件差异与排除原因').click()
  await expect(panel).toContainText('different_manifest_controls')
  await panel.getByRole('button', { name: /安全验证/ }).click()
  await expect(panel).toContainText('未授权副作用')
  await expect(panel).toContainText('guard-fixture')
})
