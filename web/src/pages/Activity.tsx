import { api } from '../api'
import * as f from '../format'
import { href, setQuery, useRoute } from '../router'
import { useApi, useApp } from '../store'
import type { ActivityItem } from '../types'
import { EmptyState, ErrorNote, FilterMenu, Loading, Page, PageHeader, TextLink } from '../ui'
import { Actor, transition } from './common'

function SubjectLink({ a }: { a: ActivityItem }) {
  const { openRun } = useApp()
  if (a.subject_type === 'proposal' && a.subject_id) {
    return <TextLink href={href(`/proposals/${a.subject_id}`, { tab: 'history' })}>{transition(a) ?? 'Proposal'}</TextLink>
  }
  if (a.subject_type === 'run' && a.subject_id) {
    return <TextLink onClick={() => openRun(a.subject_id!)}>Run details</TextLink>
  }
  if (a.subject_type === 'guardrail' && a.subject_id) {
    return <TextLink href={href(`/guardrails/${a.subject_id}`)}>Guardrail</TextLink>
  }
  if (a.subject_type === 'settings') {
    const before = a.before ? Object.values(a.before)[0] : null
    const after = a.after ? Object.values(a.after)[0] : null
    return (
      <TextLink href={href('/settings')}>
        {String(before ?? '—')} → {String(after ?? '—')}
      </TextLink>
    )
  }
  return null
}

export function ActivityPage() {
  const route = useRoute()
  const actor = route.query.get('actor') ?? 'all'
  const type = route.query.get('type') ?? ''
  const list = useApi(`activity:${actor}:${type}`, () => api.activity({ actor, type: type || undefined, limit: 200 }), 10000)
  const items = list.data?.items ?? []

  // Group by day for scanning.
  const days: { day: string; items: ActivityItem[] }[] = []
  for (const a of items) {
    const day = new Date(a.at).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' })
    const last = days.at(-1)
    if (last && last.day === day) last.items.push(a)
    else days.push({ day, items: [a] })
  }

  return (
    <Page>
      <PageHeader title="Activity" subtitle="Every change, by the agent or by a person, with before and after." />
      <div className="mb-4 flex flex-wrap gap-2">
        <FilterMenu
          label="Actor"
          value={actor}
          options={[
            { value: 'all', label: 'All' },
            { value: 'agent', label: 'Agent' },
            { value: 'user', label: 'People' },
          ]}
          onChange={(v) => setQuery(route, { actor: v === 'all' ? null : v })}
        />
        <FilterMenu
          label="Type"
          value={type}
          options={[
            { value: '', label: 'All' },
            { value: 'proposal', label: 'Proposals' },
            { value: 'run', label: 'Runs' },
            { value: 'settings', label: 'Settings' },
            { value: 'guardrail', label: 'Guardrails' },
          ]}
          onChange={(v) => setQuery(route, { type: v || null })}
        />
      </div>
      {list.error && <ErrorNote error={list.error} onRetry={list.reload} />}
      {!list.data ? (
        !list.error && <Loading />
      ) : items.length === 0 ? (
        <EmptyState title="No activity" description="Nothing matches these filters." />
      ) : (
        <div className="space-y-6">
          {days.map((d) => (
            <section key={d.day}>
              <h2 className="mb-1 text-ui-sm font-medium text-soft">{d.day}</h2>
              <ol className="divide-y divide-stroke overflow-hidden rounded-2xl bg-bg-white lg:rounded-none lg:border-y lg:border-stroke lg:bg-transparent">
                {d.items.map((a) => (
                  <li
                    key={a.id}
                    className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 px-4 py-3 lg:grid-cols-[56px_160px_1fr_auto] lg:items-center lg:gap-x-4 lg:px-0 lg:py-2.5"
                  >
                    <span className="row-span-2 pt-0.5 text-ui-sm text-soft tabular-nums lg:row-span-1 lg:pt-0 lg:text-ui">
                      {f.clock(a.at)}
                    </span>
                    <span className="hidden min-w-0 text-ui lg:block">
                      <Actor a={a} />
                    </span>
                    <span className="min-w-0 text-ui text-strong">{a.summary}</span>
                    <span className="flex items-center gap-2 text-ui-sm text-sub lg:justify-end lg:text-ui">
                      <span className="lg:hidden">
                        <Actor a={a} />
                      </span>
                      <SubjectLink a={a} />
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          ))}
        </div>
      )}
    </Page>
  )
}
