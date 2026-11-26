import { useEffect, useState } from 'react'
import { RiSearchLine } from '@remixicon/react'
import { api } from '../api'
import { ProposalRow, SignalStatusLabel, usageText } from '../features'
import * as f from '../format'
import { href, navigate, setQuery, useRoute } from '../router'
import { useApi, useApp, useNow } from '../store'
import type { SignalSummary, Usage } from '../types'
import {
  BarChart,
  Button,
  CodeBlock,
  EmptyState,
  ErrorNote,
  FilterMenu,
  Group,
  Input,
  Kpi,
  KpiStrip,
  Loading,
  Page,
  PageHeader,
  Row,
  Section,
  Tag,
  Td,
  TextLink,
  Th,
} from '../ui'
import { ReferenceTable, isDesktop } from './common'

function usedBy(u: Usage): string {
  const parts = (Object.entries(u.by_type) as [string, number][])
    .filter(([, n]) => n > 0)
    .map(([t, n]) => f.plural(n, f.REF_LABEL[t][0], f.REF_LABEL[t][1]))
  if (u.queries_30d) parts.push(f.plural(u.queries_30d, 'query', 'queries'))
  return parts.join(', ') || '—'
}

export function SignalsPage() {
  const route = useRoute()
  const kind = route.query.get('kind') ?? ''
  const service = route.query.get('service') ?? ''
  const usage = route.query.get('usage') ?? ''
  const sort = route.query.get('sort') ?? 'cost'
  const q = route.query.get('q') ?? ''
  const [text, setText] = useState(q)
  useEffect(() => {
    const t = window.setTimeout(() => {
      if (text !== q) setQuery(route, { q: text || null })
    }, 200)
    return () => window.clearTimeout(t)
  }, [text, q, route])

  const key = `signals:${kind}:${service}:${usage}:${sort}:${q}`
  const list = useApi(key, () => api.signals({ kind, service, usage, sort, q }))
  const facets = list.data?.facets
  const items = list.data?.items ?? []
  const byId = new Map(items.map((s) => [s.id, s]))
  const now = useNow(30000)

  return (
    <Page wide>
      <PageHeader
        title="Signals"
        subtitle={
          list.data
            ? `${f.plural(list.data.total, 'signal')} from the last 7 days of ingest and 30 days of query history`
            : ' '
        }
      />
      <div className="mb-4 flex flex-col gap-2 lg:flex-row lg:items-center">
        <div className="relative lg:w-72">
          <RiSearchLine className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-soft" />
          <Input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Search signals"
            aria-label="Search signals"
            className="pl-9"
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <FilterMenu
            label="Kind"
            value={kind}
            options={[{ value: '', label: 'All' }, ...(facets?.kinds ?? []).map((k) => ({ value: k, label: f.KIND_LABEL[k] }))]}
            onChange={(v) => setQuery(route, { kind: v || null })}
          />
          <FilterMenu
            label="Service"
            value={service}
            options={[{ value: '', label: 'All' }, ...(facets?.services ?? []).map((s) => ({ value: s, label: s }))]}
            onChange={(v) => setQuery(route, { service: v || null })}
          />
          <FilterMenu
            label="Usage"
            value={usage}
            options={[
              { value: '', label: 'All' },
              { value: 'unused', label: 'Unused' },
              { value: 'rarely', label: 'Rarely' },
              { value: 'used', label: 'Used' },
            ]}
            onChange={(v) => setQuery(route, { usage: v || null })}
          />
          <FilterMenu
            label="Sort"
            value={sort}
            options={[
              { value: 'cost', label: 'Cost' },
              { value: 'volume', label: 'Volume' },
              { value: 'last_used', label: 'Last used' },
              { value: 'name', label: 'Name' },
            ]}
            onChange={(v) => setQuery(route, { sort: v === 'cost' ? null : v })}
          />
          {(kind || service || usage || q) && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setText('')
                setQuery(route, { kind: null, service: null, usage: null, q: null })
              }}
            >
              Clear
            </Button>
          )}
        </div>
      </div>

      {list.error && <ErrorNote error={list.error} onRetry={list.reload} />}
      {!list.data ? (
        !list.error && <Loading />
      ) : items.length === 0 ? (
        <EmptyState title="No signals match" description="Try another filter or clear the search." />
      ) : (
        <>
          <div className="-mx-3 hidden lg:block">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <Th className="w-[34%] pl-3 first:pl-3">Signal</Th>
                  <Th>Kind</Th>
                  <Th right>Volume / day</Th>
                  <Th right>Cost / mo</Th>
                  <Th>Last used</Th>
                  <Th>Used by</Th>
                  <Th className="pr-3 last:pr-3">Status</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((s) => (
                  <tr
                    key={s.id}
                    onClick={() => navigate(`/signals/${s.id}`)}
                    className="cursor-pointer transition-colors hover:bg-bg-weak"
                  >
                    <Td className="max-w-0 pl-3 first:pl-3">
                      <a
                        href={href(`/signals/${s.id}`)}
                        onClick={(e) => e.stopPropagation()}
                        className="block truncate font-medium text-strong outline-none hover:underline focus-visible:underline"
                      >
                        {s.key}
                      </a>
                      <div className="truncate text-ui-sm text-soft">
                        {s.service}
                        {s.parent_id && byId.get(s.parent_id) ? ` · in ${byId.get(s.parent_id)!.key}` : ''}
                      </div>
                    </Td>
                    <Td>
                      <Tag>{f.KIND_TAG[s.kind]}</Tag>
                    </Td>
                    <Td right>{f.gib(s.gib_day)}</Td>
                    <Td right>{f.eur(s.cost_month)}</Td>
                    <Td className="whitespace-nowrap text-sub">{f.ago(s.usage.last_used, now)}</Td>
                    <Td className="max-w-56 truncate text-sub">{usedBy(s.usage)}</Td>
                    <Td className="pr-3 last:pr-3">
                      <SignalStatusLabel status={s.status} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Group className="lg:hidden">
            {items.map((s) => (
              <SignalRow key={s.id} s={s} />
            ))}
          </Group>
        </>
      )}
    </Page>
  )
}

