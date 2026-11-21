import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api, type RunEvent } from './api'
import { eventLabel, runLabel } from './conversation'

type Tool = { call_id: string; tool_name: string; state: string; result?: { status: string } | null }
type TestCounts = { success: string[]; failure: string[] }
type Score = {
  resolved: boolean
  tests_status: { FAIL_TO_PASS: TestCounts; PASS_TO_PASS: TestCounts }
}
type Grading = { id: string; label: string; score: Score; log: string }
type ReproductionCheck = {
  status: 'passed' | 'failed' | 'unavailable'; reason: string; evidence: unknown
}
type Reproduction = {
  status: ReproductionCheck['status'] | 'not_run'; reason: string
  baseline: ReproductionCheck; checks: (ReproductionCheck & { attempt: number })[]
}
type Experiment = {
  instance_id: string; base_commit: string; problem_statement: string; model: string
  session: { session_id: string; created_at: string }
  run_id: string
  outcome: {
    run_status: string; final_text: string; model_calls: number; tool_calls: number
    input_tokens: number; output_tokens: number; duration_ms: number
  }
  events: (RunEvent & { timestamp: string })[]
  tools: Tool[]
  messages: { position: number; body: { role: string } }[]
  candidate_patch: string
  grading: { environment: string; runs: Grading[] }
  preflight: unknown
  project_tests?: { status: 'passed' | 'failed' | 'not_run'; result?: unknown }
  reproduction?: Reproduction
}

const tabs = ['概要', '工具调用', '完整事件', '会话消息', '补丁与评分'] as const
type Tab = typeof tabs[number]
const reproductionLabels = {
  passed: '复现通过', failed: '复现仍失败', unavailable: '无法验证', not_run: '未运行',
}
const projectTestLabels = { passed: '通过', failed: '失败', not_run: '未运行' }

function Evidence({ title, data }: { title: ReactNode; data: unknown }) {
  return <details className="experiment-evidence">
    <summary>{title}</summary>
    <pre>{typeof data === 'string' ? data : JSON.stringify(data, null, 2)}</pre>
  </details>
}

function passed(tests: TestCounts) {
  return `${tests.success.length} / ${tests.success.length + tests.failure.length}`
}

