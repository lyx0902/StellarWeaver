import { useEffect, useRef, useState } from 'react'
import { api } from './api'

type Audit = { id: number; name: string; run_id: string; action: string; created_at: string }
type Usage = { member_id: string; name: string; usage_day: string; tokens: number; requests: number; daily_tokens: number; estimated_requests: number }

export function AdminPanel({ token, onClose }: { token: string; onClose: () => void }) {
  const element = useRef<HTMLDialogElement>(null)
  const [audit, setAudit] = useState<Audit[] | null>(null)
  const [usage, setUsage] = useState<Usage[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    element.current?.showModal()
    const abort = new AbortController()
    void Promise.all([
      api<Audit[]>(token, 'admin/audit', { signal: abort.signal }),
      api<Usage[]>(token, 'admin/usage', { signal: abort.signal }),
    ]).then(([actions, totals]) => {
      if (!abort.signal.aborted) { setAudit(actions); setUsage(totals) }
    }).catch(err => { if (!abort.signal.aborted) setError(String(err)) })
    return () => abort.abort()
  }, [token])

  return <dialog className="admin-panel" ref={element} aria-label="审计与用量" onCancel={onClose}>
    <header><h2>审计与用量</h2><button type="button" onClick={onClose}>关闭</button></header>
    {error && <p className="notice" role="alert">{error}</p>}
    <h3>每日模型用量（UTC）</h3>
    {usage?.length ? <table><thead><tr><th>用户</th><th>日期</th><th>Token</th><th>请求</th></tr></thead>
      <tbody>{usage.map(row => <tr key={row.member_id + row.usage_day}>
        <td>{row.name}</td><td>{row.usage_day}</td><td>{row.tokens.toLocaleString()} / {row.daily_tokens.toLocaleString()}</td>
        <td>{row.requests}{row.estimated_requests > 0 && '（' + row.estimated_requests + ' 项估算）'}</td>
      </tr>)}</tbody></table> : <p className="muted">{usage ? '获授权项目暂无模型用量。' : '正在读取用量…'}</p>}
    <h3>最近操作</h3>
    {audit?.length ? <table><thead><tr><th>操作者</th><th>操作</th><th>运行</th><th>时间</th></tr></thead>
      <tbody>{audit.map(row => <tr key={row.id}><td>{row.name}</td><td>{row.action}</td>
        <td><a href={'?run=' + row.run_id}>{row.run_id.slice(0, 8)}</a></td>
        <td>{new Date(row.created_at).toLocaleString()}</td></tr>)}</tbody></table>
      : <p className="muted">{audit ? '获授权项目暂无审计记录。' : '正在读取审计…'}</p>}
  </dialog>
}
