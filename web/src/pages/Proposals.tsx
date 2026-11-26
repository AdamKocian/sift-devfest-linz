import { useState } from 'react'
import { api } from '../api'
import { ProposalStatusLabel, proposalTone, savingsText, usageText } from '../features'
import * as f from '../format'
import { href, navigate, setQuery, useRoute } from '../router'
import { invalidate, useApi, useApp, useNow } from '../store'
import type { ProposalDetail, StatusGroup } from '../types'
import {
  BarChart,
  Button,
  CodeBlock,
  Dialog,
  Dot,
  EmptyState,
  ErrorNote,
  Group,
  Kpi,
  KpiStrip,
  Loading,
  Page,
  PageHeader,
  Progress,
  Row,
  Section,
  Segmented,
  Tabs,
  Tag,
  Td,
  TextLink,
  Th,
} from '../ui'
import { ReferenceTable, Timeline, WithEvidenceLinks, isDesktop } from './common'

const GROUPS: { value: StatusGroup; label: string }[] = [
  { value: 'awaiting', label: 'Awaiting' },
  { value: 'applied', label: 'Applied' },
  { value: 'verified', label: 'Verified' },
  { value: 'reverted', label: 'Reverted' },
  { value: 'rejected', label: 'Rejected' },
]

const EMPTY: Record<StatusGroup, string> = {
  awaiting: 'Nothing is waiting for approval. The agent adds proposals here when it finds unused telemetry.',
  applied: 'No rule is being verified right now.',
  verified: 'No verified rules yet.',
  reverted: 'No rule has been reverted.',
  rejected: 'No proposal has been rejected.',
}

