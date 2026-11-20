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

export function ExperimentPanel({ token, onClose }: { token: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [report, setReport] = useState<Experiment | null>(null)
  const [tab, setTab] = useState<Tab>('概要')
  const [error, setError] = useState('')

  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => {
    const abort = new AbortController()
    void api<Experiment>(token, 'experiment', { signal: abort.signal }).then(data => {
      if (!abort.signal.aborted) setReport(data)
    }).catch(err => {
      if (!abort.signal.aborted) setError(err instanceof Error ? err.message : String(err))
    })
    return () => abort.abort()
  }, [token])

  function download(data: Experiment) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = data.instance_id + '-experiment.json'
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  const outcome = report?.outcome
  const candidate = report?.grading.runs.find(run => run.id === 'candidate')
  return <dialog className="admin-panel experiment-panel" aria-label="实验记录" ref={dialog} onCancel={onClose}>
    <header><h2>实验记录</h2><div className="panel-actions">
      {report && <button type="button" onClick={() => download(report)}>下载完整 JSON</button>}
      <button type="button" onClick={onClose}>关闭</button>
    </div></header>
    {error && <p className="notice" role="alert">{error}</p>}
    {!error && !report && <p className="muted" role="status">正在读取实验记录…</p>}
    {report && outcome && <>
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
    </>}
  </dialog>
}
