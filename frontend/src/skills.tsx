import { useEffect, useRef, useState } from 'react'
import { api } from './api'

type Skill = {
  id: string; name: string; description: string; source: string; path: string
  sha256: string; shadowed_paths: string[]
}
type Catalog = { sources: string[]; skills: Skill[]; errors: { source: string; path: string; code: string }[] }
type SkillDetail = Skill & { content: string }

export function SkillsPanel({ token, onClose }: { token: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<SkillDetail | null>(null)
  const [version, setVersion] = useState(0)
  const [catalogError, setCatalogError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => {
    const abort = new AbortController()
    setLoading(true)
    setCatalogError('')
    void api<Catalog>(token, 'skills?refresh=true', { signal: abort.signal }).then(data => {
      if (abort.signal.aborted) return
      setCatalog(data)
      setSelected(prior => data.skills.some(skill => skill.id === prior) ? prior : '')
    }).catch(err => {
      if (!abort.signal.aborted) setCatalogError(err instanceof Error ? err.message : String(err))
    }).finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [token, version])

  useEffect(() => {
    if (!selected || loading) return
    const abort = new AbortController()
    setDetail(null)
    setDetailError('')
    void api<SkillDetail>(token, 'skills/' + selected, { signal: abort.signal }).then(data => {
      if (!abort.signal.aborted) setDetail(data)
    }).catch(err => {
      if (!abort.signal.aborted) setDetailError(err instanceof Error ? err.message : String(err))
    })
    return () => abort.abort()
  }, [token, selected, loading])

  const term = query.trim().toLowerCase()
  const visible = catalog?.skills.filter(skill =>
    `${skill.name} ${skill.description} ${skill.source}`.toLowerCase().includes(term)) ?? []
  const current = detail?.id === selected ? detail : null
  return <dialog className="admin-panel skills-panel" aria-label="Skills 库" ref={dialog} onCancel={onClose}>
    <header><h2>Skills 库</h2><div className="panel-actions">
      <button type="button" onClick={() => setVersion(value => value + 1)} disabled={loading}>刷新目录</button>
      <button type="button" onClick={onClose}>关闭</button>
    </div></header>
    {catalogError && <p className="notice" role="alert">{catalogError}</p>}
    <label className="skills-search">搜索 Skills<input type="search" value={query}
      placeholder="名称、描述或来源" onChange={event => setQuery(event.target.value)} /></label>
    {loading && !catalog ? <p className="muted">正在扫描本机技能目录…</p> : catalog && <>
      <p className="muted">{catalog.sources.length ? `${visible.length} / ${catalog.skills.length} 个技能 · ${catalog.sources.join('、')}` : '当前工作台未配置本机技能目录。'}</p>
      <div className="skills-content">
        <ul className="skills-items" aria-label="技能列表">
          {visible.map(skill => <li key={skill.id}>
            <button type="button" aria-pressed={selected === skill.id} onClick={() => setSelected(skill.id)}>
              <strong>{skill.name}</strong><small>{skill.source}</small>
            </button>
          </li>)}
          {!visible.length && catalog.sources.length > 0 && <li className="muted">没有匹配的技能。</li>}
        </ul>
        <section className="skill-detail" aria-label="技能详情">
          {current ? <>
            <h3>{current.name}</h3><p>{current.description}</p>
            <small className="muted">{current.source} · {current.path}</small>
            {current.shadowed_paths.length > 0 && <p className="muted">同名文件 {current.shadowed_paths.join('、')}</p>}
            <pre>{current.content}</pre>
          </> : detailError && selected ? <p className="notice" role="alert">{detailError}</p>
            : <p className="muted">{selected ? '正在读取技能正文…' : '选择技能以查看正文。'}</p>}
        </section>
      </div>
      {catalog.errors.length > 0 && <details className="skills-errors">
        <summary>扫描提示（{catalog.errors.length}）</summary>
        <ul>{catalog.errors.map(item => <li key={`${item.source}/${item.path}/${item.code}`}>
          {item.source} · {item.path} <code>{item.code}</code></li>)}</ul>
      </details>}
    </>}
  </dialog>
}
