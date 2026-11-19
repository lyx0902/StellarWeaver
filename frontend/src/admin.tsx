import { useEffect, useRef, useState } from 'react'
import { api } from './api'

type Audit = { id: number; name: string; run_id: string; action: string; created_at: string }
type Usage = { member_id: string; name: string; usage_day: string; tokens: number; requests: number; estimated_requests: number }
type Overview = {
  read_at: string; day: string; quota_day: string; quota_resets_at: string
  today: { tokens: number; requests: number; estimated_requests: number; unsettled_requests: number }
  quota: { limit_tokens: number; used_tokens: number }
  history: Usage[]
}
const readingTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})
const displayTime = (time: string) => readingTime.format(new Date(time))
const count = (value: number) => value.toLocaleString('zh-CN')

export function AdminPanel({ token, onClose }: { token: string; onClose: () => void }) {
  const element = useRef<HTMLDialogElement>(null)
  const refreshNow = useRef<() => void>(() => {})
  const [audit, setAudit] = useState<Audit[] | null>(null)
  const [usage, setUsage] = useState<Overview | null>(null)
  const [error, setError] = useState('')
  const [reading, setReading] = useState(false)

  useEffect(() => { element.current?.showModal() }, [])
  useEffect(() => {
    const abort = new AbortController()
    let inFlight = false
    async function refresh() {
      if (inFlight || abort.signal.aborted) return
      inFlight = true
      setReading(true)
      const [actions, totals] = await Promise.allSettled([
        api<Audit[]>(token, 'admin/audit', { signal: abort.signal }),
        api<Overview>(token, 'admin/usage/overview', { signal: abort.signal }),
      ])
      inFlight = false
      if (abort.signal.aborted) return
      if (actions.status === 'fulfilled') setAudit(actions.value)
      if (totals.status === 'fulfilled') setUsage(totals.value)
      setError([
        actions.status === 'rejected' ? '审计读取失败' : '',
        totals.status === 'rejected' ? '用量读取失败' : '',
      ].filter(Boolean).join('，'))
      setReading(false)
    }
    refreshNow.current = () => { void refresh() }
    const timer = window.setInterval(refreshNow.current, 30_000)
    const onVisible = () => { if (document.visibilityState === 'visible') void refresh() }
    document.addEventListener('visibilitychange', onVisible)
    void refresh()
    return () => {
      abort.abort()
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [token])

  return <dialog className="admin-panel" ref={element} aria-label="审计与用量" onCancel={onClose}>
    <header><h2>审计与用量</h2><div className="panel-actions">
      <button type="button" disabled={reading} onClick={() => refreshNow.current()}>刷新</button>
      <button type="button" onClick={onClose}>关闭</button>
    </div></header>
    {error && <p className="notice" role="alert">{error}</p>}
    <p className="muted">每 30 秒刷新 · 最近读取 <time data-testid="usage-read-at" dateTime={usage?.read_at}>
      {usage ? displayTime(usage.read_at) + '（北京时间）' : '等待数据'}</time></p>
    <div className="usage-cards">
      <section aria-label="今日模型用量"><h3>今日用量（北京时间）</h3>
        {usage && <><p className="muted">{usage.day} · 可见项目</p>
          <p><strong>{count(usage.today.tokens)}</strong> tokens · {count(usage.today.requests)} 次请求</p>
          {usage.today.estimated_requests > 0 && <small className="muted">估算 {usage.today.estimated_requests} 次
            {usage.today.unsettled_requests > 0 && ` · 未结算 ${usage.today.unsettled_requests} 次`}</small>}</>}
      </section>
      <section aria-label="我的日额度"><h3>我的日额度（UTC）</h3>
        {usage && <><p className="muted">额度日期 {usage.quota_day}</p>
          <p><strong>{count(usage.quota.used_tokens)}</strong> / {count(usage.quota.limit_tokens)} tokens</p>
          <small className="muted">下次重置 {displayTime(usage.quota_resets_at)}（北京时间）</small></>}
      </section>
    </div>
    <details className="usage-history"><summary>历史用量（UTC，可见项目）</summary>
    {usage?.history.length ? <table><thead><tr><th>用户</th><th>日期</th><th>Token</th><th>请求</th></tr></thead>
      <tbody>{usage.history.map(row => <tr key={row.member_id + row.usage_day}>
        <td>{row.name}</td><td>{row.usage_day}</td><td>{count(row.tokens)}</td>
        <td>{row.requests}{row.estimated_requests > 0 && '（' + row.estimated_requests + ' 项估算）'}</td>
      </tr>)}</tbody></table> : <p className="muted">{usage ? '获授权项目暂无模型用量。' : '正在读取用量…'}</p>}
    </details>
    <h3>最近操作（北京时间）</h3>
    {audit?.length ? <table><thead><tr><th>操作者</th><th>操作</th><th>运行</th><th>时间</th></tr></thead>
      <tbody>{audit.map(row => <tr key={row.id}><td>{row.name}</td><td>{row.action}</td>
        <td><a href={'?run=' + row.run_id}>{row.run_id.slice(0, 8)}</a></td>
        <td><time dateTime={row.created_at}>{displayTime(row.created_at)}</time></td></tr>)}</tbody></table>
      : <p className="muted">{audit ? '获授权项目暂无审计记录。' : '正在读取审计…'}</p>}
  </dialog>
}
