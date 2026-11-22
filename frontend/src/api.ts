export type Workspace = {
  id: string; label: string; kind?: 'chat' | 'project'; path?: string | null
  alias?: string | null; default_label?: string
}
export type Workspaces = {
  provider: string; repair_enabled: boolean; isolated?: boolean;
  context_window_tokens: number; workspaces: Workspace[]
  can_submit?: boolean; account?: { name: string; role: 'admin' | 'member' | 'viewer' } | null
  manage_workspaces?: boolean
  experiment_available?: boolean
}
export type AuthOptions = { oidc: boolean; bearer: boolean; password?: boolean }
export type Thread = {
  run_id: string; session_id: string; workspace_id: string; title: string; status: string
  needs_approval: boolean; is_review: boolean; archived?: boolean
}
export type Mode = 'read-only' | 'repair' | 'auto'
export type Approval = {
  id: string; expires_at: string; tool: string; path?: string; diff?: string
  argv?: string[]; cwd?: string; timeout_seconds?: number
  before_sha256?: string; after_sha256?: string
  base_commit?: string; patch_sha256?: string; paths?: string[]
  query?: string
  security?: { level: string; capabilities: string[]; reason_codes: string[] }
}
export type Delivery = {
  run_id: string; verification: string; scope: string
  baseline_comparable: boolean; changed_paths: string[]
  patches: { operation_id: string; path: string; diff: string }[]
  review?: { base: string; files: { path: string; header: string; hunks: { id: string; diff: string }[] }[] }
  review_error?: string
  checks: {
    operation_id: string; state: string; observed: string; exit_code: number | null
    tool?: string; diagnostic?: { action: string; target: string; framework: string } | null
    argv: string[]; cwd: string; stdout: string; stderr: string
  }[]
}
export type PatchPreflight = {
  status: 'ready' | 'base_changed' | 'conflict' | 'local_changes'
  branch: string | null; target_head: string; source_base: string; patch_sha256: string
  paths: string[]; conflict_paths: string[]; local_changes: string[]; changed_since_base: string[] | null
  verified_selection: boolean; handoff_ready: boolean
}
export type ReviewFeedback = { path: string; hunk?: string; text: string }
export type SkillSelection = { id: string; name: string; source: string; sha256: string }
export type UndoTarget = { operation_id: string; path: string; tool_name: string }
export type Run = {
  run_id: string; session_id: string; status: string; prompt: string; provider?: string
  context_window_tokens?: number | null
  mode?: Mode; cancel_requested?: boolean; pending_approval?: Approval | null; through_seq?: number
  job_state?: 'queued' | 'running' | 'done' | null
  has_report?: boolean
  can_continue?: boolean
  workspace_id?: string
  isolated?: boolean
  is_review?: boolean
  is_undo?: boolean
  skills?: SkillSelection[]
  final_text?: string | null
  created_at?: string
  requires_acknowledgement?: boolean
  unconfirmed_operations?: { operation_id: string; tool_name: string; path?: string; argv?: string[] }[]
  outcome?: {
    run_status?: string; verification_status?: string; error_code?: string
    budget_reason?: string
    failure_phase?: string; unfinished_tools?: number; recorded_results?: number
  } | null
}
export type RunEvent = {
  seq: number; session_id: string; run_id: string; type: string; schema_version: number
  payload: Partial<Approval> & {
    content?: string; status?: string; tool_name?: string; code?: string
    staged_patch?: string
    skill_name?: string; skill_source?: string
    files?: number; checks?: number; skills?: number
    strategy?: string; before_tokens_estimated?: number; after_tokens_estimated?: number; input_budget?: number
    exit_code?: number; status_code?: number
    request_index?: number
    input_tokens?: number; context_window_tokens?: number; input_estimated?: boolean; estimated?: boolean
    has_tools?: boolean
    purpose?: string
    sources?: { url: string; title?: string }[]
  }
}

export async function downloadRun(token: string, id: string, format: 'json' | 'html') {
  const headers = new Headers()
  if (token) headers.set('Authorization', 'Bearer ' + token)
  const response = await fetch('/api/v1/runs/' + id + '/export?format=' + format, { headers })
  if (!response.ok) {
    const data = await response.json()
    throw new Error(typeof data.detail === 'string' ? data.detail : '导出失败：' + response.status)
  }
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = 'stellarweaver-' + id + '.' + format
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

export async function api<T>(token: string, resource: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers)
  headers.set('X-StellarWeaver-Request', '1')
  if (token) headers.set('Authorization', 'Bearer ' + token)
  if (options?.body) headers.set('Content-Type', 'application/json')
  const response = await fetch('/api/v1/' + resource, { ...options, headers })
  if (response.status === 204) return undefined as T
  const data = await response.json()
  if (!response.ok) {
    throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败：' + response.status)
  }
  return data as T
}

export async function receive(
  token: string, runId: string, after: number, signal: AbortSignal,
  onEvent: (event: RunEvent) => void,
) {
  const headers = new Headers({ Accept: 'text/event-stream' })
  if (token) headers.set('Authorization', 'Bearer ' + token)
  const response = await fetch('/api/v1/runs/' + runId + '/events?after=' + after, { signal, headers })
  if (!response.ok || !response.body) throw new Error('无法订阅事件：' + response.status)
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  try {
    while (true) {
      const result = await reader.read()
      if (result.done) break
      buffer += result.value
      while (true) {
        const gap = /\r?\n\r?\n/.exec(buffer)
        if (!gap) break
        const frame = buffer.slice(0, gap.index)
        buffer = buffer.slice(gap.index + gap[0].length)
        const lines = frame.split(/\r?\n/)
        const data = lines.filter(line => line.startsWith('data:'))
          .map(line => line.slice(5).trimStart()).join('\n')
        if (data) {
          const event = JSON.parse(data) as RunEvent
          if (event.run_id === runId && event.schema_version === 1 && Number.isSafeInteger(event.seq)) {
            onEvent(event)
          }
        }
      }
    }
  } finally {
    await reader.cancel()
  }
}
