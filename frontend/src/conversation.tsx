import { useState } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Approval, ApprovalDecision, Run, RunEvent } from './api'

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
  'repair.diagnostic': '修复诊断', 'harness.profile': '运行配置',
  'task.evidence': '任务证据', 'budget.guidance': '预算提示',
  'skill.selected': '指定技能', 'context.prior_evidence': '复核续接证据',
  'changes.undo.started': '开始撤销文件修改', 'changes.undo.completed': '已撤销文件修改',
  'exploration.started': '开始子 Agent 探索', 'exploration.completed': '子 Agent 探索',
}

export function eventLabel(event: RunEvent) {
  if (event.type === 'exploration.tool') {
    return event.payload.tool_name ? '子 Agent · ' + event.payload.tool_name : '子 Agent 工具'
  }
  if (event.payload.tool_name === 'delegate_exploration') return '子 Agent 探索结果'
  return event.payload.tool_name ?? eventNames[event.type] ?? event.type
}

const explorationStates = { completed: '完成', partial: '部分完成', cancelled: '已取消' }
const explorationReasons: Record<string, string> = {
  stalled: '连续多轮检索重复或失败', safety_cap: '触及子任务安全上限',
  parent_budget: '触及父任务剩余预算', weekly_quota: '工作台本周模型额度不足',
  context_window: '超出可用上下文窗口', exploration_failed: '探索未能完成',
  cancelled: '用户取消',
}

export function eventStatus(event: RunEvent) {
  if (event.type === 'exploration.tool') {
    if (event.payload.status) return event.payload.status
    return event.payload.event === 'tool.started' ? '开始' : undefined
  }
  const { completion_status, stop_reason, status } = event.payload
  if (!completion_status) return status
  const reason = stop_reason ? explorationReasons[stop_reason] : undefined
  return reason ? explorationStates[completion_status] + ' · ' + reason
    : explorationStates[completion_status]
}

type ProcessEntry = { kind: 'single'; event: RunEvent } | { kind: 'model-group'; events: RunEvent[] }

function groupProcessEvents(events: RunEvent[]): ProcessEntry[] {
  const entries: ProcessEntry[] = []
  let run: RunEvent[] = []
  const flush = () => {
    if (run.length >= 3) {
      entries.push({ kind: 'model-group', events: run })
    } else {
      for (const event of run) entries.push({ kind: 'single', event })
    }
    run = []
  }
  for (const event of events) {
    if (event.type === 'model.started') {
      run.push(event)
    } else {
      if (run.length) flush()
      entries.push({ kind: 'single', event })
    }
  }
  if (run.length) flush()
  return entries
}

function modelRequestDetail(event: RunEvent) {
  const parts: string[] = []
  if (typeof event.payload.request_index === 'number') parts.push('#' + event.payload.request_index)
  if (event.payload.purpose === 'delegate_exploration') {
    parts.push(event.payload.exploration_phase === 'finalize' ? '子 Agent 总结' : '子 Agent 检索')
  }
  if (typeof event.payload.input_tokens === 'number') {
    parts.push('估算输入 ' + event.payload.input_tokens.toLocaleString() + ' token')
  }
  return parts.join(' · ')
}

