// Agent-first pieces: proposal rows, approve/reject dialogs, run status and timeline, Ask Sift.
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { RiArrowRightLine, RiArrowUpLine, RiCloseLine } from '@remixicon/react'
import { api } from './api'
import * as f from './format'
import { href, navigate } from './router'
import { invalidate, useApi, useApp, useNow, type ChatMessage } from './store'
import type {
  ProposalStatus,
  ProposalSummary,
  RunDetail,
  RunEvent,
  RunSummary,
  SignalStatus,
  Usage,
} from './types'
import {
  AgentMark,
  Button,
  cn,
  Dialog,
  Dot,
  Drawer,
  Loading,
  Progress,
  Spinner,
  Status,
  Tag,
  Textarea,
  TextLink,
  ToolCallRow,
  type Tone,
} from './ui'

// --- status mapping (DESIGN.md "Status tones") ------------------------------------------

const PROPOSAL_STATUS: Record<ProposalStatus, [Tone, string]> = {
  draft: ['gray', 'Draft'],
  awaiting_approval: ['away', 'Awaiting approval'],
  approved: ['feature', 'Approved'],
  applied: ['feature', 'Applied'],
  verifying: ['feature', 'Verifying'],
  verified: ['success', 'Verified'],
  rejected: ['gray', 'Rejected'],
  reverted: ['gray', 'Reverted'],
}

export function proposalTone(p: Pick<ProposalSummary, 'status' | 'failed_verification'>): [Tone, string] {
  if (p.status === 'reverted' && p.failed_verification) return ['error', 'Reverted: failed verification']
  return PROPOSAL_STATUS[p.status]
}

export function ProposalStatusLabel({ p }: { p: Pick<ProposalSummary, 'status' | 'failed_verification'> }) {
  const [tone, label] = proposalTone(p)
  if (p.status === 'verifying' || p.status === 'applied' || p.status === 'approved') {
    return (
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-strong">
        <Spinner className="size-2.5" />
        {label}
      </span>
    )
  }
  return <Status tone={tone}>{label}</Status>
}

const SIGNAL_STATUS: Partial<Record<SignalStatus, [Tone, string]>> = {
  protected: ['gray', 'Protected'],
  used: ['success', 'Used'],
  rarely: ['info', 'Rarely used'],
  unused: ['away', 'Unused'],
}

export function SignalStatusLabel({ status }: { status: SignalStatus }) {
  const s = SIGNAL_STATUS[status]
  if (s) return <Status tone={s[0]}>{s[1]}</Status>
  return <ProposalStatusLabel p={{ status: status as ProposalStatus, failed_verification: false }} />
}

export function usageText(u: Usage): string {
  const parts = (Object.entries(u.by_type) as [string, number][])
    .filter(([, n]) => n > 0)
    .map(([t, n]) => f.plural(n, f.REF_LABEL[t][0], f.REF_LABEL[t][1]))
  if (u.queries_30d) parts.push(f.plural(u.queries_30d, 'query', 'queries'))
  return parts.length ? `used by ${parts.join(', ')} in 30 d` : 'used by nothing in 30 d'
}

export function savingsText(p: ProposalSummary): string {
  if (p.action === 'redact' || p.savings.eur_month < 0.5) return 'no saving · removes personal data'
  return `−${f.gib(p.savings.gib_week)}/wk · ${f.eur(p.savings.eur_month)}/mo`
}

// --- proposal row -----------------------------------------------------------------------