function LegacyDetail({ report }: { report: Experiment }) {
  const [tab, setTab] = useState<Tab>('概要')
  const outcome = report.outcome
  const candidate = report.grading?.runs.find(run => run.id === 'candidate')
  if (!outcome || !report.grading || !report.session) {
    return <Evidence title="历史记录" data={report} />
  }
  return <>
      <h3>{report.instance_id}</h3>
      <p className="muted">SWE-bench Lite 开发集 · {report.model} · {new Date(report.session.created_at).toLocaleString()}</p>
      <nav className="experiment-tabs" aria-label="实验记录视图">
        {tabs.map(label => <button type="button" key={label} aria-pressed={tab === label}
          onClick={() => setTab(label)}>{label}</button>)}
      </nav>
      {tab === '概要' && <section aria-label="实验概要">
        <p>{runLabel(outcome.run_status)} · {(outcome.duration_ms / 1000).toFixed(1)} 秒 · {outcome.model_calls} 次模型请求 · {outcome.tool_calls} 次工具调用</p>
        {candidate && <p>独立评分 · {candidate.score.resolved ? '通过' : '未通过'} · 缺陷测试 {passed(candidate.score.tests_status.FAIL_TO_PASS)}</p>}
        {report.project_tests && <p>常规测试 · {projectTestLabels[report.project_tests.status]}</p>}
        {report.reproduction && <p>缺陷复现 · {reproductionLabels[report.reproduction.status]}</p>}
        <p className="muted">累计输入 {outcome.input_tokens.toLocaleString()} tokens · 累计输出 {outcome.output_tokens.toLocaleString()} tokens</p>
        <Evidence title="原始题目" data={report.problem_statement} />
        <Evidence title="Agent 最终回答" data={outcome.final_text} />
        <Evidence title="运行标识" data={{ base_commit: report.base_commit, session_id: report.session.session_id, run_id: report.run_id }} />
      </section>}
      {tab === '工具调用' && <section aria-label="工具调用">
        {report.tools.map((tool, index) => <Evidence key={tool.call_id}
          title={<><code>{index + 1}</code> {tool.tool_name} · {tool.result?.status ?? tool.state}</>} data={tool} />)}
      </section>}
      {tab === '完整事件' && <section aria-label="完整事件">
        {report.events.map(event => <Evidence key={event.seq}
          title={<><code>#{event.seq}</code> {eventLabel(event)} · <span className="muted">{event.type}</span></>}
          data={event} />)}
      </section>}
      {tab === '会话消息' && <section aria-label="会话消息">
        {report.messages.map(message => <Evidence key={message.position}
          title={`#${message.position} ${message.body.role}`} data={message.body} />)}
      </section>}
      {tab === '补丁与评分' && <section aria-label="补丁与评分">
        <p className="muted">{report.grading.environment}</p>
        <table><thead><tr><th scope="col">对照</th><th scope="col">缺陷回归测试</th><th scope="col">原有回归检查</th></tr></thead>
          <tbody>{report.grading.runs.map(run => <tr key={run.id}>
            <th scope="row">{run.label}</th><td>{passed(run.score.tests_status.FAIL_TO_PASS)}</td>
            <td>{passed(run.score.tests_status.PASS_TO_PASS)}</td>
          </tr>)}</tbody></table>
        <Evidence title="候选补丁" data={report.candidate_patch} />
        {report.project_tests?.result !== undefined &&
          <Evidence title="候选补丁 · 常规测试" data={report.project_tests.result} />}
        {report.reproduction && <>
          <h3>缺陷复现</h3>
          <p>{reproductionLabels[report.reproduction.status]}</p>
          <Evidence title="改动前 · 基线复现" data={report.reproduction.baseline} />
          {report.reproduction.checks.map(check => <Evidence key={check.attempt}
            title={'改动后 · 第 ' + check.attempt + ' 次检查 · ' + reproductionLabels[check.status]}
            data={check} />)}
        </>}
        <Evidence title="项目测试预检" data={report.preflight} />
        {report.grading.runs.map(run => <Evidence key={run.id} title={run.label + ' · 评分日志'} data={run.log} />)}
      </section>}
  </>
}

type Kind = 'swebench' | 'security' | 'legacy'
type Entry = { id: string; kind: Kind; title: string; sha256: string; split?: string; planned: number }
type Catalog = { reports: Entry[]; unavailable: string[] }
type Attempt = {
  attempt_id: string; run_status: string; candidate_status: string; patch_sha256?: string
  failure_category?: string
  model_calls?: number; tool_calls?: number; input_tokens?: number; output_tokens?: number
  duration_ms?: number; usage_source: string; evidence: unknown[]
}
type Task = {
  instance_id: string; latest_attempt_id?: string; latest_score_id?: string; attempts: Attempt[]
  scores: { evaluation_run_id: string; attempt_id?: string; evaluation_status: string; resolved?: boolean }[]
  evidence: unknown[]
}
type Batch = {
  manifest: { split: string; suite_revision?: string; revision_verified: boolean; grader_version: string; frozen_config_sha256: string }
  aggregate: {
    graded: number; resolved_rate: number
    by_evaluation_status: Record<string, number>
    by_run_status: Record<string, number>
    by_candidate_status: Record<string, number>
  }
  tasks: Task[]
}
type Security = {
  manifest: { model: string; fixture_sha256: string; oracle_source_sha256: string }
  security: { planned: number; boundary_checks_passed: number; unauthorized_effects: number }
  utility: { planned: number; passed: number }
  tool_efficiency: { tool_calls: number; model_calls: number; approvals: number }
  cases: { instance_id: string; kind: string; passed: boolean; unsafe_effect: boolean; tool_calls: number; approvals: number }[]
}
type Detail = { id: string; title: string; sha256: string } & (
  { kind: 'legacy'; report: Experiment } | { kind: 'swebench'; report: Batch } | { kind: 'security'; report: Security }
)
type Comparison = {
  paired_graded: number; common_planned: number; pairs: Record<string, number>
  excluded: Record<string, string>; control_differences: string[]
  left_plan_only: string[]; right_plan_only: string[]
}

const kinds: Record<Kind, string> = { swebench: 'SWE-bench Lite', security: '安全边界夹具', legacy: '历史轨迹' }
const titles: Record<string, string> = {
  'swebench-three-saved': '三题历史评分', 'swebench-dev23-plan': '开发集 23 题清单',
  'security-m2-baseline': '边界夹具 · 初始基线', 'security-m2-verified': '边界夹具 · 验收结果',
}
const evaluations: Record<string, string> = {
  resolved: '通过', unresolved: '未通过', not_evaluated: '未评分', evaluation_error: '评分错误',
}
const candidates: Record<string, string> = {
  valid_patch: '有效补丁', empty_patch: '空补丁', invalid_patch: '无效补丁', not_started: '未开始',
}
const usageLabels: Record<string, string> = {
  reported: '服务端统计', estimated: '估算', incomplete: '统计不完整', unavailable: '无用量数据',
}
const tokenNumber = new Intl.NumberFormat('zh-CN')
const caseKinds: Record<string, string> = {
  protected: '保护路径', egress: '网络外发', public: '正常搜索', repair: '冲突恢复',
}

function Metrics({ items }: { items: [string, string | number][] }) {
  return <dl className="experiment-metrics">{items.map(([name, value]) =>
    <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
}

function BatchDetail({ report }: { report: Batch }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('')
  const { aggregate, manifest, tasks } = report
  const rows = tasks.map(task => {
    const attempt = task.attempts.find(item => item.attempt_id === task.latest_attempt_id)
    const score = attempt && task.scores.find(item =>
      item.evaluation_run_id === task.latest_score_id && item.attempt_id === attempt.attempt_id)
    return { task, attempt, state: score?.evaluation_status ?? 'not_evaluated' }
  }).filter(({ task, state }) =>
    task.instance_id.toLowerCase().includes(query.trim().toLowerCase()) && (!filter || state === filter))
  return <>
    <Metrics items={[
      ['计划样本', tasks.length], ['独立已评分', aggregate.graded],
      ['独立通过', aggregate.by_evaluation_status.resolved ?? 0],
      ['通过 / 计划', aggregate.graded ? `${(aggregate.resolved_rate * 100).toFixed(1)}%` : '—'],
    ]} />
    <p className="muted">{manifest.split === 'dev' ? '开发集' : '测试集'} · 评分器 {manifest.grader_version}
      {' · '}{manifest.revision_verified ? '已校验数据修订' : '数据修订未校验'}
      {!aggregate.graded && ' · 尚无独立评分'}</p>
    <div className="experiment-controls">
      <input aria-label="按题号搜索" placeholder="按题号搜索" value={query} onChange={e => setQuery(e.target.value)} />
      <select aria-label="筛选评分状态" value={filter} onChange={e => setFilter(e.target.value)}>
        <option value="">全部评分状态</option>
        {Object.entries(evaluations).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
    </div>
    <div className="experiment-table" role="region" aria-label="样本结果" tabIndex={0}>
      <table><thead><tr><th scope="col">题号</th><th scope="col">运行</th>
        <th scope="col">候选</th><th scope="col">独立评分</th><th scope="col">工具调用</th>
        <th scope="col">耗时</th><th scope="col">输入 / 输出 Token</th></tr></thead>
        <tbody>{rows.map(({ task, attempt, state }) => <tr key={task.instance_id}>
          <td><Evidence title={<code>{task.instance_id}</code>} data={task} /></td>
          <td>{attempt ? runLabel(attempt.run_status) : '未开始'}
            {attempt?.failure_category && <div className="muted"><code>{attempt.failure_category}</code></div>}</td>
          <td>{attempt ? candidates[attempt.candidate_status] ?? attempt.candidate_status : '—'}</td>
          <td>{evaluations[state] ?? state}</td>
          <td>{attempt?.tool_calls ?? '—'}</td>
          <td>{attempt?.duration_ms != null ? `${(attempt.duration_ms / 1000).toFixed(1)} 秒` : '—'}</td>
          <td>{attempt?.input_tokens == null && attempt?.output_tokens == null ? '—' : <>
            {attempt?.input_tokens == null ? '—' : tokenNumber.format(attempt.input_tokens)} / {attempt?.output_tokens == null ? '—' : tokenNumber.format(attempt.output_tokens)}
            {attempt?.usage_source && <div className="muted">{usageLabels[attempt.usage_source]}</div>}
          </>}</td>
        </tr>)}</tbody></table>
      {!rows.length && <p className="muted">当前筛选没有匹配样本。</p>}
    </div>
    <Evidence title="冻结配置与证据来源" data={manifest} />
  </>
}

function SecurityDetail({ report }: { report: Security }) {
  return <>
    <Metrics items={[
      ['边界检查通过', `${report.security.boundary_checks_passed} / ${report.security.planned}`],
      ['正常任务通过', `${report.utility.passed} / ${report.utility.planned}`],
      ['未授权副作用', report.security.unauthorized_effects], ['人工批准', report.tool_efficiency.approvals],
    ]} />
    <p className="muted">{report.manifest.model} · {report.tool_efficiency.tool_calls} 次工具调用</p>
    <div className="experiment-table" role="region" aria-label="边界和正常任务结果" tabIndex={0}><table>
      <thead><tr><th scope="col">场景</th><th scope="col">类别</th><th scope="col">独立判定</th><th scope="col">副作用</th></tr></thead>
      <tbody>{report.cases.map(row => <tr key={row.instance_id}>
        <td><Evidence title={<code>{row.instance_id}</code>} data={row} /></td>
        <td>{caseKinds[row.kind] ?? row.kind}</td><td>{row.passed ? '通过' : '未通过'}</td>
        <td>{row.unsafe_effect ? '已观察到' : '未观察到'}</td>
      </tr>)}</tbody>
    </table></div>
    <Evidence title="夹具与判定代码哈希" data={report.manifest} />
  </>
}

function download(detail: Detail) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(detail, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = detail.id + '-experiment.json'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

export function ExperimentPanel({ token, onClose }: { token: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<Detail | null>(null)
  const [kind, setKind] = useState<Kind | ''>('')
  const [revision, refresh] = useState(0)
  const [error, setError] = useState('')
  const [comparison, setComparison] = useState<Comparison | null>(null)
  const [compareError, setCompareError] = useState('')
  const [compareWith, setCompareWith] = useState('')
  const [variation, setVariation] = useState('')
  const [comparing, setComparing] = useState(false)
  const compareRequest = useRef<AbortController | null>(null)

  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => () => compareRequest.current?.abort(), [])
  useEffect(() => {
    const abort = new AbortController()
    setError('')
    void api<Catalog>(token, 'experiments', { signal: abort.signal }).then(data => {
      if (abort.signal.aborted) return
      setCatalog(data)
      setSelected(previous => data.reports.some(entry => entry.id === previous)
        ? previous : data.reports.find(entry => entry.id === 'swebench-three-saved')?.id
          ?? data.reports.find(entry => entry.kind === 'swebench')?.id ?? data.reports[0]?.id ?? '')
    }).catch(err => { if (!abort.signal.aborted) setError(String(err.message ?? err)) })
    return () => abort.abort()
  }, [token, revision])
  useEffect(() => {
    setDetail(null)
    if (!selected) return
    const abort = new AbortController()
    setError('')
    void api<Detail>(token, 'experiments/' + encodeURIComponent(selected), { signal: abort.signal })
      .then(data => { if (!abort.signal.aborted) setDetail(data) })
      .catch(err => { if (!abort.signal.aborted) setError(String(err.message ?? err)) })
    return () => abort.abort()
  }, [token, selected, revision])

  function resetComparison() {
    compareRequest.current?.abort()
    setComparison(null)
    setCompareError('')
    setComparing(false)
  }

  async function compareReports() {
    resetComparison()
    const abort = new AbortController()
    compareRequest.current = abort
    setComparing(true)
    const query = new URLSearchParams({ left: selected, right: compareWith, vary: variation })
    try {
      const result = await api<Comparison>(token, 'experiments/compare?' + query, { signal: abort.signal })
      if (!abort.signal.aborted) setComparison(result)
    } catch (err) {
      if (!abort.signal.aborted) setCompareError(err instanceof Error ? err.message : String(err))
    } finally {
      if (!abort.signal.aborted) setComparing(false)
    }
  }

  const entries = catalog?.reports.filter(entry => !kind || entry.kind === kind) ?? []
  const alternatives = catalog?.reports.filter(entry => entry.kind === 'swebench' && entry.id !== selected) ?? []
  return <dialog className="admin-panel experiment-panel" aria-label="实验记录" ref={dialog} onCancel={onClose}>
    <header><h2>实验记录</h2><div className="panel-actions">
      <button type="button" onClick={() => { resetComparison(); refresh(n => n + 1) }}>刷新</button>
      <button type="button" onClick={onClose}>关闭</button>
    </div></header>
    {error && <p className="notice" role="alert">{error}</p>}
    {!error && !catalog && <p className="muted" role="status">正在读取实验记录…</p>}
    {catalog && <div className="experiment-layout">
      <aside className="experiment-index" aria-label="实验列表">
        <select aria-label="筛选实验类型" value={kind} onChange={event => {
          const next = event.target.value as Kind | ''
          setKind(next); resetComparison(); setCompareWith('')
          const available = catalog.reports.filter(entry => !next || entry.kind === next)
          if (!available.some(entry => entry.id === selected)) setSelected(available[0]?.id ?? '')
        }}>
          <option value="">全部记录</option>
          {Object.entries(kinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        {entries.map(entry => <button type="button" key={entry.id} aria-pressed={entry.id === selected}
          onClick={() => { resetComparison(); setCompareWith(''); setSelected(entry.id) }}>
          <strong>{titles[entry.id] ?? entry.title}</strong>
          <small>{kinds[entry.kind]} · {entry.planned} 项</small>
        </button>)}
        {!entries.length && <p className="muted">没有可显示的记录。</p>}
        {catalog.unavailable.length > 0 && <p className="notice">无法读取 {catalog.unavailable.length} 份已发布记录。</p>}
      </aside>
      <div className="experiment-detail">
        {selected && !detail && !error && <p className="muted" role="status">正在读取所选实验…</p>}
        {detail && detail.id === selected && <section>
          <div className="experiment-heading">
            <div><small className="muted">{kinds[detail.kind]}</small><h3>{titles[detail.id] ?? detail.title}</h3></div>
            <button type="button" onClick={() => download(detail)}>下载脱敏 JSON</button>
          </div>
          {detail.kind === 'legacy' && <LegacyDetail key={detail.id} report={detail.report} />}
          {detail.kind === 'security' && <SecurityDetail report={detail.report} />}
          {detail.kind === 'swebench' && <>
            <BatchDetail key={detail.id} report={detail.report} />
            {alternatives.length > 0 && <section className="experiment-compare" aria-label="配对对照">
              <h3>配对对照</h3>
              <div className="experiment-controls">
                <select aria-label="选择对照实验" value={compareWith} onChange={e => { resetComparison(); setCompareWith(e.target.value) }}>
                  <option value="">选择对照实验</option>
                  {alternatives.map(entry => <option key={entry.id} value={entry.id}>{titles[entry.id] ?? entry.title}</option>)}
                </select>
                <select aria-label="声明对照变量" value={variation} onChange={e => { resetComparison(); setVariation(e.target.value) }}>
                  <option value="">相同控制条件</option><option value="source_sha256">允许源码变化</option>
                  <option value="config_sha256">允许配置变化</option><option value="prompt_sha256">允许提示变化</option>
                  <option value="tools_schema_sha256">允许工具定义变化</option>
                  <option value="config_sha256,tools_schema_sha256">允许配置与工具定义同时变化</option>
                </select>
                <button type="button" disabled={!compareWith || comparing} onClick={() => void compareReports()}>
                  {comparing ? '正在对照…' : '按题号对照'}
                </button>
              </div>
              {compareError && <p role="alert" className="notice">{compareError}</p>}
              {comparison && <>
                <Metrics items={[
                  ['共同计划', comparison.common_planned], ['可比已评分', comparison.paired_graded],
                  ['仅当前通过', comparison.pairs.left_only_resolved], ['仅对照通过', comparison.pairs.right_only_resolved],
                ]} />
                {comparison.paired_graded === 0 && <p className="muted">目前没有达到配对条件的已评分样本。</p>}
                <Evidence title="条件差异与排除原因" data={comparison} />
              </>}
            </section>}
          </>}
          <p className="experiment-digest muted">源记录 SHA-256 <code>{detail.sha256}</code></p>
        </section>}
      </div>
    </div>}
  </dialog>
}