function Process({ events, settled, onLogs }: {
  events: RunEvent[]; settled: boolean; onLogs: () => void
}) {
  const [choice, setChoice] = useState<{ settled: boolean; open: boolean }>()
  const open = choice?.settled === settled ? choice.open : !settled
  if (!events.length) return null
  const tools = events.filter(event => event.type === 'tool.completed').length
  const entries = groupProcessEvents(events)
  return <section className="process" aria-label="执行过程">
    <button className="process-toggle" type="button" aria-expanded={open}
      onClick={() => setChoice({ settled, open: !open })}>
      <span aria-hidden="true">{open ? '▾' : '▸'}</span>
      <span>执行过程{tools ? ' · ' + tools + ' 次工具调用' : ''}</span>
      <span className="muted">{settled ? '已结束' : '进行中'}</span>
    </button>
    {open && <div className="process-body">
      <ul>
        {entries.map(entry => entry.kind === 'model-group' ? (
          <li key={'model-group-' + entry.events[0].seq}>
            <details>
              <summary>
                <strong>请求模型</strong>
                <span> · 连续 {entry.events.length} 次</span>
                {entry.events.some(item => item.payload.purpose === 'delegate_exploration') && (
                  <span className="muted">
                    （含 {entry.events.filter(item => item.payload.purpose === 'delegate_exploration').length} 次子 Agent）
                  </span>
                )}
              </summary>
              <ul>
                {entry.events.map(item => <li key={item.seq}>
                  <strong>{eventLabel(item)}</strong>
                  {modelRequestDetail(item) && <span className="muted">{modelRequestDetail(item)}</span>}
                </li>)}
              </ul>
            </details>
          </li>
        ) : (() => {
          const event = entry.event
          return <li key={event.seq}>
          {event.type === 'assistant.completed'
            ? <Answer text={event.payload.content ?? ''} />
            : <><strong>{eventLabel(event)}</strong>{eventStatus(event) && <span>{eventStatus(event)}</span>}
              {event.type === 'model.started' && modelRequestDetail(event) && (
                <span className="muted">{modelRequestDetail(event)}</span>
              )}
              {event.payload.skill_name && <span title={event.payload.skill_source}>{event.payload.skill_name}</span>}
              {event.type === 'context.prior_evidence' && <span>
                {event.payload.files ?? 0} 个文件 · {event.payload.checks ?? 0} 项检查 · {event.payload.skills ?? 0} 个技能版本
              </span>}
              {event.type === 'context.compacted' && event.payload.after_tokens_estimated != null && <span>
                {event.payload.strategy === 'adaptive_complete_rounds' && '自适应压缩 · '}
                {event.payload.before_tokens_estimated?.toLocaleString()} → {event.payload.after_tokens_estimated.toLocaleString()} token（估算）
              </span>}
              {(event.type === 'exploration.started' || event.type === 'exploration.completed' || event.payload.tool_name === 'delegate_exploration')
                && event.payload.paths?.length ? (
                <span>{event.payload.paths.map(path => <code key={path}>{path}</code>)}</span>
              ) : null}
              {event.type === 'exploration.started' && event.payload.goal && (
                event.payload.goal.length > 160 ? <details>
                  <summary>任务说明：{event.payload.goal.slice(0, 160)}…</summary>
                  <pre>{event.payload.goal}</pre>
                </details> : <div className="muted">任务说明：{event.payload.goal}</div>
              )}
              {(event.type === 'exploration.completed' || event.payload.tool_name === 'delegate_exploration')
                && !event.payload.paths?.length && event.payload.goal && (
                <span className="muted">{event.payload.goal.length > 80 ? event.payload.goal.slice(0, 80) + '…' : event.payload.goal}</span>
              )}
              {event.type.startsWith('changes.undo.') && event.payload.path && <code>{event.payload.path}</code>}
              {event.payload.code && <code>{event.payload.code}</code>}
              {event.payload.staged_patch && <button type="button" onClick={() => {
                const url = URL.createObjectURL(new Blob([event.payload.staged_patch ?? ''], { type: 'text/x-diff' }))
                const link = document.createElement('a')
                link.href = url
                link.download = 'stellarweaver-staged.diff'
                link.click()
                setTimeout(() => URL.revokeObjectURL(url), 0)
              }}>下载待审补丁</button>}
              {event.payload.sources?.length ? <details>
                <summary>查看搜索来源（{event.payload.sources.length}）</summary>
                <div className="search-sources">{event.payload.sources.map(source =>
                  <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>
                )}</div>
              </details> : event.payload.content && <details><summary>查看工具输出</summary><pre>{event.payload.content}</pre></details>}</>}
        </li>
        })())}
      </ul>
      <button className="quiet" type="button" onClick={onLogs}>查看完整日志</button>
    </div>}
  </section>
}