export function ProposalRow({ p, className }: { p: ProposalSummary; className?: string }) {
  const { setApproveId, setRejectId } = useApp()
  const awaiting = p.status === 'awaiting_approval'
  return (
    <div
      className={cn(
        'flex flex-col gap-3 px-4 py-3.5 lg:flex-row lg:items-center lg:gap-4 lg:px-0 lg:py-3',
        className,
      )}
    >
      <a href={href(`/proposals/${p.id}`)} className="group min-w-0 flex-1 outline-none">
        <div className="flex items-center gap-2">
          <Tag>{f.KIND_TAG[p.kind]}</Tag>
          <span className="truncate text-ui font-medium text-strong group-hover:underline group-focus-visible:underline">
            {p.title}
          </span>
        </div>
        <div className="mt-1 text-ui-sm text-sub lg:text-ui">
          {savingsText(p)} · {usageText(p.usage)}
        </div>
      </a>
      <div className="flex shrink-0 items-center gap-2">
        {awaiting ? (
          <>
            <Button variant="ghost" onClick={() => setRejectId(p.id)} className="flex-1 lg:flex-none">
              Reject
            </Button>
            <Button onClick={() => setApproveId(p.id)} className="flex-1 lg:flex-none">
              Approve
            </Button>
          </>
        ) : (
          <span className="text-ui">
            <ProposalStatusLabel p={p} />
          </span>
        )}
      </div>
    </div>
  )
}

// --- approve / reject -------------------------------------------------------------------

function blastSentence(b: Record<string, number>): string {
  const n = (k: string, one: string, many: string) => f.plural(b[k] ?? 0, one, many)
  return `Used by ${n('dashboard', 'dashboard', 'dashboards')}, ${n('alert', 'alert', 'alerts')}, ${n(
    'slo',
    'SLO',
    'SLOs',
  )} and ${n('workflow', 'workflow', 'workflows')} in the last 90 days.`
}

