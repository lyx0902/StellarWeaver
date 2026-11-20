import { useState } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Approval, Run, RunEvent } from './api'

const markdownComponents: Components = {
  table: ({ children }) => <div className="answer-table" role="region" aria-label="回复表格" tabIndex={0}>
    <table>{children}</table>
  </div>,
  th: ({ node: _node, ...props }) => <th scope="col" {...props} />,
}

export function Answer({ text }: { text: string }) {
  return <div className="answer">
    <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}
      skipHtml disallowedElements={['img']}>{text}</Markdown>
  </div>
}

const stateNames: Record<string, string> = {
  active: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消',
  interrupted: '已中断', blocked: '已阻止', budget_exceeded: '预算已耗尽',
}

export function runLabel(status: string) {
  return stateNames[status] ?? status
}

const eventNames: Record<string, string> = {
  'run.claimed': '领取任务', 'run.started': '开始执行', 'run.completed': '运行结束',
  'model.started': '请求模型', 'model.completed': '收到模型回复',
  'model.failed': '模型请求失败', 'model.retry_scheduled': '等待重试',
  'context.compacted': '压缩上下文', 'approval.requested': '请求审批',
  'approval.decided': '收到审批决定', 'approval.expired': '审批已过期',
  'review.applying': '正在应用补丁', 'review.applied': '已应用补丁',
  'verification.started': '开始验证', 'verification.completed': '验证完成',
}

export function eventLabel(event: RunEvent) {
  return event.payload.tool_name ?? eventNames[event.type] ?? event.type
}

function Process({ events, settled, onLogs }: {
  events: RunEvent[]; settled: boolean; onLogs: () => void
}) {
  const [choice, setChoice] = useState<{ settled: boolean; open: boolean }>()
  const open = choice?.settled === settled ? choice.open : !settled
  if (!events.length) return null
  const tools = events.filter(event => event.type === 'tool.completed').length
  return <section className="process" aria-label="执行过程">
    <button className="process-toggle" type="button" aria-expanded={open}
      onClick={() => setChoice({ settled, open: !open })}>
      <span aria-hidden="true">{open ? '▾' : '▸'}</span>
      <span>执行过程{tools ? ' · ' + tools + ' 次工具调用' : ''}</span>
      <span className="muted">{settled ? '已结束' : '进行中'}</span>
    </button>
    {open && <div className="process-body">
      <ul>
        {events.map(event => <li key={event.seq}>
          {event.type === 'assistant.completed'
            ? <Answer text={event.payload.content ?? ''} />
            : <><strong>{eventLabel(event)}</strong>{event.payload.status && <span>{event.payload.status}</span>}
              {event.payload.code && <code>{event.payload.code}</code>}
              {event.payload.content && <details><summary>查看工具输出</summary><pre>{event.payload.content}</pre></details>}</>}
        </li>)}
      </ul>
      <button className="quiet" type="button" onClick={onLogs}>查看完整日志</button>
    </div>}
  </section>
}

export function ApprovalCard({ item, busy, onDecision }: {
  item: Approval; busy: boolean; onDecision: (decision: 'approve' | 'deny') => void
}) {
  return <section className="approval" aria-label="待审批操作">
    <h3>待审批操作</h3>
    <p><code>{item.tool}</code> {item.path}</p>
    <pre>{item.diff ?? JSON.stringify(item.argv)}</pre>
    {item.argv && <p>工作目录 {item.cwd} · 超时 {item.timeout_seconds} 秒</p>}
    {(item.before_sha256 || item.after_sha256) && <details><summary>文件哈希</summary>
      {item.before_sha256 && <p>原始 <code>{item.before_sha256}</code></p>}
      {item.after_sha256 && <p>预期 <code>{item.after_sha256}</code></p>}
    </details>}
    <div className="actions">
      <button className="primary" type="button" disabled={busy} onClick={() => onDecision('approve')}>批准一次</button>
      <button type="button" disabled={busy} onClick={() => onDecision('deny')}>拒绝</button>
      <span className="muted">有效至 {new Date(item.expires_at).toLocaleTimeString()}</span>
    </div>
  </section>
}

