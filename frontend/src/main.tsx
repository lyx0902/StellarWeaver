import { useEffect, useRef, useState, type FormEvent, type PointerEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { api, receive, type Approval, type AuthOptions, type Delivery, type Mode, type PatchPreflight, type ReviewFeedback, type Run, type RunEvent, type Thread, type Workspaces } from './api'
import { AdminPanel } from './admin'
import { ApprovalCard, ContextMeter, Turn, runLabel } from './conversation'
import { Inspector, type InspectorTab } from './inspector'
import { ProjectPicker, WorkspaceGroup } from './projects'
import { SkillsPanel } from './skills'
import './style.css'

const tokenKey = 'stellarweaver.local-token'
const widthKey = 'stellarweaver.inspector-width'
const readRunId = () => new URLSearchParams(location.search).get('run') ?? ''
const clampWidth = (width: number) => Math.min(680, Math.max(330, width))

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem(tokenKey) ?? '')
  const [draftToken, setDraftToken] = useState('')
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [authorizing, setAuthorizing] = useState(false)
  const [authOptions, setAuthOptions] = useState<AuthOptions | null>(null)
  const [browserSession, setBrowserSession] = useState(false)
  const [showAdmin, setShowAdmin] = useState(false)
  const [showSkills, setShowSkills] = useState(false)
  const [listing, setListing] = useState<Workspaces | null>(null)
  const [workspace, setWorkspace] = useState('')
  const [threads, setThreads] = useState<Thread[]>([])
  const [turns, setTurns] = useState<Run[]>([])
  const [draft, setDraft] = useState('')
  const [mode, setMode] = useState<Mode>('read-only')
  const [runId, setRunId] = useState(readRunId)
  const [snapshot, setSnapshot] = useState<Run | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [delivery, setDelivery] = useState<Delivery | null>(null)
  const [approval, setApproval] = useState<Approval | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [deciding, setDeciding] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [continuation, setContinuation] = useState<'history' | 'fresh'>('history')
  const [acknowledged, setAcknowledged] = useState(false)
  const [panel, setPanel] = useState<InspectorTab | null>(null)
  const [showNavigation, setShowNavigation] = useState(false)
  const [showPicker, setShowPicker] = useState(false)
  const [width, setWidth] = useState(() => clampWidth(Number(localStorage.getItem(widthKey)) || 420))
  const request = useRef<{ input: string; id: string } | null>(null)
  const reviewRequest = useRef<{ input: string; id: string } | null>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const composer = useRef<HTMLTextAreaElement>(null)
  const nearBottom = useRef(true)
  const drag = useRef<{ x: number; width: number } | null>(null)
  const signedIn = Boolean((token && authOptions?.bearer) || browserSession)
  const writable = listing?.can_submit !== false

  const run = snapshot?.run_id === runId ? snapshot : null
  const activeThread = threads.find(thread => thread.session_id === run?.session_id)
  const visibleEvents = run ? events : []
  const historyTurns = run ? turns.filter(turn => turn.session_id === run.session_id) : []
  const newest = historyTurns[0]
  const latestIsKnown = !newest || newest.run_id === runId
  const latestStatus = latestIsKnown ? run?.status : newest.status
  const threadWorkspace = run?.workspace_id
  const selectedWorkspace = runId ? threadWorkspace : workspace
  const currentProject = listing?.workspaces.find(item => item.id === selectedWorkspace)
  const chatWorkspace = listing?.workspaces.find(item => item.kind === 'chat')
  const chatting = currentProject?.kind === 'chat'
  const sameMode = Boolean(run?.isolated) === Boolean(!chatting && listing?.isolated)
  const canContinue = Boolean(threadWorkspace && sameMode && !run?.is_review && run?.can_continue !== false)
  const recovery = Boolean(run && latestIsKnown &&
    (run.status === 'interrupted' || run.outcome?.unfinished_tools))
  const needLatest = Boolean(canContinue && !latestIsKnown && newest?.requires_acknowledgement)
  const loaded = Boolean(!runId || run)
  const conversation = run ? [run, ...historyTurns.filter(turn => turn.run_id !== runId)]
    .sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? '') || a.run_id.localeCompare(b.run_id)) : []
  const active = latestStatus === 'active'
  const canSend = writable && loaded && Boolean(selectedWorkspace) && (!runId || (canContinue && historyTurns.length > 0))
    && !activeThread?.archived && !needLatest && !active && (!recovery || acknowledged) && !submitting
  const currentReport = delivery?.run_id === runId ? delivery : null

  useEffect(() => {
    const abort = new AbortController()
    void api<AuthOptions>('', 'auth/config', { signal: abort.signal }).then(async methods => {
      if (abort.signal.aborted) return
      setAuthOptions(methods)
      if ((methods.oidc || methods.password) && (!token || !methods.bearer)) {
        if (token && !methods.bearer) {
          sessionStorage.removeItem(tokenKey)
          setToken('')
        }
        try {
          await api<Workspaces>('', 'workspaces', { signal: abort.signal })
          if (!abort.signal.aborted) setBrowserSession(true)
        } catch {
          // The login page is also the signed-out state.
        }
      }
    }).catch(err => { if (!abort.signal.aborted) setError(String(err)) })
    return () => abort.abort()
  }, [token])

  useEffect(() => {
    if (!signedIn) return
    const abort = new AbortController()
    api<Workspaces>(token, 'workspaces', { signal: abort.signal })
      .then(config => {
        if (!abort.signal.aborted) {
          setListing(config)
          setWorkspace(old => config.workspaces.some(item => item.id === old) ? old :
            config.workspaces.find(item => item.kind === 'chat')?.id ?? config.workspaces[0]?.id ?? '')
        }
      })
      .catch(err => { if (!abort.signal.aborted) setError(String(err)) })
    return () => abort.abort()
  }, [token, signedIn])

  useEffect(() => {
    if (!signedIn || !listing) return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function refresh() {
      try {
        const rows = await api<Thread[]>(token, 'threads', { signal: abort.signal })
        if (!abort.signal.aborted) setThreads(rows)
      } catch (err) {
        if (!abort.signal.aborted) setError(String(err))
      }
      if (!abort.signal.aborted) timer = setTimeout(refresh, 5000)
    }
    void refresh()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [token, signedIn, listing, runId, run?.status, approval?.id])

  useEffect(() => {
    if (!signedIn || !run?.session_id) return
    const abort = new AbortController()
    api<Run[]>(token, 'runs?session_id=' + run.session_id, { signal: abort.signal })
      .then(items => { if (!abort.signal.aborted) setTurns(items) })
      .catch(err => { if (!abort.signal.aborted) setError(String(err)) })
    return () => abort.abort()
  }, [token, signedIn, run?.session_id, run?.status, runId, activeThread?.status, activeThread?.run_id])

  useEffect(() => {
    if (!signedIn || !runId) return
    const abort = new AbortController()
    let cursor = 0
    async function follow() {
      while (!abort.signal.aborted) {
        try {
          const initial = await api<Run>(token, 'runs/' + runId, { signal: abort.signal })
          if (abort.signal.aborted) return
          setSnapshot(initial)
          setApproval(initial.pending_approval ?? null)
          setStopping(Boolean(initial.cancel_requested))
          setReconnecting(false)
          await receive(token, runId, cursor, abort.signal, event => {
            if (abort.signal.aborted || event.seq <= cursor) return
            cursor = event.seq
            setEvents(prior => [...prior, event])
            if (event.seq <= (initial.through_seq ?? 0)) return
            if (event.type === 'run.claimed') setSnapshot(prior =>
              prior?.run_id === runId ? { ...prior, job_state: 'running' } : prior)
            if (event.type === 'approval.requested') {
              const item = event.payload as Approval
              if (Date.parse(item.expires_at) > Date.now()) setApproval(item)
            }
            if (event.type === 'approval.decided' || event.type === 'approval.expired') {
              setApproval(prior => prior?.id === event.payload.id ? null : prior)
            }
            if (event.type === 'run.cancel_requested') {
              setApproval(null)
              setStopping(true)
            }
          })
          const current = await api<Run>(token, 'runs/' + runId, { signal: abort.signal })
          if (abort.signal.aborted) return
          setSnapshot(current)
          if (current.status !== 'active') {
            setApproval(null)
            if (current.has_report) {
              const report = await api<Delivery>(token, 'runs/' + runId + '/report', { signal: abort.signal })
              if (!abort.signal.aborted) setDelivery(report)
            }
            return
          }
        } catch (err) {
          if (abort.signal.aborted) return
          if (/Run not found|access token required/i.test(String(err))) {
            setError(String(err))
            return
          }
        }
        if (!abort.signal.aborted) setReconnecting(true)
        await new Promise(resolve => setTimeout(resolve, 750))
      }
    }
    void follow()
    return () => abort.abort()
  }, [token, signedIn, runId])

  useEffect(() => {
    if (!approval) return
    const timer = setTimeout(() => setApproval(null), Math.max(0, Date.parse(approval.expires_at) - Date.now()))
    return () => clearTimeout(timer)
  }, [approval])

  useEffect(() => {
    if (nearBottom.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight
  }, [runId, events.length, historyTurns.length, approval?.id])

  useEffect(() => {
    localStorage.setItem(widthKey, String(width))
  }, [width])

  useEffect(() => {
    const restore = () => {
      if (readRunId() === runId) return
      setRunId(readRunId())
      setSnapshot(null)
      setEvents([])
      setApproval(null)
      setDelivery(null)
      setError('')
      setPanel(null)
      setAcknowledged(false)
      setContinuation('history')
      setStopping(false)
      setReconnecting(false)
      nearBottom.current = true
    }
    window.addEventListener('popstate', restore)
    return () => window.removeEventListener('popstate', restore)
  }, [runId])

  function openRun(id: string, destination: InspectorTab | null = null, replace = false) {
    setShowNavigation(false)
    setPanel(destination)
    if (id === runId) return
    setDraft('')
    const url = new URL(location.href)
    if (id) url.searchParams.set('run', id)
    else url.searchParams.delete('run')
    if (replace) history.replaceState(null, '', url)
    else history.pushState(null, '', url)
    setRunId(id)
    setSnapshot(null)
    setEvents([])
    setApproval(null)
    setDelivery(null)
    setError('')
    setAcknowledged(false)
    setContinuation('history')
    setStopping(false)
    setReconnecting(false)
    nearBottom.current = true
  }

  function newThread(workspaceId?: string) {
    if (workspaceId) setWorkspace(workspaceId)
    else if (threadWorkspace) setWorkspace(threadWorkspace)
    setDraft('')
    setMode('read-only')
    openRun('')
    composer.current?.focus()
  }

  async function connect(event: FormEvent) {
    event.preventDefault()
    if (authOptions?.password) {
      if (authorizing) return
      setAuthorizing(true)
      setError('')
      try {
        await api<void>('', 'auth/' + authMode, {
          method: 'POST', body: JSON.stringify({ username, password }),
        })
        setPassword('')
        setBrowserSession(true)
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        setAuthorizing(false)
      }
      return
    }
    const value = draftToken.trim()
    if (!value) return
    sessionStorage.setItem(tokenKey, value)
    setToken(value)
    setDraftToken('')
    setError('')
  }

  async function disconnect() {
    if (browserSession) {
      try {
        await api<void>('', 'auth/logout', { method: 'POST' })
      } catch (err) {
        setError(String(err))
        return
      }
      setBrowserSession(false)
    }
    sessionStorage.removeItem(tokenKey)
    setListing(null)
    setToken('')
    setAuthMode('login')
    setPassword('')
    setThreads([])
    setTurns([])
    setDraft('')
    setMode('read-only')
    setShowAdmin(false)
    setShowSkills(false)
    setShowPicker(false)
    openRun('', null, true)
    setError('')
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSend || !draft.trim()) return
    const payload = {
      workspace_id: selectedWorkspace, prompt: draft.trim(), mode: chatting ? 'read-only' : mode,
      ...(run ? { session_id: run.session_id, continuation, acknowledge_interrupted: acknowledged } : {}),
    }
    const input = JSON.stringify(payload)
    if (request.current?.input !== input) request.current = { input, id: crypto.randomUUID() }
    setSubmitting(true)
    setError('')
    try {
      const created = await api<Run>(token, 'runs', {
        method: 'POST', body: JSON.stringify({ ...payload, request_id: request.current.id }),
      })
      request.current = null
      setDraft('')
      openRun(created.run_id)
    } catch (err) {
      setError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function createProject(directory: { path: string; display_path: string }) {
    await api<void>(token, 'workspaces', { method: 'POST', body: JSON.stringify({ path: directory.path }) })
    const config = await api<Workspaces>(token, 'workspaces')
    setListing(config)
    setShowPicker(false)
    newThread(config.workspaces.find(item => item.path === directory.display_path)?.id)
  }

  async function archive(thread: Thread) {
    try {
      await api<void>(token, 'threads/' + thread.session_id, {
        method: 'PATCH', body: JSON.stringify({ archived: !thread.archived }),
      })
      setThreads(await api<Thread[]>(token, 'threads'))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function decide(item: Approval, decision: 'approve' | 'deny') {
    setDeciding(true)
    setError('')
    try {
      await api<void>(token, 'runs/' + runId + '/approvals/' + item.id, {
        method: 'POST', body: JSON.stringify({ decision }),
      })
      setApproval(prior => prior?.id === item.id ? null : prior)
    } catch (err) {
      setError(String(err))
    } finally {
      setDeciding(false)
    }
  }

  async function cancel() {
    setStopping(true)
    setError('')
    try {
      await api<void>(token, 'runs/' + runId + '/cancel', { method: 'POST' })
    } catch (err) {
      setError(String(err))
      setStopping(false)
    }
  }

  async function submitReview(hunks: string[]) {
    const input = JSON.stringify([runId, hunks])
    if (reviewRequest.current?.input !== input) reviewRequest.current = { input, id: crypto.randomUUID() }
    setError('')
    try {
      const created = await api<Run>(token, 'runs/' + runId + '/review', {
        method: 'POST', body: JSON.stringify({ hunks, request_id: reviewRequest.current.id }),
      })
      reviewRequest.current = null
      openRun(created.run_id, 'checks')
    } catch (err) {
      setError(String(err))
    }
  }

  async function submitFeedback(feedback: ReviewFeedback) {
    const input = JSON.stringify([runId, feedback])
    if (reviewRequest.current?.input !== input) reviewRequest.current = { input, id: crypto.randomUUID() }
    const created = await api<Run>(token, 'runs/' + runId + '/review/feedback', {
      method: 'POST', body: JSON.stringify({ ...feedback, request_id: reviewRequest.current.id }),
    })
    reviewRequest.current = null
    openRun(created.run_id)
  }

  function preflightReview(hunks: string[]) {
    return api<PatchPreflight>(token, 'runs/' + runId + '/review/preflight', {
      method: 'POST', body: JSON.stringify({ hunks }),
    })
  }

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { x: event.clientX, width }
  }

  if (!listing) return <main className="login-screen">
    <form className="access" onSubmit={event => void connect(event)}>
      <div className="brand"><span className="brand-mark">✦</span>StellarWeaver</div>
      <h1>{authOptions?.password ? authMode === 'login' ? '登录工作台' : '注册账号' : '连接本地工作台'}</h1>
      {error && <p className="notice" role="alert">{error}</p>}
      {authOptions?.password && <>
        <label htmlFor="username">账号名</label>
        <input id="username" autoComplete="username" value={username} required minLength={3} maxLength={40}
          onChange={event => setUsername(event.target.value)} />
        <label htmlFor="password">密码</label>
        <input id="password" type="password" autoComplete={authMode === 'login' ? 'current-password' : 'new-password'}
          value={password} required minLength={8} maxLength={128} onChange={event => setPassword(event.target.value)} />
        <button className="primary" disabled={authorizing}>{authMode === 'login' ? '登录' : '注册并登录'}</button>
        <button type="button" onClick={() => { setAuthMode(authMode === 'login' ? 'register' : 'login'); setError(''); setPassword('') }}>
          {authMode === 'login' ? '注册账号' : '返回登录'}</button>
      </>}
      {authOptions?.oidc && <a className="login-link" href="/api/v1/auth/login">使用组织账号登录</a>}
      {authOptions?.bearer && !authOptions.password && <>
        <label htmlFor="connection-token">访问令牌</label>
        <input id="connection-token" type="password" autoComplete="off" value={draftToken}
          onChange={event => setDraftToken(event.target.value)} />
        <button className="primary">连接</button>
      </>}
    </form>
  </main>

  const workspaceName = currentProject?.label
  const title = activeThread?.is_review ? '补丁验证' : activeThread?.title.split('\n')[0] ?? run?.prompt.split('\n')[0] ?? (chatting ? '普通对话' : '新任务')

  return <main className={'workbench' + (panel ? ' panel-open' : '') + (showNavigation ? ' nav-open' : '')}
    style={{ '--inspector-width': width + 'px' } as React.CSSProperties}
    onKeyDown={event => { if (event.key === 'Escape') { setPanel(null); setShowNavigation(false) } }}>
    {showNavigation && <button className="nav-scrim" aria-label="关闭线程导航" onClick={() => setShowNavigation(false)} />}
    <aside className="sidebar" aria-label="线程导航">
      <div className="brand"><span className="brand-mark" aria-hidden="true">✦</span><span>StellarWeaver</span></div>
      {writable ? <div className="sidebar-actions">
        <button className="new-thread" type="button" onClick={() => newThread(chatWorkspace?.id)}><span aria-hidden="true">＋</span> 新任务</button>
        {listing.manage_workspaces && <button className="new-workspace" type="button" onClick={() => setShowPicker(true)}>新建工作区</button>}
      </div>
        : <div className="sidebar-empty">查看模式</div>}
      <nav className="threads" aria-label="项目与线程">
        {listing.workspaces.map(item => <WorkspaceGroup key={item.id} item={item}
          threads={threads.filter(thread => thread.workspace_id === item.id)} selectedSession={run?.session_id}
          runId={runId} selectedWorkspace={selectedWorkspace} waiting={Boolean(approval)} writable={writable}
          manage={Boolean(listing.manage_workspaces)} onNew={newThread} onOpen={openRun}
          onArchive={thread => void archive(thread)} />)}
        {!threads.length && <p className="sidebar-empty">还没有线程。</p>}
      </nav>
      <div className="sidebar-footer">
        <button type="button" className="sidebar-skills" onClick={() => setShowSkills(true)}>Skills 库</button>
        <span className="local-indicator"><i />{listing.account?.name ?? '本机工作台'}</span>
        {listing.account?.role === 'admin' && <button type="button" onClick={() => setShowAdmin(true)}>审计与用量</button>}
        <button type="button" onClick={() => void disconnect()}>{listing.manage_workspaces ? '退出登录' : '断开'}</button>
      </div>
    </aside>

    <section className="conversation" aria-label="当前对话">
      <header className="topbar">
        <button className="icon-btn nav-toggle" type="button" aria-label="打开线程导航" onClick={() => setShowNavigation(true)}>☰</button>
        <div className="thread-heading"><h1 title={title}>{title}</h1>
          <div className="thread-meta">
            {workspaceName && <span>{workspaceName}</span>}
            {!chatting && (run?.isolated || (!runId && listing.isolated)) && <span className="meta-segment">独立工作区</span>}
            {run && <><span className="meta-segment" data-testid="run-status" aria-label={runLabel(run.status)}>{runLabel(run.status)}</span>
              <span className="sr-only" data-testid="session-id">{run.session_id}</span></>}
          </div>
        </div>
        {run && <button className="panel-toggle" type="button" aria-expanded={Boolean(panel)} aria-controls={panel ? 'run-inspector' : undefined}
          onClick={() => setPanel(panel ? null : run.has_report ? 'changes' : 'logs')}>运行详情</button>}
      </header>

      <div className="chat-scroll" ref={scroll}
        onScroll={event => { const el = event.currentTarget; nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 110 }}>
        {error && <p className="notice chat-width" role="alert">{error}</p>}
        {reconnecting && <p className="chat-width muted" role="status">正在重新连接运行事件…</p>}
        {runId && !run && !error && <p className="chat-width muted" role="status">正在加载对话…</p>}
        {!runId && <div className="empty-state">
          <span className="empty-mark" aria-hidden="true">✦</span>
          <h2>今天想完成什么？</h2>
          <p>{chatting ? '直接开始对话。' : listing.isolated ? '新线程从仓库已提交的 HEAD 创建独立工作区。' : '选择项目，开始一个新任务。'}</p>
        </div>}
        <div className="chat-width">
          {conversation.map(turn => <Turn key={turn.run_id} run={turn} focused={turn.run_id === runId}
            events={turn.run_id === runId ? visibleEvents : undefined}
            hasReport={Boolean(turn.has_report)} approval={turn.run_id === runId && approval && run?.status === 'active' && !stopping
              ? <ApprovalCard item={approval} busy={deciding} onDecision={decision => void decide(approval, decision)} /> : null}
            onOpen={() => openRun(turn.run_id)}
            onLogs={() => openRun(turn.run_id, 'logs')}
            onReview={() => openRun(turn.run_id, 'changes')}
            onChecks={() => openRun(turn.run_id, 'checks')} />)}
        </div>
      </div>

      <div className="composer-wrap">
        <form className="composer" onSubmit={submit}>
          {activeThread?.archived && <div className="composer-notice"><span>此会话已归档。</span>
            {writable && <button type="button" onClick={() => void archive(activeThread)}>恢复会话</button>}
          </div>}
          {recovery && <div className="recovery">
            <label className="inline-label"><input type="checkbox" checked={acknowledged}
              onChange={event => setAcknowledged(event.target.checked)} />我已检查中断记录，确认继续</label>
            <label>继续方式<select value={continuation} onChange={event => setContinuation(event.target.value as 'history' | 'fresh')}>
              <option value="history">带上已完成的对话</option>
              <option value="fresh">用新提示检查工作区</option>
            </select></label>
          </div>}
          {needLatest && <div className="composer-notice"><span>最新一轮已中断。</span>
            <button type="button" onClick={() => newest && openRun(newest.run_id)}>查看中断记录</button>
          </div>}
          {run && writable && !canContinue && !run.is_review && <div className="composer-notice">
            <span>此线程仅供查看。</span><button type="button" onClick={() => newThread()}>开始新任务</button>
          </div>}
          {run?.is_review && writable && <div className="composer-notice">
            <span>这是独立的补丁验证记录。</span><button type="button" onClick={() => newThread()}>开始新任务</button>
          </div>}
          <label className="sr-only" htmlFor="prompt">任务说明</label>
          <textarea id="prompt" ref={composer} value={draft} maxLength={8000} rows={2} disabled={!writable}
            onChange={event => setDraft(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) {
                event.preventDefault()
                event.currentTarget.form?.requestSubmit()
              }
            }}
            placeholder={!writable ? '你的角色可以查看此项目的会话。' : active ? '本轮运行中，可先写下追问…' : canContinue ? '继续追问这个线程…' : '描述我可以帮你完成的任务…'} />
          <div className="composer-toolbar">
            {!runId && listing.workspaces.length > 1 && <label className="inline-label">项目
              <select value={workspace} onChange={event => setWorkspace(event.target.value)}>
                {listing.workspaces.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select></label>}
            {currentProject?.path && <code className="composer-path" title={currentProject.path}>{currentProject.path}</code>}
            {listing.repair_enabled && !chatting && <label className="inline-label">
              <span className="sr-only">运行模式</span>
              <select value={mode} onChange={event => setMode(event.target.value as Mode)}>
                <option value="read-only">只读</option><option value="repair">修复 · 逐项审批</option>
              </select></label>}
            <span className="model-label">{listing.provider === 'deepseek' ? 'DeepSeek · deepseek-flash' : 'FakeModel · 离线'}</span>
            <div className="composer-end">
              <ContextMeter events={visibleEvents} limit={listing.context_window_tokens} />
              {writable && run?.status === 'active' && <button type="button" disabled={stopping} onClick={() => void cancel()}>
                {stopping ? '正在取消…' : '取消运行'}</button>}
              <button className="primary send-button" disabled={!canSend || !draft.trim()}>
                {submitting ? '提交中…' : runId ? '发送追问' : chatting ? '发送' : mode === 'repair' ? '运行修复任务' : '运行只读任务'}
              </button>
            </div>
          </div>
        </form>
      </div>
    </section>
    {panel && <>
      <div className="resize-handle" role="separator" aria-label="调整运行详情宽度" aria-orientation="vertical"
        aria-valuenow={width} aria-valuemin={330} aria-valuemax={680} tabIndex={0}
        onPointerDown={startDrag} onPointerMove={event => {
          if (drag.current) setWidth(clampWidth(drag.current.width + drag.current.x - event.clientX))
        }}
        onPointerUp={() => { drag.current = null }} onLostPointerCapture={() => { drag.current = null }}
        onKeyDown={event => {
          if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.preventDefault()
            setWidth(clampWidth(width + (event.key === 'ArrowLeft' ? 24 : -24)))
          }
        }} />
      <button className="panel-scrim" type="button" aria-label="收起运行详情" onClick={() => setPanel(null)} />
      <Inspector key={runId} active={panel} onSelect={setPanel} onClose={() => setPanel(null)}
        delivery={currentReport} events={visibleEvents}
        canApply={run?.status === 'completed' && Boolean(listing.isolated && listing.repair_enabled)}
        canFeedback={Boolean(canContinue && !activeThread?.archived && latestIsKnown && !active)}
        isReview={Boolean(run?.is_review)} targetLabel={currentProject?.path ?? currentProject?.label}
        onReview={submitReview} onFeedback={submitFeedback} onPreflight={preflightReview} />
    </>}
    {showAdmin && <AdminPanel token={token} onClose={() => setShowAdmin(false)} />}
    {showSkills && <SkillsPanel token={token} onClose={() => setShowSkills(false)} />}
    {showPicker && <ProjectPicker onClose={() => setShowPicker(false)} onCreate={createProject} />}
  </main>
}

createRoot(document.getElementById('root')!).render(<App />)