export function ProposalsPage() {
  const route = useRoute()
  const status = (route.query.get('status') as StatusGroup) || 'awaiting'
  const list = useApi(`proposals:${status}`, () => api.proposals(status), status === 'applied' ? 1500 : 15000)
  const counts = list.data?.counts
  const now = useNow(30000)

  return (
    <Page wide>
      <PageHeader title="Proposals" subtitle="Rules the agent wants to apply. Nothing changes until you approve." />
      <Tabs
        className="mb-4 lg:mb-2"
        items={GROUPS.map((g) => ({ ...g, count: counts?.[g.value] }))}
        value={status}
        onChange={(v) => setQuery(route, { status: v === 'awaiting' ? null : v })}
      />
      {list.error && <ErrorNote error={list.error} onRetry={list.reload} />}
      {!list.data ? (
        !list.error && <Loading />
      ) : list.data.items.length === 0 ? (
        <EmptyState title="Nothing here" description={EMPTY[status]} />
      ) : (
        <>
          <div className="-mx-3 hidden lg:block">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <Th className="w-[34%] pl-3 first:pl-3">Proposal</Th>
                  <Th>Kind</Th>
                  <Th>Service</Th>
                  <Th right>Savings / mo</Th>
                  <Th>Confidence</Th>
                  <Th>Status</Th>
                  <Th right className="pr-3 last:pr-3">
                    Proposed
                  </Th>
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((p) => (
                  <tr
                    key={p.id}
                    onClick={() => navigate(`/proposals/${p.id}`)}
                    className="cursor-pointer transition-colors hover:bg-bg-weak"
                  >
                    <Td className="max-w-0 pl-3 first:pl-3">
                      <a
                        href={href(`/proposals/${p.id}`)}
                        onClick={(e) => e.stopPropagation()}
                        className="block truncate font-medium text-strong outline-none hover:underline focus-visible:underline"
                      >
                        {p.title}
                      </a>
                      <div className="truncate text-ui-sm text-soft">{usageText(p.usage)}</div>
                    </Td>
                    <Td>
                      <Tag>{f.KIND_TAG[p.kind]}</Tag>
                    </Td>
                    <Td className="text-sub">{p.service}</Td>
                    <Td right className="text-strong">
                      {p.savings.eur_month >= 0.5 ? f.eur(p.savings.eur_month) : '—'}
                      <div className="text-ui-sm text-soft">
                        {p.savings.gib_week > 0 ? `−${f.gib(p.savings.gib_week)}/wk` : 'redaction'}
                      </div>
                    </Td>
                    <Td className="text-sub capitalize">{p.confidence}</Td>
                    <Td>
                      <ProposalStatusLabel p={p} />
                    </Td>
                    <Td right className="pr-3 text-sub last:pr-3">
                      {f.ago(p.created_at, now)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Group className="lg:hidden">
            {list.data.items.map((p) => (
              <Row
                key={p.id}
                href={href(`/proposals/${p.id}`)}
                title={p.title}
                description={`${p.service} · ${savingsText(p)}`}
                meta={
                  <span className="inline-flex items-center gap-1.5">
                    <Dot tone={proposalTone(p)[0]} />
                    {proposalTone(p)[1]}
                  </span>
                }
              />
            ))}
          </Group>
        </>
      )}
    </Page>
  )
}

// --- detail -----------------------------------------------------------------------------

type DetailTab = 'evidence' | 'rule' | 'impact' | 'history'

export function ProposalDetailPage({ id }: { id: string }) {
  const route = useRoute()
  const tab = (route.query.get('tab') as DetailTab) || 'evidence'
  const { setApproveId, setRejectId, openChat } = useApp()
  const [busy, setBusy] = useState(false)
  const [confirmRevert, setConfirmRevert] = useState(false)
  const detail = useApi(`proposal:${id}`, () => api.proposal(id), (d) =>
    d && ['approved', 'applied', 'verifying'].includes(d.status) ? 1000 : false,
  )
  const p = detail.data
  const { toast } = useApp()

  if (detail.error && !p) {
    return (
      <Page>
        <PageHeader title="Proposal" back={{ href: href('/proposals'), label: 'Proposals' }} />
        <ErrorNote error={detail.error} onRetry={detail.reload} />
      </Page>
    )
  }
  if (!p) {
    return (
      <Page>
        <Loading />
      </Page>
    )
  }

  const awaiting = p.status === 'awaiting_approval'
  const active = ['applied', 'verifying', 'verified'].includes(p.status)
  const ask = () => {
    const ctx = { proposal_id: p.id, label: p.title }
    if (isDesktop()) openChat({ context: ctx, send: 'Why is this safe?' })
    else {
      openChat({ context: ctx, send: 'Why is this safe?' })
      navigate('/ask')
    }
  }
  const revert = async () => {
    setBusy(true)
    try {
      await api.revert(p.id, 'Reverted by hand')
      invalidate()
      toast('Rule reverted. Previous state restored.')
    } catch (e) {
      toast((e as Error).message)
    } finally {
      setBusy(false)
      setConfirmRevert(false)
    }
  }
  const goEvidence = (n: number) => {
    const target = p.evidence.find((e) => e.n === n)?.target
    if (target === 'volume') document.getElementById('volume')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    else if (target === 'guardrails') navigate('/guardrails')
    else if (target) setQuery(route, { tab: target })
  }

  return (
    <Page>
      <PageHeader
        back={{ href: href('/proposals'), label: 'Proposals' }}
        title={p.title}
        subtitle={
          <>
            Proposed by agent · {f.dateTime(p.created_at)} · confidence {p.confidence}
            {p.run_id && (
              <>
                {' · '}
                <RunLink id={p.run_id} />
              </>
            )}
          </>
        }
        actions={
          <div className="hidden items-center gap-2 lg:flex">
            <Button variant="ghost" onClick={ask}>
              Ask Sift
            </Button>
            {awaiting && (
              <>
                <Button variant="ghost" onClick={() => setRejectId(p.id)}>
                  Reject
                </Button>
                <Button variant="primary" onClick={() => setApproveId(p.id)}>
                  Apply rule
                </Button>
              </>
            )}
            {active && (
              <Button onClick={() => setConfirmRevert(true)} disabled={busy}>
                Revert rule
              </Button>
            )}
          </div>
        }
      />

      <StatusBar p={p} />

      <Tabs
        className="mb-6"
        items={[
          { value: 'evidence', label: 'Evidence' },
          { value: 'rule', label: 'Rule' },
          { value: 'impact', label: 'Impact', count: p.impact.length },
          { value: 'history', label: 'History', count: p.history.length },
        ]}
        value={tab}
        onChange={(v) => setQuery(route, { tab: v === 'evidence' ? null : v })}
      />

      {tab === 'evidence' && <EvidenceTab p={p} onRef={goEvidence} />}
      {tab === 'rule' && <RuleTab p={p} />}
      {tab === 'impact' && <ImpactTab p={p} />}
      {tab === 'history' && <Timeline items={p.history} />}

      {/* Mobile: one large primary action within thumb reach. */}
      <div className="fixed inset-x-0 bottom-14 z-10 border-t border-stroke bg-bg-white px-4 py-3 pb-safe lg:hidden">
        {awaiting ? (
          <div className="flex gap-2">
            <Button variant="ghost" size="lg" onClick={() => setRejectId(p.id)}>
              Reject
            </Button>
            <Button variant="primary" size="lg" className="flex-1" onClick={() => setApproveId(p.id)}>
              Apply rule
            </Button>
          </div>
        ) : active ? (
          <Button size="lg" className="w-full" onClick={() => setConfirmRevert(true)} disabled={busy}>
            Revert rule
          </Button>
        ) : (
          <Button size="lg" className="w-full" onClick={ask}>
            Ask Sift about this proposal
          </Button>
        )}
      </div>
      <div className="h-20 lg:hidden" aria-hidden />

      <Dialog
        open={confirmRevert}
        onClose={() => setConfirmRevert(false)}
        title="Revert this rule?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmRevert(false)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={revert} disabled={busy}>
              Revert rule
            </Button>
          </>
        }
      >
        Sift restores the previous state, so the data is ingested again from now on. Records dropped while the rule
        was active do not come back.
      </Dialog>
    </Page>
  )
}

function RunLink({ id }: { id: string }) {
  const { openRun } = useApp()
  return <TextLink onClick={() => openRun(id)}>from run</TextLink>
}

function StatusBar({ p }: { p: ProposalDetail }) {
  const now = useNow(500)
  const verifying = p.status === 'verifying' && p.applied_at && p.verify_due_at
  let progress = 0
  let left = ''
  if (verifying) {
    const start = Date.parse(p.applied_at!)
    const end = Date.parse(p.verify_due_at!)
    progress = (now - start) / Math.max(1, end - start)
    const s = Math.max(0, Math.ceil((end - now) / 1000))
    left = s > 0 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} left` : 'finishing'
  }
  return (
    <div className="mb-5 rounded-2xl bg-bg-white px-4 py-3 lg:rounded-none lg:bg-transparent lg:p-0">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-ui">
        <ProposalStatusLabel p={p} />
        <span className="text-sub">{savingsText(p)}</span>
        {verifying && <span className="text-sub tabular-nums">Checking volume, references and Davis problems · {left}</span>}
        {p.status === 'verified' && p.verified_at && <span className="text-sub">since {f.dateTime(p.verified_at)}</span>}
        {p.decided_by && p.status !== 'awaiting_approval' && (
          <span className="text-soft">
            {p.status === 'rejected' ? 'Rejected' : 'Approved'} by {p.decided_by}
          </span>
        )}
      </div>
      {verifying && <Progress className="mt-2.5 max-w-md" value={progress} />}
      {p.reject_reason && <p className="mt-2 text-ui text-sub">“{p.reject_reason}”</p>}
    </div>
  )
}

function EvidenceTab({ p, onRef }: { p: ProposalDetail; onRef: (n: number) => void }) {
  const k = p.kpis
  return (
    <div>
      {p.verify_result && <Verification p={p} />}
      <KpiStrip>
        <Kpi
          label="Volume / day"
          value={f.gib(k.gib_day)}
          hint={`${f.count(k.records_day)} records${['applied', 'verifying', 'verified'].includes(p.status) ? ' before the rule' : ''}`}
        />
        <Kpi label="Cost / month" value={f.eur(k.cost_month)} hint="at your rates" />
        <Kpi label="Last queried" value={f.ago(k.last_used)} hint={k.last_used ? f.dateTime(k.last_used) : 'no record'} />
        <Kpi label="References" value={k.references_30d} hint="in the last 30 days" />
      </KpiStrip>

      <Section title="Volume" id="volume" action={<span className="text-ui-sm text-soft">GiB per day, 14 days</span>}>
        <div className="rounded-2xl bg-bg-white p-4 lg:rounded-none lg:bg-transparent lg:p-0">
          <BarChart series={p.series} markers={p.markers} format={f.gib} label={`Daily volume of ${p.signal_key}`} />
        </div>
      </Section>

      <Section title="Why it's safe">
        <div className="rounded-2xl bg-bg-white p-4 lg:rounded-none lg:bg-transparent lg:p-0">
          <p className="max-w-[68ch] text-ui leading-relaxed text-strong">
            {p.explanation.map((s, i) => (
              <span key={i}>
                <WithEvidenceLinks text={s} onRef={onRef} />{' '}
              </span>
            ))}
          </p>
          <ol className="mt-4 divide-y divide-stroke border-t border-stroke">
            {p.evidence.map((e) => (
              <li key={e.n} className="flex gap-3 py-2.5 text-ui">
                <span className="w-4 shrink-0 text-soft tabular-nums">{e.n}</span>
                <span className="w-20 shrink-0 font-medium text-strong">{e.label}</span>
                <span className="min-w-0 flex-1 text-sub">{e.text}</span>
                <TextLink onClick={() => onRef(e.n)} className="shrink-0">
                  {e.target === 'volume' ? 'Chart' : e.target === 'guardrails' ? 'Guardrails' : e.target === 'rule' ? 'Rule' : 'Impact'}
                </TextLink>
              </li>
            ))}
          </ol>
        </div>
      </Section>

      <Section title="Sample records" action={<span className="text-ui-sm text-soft">redacted</span>}>
        <CodeBlock code={p.samples.join('\n')} copy={false} />
      </Section>
    </div>
  )
}

function Verification({ p }: { p: ProposalDetail }) {
  const v = p.verify_result!
  return (
    <div className="mb-8">
      <h2 className="mb-2 text-ui-lg font-semibold text-strong">
        Verification {v.passed ? 'passed' : 'failed'}
        <span className="ml-2 text-ui font-normal text-soft">{f.dateTime(v.at)}</span>
      </h2>
      {v.incident && (
        <p className="mb-3 rounded-lg border border-error-light bg-error-lighter px-3 py-2.5 text-ui text-error-dark">
          {v.incident}
        </p>
      )}
      <ul className="divide-y divide-stroke rounded-2xl bg-bg-white px-4 lg:rounded-none lg:border-y lg:border-stroke lg:bg-transparent lg:px-0">
        {v.checks.map((c) => (
          <li key={c.name} className="flex flex-col gap-0.5 py-2.5 text-ui lg:flex-row lg:items-center lg:gap-4">
            <span className="flex items-center gap-2 lg:w-80">
              <Dot tone={c.passed ? 'success' : 'error'} />
              <span className="text-strong">{c.label}</span>
            </span>
            <span className="pl-3.5 text-sub lg:pl-0">{c.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function RuleTab({ p }: { p: ProposalDetail }) {
  const route = useRoute()
  const target = route.query.get('target') === 'otel' ? 'otel' : 'openpipeline'
  const { meta } = useApp()
  const art = p.rule[target]
  const d = p.dry_run
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented
          items={[
            { value: 'openpipeline', label: 'OpenPipeline' },
            { value: 'otel', label: 'OTel Collector' },
          ]}
          value={target}
          onChange={(v) => setQuery(route, { target: v === 'openpipeline' ? null : v })}
        />
        <span className="text-ui text-sub">
          Apply target: <span className="font-mono text-[12px] text-strong">{p.apply_target}</span>
          {meta?.mode === 'fixture' && ' (fixture mode never writes)'}
        </span>
      </div>
      {target === 'openpipeline' && art.stage && (
        <dl className="grid gap-x-6 gap-y-1 rounded-2xl bg-bg-white p-4 text-ui sm:grid-cols-[auto_1fr] lg:rounded-none lg:bg-transparent lg:p-0">
          <dt className="text-soft">Stage</dt>
          <dd className="text-strong">{art.stage}</dd>
          <dt className="text-soft">Processor</dt>
          <dd className="text-strong">{art.processor}</dd>
          <dt className="text-soft">Matcher</dt>
          <dd className="font-mono text-[12px] break-all text-strong">{art.matcher}</dd>
        </dl>
      )}
      <CodeBlock code={art.code} />
      <p className="text-ui text-strong">
        Dry run: matcher hits {f.count(d.matched_records_day)} of {f.count(d.scope_records_day)} {d.scope_label} a day (
        {f.pct(d.match_ratio)}).
        {p.data_type === 'logs' && ` ${f.plural(d.errors_matched, 'ERROR or FATAL record', 'ERROR or FATAL records')} matched.`}
      </p>
      <p className="max-w-[80ch] text-ui-sm text-soft">{art.note}</p>
    </div>
  )
}

function ImpactTab({ p }: { p: ProposalDetail }) {
  const b = p.blast_radius
  const reads = p.impact.filter((r) => r.effect === 'reads').length
  return (
    <div className="space-y-4">
      <p className="rounded-2xl bg-bg-white px-4 py-3 text-ui text-strong lg:rounded-none lg:bg-transparent lg:p-0">
        {reads === 0
          ? 'Nothing reads this data.'
          : `${f.plural(reads, 'reference')} read this data.`}{' '}
        <span className="text-sub">
          Last 90 days: {f.plural(b.dashboard, 'dashboard')}, {f.plural(b.alert, 'alert')}, {f.plural(b.slo, 'SLO')},{' '}
          {f.plural(b.workflow, 'workflow')}, {f.plural(b.query, 'ad-hoc query', 'ad-hoc queries')} in 30 days.
        </span>
      </p>
      <ReferenceTable refs={p.impact} empty="Nothing references this data or anything near it." />
      {p.blocking_guardrails.length > 0 && (
        <p className="text-ui text-error-dark">Blocked by guardrail: {p.blocking_guardrails.join(', ')}</p>
      )}
    </div>
  )
}
