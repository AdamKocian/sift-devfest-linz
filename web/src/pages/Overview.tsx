import { api } from '../api'
import { AgentStatusLine, ArrowLink, ProposalRow } from '../features'
import * as f from '../format'
import { href, navigate } from '../router'
import { invalidate, useApi, useApp } from '../store'
import {
  Button,
  CompositionBar,
  Dot,
  EmptyState,
  ErrorNote,
  Kpi,
  KpiStrip,
  Loading,
  Page,
  PageHeader,
  Section,
  SegmentDot,
  Spinner,
} from '../ui'
import { isDesktop } from './common'

export function OverviewPage() {
  const { live, startRun, openChat, meta, toast } = useApp()
  const running = live?.run.status === 'running'
  const ov = useApi('overview', api.overview, running ? 2000 : 15000)

  const askSift = () => (isDesktop() ? openChat() : navigate('/ask'))
  const togglePause = async () => {
    if (!ov.data) return
    const paused = ov.data.agent.state === 'paused'
    try {
      await api.saveSettings({ agent_paused: !paused })
      invalidate()
      toast(paused ? 'Agent resumed.' : 'Agent paused. Scheduled runs stop; verification keeps running.')
    } catch (e) {
      toast((e as Error).message)
    }
  }

  const o = ov.data
  const k = o?.kpis
  const trend = k && k.ingest_prev_7d_gib ? (k.ingest_7d_gib - k.ingest_prev_7d_gib) / k.ingest_prev_7d_gib : 0
  const total = o ? o.composition.reduce((s, c) => s + c.gib, 0) : 0

  return (
    <Page>
      <PageHeader
        title="Overview"
        subtitle={o && meta ? `${f.longDate(o.now)} · env ${meta.environment.id}` : ' '}
        actions={
          <>
            <Button onClick={askSift} className="flex-1 lg:flex-none">
              Ask Sift
            </Button>
            <Button variant="primary" onClick={() => startRun('analyze')} disabled={running} className="flex-1 lg:flex-none">
              {running && <Spinner className="border-white/40 border-t-white" />}
              Run analysis
            </Button>
          </>
        }
      />

      {ov.error && !o && <ErrorNote error={ov.error} onRetry={ov.reload} />}
      {!o || !k ? (
        !ov.error && <Loading />
      ) : (
        <>
          <AgentStatusLine agent={o.agent} onTogglePause={togglePause} />

          <KpiStrip>
            <Kpi label="Ingest (7 d)" value={f.gib(k.ingest_7d_gib)} hint={`${f.signedPct(trend)} vs previous week`} />
            <Kpi label="Est. cost / month" value={f.eur(k.cost_month_eur)} hint="at your rates" />
            <Kpi
              label="Waste found"
              value={f.pct(k.waste_ratio, 0)}
              hint={`${f.gib(k.waste_gib_week)}/wk in ${f.plural(k.awaiting_count, 'proposal')}`}
            />
            <Kpi
              label="Saved so far"
              value={
                <>
                  {f.eur(k.saved_eur_month)}
                  <span className="text-ui font-normal tracking-normal text-sub"> / month</span>
                </>
              }
              hint={`${f.plural(k.verified_count, 'rule')} verified`}
            />
          </KpiStrip>

          <div className="grid grid-cols-[minmax(0,1fr)] gap-0 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)] xl:gap-16">
            <Section
              title="Awaiting approval"
              count={k.awaiting_count}
              action={<ArrowLink href={href('/proposals')}>View all</ArrowLink>}
            >
              {o.awaiting.length === 0 ? (
                <EmptyState
                  title="Nothing to approve"
                  description="Run an analysis to look for unused telemetry again."
                  className="lg:py-8 lg:text-left"
                />
              ) : (
                <div className="divide-y divide-stroke overflow-hidden rounded-2xl bg-bg-white lg:rounded-none lg:border-y lg:border-stroke lg:bg-transparent">
                  {o.awaiting.map((p) => (
                    <ProposalRow key={p.id} p={p} />
                  ))}
                </div>
              )}
            </Section>

            <Section title="Ingest by signal">
              <div className="rounded-2xl bg-bg-white p-4 lg:rounded-none lg:bg-transparent lg:p-0">
                <div className="mb-3 flex items-baseline justify-between">
                  <span className="text-ui text-strong">
                    <span className="font-semibold tabular-nums">{f.gib(total)}</span> this week
                  </span>
                </div>
                <CompositionBar
                  parts={o.composition.map((c) => ({ key: c.key, label: c.label, value: c.gib }))}
                  format={f.gib}
                />
                <ul className="mt-3 divide-y divide-stroke">
                  {o.composition.map((c) => (
                    <li key={c.key} className="flex h-10 items-center gap-2.5 text-ui lg:h-9">
                      <SegmentDot k={c.key} />
                      <span className="flex-1 text-strong">{c.label}</span>
                      <span className="text-sub tabular-nums">{f.gib(c.gib)}</span>
                    </li>
                  ))}
                  {o.failed_verifications.count > 0 && (
                    <li className="flex h-10 items-center gap-2.5 text-ui lg:h-9">
                      <Dot tone="error" className="size-2" />
                      <span className="flex-1 text-strong">
                        {f.plural(o.failed_verifications.count, 'rule')} failed verification
                      </span>
                      <ArrowLink
                        href={
                          o.failed_verifications.latest_proposal_id
                            ? href(`/proposals/${o.failed_verifications.latest_proposal_id}`)
                            : href('/proposals', { status: 'reverted' })
                        }
                      >
                        Review
                      </ArrowLink>
                    </li>
                  )}
                </ul>
              </div>
            </Section>
          </div>

          <p className="mt-10 text-ui-sm text-soft">
            Analyzed {f.plural(o.footer.signals, 'signal')} · {o.footer.query_history_days} days of query history ·
            Gemini usage today {f.tokens(o.footer.gemini_tokens_today)} tokens
            {o.footer.scripted && ' (scripted, fixture mode)'}
          </p>
        </>
      )}
    </Page>
  )
}
