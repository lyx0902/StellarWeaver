import { useEffect, useId, useRef, useState } from 'react'
import { api, type SkillSelection } from './api'

type Skill = {
  id: string; name: string; description: string; source: string; path: string
  sha256: string; shadowed_paths: string[]
}
type Catalog = {
  sources: string[]; skills: Skill[]
  errors: { source: string; path: string; code: string }[]
  project_trusted?: boolean; can_manage_project?: boolean
}
type SkillDetail = Skill & { content: string }
function matchingSkills(catalog: Catalog | null, query: string) {
  const term = query.trim().toLowerCase()
  return catalog?.skills.filter(skill =>
    `${skill.name} ${skill.description} ${skill.source}`.toLowerCase().includes(term)) ?? []
}

export function ComposerSkills({ token, workspace, picks, onPick, onLibrary }: {
  token: string; workspace?: string; picks: SkillSelection[]
  onPick: (skill: SkillSelection) => void; onLibrary: () => void
}) {
  const id = useId()
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const [hovered, setHovered] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const open = hovered || expanded

  useEffect(() => {
    if (!open) return
    const abort = new AbortController()
    const scope = workspace ? '?workspace_id=' + encodeURIComponent(workspace) : ''
    function dismiss(event: PointerEvent) {
      if (event.target instanceof Node && !root.current?.contains(event.target)) {
        setHovered(false)
        setExpanded(false)
      }
    }
    document.addEventListener('pointerdown', dismiss)
    setError('')
    void api<Catalog>(token, 'skills' + scope, { signal: abort.signal })
      .then(data => { if (!abort.signal.aborted) setCatalog(data) })
      .catch(err => { if (!abort.signal.aborted) setError(err instanceof Error ? err.message : String(err)) })
    return () => {
      abort.abort()
      document.removeEventListener('pointerdown', dismiss)
    }
  }, [open, token, workspace])

  const visible = matchingSkills(catalog, query)
  function close() {
    setExpanded(false)
    setHovered(false)
  }

  return <div className="composer-skill-picker" ref={root}
    onPointerEnter={event => { if (event.pointerType === 'mouse') setHovered(true) }}
    onPointerLeave={event => { if (event.pointerType === 'mouse') setHovered(false) }}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setExpanded(false) }}
    onKeyDown={event => {
      if (event.key === 'Escape' && open) {
        event.stopPropagation()
        close()
        trigger.current?.focus()
      }
    }}>
    <button type="button" className="skill-trigger" aria-label="技能" aria-controls={id}
      ref={trigger} aria-expanded={open} onClick={() => {
        if (expanded) close()
        else setExpanded(true)
      }}>
      <span aria-hidden="true">+</span>
      {picks.length > 0 && <span className="skill-count">{picks.length}</span>}
    </button>
    {open && <div className="skill-picker-layer">
      <section className="skill-picker-menu" id={id} aria-label="技能">
        <strong>技能</strong>
        {picks.length > 0 && <div className="skill-picker-selected">
          {picks.map(skill => <button type="button" key={skill.id} title={skill.source}
            aria-label={'移除技能 ' + skill.name} onClick={() => onPick(skill)}>{skill.name} ×</button>)}
        </div>}
        <input type="search" aria-label="搜索技能" placeholder="搜索技能" value={query}
          onKeyDown={event => { if (event.key === 'Enter') event.preventDefault() }}
          onFocus={() => setExpanded(true)} onChange={event => setQuery(event.target.value)} />
        {error ? <p role="alert">{error}</p> : !catalog ? <p role="status">正在读取技能…</p>
          : !visible.length ? <p className="muted">没有匹配的技能。</p>
          : <ul aria-label="可选技能">
            {visible.map(skill => {
              const chosen = picks.some(item => item.id === skill.id)
              return <li key={skill.id}>
                <button type="button" aria-pressed={picks.some(item => item.id === skill.id && item.sha256 === skill.sha256)}
                  title={skill.description} disabled={!chosen && picks.length >= 3}
                  onClick={() => { setExpanded(true); onPick(skill) }}>
                  <span>{skill.name}</span><small>{skill.source}</small>
                </button>
              </li>
            })}
          </ul>}
        <button className="quiet skill-library-link" type="button"
          onClick={() => { close(); onLibrary() }}>打开 Skills 库</button>
      </section>
    </div>}
  </div>
}

export function SkillsPanel({ token, workspace, picks, onPick, onTrustChange, onClose }: {
  token: string; workspace?: string; picks: SkillSelection[]
  onPick?: (skill: SkillSelection) => void; onTrustChange: (enabled: boolean) => void
  onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<SkillDetail | null>(null)
  const [version, setVersion] = useState(0)
  const [catalogError, setCatalogError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [loading, setLoading] = useState(true)
  const [savingTrust, setSavingTrust] = useState(false)
  const scope = workspace ? 'workspace_id=' + encodeURIComponent(workspace) : ''

  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => {
    const abort = new AbortController()
    setLoading(true)
    setCatalogError('')
    void api<Catalog>(token, 'skills?refresh=true' + (scope ? '&' + scope : ''), { signal: abort.signal }).then(data => {
      if (abort.signal.aborted) return
      setCatalog(data)
      setSelected(prior => data.skills.some(skill => skill.id === prior) ? prior : '')
    }).catch(err => {
      if (!abort.signal.aborted) setCatalogError(err instanceof Error ? err.message : String(err))
    }).finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [token, version, scope])

  useEffect(() => {
    if (!selected || loading) return
    const abort = new AbortController()
    setDetail(null)
    setDetailError('')
    void api<SkillDetail>(token, 'skills/' + selected + (scope ? '?' + scope : ''), { signal: abort.signal }).then(data => {
      if (!abort.signal.aborted) setDetail(data)
    }).catch(err => {
      if (!abort.signal.aborted) setDetailError(err instanceof Error ? err.message : String(err))
    })
    return () => abort.abort()
  }, [token, selected, loading, scope])

  async function trust(enabled: boolean) {
    setSavingTrust(true)
    setCatalogError('')
    try {
      await api(token, 'workspaces/' + workspace + '/skill-trust', {
        method: 'PUT', body: JSON.stringify({ enabled }),
      })
      onTrustChange(enabled)
      setVersion(value => value + 1)
    } catch (err) {
      setCatalogError(err instanceof Error ? err.message : String(err))
    } finally {
      setSavingTrust(false)
    }
  }

  const visible = matchingSkills(catalog, query)
  const current = detail?.id === selected ? detail : null
  return <dialog className="admin-panel skills-panel" aria-label="Skills 库" ref={dialog} onCancel={onClose}>
    <header><h2>Skills 库</h2><div className="panel-actions">
      <button type="button" onClick={() => setVersion(value => value + 1)} disabled={loading}>刷新目录</button>
      <button type="button" onClick={onClose}>关闭</button>
    </div></header>
    {catalogError && <p className="notice" role="alert">{catalogError}</p>}
    {catalog?.can_manage_project && <label className="inline-label">
      <input type="checkbox" checked={Boolean(catalog.project_trusted)} disabled={savingTrust || loading}
        onChange={event => void trust(event.target.checked)} />
      信任此工作区的 .stellarweaver/skills
    </label>}
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
            {onPick && <button type="button"
              disabled={picks.length >= 3 && !picks.some(item => item.id === current.id)}
              onClick={() => onPick(current)}>
              {picks.some(item => item.id === current.id && item.sha256 === current.sha256)
                ? '从此会话移除' : '为此会话指定'}
            </button>}
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
