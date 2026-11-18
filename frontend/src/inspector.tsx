import { useState } from 'react'
import type { Delivery, RunEvent } from './api'
import { eventLabel } from './conversation'

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

export function Inspector({ active, onSelect, onClose, delivery, events, canApply, onReview }: {
  active: InspectorTab; onSelect: (tab: InspectorTab) => void; onClose: () => void
  delivery: Delivery | null; events: RunEvent[]; canApply: boolean
  onReview: (hunks: string[]) => Promise<void>
}) {
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const files = delivery?.review?.files ?? []
  const all = files.flatMap(file => file.hunks.map(hunk => hunk.id))
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
      {active === 'logs' ? <>
        <h2>运行日志</h2>
        {!events.length && <p className="muted">等待运行事件。</p>}
        <ol className="event-list" aria-label="运行事件">
          {events.filter(event => event.type !== 'assistant.delta').map(event =>
            <li key={event.seq} data-seq={event.seq}>
              <strong>{eventLabel(event)}</strong>
              {eventLabel(event) !== event.type && <small>{event.type}</small>}
              {event.payload.status && <span className="muted">{event.payload.status}</span>}
              {event.payload.code && <code>{event.payload.code}</code>}
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
          {!delivery.checks.length && <p className="muted">本轮没有运行验证命令。</p>}
          {delivery.checks.map(check => <details className="check" key={check.operation_id}>
            <summary>退出码 {check.exit_code ?? '无'} · {check.state}
              {check.state === 'stale' && '（原始结果 ' + check.observed + '）'}</summary>
            <pre>{JSON.stringify(check.argv)}{'\n'}cwd: {check.cwd}{'\n'}{check.stdout}{check.stderr}</pre>
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
              <button type="button" onClick={() => setSelected(selected.length === all.length ? [] : all)}>
                {selected.length === all.length ? '清空选择' : '全选改动块'}
              </button>
              <button type="button" disabled={!selected.length} onClick={() => exportPatch(selected)}>下载所选 Diff</button>
            </div>
            {files.map(file => <details className="diff-file" key={file.path}>
              <summary>{file.path} · {file.hunks.length} 个改动块</summary>
              {file.hunks.map(hunk => <div key={hunk.id} className="hunk">
                <label><input type="checkbox" checked={selected.includes(hunk.id)}
                  onChange={e => setSelected(e.target.checked ? [...selected, hunk.id] : selected.filter(id => id !== hunk.id))} />
                  选择 {hunk.diff.split('\n', 1)[0]}</label>
                <Diff patch={hunk.diff} />
              </div>)}
            </details>)}
            {canApply && <div className="apply-bar">
              <span className="muted">已选 {selected.length} 个改动块</span>
              <button className="primary" type="button" disabled={!selected.length || busy} onClick={async () => {
                setBusy(true)
                try { await onReview(selected) } finally { setBusy(false) }
              }}>在独立工作区应用所选并验证</button>
            </div>}
          </section>}
        </>}
      </section>}
    </div>
  </aside>
}