function SignalRow({ s }: { s: SignalSummary }) {
  return (
    <Row
      href={href(`/signals/${s.id}`)}
      title={s.kind === 'log_source' ? `${s.service} ${s.key}` : s.key}
      description={`${s.kind === 'log_source' ? '' : `${s.service} · `}${f.gib(s.gib_day)}/day · ${f.eur(s.cost_month)}/mo`}
      meta={<SignalStatusLabel status={s.status} />}
    />
  )
}

export function SignalDetailPage({ id }: { id: string }) {
  const { openChat } = useApp()
  const detail = useApi(`signal:${id}`, () => api.signal(id))
  const s = detail.data
  if (detail.error && !s) {
    return (
      <Page>
        <PageHeader title="Signal" back={{ href: href('/signals'), label: 'Signals' }} />
        <ErrorNote error={detail.error} onRetry={detail.reload} />
      </Page>
    )
  }
  if (!s) {
    return (
      <Page>
        <Loading />
      </Page>
    )
  }
  const ask = () => {
    const ctx = { signal_id: s.id, label: `${s.service} · ${s.key}` }
    openChat({ context: ctx, send: 'Tell me about this signal' })
    if (!isDesktop()) navigate('/ask')
  }
  const reduced = s.gib_day < s.baseline_gib_day - 1e-6
  return (
    <Page>
      <PageHeader
        back={{ href: href('/signals'), label: 'Signals' }}
        title={s.kind === 'log_source' ? `${s.service} ${s.key}` : s.key}
        subtitle={`${s.service} · ${f.KIND_LABEL[s.kind]} · first seen ${f.ago(s.first_seen)}`}
        actions={
          <Button onClick={ask} className="w-full lg:w-auto">
            Ask Sift about this signal
          </Button>
        }
      />
      <div className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-2xl bg-bg-white px-4 py-3 text-ui lg:rounded-none lg:bg-transparent lg:p-0">
        <SignalStatusLabel status={s.status} />
        <span className="text-sub">{usageText(s.usage)}</span>
        {s.parent && (
          <span className="text-sub">
            Part of <TextLink href={href(`/signals/${s.parent.id}`)}>{s.parent.key}</TextLink>
          </span>
        )}
      </div>

      <KpiStrip>
        <Kpi
          label="Volume / day"
          value={f.gib(s.gib_day)}
          hint={reduced ? `was ${f.gib(s.baseline_gib_day)} before rules` : `${f.count(s.records_day)} records`}
        />
        <Kpi label="Cost / month" value={f.eur(s.cost_month)} hint="at your rates" />
        <Kpi label="Last used" value={f.ago(s.usage.last_used)} hint={s.usage.last_used ? f.dateTime(s.usage.last_used) : 'no record'} />
        <Kpi label="References" value={s.usage.references_30d} hint="in the last 30 days" />
      </KpiStrip>

      {(s.protected_by.length > 0 || s.finding?.note) && (
        <Section title={s.protected_by.length ? 'Protected' : 'Finding'}>
          <div className="space-y-2 rounded-2xl bg-bg-white p-4 text-ui lg:rounded-none lg:bg-transparent lg:p-0">
            {s.protected_by.length > 0 && (
              <p className="text-strong">
                Sift will not propose dropping or sampling this. Guardrails:{' '}
                {s.protected_by.map((g, i) => (
                  <span key={g}>
                    {i > 0 && ', '}
                    <TextLink href={href('/guardrails')}>{g}</TextLink>
                  </span>
                ))}
                .
              </p>
            )}
            {s.finding?.note && <p className="max-w-[68ch] text-sub">{s.finding.note}</p>}
          </div>
        </Section>
      )}

      {s.proposals.length > 0 && (
        <Section title="Proposals" count={s.proposals.length}>
          <div className="divide-y divide-stroke overflow-hidden rounded-2xl bg-bg-white lg:rounded-none lg:border-y lg:border-stroke lg:bg-transparent">
            {s.proposals.map((p) => (
              <ProposalRow key={p.id} p={p} />
            ))}
          </div>
        </Section>
      )}

      <Section title="Volume" action={<span className="text-ui-sm text-soft">GiB per day, 14 days</span>}>
        <div className="rounded-2xl bg-bg-white p-4 lg:rounded-none lg:bg-transparent lg:p-0">
          <BarChart series={s.series} markers={s.markers} format={f.gib} label={`Daily volume of ${s.key}`} />
        </div>
      </Section>

      {s.children.length > 0 && (
        <Section title="Patterns and fields" count={s.children.length}>
          <Group>
            {s.children.map((c) => (
              <SignalRow key={c.id} s={c} />
            ))}
          </Group>
        </Section>
      )}

      <Section title="References" count={s.references.length}>
        <ReferenceTable refs={s.references} empty="No query, dashboard, notebook, workflow or alert reads this data." />
      </Section>

      <Section title="Sample records" action={<span className="text-ui-sm text-soft">redacted</span>}>
        <CodeBlock code={s.samples.join('\n')} copy={false} />
      </Section>
    </Page>
  )
}