export function Turn({ run, events, focused, approval, hasReport, onOpen, onLogs, onReview, onChecks }: {
  run: Run; events?: RunEvent[]; focused: boolean; approval?: React.ReactNode; hasReport: boolean
  onOpen: () => void; onLogs: () => void; onReview: () => void; onChecks: () => void
}) {
  const final = events?.filter(event =>
    event.type === 'assistant.completed' && !event.payload.has_tools && Boolean(event.payload.content)).at(-1)
  const interim = events?.filter(event => {
    if (event.type === 'assistant.completed') return event !== final && Boolean(event.payload.content)
    return ['tool.completed', 'model.started', 'model.failed', 'context.compacted',
      'approval.requested', 'approval.decided', 'approval.expired', 'review.applied',
      'verification.started', 'verification.completed'].includes(event.type)
  }) ?? []
  const result = final?.payload.content ?? run.final_text
  let live = ''
  for (const event of events ?? []) {
    if (event.type === 'assistant.delta') live = (live + (event.payload.content ?? '')).slice(-24000)
    if (['assistant.completed', 'model.started', 'model.failed', 'model.interrupted'].includes(event.type)) live = ''
  }
  return <article className="turn" aria-label="对话轮次">
    <div className="turn-user"><span>{run.is_review ? '补丁验证' : '你'}</span>
      <div className="user-text">{run.is_review ? '在独立工作区应用所选补丁并验证' : run.prompt}</div>
    </div>
    <div className="turn-agent">
      <div className="agent-label">
        <span className="agent-mark" aria-hidden="true">✦</span><strong>StellarWeaver</strong>
        <span className={'status status-' + run.status}>{runLabel(run.status)}</span>
      </div>
      {focused && <Process events={interim} settled={Boolean(final) || run.status !== 'active'} onLogs={onLogs} />}
      {approval}
      {result && <Answer text={result} />}
      {live && <div className="live-answer" aria-label="实时回复"><Answer text={live} /></div>}
      {focused && !result && !live && run.status === 'active' && !approval &&
        <p className="wait-state" role="status">{run.job_state === 'queued' ? '已排队，等待执行' : '正在处理任务…'}</p>}
      {focused && run.outcome?.error_code && <p className="run-warning">
        {run.outcome.error_code === 'RUNNER_CLEANUP_UNCONFIRMED' ?
          '执行服务未确认验证结果及清理状态。' : <code>{run.outcome.error_code}</code>}
      </p>}
      {focused && run.status === 'interrupted' && <section className="run-warning">
        <strong>运行已中断</strong>
        {Boolean(run.outcome?.unfinished_tools) && <p>{run.outcome?.unfinished_tools} 项工具结果未确认</p>}
        {Boolean(run.unconfirmed_operations?.length) && <ul aria-label="未确认的操作">
          {run.unconfirmed_operations?.map(operation => <li key={operation.operation_id}>
            {operation.tool_name} <code>{operation.path ?? operation.argv?.join(' ')}</code>
          </li>)}
        </ul>}
      </section>}
      <div className="turn-actions">
        {!focused && <button type="button" className="quiet" onClick={onOpen}>查看本轮过程</button>}
        {hasReport && <button type="button" onClick={onReview}>查看改动</button>}
        {hasReport && <button type="button" className="quiet" onClick={onChecks}>查看验证</button>}
      </div>
    </div>
  </article>
}

export function ContextMeter({ events, limit }: { events: RunEvent[]; limit: number }) {
  const started = events.filter(event => event.type === 'model.started').at(-1)
  const completed = events.filter(event =>
    event.type === 'model.completed' && event.payload.request_index === started?.payload.request_index).at(-1)
  const payload = completed?.payload ?? started?.payload
  const used = payload?.input_tokens
  const window = started?.payload.context_window_tokens ?? limit
  const estimated = payload?.input_estimated ?? payload?.estimated ?? true
  const formatted = (n: number) => n.toLocaleString('zh-CN')
  const label = used === undefined ? '等待统计' : (estimated ? '约 ' : '') + formatted(used)
  const ratio = used === undefined ? 0 : Math.min(100, (used / window) * 100)
  const help = '最近一次模型请求的输入 token，包含系统指令、工具和已投影的对话。发送前按估算显示，收到模型用量后更新。窗口含预留的输出空间。'
  return <span className="context-meter" data-testid="context-usage" title={help}
    aria-label={'上下文用量 ' + label + ' / ' + formatted(window) + ' tokens'} tabIndex={0}>
    <span className="context-ring" aria-hidden="true"
      style={{ background: 'conic-gradient(var(--accent) ' + ratio + '%, var(--line) 0)' }} />
    <span>上下文 <b>{label}</b> / {formatted(window)}</span>
  </span>
}
