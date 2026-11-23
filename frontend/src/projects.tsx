import { useEffect, useId, useRef, useState, type FormEvent, type MouseEvent } from 'react'
import { api, type Thread, type Workspace } from './api'
import { runLabel } from './conversation'

type Directory = { path: string; display_path: string; selectable: boolean; folders: { name: string; path: string }[] }

export function ProjectPicker({ onClose, onCreate }: { onClose: () => void; onCreate: (path: Directory) => Promise<void> }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [path, setPath] = useState('')
  const [directory, setDirectory] = useState<Directory | null>(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => {
    const abort = new AbortController()
    setError('')
    setDirectory(null)
    void api<Directory>('', 'workspaces/folders?path=' + encodeURIComponent(path), { signal: abort.signal })
      .then(value => { if (!abort.signal.aborted) setDirectory(value) })
      .catch(err => { if (!abort.signal.aborted) setError(err.message) })
    return () => abort.abort()
  }, [path])

  async function create() {
    if (!directory?.selectable) return
    setSaving(true)
    setError('')
    try { await onCreate(directory) }
    catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setSaving(false) }
  }

  return <dialog className="project-picker" ref={dialog} onCancel={onClose} aria-label="新建工作区">
    <header><h2>新建工作区</h2><button type="button" onClick={onClose}>取消</button></header>
    <div className="picker-path">
      <button type="button" disabled={!path || saving} onClick={() => setPath(path.split('/').slice(0, -1).join('/'))}>上一级</button>
      <code title={directory?.display_path}>{directory?.display_path ?? path}</code>
    </div>
    {error && <p className="notice" role="alert">{error}</p>}
    {directory ? <ul className="folder-list" aria-label="项目文件夹">
      {directory.folders.map(folder => <li key={folder.path}><button type="button" disabled={saving}
        onClick={() => setPath(folder.path)}>{folder.name}</button></li>)}
      {!directory.folders.length && <li className="muted">此目录无子文件夹。</li>}
    </ul> : !error && <p className="muted">正在读取文件夹…</p>}
    <footer>
      {directory && !directory.selectable && <span className="muted">请选择 Git 项目文件夹。</span>}
      <button type="button" className="primary" disabled={saving || !directory?.selectable}
        onClick={() => void create()}>{saving ? '正在创建…' : '选择此文件夹'}</button>
    </footer>
  </dialog>
}

export function ProjectAliasDialog({ item, onClose, onSave }: {
  item: Workspace; onClose: () => void; onSave: (alias: string) => Promise<void>
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [alias, setAlias] = useState(item.alias ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { dialog.current?.showModal() }, [])

  function close() {
    dialog.current?.close()
    onClose()
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving) return
    setSaving(true)
    setError('')
    try {
      await onSave(alias.trim())
      close()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSaving(false)
    }
  }

  return <dialog className="project-picker project-alias-dialog" ref={dialog}
    aria-labelledby="project-alias-heading" onCancel={event => {
      event.preventDefault()
      if (!saving) close()
    }} onKeyDown={event => { if (event.key === 'Escape') event.stopPropagation() }}>
    <form onSubmit={save}>
      <h2 id="project-alias-heading">修改工作区别名</h2>
      <label htmlFor="project-alias">工作区别名</label>
      <input id="project-alias" autoFocus value={alias} maxLength={80} disabled={saving}
        aria-describedby="project-alias-hint" placeholder={item.default_label ?? item.label}
        onChange={event => setAlias(event.target.value)} />
      <p id="project-alias-hint" className="muted">留空恢复原名称。</p>
      {error && <p className="notice" role="alert">{error}</p>}
      <footer>
        <button type="button" disabled={saving} onClick={close}>取消</button>
        <button className="primary" type="submit" disabled={saving}>{saving ? '正在保存…' : '保存'}</button>
      </footer>
    </form>
  </dialog>
}

