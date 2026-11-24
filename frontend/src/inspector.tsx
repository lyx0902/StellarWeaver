import { useState } from 'react'
import type { Delivery, PatchPreflight, ReviewFeedback, RunEvent, UndoTarget } from './api'
import { eventLabel, eventStatus } from './conversation'

export type InspectorTab = 'changes' | 'checks' | 'logs'
const tabs: { id: InspectorTab; label: string }[] = [
  { id: 'changes', label: '改动' }, { id: 'checks', label: '验证' }, { id: 'logs', label: '日志' },
]

function download(content: string, name: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function Diff({ patch }: { patch: string }) {
  let before = 0
  let after = 0
  let inHunk = false
  return <div className="diff" role="region" aria-label="补丁差异" tabIndex={0}>
    {patch.replace(/\n$/, '').split('\n').map((line, i) => {
      const header = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line)
      if (header) {
        inHunk = true
        before = Number(header[1])
        after = Number(header[2])
      }
      const add = inHunk && line.startsWith('+')
      const remove = inHunk && line.startsWith('-')
      const context = inHunk && line.startsWith(' ')
      return <div key={i} className={'diff-line ' + (header ? 'diff-header' : add ? 'diff-added' : remove ? 'diff-removed' : '')}>
        <span aria-hidden="true">{remove || context ? before++ : ''}</span>
        <span aria-hidden="true">{add || context ? after++ : ''}</span>
        <code>{line || ' '}</code>
      </div>
    })}
  </div>
}

const statusLabels: Record<PatchPreflight['status'], string> = {
  ready: '目标预检通过', base_changed: '目标分支基线已变化', conflict: '所选补丁与目标提交冲突',
  local_changes: '补丁涉及的文件有本地改动',
}

function PreflightResult({ result, targetLabel, onDownload }: {
  result: PatchPreflight; targetLabel?: string; onDownload: () => void
}) {
  return <section className="review-result" aria-label="补丁预检结果" aria-live="polite">
    <strong>{statusLabels[result.status]}</strong>
    <dl>
      {targetLabel && <><dt>目标项目</dt><dd>{targetLabel}</dd></>}
      <dt>目标分支</dt><dd>{result.branch ?? '分离 HEAD'}</dd>
      <dt>目标提交</dt><dd><code>{result.target_head}</code></dd>
      <dt>补丁基线</dt><dd><code>{result.source_base}</code></dd>
      <dt>补丁 SHA-256</dt><dd><code>{result.patch_sha256}</code></dd>
    </dl>
    {[
      { label: '所选文件', paths: result.paths },
      { label: '冲突文件', paths: result.conflict_paths },
      { label: '有本地改动的文件', paths: result.local_changes },
      { label: '相比补丁基线已变动的文件', paths: result.changed_since_base ?? [] },
    ].filter(group => group.paths.length).map(group => <div key={group.label}>
      <h3>{group.label}</h3><ul>{group.paths.map(path => <li key={path}><code>{path}</code></li>)}</ul>
    </div>)}
    {result.target_head !== result.source_base && <p className="muted">在目标提交上重新修订和复验后再交接。</p>}
    {result.handoff_ready ? <>
      <p className="muted">下载后在 IDE 核对目标分支和提交，再应用补丁。</p>
      <button type="button" onClick={onDownload}>下载已复验补丁</button>
    </> : !result.verified_selection && <p className="muted">要交接到 IDE，请先复验所选改动，再从复验结果预检全部改动。</p>}
  </section>
}