export function ApproveDialog() {
  const { approveId, setApproveId, meta, toast } = useApp()
  const detail = useApi(approveId ? `approve:${approveId}` : null, () => api.proposal(approveId!))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const close = () => {
    setApproveId(null)
    setError(null)
  }
  const p = detail.data
  const blocked = p ? p.blocking_guardrails.length > 0 : false

  const apply = async () => {
    if (!p) return
    setBusy(true)
    setError(null)
    try {
      await api.approve(p.id)
      invalidate()
      close()
      toast('Rule applied. Verification has started.')
      navigate(`/proposals/${p.id}`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={approveId !== null}
      onClose={close}
      title="Apply this rule?"
      footer={
        <>
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button variant="primary" onClick={apply} disabled={!p || busy || blocked || p.status !== 'awaiting_approval'}>
            {busy && <Spinner className="border-white/40 border-t-white" />}
            Apply rule
          </Button>
        </>
      }
    >
      {!p ? (
        <Loading />
      ) : (
        <div className="space-y-3">
          <div className="font-medium text-strong">{p.title}</div>
          <p>
            {blastSentence(p.blast_radius)} Matcher hits {f.count(p.dry_run.matched_records_day)} of{' '}
            {f.count(p.dry_run.scope_records_day)} {p.dry_run.scope_label} a day
            {p.data_type === 'logs' && p.dry_run.errors_matched === 0 ? ', none of them ERROR or FATAL' : ''}.
          </p>
          <p>
            {p.savings.eur_month >= 0.5 ? `Saves about ${f.eur(p.savings.eur_month)} a month. ` : ''}
            Sift verifies volume, references and Davis problems after applying. Revert is automatic if
            verification fails.
          </p>
          {meta?.mode === 'fixture' && (
            <p className="text-ui-sm text-soft">Fixture mode: nothing is written to Dynatrace.</p>
          )}
          {blocked && (
            <p className="text-error-dark">Blocked by guardrail: {p.blocking_guardrails.join(', ')}.</p>
          )}
          {error && <p className="text-error-dark">{error}</p>}
        </div>
      )}
    </Dialog>
  )
}

export function RejectDialog() {
  const { rejectId, setRejectId, toast } = useApp()
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const detail = useApi(rejectId ? `reject:${rejectId}` : null, () => api.proposal(rejectId!))
  const close = () => {
    setRejectId(null)
    setReason('')
    setError(null)
  }
  const submit = async (e?: FormEvent) => {
    e?.preventDefault()
    if (!rejectId) return
    setBusy(true)
    try {
      await api.reject(rejectId, reason)
      invalidate()
      close()
      toast('Proposal rejected. The agent will not propose it again.')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      open={rejectId !== null}
      onClose={close}
      title="Reject this proposal?"
      footer={
        <>
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => submit()} disabled={busy}>
            Reject proposal
          </Button>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        {detail.data && <div className="font-medium text-strong">{detail.data.title}</div>}
        <label htmlFor="reject-reason" className="block">
          Why? The reason goes into the activity log.
        </label>
        <Textarea
          id="reject-reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Optional"
          maxLength={500}
        />
        {error && <p className="text-error-dark">{error}</p>}
      </form>
    </Dialog>
  )
}

// --- run status line --------------------------------------------------------------------

export function runLabel(type: RunSummary['type']): string {
  return { analyze: 'Analysis', discover: 'Discovery', verify: 'Verification' }[type]
}

export function RunningLine({ run, onDetails }: { run: RunSummary; onDetails: () => void }) {
  const now = useNow(1000)
  return (
    <div className="flex min-w-0 items-center gap-2 text-ui text-strong">
      <Spinner />
      <span className="min-w-0 truncate">{run.current_step ?? `${runLabel(run.type)} starting`}</span>
      <span className="shrink-0 text-sub tabular-nums">· {f.elapsed(run.started_at, now)}</span>
      <TextLink onClick={onDetails} className="shrink-0">
        Details
      </TextLink>
    </div>
  )
}

export function AgentStatusLine({
  agent,
  onTogglePause,
}: {
  agent: {
    state: 'idle' | 'running' | 'paused'
    last_run: RunSummary | null
    next_run_at: string | null
    verify_window_min: number
    fixture_verify_seconds: number | null
  }
  onTogglePause: () => void
}) {
  const { live, openRun } = useApp()
  const now = useNow(15000)
  const running = live && live.run.status === 'running' ? live.run : null
  return (
    <div className="mb-6 rounded-2xl bg-bg-white px-4 py-3 lg:mb-8 lg:rounded-none lg:bg-transparent lg:p-0">
      <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between lg:gap-6">
        {running ? (
          <RunningLine run={running} onDetails={() => openRun(running.id)} />
        ) : (
          <div className="flex min-w-0 items-center gap-2 text-ui text-strong">
            <Dot tone={agent.state === 'paused' ? 'away' : 'gray'} />
            <span className="truncate">
              {agent.state === 'paused' ? 'Agent paused' : 'Agent idle'}
              {agent.last_run && (
                <>
                  <span className="text-sub"> · last run </span>
                  <TextLink onClick={() => openRun(agent.last_run!.id)}>{f.ago(agent.last_run.started_at, now)}</TextLink>
                </>
              )}
            </span>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-ui text-sub">
          {agent.next_run_at && <span>Next run {f.clock(agent.next_run_at)}</span>}
          <span>
            Verify window {agent.verify_window_min} min
            {agent.fixture_verify_seconds != null && (
              <span className="text-soft"> (fixture: {agent.fixture_verify_seconds} s)</span>
            )}
          </span>
          <Button variant="ghost" size="sm" onClick={onTogglePause} className="-mx-2 lg:-mx-2">
            {agent.state === 'paused' ? 'Resume agent' : 'Pause agent'}
          </Button>
        </div>
      </div>
      {running && <Progress className="mt-2.5" value={(running.steps_done + 0.5) / Math.max(1, running.steps_total)} />}
    </div>
  )
}

// --- run timeline drawer ----------------------------------------------------------------

interface Step {
  n: number
  label: string
  done: boolean
  duration: number | null
  tools: RunEvent[]
  proposals: RunEvent[]
}

function groupSteps(events: RunEvent[]): Step[] {
  const steps = new Map<number, Step>()
  for (const e of events) {
    const n = Number((e.result as { step?: number } | null)?.step ?? 0)
    if (e.type === 'step') steps.set(n, { n, label: e.label, done: false, duration: null, tools: [], proposals: [] })
    const s = steps.get(n) ?? [...steps.values()].at(-1)
    if (!s) continue
    if (e.type === 'tool') s.tools.push(e)
    if (e.type === 'step_done') {
      s.done = true
      s.duration = e.duration_ms
    }
    if (e.type === 'proposal') s.proposals.push(e)
  }
  return [...steps.values()].sort((a, b) => a.n - b.n)
}

function StepRow({ step, running, failed }: { step: Step; running: boolean; failed: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <li className="py-2.5">
      <div className="flex items-center gap-2.5">
        {step.done ? <Dot tone="success" /> : running ? <Spinner /> : <Dot tone={failed ? 'error' : 'gray'} />}
        <span className="min-w-0 flex-1 text-ui text-strong">{step.label}</span>
        <span className="shrink-0 text-ui-sm text-soft tabular-nums">{f.duration(step.duration)}</span>
      </div>
      {step.tools.length > 0 && (
        <div className="mt-1 pl-4">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            className="rounded-sm text-ui-sm text-sub outline-none hover:text-strong focus-visible:ring-2 focus-visible:ring-primary/40"
          >
            {open ? 'Hide' : 'Show'} {f.plural(step.tools.length, 'tool call')}
          </button>
          {open && (
            <div className="mt-1.5 space-y-1">
              {step.tools.map((t) => (
                <ToolCallRow key={t.seq} label={t.label} duration={f.duration(t.duration_ms)} rows={t.rows} />
              ))}
            </div>
          )}
        </div>
      )}
      {step.proposals.map((p) => {
        const id = String((p.result as { proposal?: string } | null)?.proposal ?? '')
        return (
          <div key={p.seq} className="mt-1 pl-4 text-ui-sm">
            <TextLink href={href(`/proposals/${id}`)}>New proposal: {p.label}</TextLink>
          </div>
        )
      })}
    </li>
  )
}

export function RunDrawer() {
  const { runDrawer, closeRun, openRun, live, meta } = useApp()
  const open = runDrawer !== false
  const runs = useApi(open ? 'runs' : null, api.runs)
  const id = runDrawer || runs.data?.items[0]?.id || null
  const isLive = live && live.run.id === id
  const detail = useApi(open && id ? `run:${id}:${isLive ? live.run.status : ''}` : null, () => api.run(id!))
  const run: RunDetail | undefined = useMemo(() => {
    if (!detail.data) return undefined
    if (!isLive) return detail.data
    const seen = new Set(detail.data.events.map((e) => e.seq))
    const events = [...detail.data.events, ...live.events.filter((e) => !seen.has(e.seq))].sort((a, b) => a.seq - b.seq)
    return { ...detail.data, ...live.run, events }
  }, [detail.data, isLive, live])
  const steps = run ? groupSteps(run.events) : []
  const earlier = (runs.data?.items ?? []).filter((r) => r.id !== id).slice(0, 5)

  return (
    <Drawer open={open} onClose={closeRun} title={run ? `${runLabel(run.type)} run` : 'Run'}>
      {!run ? (
        <div className="px-4">
          <Loading />
        </div>
      ) : (
        <div className="px-4 pt-3 pb-6">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui text-sub">
            {run.status === 'running' ? (
              <span className="inline-flex items-center gap-1.5 text-strong">
                <Spinner /> Running
              </span>
            ) : (
              <Status tone={run.status === 'succeeded' ? 'success' : 'error'}>
                {run.status === 'succeeded' ? 'Finished' : 'Failed'}
              </Status>
            )}
            <span>
              {f.dateTime(run.started_at)} · {run.trigger === 'schedule' ? 'scheduled' : `started by ${meta?.user.name ?? 'you'}`}
            </span>
          </div>
          {run.status === 'running' && <Progress className="mt-3" value={run.steps_done / Math.max(1, run.steps_total)} />}

          <ol className="mt-3 divide-y divide-stroke border-y border-stroke">
            {steps.map((s, i) => (
              <StepRow
                key={s.n}
                step={s}
                running={run.status === 'running' && i === steps.length - 1}
                failed={run.status === 'failed'}
              />
            ))}
          </ol>

          {run.summary && (
            <div className="mt-5">
              <div className="text-ui-sm text-soft">Summary</div>
              <p className="mt-1 text-ui text-strong">{run.summary}</p>
            </div>
          )}

          <div className="mt-5 space-y-1 text-ui text-sub">
            <div className="text-ui-sm text-soft">Gemini usage this run</div>
            {run.status === 'running' ? (
              <div>Recorded when the run finishes.</div>
            ) : run.served_model ? (
              <>
                <div className="text-strong tabular-nums">
                  {run.tokens_in.toLocaleString('en-US')} input · {run.tokens_out.toLocaleString('en-US')} output tokens
                  {run.llm_calls != null && <span className="text-sub"> · {f.plural(run.llm_calls, 'call')}</span>}
                </div>
                <div>
                  Served by <span className="font-mono text-[12px] text-strong">{run.served_model}</span>
                  {run.requested_model && run.requested_model !== run.served_model && (
                    <>
                      {' '}
                      · requested <span className="font-mono text-[12px]">{run.requested_model}</span>
                    </>
                  )}
                </div>
                {run.fallback_reason && <div>Fallback: {run.fallback_reason}</div>}
                <div>
                  Est. cost:{' '}
                  {run.est_cost_usd != null ? (
                    `$${run.est_cost_usd.toFixed(3)}`
                  ) : (
                    <>
                      set your Vertex AI rate in <TextLink href={href('/settings/gemini')}>Settings</TextLink>
                    </>
                  )}
                </div>
              </>
            ) : (
              <div>No Gemini calls in this run.</div>
            )}
            {run.replay_of ? (
              <p className="pt-2 text-ui-sm text-soft">
                Fixture mode: a replay of a real Gemini run (<span className="font-mono text-[12px]">{run.replay_of}</span>
                ). Its tool calls ran again on the fixture data, with no Gemini call; tokens and cost are the
                recorded run&apos;s.
              </p>
            ) : (
              run.scripted && (
                <p className="pt-2 text-ui-sm text-soft">
                  Fixture mode: a scripted run with no Gemini calls. Numbers come from the fixture data; token
                  figures are illustrative.
                </p>
              )
            )}
          </div>

          {earlier.length > 0 && (
            <div className="mt-6">
              <div className="text-ui-sm text-soft">Earlier runs</div>
              <ul className="mt-1 divide-y divide-stroke">
                {earlier.map((r) => (
                  <li key={r.id}>
                    <button
                      type="button"
                      onClick={() => openRun(r.id)}
                      className="flex min-h-11 w-full items-center gap-2 text-left text-ui outline-none hover:text-strong focus-visible:underline lg:min-h-9"
                    >
                      <Dot tone={r.status === 'succeeded' ? 'success' : r.status === 'failed' ? 'error' : 'gray'} />
                      <span className="flex-1 text-strong">{runLabel(r.type)}</span>
                      <span className="text-sub">{f.dateTime(r.started_at)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Drawer>
  )
}

// --- Ask Sift ---------------------------------------------------------------------------

function AgentMessage({ m }: { m: ChatMessage }) {
  return (
    <div className="flex gap-2.5">
      <AgentMark size="sm" className="mt-0.5" />
      <div className="min-w-0 flex-1 space-y-2">
        {m.tools.length > 0 && (
          <div className="space-y-1">
            {m.tools.map((t, i) => (
              <ToolCallRow key={i} label={t.label} duration={f.duration(t.duration_ms)} rows={t.rows} />
            ))}
          </div>
        )}
        {m.text ? (
          <p className="text-ui whitespace-pre-wrap text-strong">{m.text}</p>
        ) : (
          m.streaming && (
            <div className="flex items-center gap-2 text-ui text-sub">
              <Spinner /> {m.tools.length ? 'Writing the answer' : 'Reading the data'}
            </div>
          )
        )}
        {m.error && <p className="text-ui text-error-dark">{m.error}</p>}
        {m.proposals.length > 0 && (
          <div className="divide-y divide-stroke rounded-xl border border-stroke lg:rounded-lg [&>*]:px-3 [&>*]:lg:px-3">
            {m.proposals.map((p) => (
              <ProposalRow key={p.id} p={p} />
            ))}
          </div>
        )}
        {m.suggestions.length > 0 && <Suggestions items={m.suggestions} />}
      </div>
    </div>
  )
}

function Suggestions({ items }: { items: string[] }) {
  const { sendChat } = useApp()
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((s) => (
        <Button key={s} size="sm" onClick={() => sendChat(s)} className="h-auto min-h-9 whitespace-normal py-1.5 text-left lg:min-h-7">
          {s}
        </Button>
      ))}
    </div>
  )
}

export function ChatPanel({ className, header }: { className?: string; header?: ReactNode }) {
  const { messages, sendChat, meta, chatContext, setChatContext } = useApp()
  const [text, setText] = useState('')
  const end = useRef<HTMLDivElement>(null)
  const busy = messages.some((m) => m.streaming)
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' })
  }, [messages])

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (busy || !text.trim()) return
    sendChat(text)
    setText('')
  }

  return (
    <div className={cn('flex min-h-0 flex-col', className)}>
      {header}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="space-y-4">
            <p className="text-ui text-sub">
              Ask about cost, usage or a proposal, or tell Sift what to change. Sift answers with evidence and turns
              changes into proposals; it never applies them itself.
            </p>
            <Suggestions items={meta?.chat_suggestions ?? []} />
          </div>
        ) : (
          <div className="space-y-5">
            {messages.map((m) =>
              m.role === 'user' ? (
                <div key={m.id} className="flex justify-end">
                  <div className="max-w-[85%] rounded-lg bg-bg-weak px-3 py-2 text-ui text-strong">{m.text}</div>
                </div>
              ) : (
                <AgentMessage key={m.id} m={m} />
              ),
            )}
          </div>
        )}
        <div ref={end} />
      </div>
      <form onSubmit={submit} className="shrink-0 border-t border-stroke p-3">
        {chatContext && (
          <div className="mb-2 flex items-center gap-1 text-ui-sm text-sub">
            <span className="truncate">About: {chatContext.label}</span>
            <button
              type="button"
              onClick={() => setChatContext(null)}
              aria-label="Remove context"
              className="inline-flex size-6 items-center justify-center rounded-md text-soft hover:bg-bg-weak hover:text-strong"
            >
              <RiCloseLine className="size-3.5" />
            </button>
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit(e as unknown as FormEvent)
              }
            }}
            rows={1}
            placeholder="Ask Sift…"
            aria-label="Message"
            data-autofocus
            className="max-h-32 min-h-11 flex-1 resize-none rounded-xl border border-stroke bg-bg-white px-3 py-2.5 text-ui text-strong outline-none placeholder:text-soft focus-visible:ring-2 focus-visible:ring-primary/40 lg:min-h-8 lg:rounded-md lg:py-1.5"
          />
          <Button type="submit" variant="primary" size="icon" disabled={busy || !text.trim()} aria-label="Send">
            <RiArrowUpLine />
          </Button>
        </div>
      </form>
    </div>
  )
}

export function ChatDrawer() {
  const { chatOpen, closeChat, clearChat, messages } = useApp()
  return (
    <Drawer
      open={chatOpen}
      onClose={closeChat}
      title="Ask Sift"
      actions={
        messages.length > 0 && (
          <Button variant="ghost" size="sm" onClick={clearChat}>
            Clear
          </Button>
        )
      }
    >
      <ChatPanel className="h-full" />
    </Drawer>
  )
}

/** "View all →" style link. */
export function ArrowLink({ href: to, children }: { href: string; children: ReactNode }) {
  return (
    <TextLink href={to} className="inline-flex items-center gap-1">
      {children}
      <RiArrowRightLine className="size-3.5" />
    </TextLink>
  )
}