export function DeleteThreadDialog({ title, project, onClose, onDelete }: {
  title: string; project: boolean; onClose: () => void; onDelete: () => Promise<void>
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { dialog.current?.showModal() }, [])

  async function remove() {
    setBusy(true)
    setError('')
    try {
      await onDelete()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return <dialog className="project-picker delete-thread-dialog" ref={dialog}
    aria-labelledby="delete-thread-title" aria-describedby="delete-thread-description"
    onCancel={event => {
      event.preventDefault()
      if (!busy) onClose()
    }}>
    <h2 id="delete-thread-title">删除会话</h2>
    <p className="delete-thread-name">{title}</p>
    <p id="delete-thread-description">此会话的所有消息、运行记录和制品将被永久删除。
      {project && '项目会话专属工作区中尚未导出的改动也会一并清理。'}</p>
    {error && <p className="error" role="alert">{error}</p>}
    <footer>
      <button type="button" autoFocus disabled={busy} onClick={onClose}>取消</button>
      <button type="button" className="delete-thread-confirm" disabled={busy}
        onClick={() => void remove()}>{busy ? '正在删除…' : '确认删除'}</button>
    </footer>
  </dialog>
}

function ThreadActions({ thread, label, onArchive, onDelete }: {
  thread: Thread; label: string; onArchive: () => void; onDelete: () => void
}) {
  const id = useId()
  const popup = useRef<HTMLDivElement>(null)

  function place(event: MouseEvent<HTMLButtonElement>) {
    const menu = popup.current
    if (!menu) return
    const anchor = event.currentTarget.getBoundingClientRect()
    menu.style.left = Math.max(6, anchor.right - 112) + 'px'
    menu.style.top = (anchor.bottom + 84 <= window.innerHeight ? anchor.bottom + 4 : anchor.top - 80) + 'px'
  }

  return <>
    <button className="thread-action-trigger" type="button" title="会话操作"
      aria-label={'会话操作 ' + label} popoverTarget={id} onClick={place}>⋯</button>
    <div id={id} ref={popup} className="thread-actions" popover="auto" role="group" aria-label={'会话操作 ' + label}>
      <button type="button" disabled={thread.status === 'active'} onClick={() => {
        popup.current?.hidePopover()
        onArchive()
      }}>{thread.archived ? '恢复' : '归档'}</button>
      {thread.can_delete && <button type="button" className="thread-delete-action"
        disabled={thread.status === 'active'} onClick={() => {
          popup.current?.hidePopover()
          onDelete()
        }}>删除</button>}
    </div>
  </>
}

export function WorkspaceGroup({ item, threads, selectedSession, runId, selectedWorkspace, waiting, writable, manage,
  onNew, onOpen, onArchive, onRename, onDelete }: {
  item: Workspace; threads: Thread[]; selectedSession?: string; runId: string; selectedWorkspace?: string
  waiting: boolean; writable: boolean; manage: boolean; onNew: (id: string) => void
  onOpen: (id: string) => void; onArchive: (thread: Thread) => void
  onRename: (item: Workspace) => void
  onDelete: (thread: Thread, label: string, project: boolean) => void
}) {
  function row(thread: Thread) {
    const selected = thread.session_id === selectedSession || thread.run_id === runId
    const label = thread.is_review ? '补丁验证' : thread.title.split('\n')[0]
    const status = (selected && waiting) || thread.needs_approval ? '待审批' : runLabel(thread.status)
    return <div key={thread.session_id} className="thread-item">
      <button className="thread-row" type="button" title={label + ' · ' + status}
        aria-current={selected ? 'page' : undefined} onClick={() => onOpen(thread.run_id)}>
        <span className="thread-title">{label}</span>
        {(thread.status !== 'completed' || thread.needs_approval || (selected && waiting)) &&
          <small className={status === '待审批' ? 'pending-label' : ''}>{status}</small>}
      </button>
      {writable && (manage || thread.can_delete) && <ThreadActions thread={thread} label={label}
        onArchive={() => onArchive(thread)} onDelete={() => onDelete(thread, label, item.kind !== 'chat')} />}
    </div>
  }
  const archived = threads.filter(thread => thread.archived)
  return <section className={'project' + (item.id === selectedWorkspace ? ' project-selected' : '')}>
    <details open>
      <summary className="project-label"><span title={item.path ?? item.label}>{item.label}</span></summary>
      {threads.filter(thread => !thread.archived).map(row)}
      {archived.length > 0 && <details className="project-archive">
        <summary>已归档 · {archived.length}</summary>{archived.map(row)}
      </details>}
    </details>
    {writable && <div className="project-actions">
      {manage && item.kind === 'project' && <button type="button" className="project-edit"
        aria-label={'修改 ' + item.label + ' 的别名'} title="修改工作区别名"
        onClick={() => onRename(item)}>✎</button>}
      <button type="button" className="project-new" aria-label={'在 ' + item.label + ' 新建任务'}
        title="在此工作区新建任务" onClick={() => onNew(item.id)}>＋</button>
    </div>}
  </section>
}