export function ApprovalCard({ item, busy, onDecision }: {
  item: Approval; busy: boolean; onDecision: (decision: ApprovalDecision) => void
}) {
  return <section className="approval" aria-label="待审批操作">
    <h3>待审批操作</h3>
    <p><code>{item.tool}</code> {item.path}</p>
    {item.paths && <p>{item.paths.join(' · ')}</p>}
    {item.patch_sha256 && <p>补丁 SHA-256 <code>{item.patch_sha256}</code></p>}
    {item.query && <p>以下查询将发送给 DeepSeek 网页搜索</p>}
    <pre>{item.query ?? item.diff ?? JSON.stringify(item.argv)}</pre>
    {item.security && <p>风险等级 {item.security.level} · {item.security.capabilities.join(' · ')}</p>}
    {item.argv && <p>工作目录 {item.cwd} · 超时 {item.timeout_seconds} 秒</p>}
    {(item.before_sha256 || item.after_sha256) && <details><summary>文件哈希</summary>
      {item.before_sha256 && <p>原始 <code>{item.before_sha256}</code></p>}
      {item.after_sha256 && <p>预期 <code>{item.after_sha256}</code></p>}
    </details>}
    <div className="actions">
      <button className="primary" type="button" disabled={busy} onClick={() => onDecision('approve')}>批准一次</button>
      {item.offer_test_scope && <button type="button" disabled={busy} onClick={() => onDecision('approve-session')}>允许本会话更换目标文件</button>}
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
  const explorationResults = new Set(events?.filter(event =>
    event.type === 'tool.completed' && event.payload.tool_name === 'delegate_exploration'
      && event.payload.completion_status).map(event => event.payload.operation_id))
  const interim = events?.filter(event => {
    if (event.type === 'assistant.completed') return event !== final && Boolean(event.payload.content)
    if (event.type === 'exploration.completed') return !explorationResults.has(event.payload.exploration_id)
    return ['tool.completed', 'model.started', 'model.failed', 'context.compacted',
      'approval.requested', 'approval.decided', 'approval.expired', 'review.applied',
      'verification.started', 'verification.completed', 'skill.selected', 'context.prior_evidence',
      'changes.undo.started', 'changes.undo.completed', 'exploration.started'].includes(event.type)
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
        {({
          RUNNER_CLEANUP_UNCONFIRMED: '执行服务未确认验证结果及清理状态。',
          WEEKLY_MODEL_QUOTA: '已达到工作台本周模型额度。',
          CONTEXT_WINDOW_EXCEEDED: '下一次模型请求超出上下文窗口。',
          UNDO_CONFLICT: '文件内容已变化，撤销未写入文件。',
          SKILL_VERSION_CHANGED: '所选技能的内容已更新，请重新选择版本。',
          SKILL_NOT_FOUND: '所选技能不可用，请检查工作区授权或移除该技能。',
        } as Record<string, string>)[run.outcome.error_code] ?? <code>{run.outcome.error_code}</code>}
        {run.outcome.budget_reason && <small title={run.outcome.budget_reason}> {run.outcome.budget_reason.split(';')[0]}</small>}
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

export function ContextMeter({ events, limit }: { events: RunEvent[]; limit: number | null }) {
  const [open, setOpen] = useState(false)
  const started = events.filter(event => event.type === 'model.started' &&
    event.payload.purpose !== 'ds_search' && event.payload.purpose !== 'delegate_exploration').at(-1)
  const completed = events.filter(event =>
    event.type === 'model.completed' && event.payload.request_index === started?.payload.request_index).at(-1)
  const payload = completed?.payload ?? started?.payload
  const used = payload?.input_tokens
  const window = started?.payload.context_window_tokens ?? limit
  const estimated = payload?.input_estimated ?? payload?.estimated ?? true
  const formatted = (n: number) => n.toLocaleString('zh-CN')
  const label = used === undefined ? '等待统计' : (estimated ? '约 ' : '') + formatted(used)
  const ratio = used === undefined || window === null ? 0 : Math.min(100, (used / window) * 100)
  const display = window === null ? '未记录' : label + ' / ' + formatted(window)
  return <span className="context-meter" data-testid="context-usage"
    onMouseEnter={() => setOpen(true)} onMouseLeave={event => {
      if (!event.currentTarget.contains(document.activeElement)) setOpen(false)
    }}>
    <button type="button" className="context-trigger"
      aria-label={'上下文用量 ' + display + (window === null ? '' : ' tokens')}
      onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onClick={() => setOpen(true)}
      onKeyDown={event => {
        if (event.key === 'Escape' && open) {
          event.stopPropagation()
          setOpen(false)
        }
      }}>
      <span className="context-ring" aria-hidden="true"
        style={{ background: 'conic-gradient(var(--accent) ' + ratio + '%, var(--line) 0)' }} />
    </button>
    {open && <span className="context-tooltip" role="tooltip">
      <span>上下文</span><span>{window === null ? '未记录' : <><b>{label}</b> / {formatted(window)}</>}</span>
    </span>}
  </span>
}