export function Inspector({ active, onSelect, onClose, delivery, events, canApply, canFeedback, isReview,
  targetLabel, onReview, onFeedback, onPreflight, exportRun, undoTarget, onUndo }: {
  active: InspectorTab; onSelect: (tab: InspectorTab) => void; onClose: () => void
  delivery: Delivery | null; events: RunEvent[]; canApply: boolean; canFeedback: boolean
  isReview: boolean; targetLabel?: string
  onReview: (hunks: string[]) => Promise<void>
  onFeedback: (feedback: ReviewFeedback) => Promise<void>
  onPreflight: (hunks: string[]) => Promise<PatchPreflight>
  exportRun?: (format: 'json' | 'html') => Promise<void>
  undoTarget: UndoTarget | null; onUndo: (target: UndoTarget) => Promise<void>
}) {
  const [selection, setSelection] = useState<string[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState<ReviewFeedback | null>(null)
  const [reviewError, setReviewError] = useState('')
  const [confirmUndo, setConfirmUndo] = useState<string | null>(null)
  const [actionError, setActionError] = useState('')
  const [preview, setPreview] = useState<{ key: string; result: PatchPreflight } | null>(null)
  const files = delivery?.review?.files ?? []
  const all = files.flatMap(file => file.hunks.map(hunk => hunk.id))
  const reviewed = isReview && delivery?.verification === 'passed'
  const selected = selection ?? (reviewed ? all : [])
  const selectionKey = JSON.stringify(selected)

  async function act(action: () => Promise<void>) {
    setBusy(true)
    setActionError('')
    try {
      await action()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  function selectHunks(ids: string[]) {
    setSelection(ids)
    setPreview(null)
    setReviewError('')
  }

  async function preflight() {
    setBusy(true)
    setPreview(null)
    setReviewError('')
    try {
      const result = await onPreflight(selected)
      setPreview({ key: selectionKey, result })
    } catch (err) {
      setReviewError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  function feedbackControl(path: string, hunk?: string) {
    if (!canApply || !canFeedback) return null
    if (!feedback || feedback.path !== path || feedback.hunk !== hunk) {
      return <button className="quiet" type="button" disabled={busy} onClick={() => {
        setReviewError('')
        setFeedback({ path, hunk, text: '' })
      }}>{hunk ? '反馈此改动块' : '反馈此文件'}</button>
    }
    return <form className="review-feedback" onSubmit={async event => {
      event.preventDefault()
      if (!feedback.text.trim() || busy) return
      setBusy(true)
      setReviewError('')
      try { await onFeedback(feedback) }
      catch (err) { setReviewError(err instanceof Error ? err.message : String(err)) }
      finally { setBusy(false) }
    }}>
      <label>审阅意见<textarea rows={3} maxLength={2000} value={feedback.text} disabled={busy}
        onChange={event => setFeedback({ ...feedback, text: event.target.value })} /></label>
      <button type="submit" disabled={busy || !feedback.text.trim()}>发送到原线程并修订</button>
      <button type="button" disabled={busy} onClick={() => setFeedback(null)}>取消</button>
    </form>
  }

  function exportPatch(hunks: string[]) {
    const chosen = new Set(hunks)
    const patch = files.map(file => {
      const blocks = file.hunks.filter(hunk => chosen.has(hunk.id))
      return blocks.length ? file.header + blocks.map(hunk => hunk.diff).join('') : ''
    }).join('')
    download(patch, 'stellarweaver-' + delivery?.run_id + '.patch', 'text/plain')
  }
  return <aside className="inspector" id="run-inspector" aria-label="运行详情">
    <div className="inspector-header">
      <nav aria-label="运行详情视图">
        {tabs.map(tab => <button key={tab.id} type="button" aria-pressed={active === tab.id}
          onClick={() => onSelect(tab.id)}>{tab.label}</button>)}
      </nav>
      <button type="button" className="icon-btn" aria-label="关闭运行详情" onClick={onClose}>×</button>
    </div>
    <div className="inspector-body">
      {actionError && <p className="notice" role="alert">{actionError}</p>}
      {active === 'changes' && undoTarget && <section className="undo-file" aria-label="撤销文件修改">
        {confirmUndo === undoTarget.operation_id ? <>
          {undoTarget.paths ? <>
            <p>撤销这次补丁对以下 {undoTarget.paths.length} 个文件的修改？</p>
            <ul>{undoTarget.paths.map(path => <li key={path}><code>{path}</code></li>)}</ul>
          </> : <p>撤销隔离工作区中对 <code>{undoTarget.path}</code> 的最近一次修改？</p>}
          <div className="actions">
            <button type="button" disabled={busy} onClick={() => void act(() => onUndo(undoTarget))}>确认撤销</button>
            <button type="button" disabled={busy} onClick={() => setConfirmUndo(null)}>取消</button>
          </div>
        </> : <button type="button" disabled={busy}
          onClick={() => setConfirmUndo(undoTarget.operation_id)}>撤销最近一次文件修改</button>}
      </section>}
      {active === 'logs' ? <>
        <h2>运行日志</h2>
        {exportRun && <div className="actions">
          <button type="button" disabled={busy} onClick={() => void act(() => exportRun('html'))}>下载离线回放</button>
          <button type="button" disabled={busy} onClick={() => void act(() => exportRun('json'))}>导出运行 JSON</button>
        </div>}
        {!events.length && <p className="muted">等待运行事件。</p>}
        <ol className="event-list" aria-label="运行事件">
          {events.filter(event => event.type !== 'assistant.delta').map(event =>
            <li key={event.seq} data-seq={event.seq}>
              <strong>{eventLabel(event)}</strong>
              {eventLabel(event) !== event.type && <small>{event.type}</small>}
              {eventStatus(event) && <span className="muted">{eventStatus(event)}</span>}
              {event.payload.code && <code>{event.payload.code}</code>}
              {(event.type === 'exploration.started' || event.type === 'exploration.completed' || event.payload.tool_name === 'delegate_exploration')
                && event.payload.paths?.length ? (
                <span>{event.payload.paths.map(path => <code key={path}>{path}</code>)}</span>
              ) : null}
              {event.type === 'exploration.started' && event.payload.goal && <pre>{event.payload.goal}</pre>}
              {typeof event.payload.status_code === 'number' && <span>HTTP {event.payload.status_code}</span>}
              {typeof event.payload.exit_code === 'number' && <span>退出码 {event.payload.exit_code}</span>}
              {event.payload.argv && <pre>{JSON.stringify(event.payload.argv)}</pre>}
              {event.payload.content && <pre>{event.payload.content}</pre>}
            </li>)}
        </ol>
      </> : !delivery ? <p className="muted">本轮尚无交付报告。</p> : <section aria-label="交付报告">
        <div className="report-head">
          <div><h2>{active === 'changes' ? '交付改动' : '验证结果'}</h2>
            <span className={'check-status check-' + delivery.verification} data-testid="verification">{delivery.verification}</span></div>
          <button className="quiet" type="button" onClick={() => download(
            JSON.stringify(delivery, null, 2), 'stellarweaver-' + delivery.run_id + '.json', 'application/json',
          )}>下载 JSON 报告</button>
        </div>
        {active === 'checks' ? <>
          {canApply && reviewed && all.length > 0 && <button type="button" onClick={() => onSelect('changes')}>预检并交接到 IDE</button>}
          {!delivery.checks.length && <p className="muted">本轮没有运行验证命令。</p>}
          {delivery.checks.map(check => <details className="check" key={check.operation_id}>
            <summary>{check.tool && check.tool !== 'run_command' && check.tool + ' · '}退出码 {check.exit_code ?? '无'} · {check.state}
              {check.state === 'stale' && '（原始结果 ' + check.observed + '）'}</summary>
            <pre>{check.diagnostic ? check.diagnostic.framework + ' ' + check.diagnostic.target : JSON.stringify(check.argv)}{'\n'}cwd: {check.cwd}{'\n'}{check.stdout}{check.stderr}</pre>
          </details>)}
        </> : <>
          {!delivery.baseline_comparable && <p className="muted">工作区快照不完整，无法列出全部变更。</p>}
          {delivery.review_error && <p>无法生成候选补丁 <code>{delivery.review_error}</code></p>}
          {!all.length && delivery.patches.map(patch => <details className="diff-file" key={patch.operation_id}>
            <summary>{patch.path}</summary><Diff patch={patch.diff} />
          </details>)}
          {!all.length && !delivery.patches.length && <p className="muted">本轮没有可审阅的改动。</p>}
          {all.length > 0 && <section aria-label="补丁审阅">
            <p className="muted">相对基线 <code>{delivery.review?.base.slice(0, 12)}</code> · {files.length} 个文件</p>
            <div className="actions review-actions">
              <button type="button" onClick={() => exportPatch(all)}>下载完整 Diff</button>
              <button type="button" onClick={() => selectHunks(selected.length === all.length ? [] : all)}>
                {selected.length === all.length ? '清空选择' : '全选改动块'}
              </button>
              <button type="button" disabled={!selected.length} onClick={() => exportPatch(selected)}>下载所选 Diff</button>
            </div>
            {files.map(file => <details className="diff-file" key={file.path}>
              <summary>{file.path} · {file.hunks.length} 个改动块</summary>
              {feedbackControl(file.path)}
              {file.hunks.map(hunk => <div key={hunk.id} className="hunk">
                <label><input type="checkbox" checked={selected.includes(hunk.id)}
                  onChange={e => selectHunks(e.target.checked ? [...selected, hunk.id] : selected.filter(id => id !== hunk.id))} />
                  选择 {hunk.diff.split('\n', 1)[0]}</label>
                <Diff patch={hunk.diff} />
                {feedbackControl(file.path, hunk.id)}
              </div>)}
            </details>)}
            {canApply && <div className="apply-bar">
              <span className="muted">已选 {selected.length} 个改动块</span>
              {!isReview && <button className="primary" type="button" disabled={!selected.length || busy} onClick={async () => {
                setBusy(true)
                try { await onReview(selected) } finally { setBusy(false) }
              }}>在独立工作区应用所选并验证</button>}
              <button type="button" disabled={!selected.length || busy} onClick={() => void preflight()}>预检目标分支</button>
            </div>}
            {reviewError && <p className="notice" role="alert">{reviewError}</p>}
            {preview?.key === selectionKey && <PreflightResult result={preview.result} targetLabel={targetLabel}
              onDownload={() => exportPatch(selected)} />}
          </section>}
        </>}
      </section>}
    </div>
  </aside>
}
