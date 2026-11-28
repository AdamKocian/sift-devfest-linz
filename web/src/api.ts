// Typed client for the Sift API. In dev, Vite proxies /api to the FastAPI server.
import type {
  ActivityItem,
  ChatEvent,
  Connection,
  Guardrail,
  Meta,
  Overview,
  ProposalDetail,
  ProposalSummary,
  RunDetail,
  RunEvent,
  RunSummary,
  SettingsResponse,
  SettingsValues,
  SignalDetail,
  SignalSummary,
  StatusGroup,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const data = await res.json()
      if (typeof data.detail === 'string') message = data.detail
      else if (Array.isArray(data.detail)) message = data.detail.map((d: { msg: string }) => d.msg).join(', ')
    } catch {
      // not JSON: keep the status text
    }
    throw new ApiError(res.status, message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function qs(params: Record<string, string | undefined | null>): string {
  const entries = Object.entries(params).filter(([, v]) => v != null && v !== '') as [string, string][]
  return entries.length ? `?${new URLSearchParams(entries)}` : ''
}

export interface SignalQuery {
  kind?: string
  service?: string
  usage?: string
  status?: string
  q?: string
  sort?: string
}

export const api = {
  meta: () => request<Meta>('GET', '/meta'),
  overview: () => request<Overview>('GET', '/overview'),
  signals: (query: SignalQuery = {}) =>
    request<{ items: SignalSummary[]; total: number; facets: { kinds: string[]; services: string[] } }>(
      'GET',
      `/signals${qs({ ...query })}`,
    ),
  signal: (id: string) => request<SignalDetail>('GET', `/signals/${encodeURIComponent(id)}`),
  proposals: (status?: StatusGroup) =>
    request<{ items: ProposalSummary[]; counts: Record<StatusGroup, number> }>(
      'GET',
      `/proposals${qs({ status })}`,
    ),
  proposal: (id: string) => request<ProposalDetail>('GET', `/proposals/${encodeURIComponent(id)}`),
  approve: (id: string, ruleVersion?: string) =>
    request<ProposalDetail>('POST', `/proposals/${encodeURIComponent(id)}/approve`, { rule_version: ruleVersion }),
  reject: (id: string, reason?: string) =>
    request<ProposalDetail>('POST', `/proposals/${encodeURIComponent(id)}/reject`, { reason }),
  revert: (id: string, reason?: string) =>
    request<ProposalDetail>('POST', `/proposals/${encodeURIComponent(id)}/revert`, { reason }),
  activity: (params: { actor?: string; type?: string; subject_id?: string; limit?: number } = {}) =>
    request<{ items: ActivityItem[] }>(
      'GET',
      `/activity${qs({ ...params, limit: params.limit ? String(params.limit) : undefined })}`,
    ),
  runs: () => request<{ items: RunSummary[] }>('GET', '/runs'),
  run: (id: string) => request<RunDetail>('GET', `/runs/${encodeURIComponent(id)}`),
  startRun: (type: RunSummary['type']) => request<RunSummary>('POST', '/runs', { type }),
  settings: () => request<SettingsResponse>('GET', '/settings'),
  saveSettings: (patch: Partial<SettingsValues>) => request<SettingsResponse>('PUT', '/settings', patch),
  guardrails: () => request<{ items: Guardrail[] }>('GET', '/guardrails'),
  addGuardrail: (body: { name: string; reason: string; service?: string; kind?: string; key_pattern?: string }) =>
    request<Guardrail>('POST', '/guardrails', body),
  deleteGuardrail: (id: string) => request<void>('DELETE', `/guardrails/${encodeURIComponent(id)}`),
  connections: () => request<{ items: Connection[] }>('GET', '/connections'),
  resetDemo: () => request<{ status: string }>('POST', '/demo/reset'),
}

// --- server-sent events -----------------------------------------------------------------

interface RawEvent {
  event: string
  data: string
}

async function* readSse(res: Response, signal?: AbortSignal): AsyncGenerator<RawEvent> {
  if (!res.body) return
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (!signal?.aborted) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let cut: number
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, cut)
        buffer = buffer.slice(cut + 2)
        let event = 'message'
        const data: string[] = []
        for (const line of block.split('\n')) {
          if (line.startsWith('event: ')) event = line.slice(7)
          else if (line.startsWith('data: ')) data.push(line.slice(6))
        }
        if (data.length) yield { event, data: data.join('\n') }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/** Ask Sift. Streams tool calls, text tokens and proposal rows. */
export async function* streamChat(
  body: { message: string; proposal_id?: string; signal_id?: string },
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`)
  for await (const raw of readSse(res, signal)) {
    yield JSON.parse(raw.data) as ChatEvent
  }
}

/** Follow a run. Calls onEvent for each run event and onDone with the final summary. */
export function followRun(
  id: string,
  handlers: { onEvent: (e: RunEvent) => void; onDone: (run: RunSummary) => void; onError?: () => void },
): () => void {
  const source = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`)
  source.addEventListener('run_event', (e) => handlers.onEvent(JSON.parse((e as MessageEvent).data)))
  source.addEventListener('run', (e) => {
    handlers.onDone(JSON.parse((e as MessageEvent).data))
    source.close()
  })
  source.onerror = () => {
    // EventSource reconnects by itself (with Last-Event-ID). Only report a closed stream.
    if (source.readyState === EventSource.CLOSED) handlers.onError?.()
  }
  return () => source.close()
}
