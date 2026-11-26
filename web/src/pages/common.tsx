// Pieces shared by several pages.
import { Fragment, useState, type ReactNode } from 'react'
import * as f from '../format'
import { href } from '../router'
import type { ActivityItem, Reference } from '../types'
import { AgentMark, Avatar, cn, CodeBlock, Group, Row, Status, Tag, Td, Th, TextLink } from '../ui'

export function isDesktop(): boolean {
  return window.matchMedia('(min-width: 64rem)').matches
}

/** Renders "[1]" markers in agent sentences as links to the numbered evidence. */
export function WithEvidenceLinks({ text, onRef }: { text: string; onRef: (n: number) => void }) {
  // Keep "[1]." together so a citation never wraps away from its sentence.
  const parts = text.split(/(\s?\[\d+\][.,;:]?)/g)
  return (
    <>
      {parts.map((part, i) => {
        const m = part.match(/^\s?\[(\d+)\]([.,;:]?)$/)
        if (!m) return <Fragment key={i}>{part}</Fragment>
        const n = Number(m[1])
        return (
          <span key={i} className="whitespace-nowrap">
            {'\u00a0'}
            <button
              type="button"
              onClick={() => onRef(n)}
              className="rounded-sm align-baseline text-ui-sm text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary/40"
              aria-label={`Evidence ${n}`}
            >
              [{n}]
            </button>
            {m[2]}
          </span>
        )
      })}
    </>
  )
}

const REF_TYPE: Record<string, string> = {
  query: 'Query',
  dashboard: 'Dashboard',
  notebook: 'Notebook',
  workflow: 'Workflow',
  alert: 'Alert',
  slo: 'SLO',
}

function effectText(r: Reference): ReactNode {
  if (r.effect === 'reads') return <Status tone="away">Reads this data</Status>
  return <Status tone="success">Not affected</Status>
}

/** Everything that references a signal, and whether a rule on it would touch it. */
export function ReferenceTable({ refs, empty }: { refs: Reference[]; empty: string }) {
  const [open, setOpen] = useState<number | null>(null)
  if (!refs.length) {
    return <p className="rounded-2xl bg-bg-white px-4 py-4 text-ui text-sub lg:rounded-none lg:bg-transparent lg:p-0">{empty}</p>
  }
  return (
    <>
      <div className="-mx-3 hidden lg:block">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <Th className="pl-3 first:pl-3">Reference</Th>
              <Th>Owner</Th>
              <Th>Last used</Th>
              <Th right>Uses / 30 d</Th>
              <Th className="pr-3 last:pr-3">Effect</Th>
            </tr>
          </thead>
          <tbody>
            {refs.map((r) => (
              <Fragment key={`${r.id}-${r.effect}`}>
                <tr className="hover:bg-bg-weak">
                  <Td className="pl-3 first:pl-3">
                    <div className="flex items-center gap-2">
                      <Tag>{REF_TYPE[r.type]}</Tag>
                      <span className="font-medium text-strong">{r.name}</span>
                    </div>
                    {(r.effect_note || r.note) && (
                      <div className="mt-0.5 text-ui-sm text-soft">{r.effect_note ?? r.note}</div>
                    )}
                  </Td>
                  <Td className="text-sub">{r.owner}</Td>
                  <Td className="text-sub">{f.ago(r.last_used)}</Td>
                  <Td right className="text-sub">
                    {r.count_30d.toLocaleString('en-US')}
                  </Td>
                  <Td className="pr-3 last:pr-3">
                    <div className="flex items-center justify-between gap-3">
                      {effectText(r)}
                      {r.dql && (
                        <TextLink onClick={() => setOpen(open === r.id ? null : r.id)}>
                          {open === r.id ? 'Hide query' : 'Query'}
                        </TextLink>
                      )}
                    </div>
                  </Td>
                </tr>
                {open === r.id && r.dql && (
                  <tr>
                    <td colSpan={5} className="border-b border-stroke px-3 pb-3">
                      <CodeBlock code={r.dql} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <Group className="lg:hidden">
        {refs.map((r) => (
          <Row
            key={`${r.id}-${r.effect}`}
            title={r.name}
            description={`${REF_TYPE[r.type]} · ${r.owner} · ${f.ago(r.last_used)}`}
            meta={r.effect_note ?? r.note ?? undefined}
            right={r.effect === 'reads' ? <Status tone="away">Reads</Status> : <Status tone="success">Not affected</Status>}
          />
        ))}
      </Group>
    </>
  )
}

const ACTIVITY_STATUS: Record<string, string> = {
  awaiting_approval: 'awaiting approval',
  approved: 'approved',
  applied: 'applied',
  verifying: 'verifying',
  verified: 'verified',
  rejected: 'rejected',
  reverted: 'reverted',
}

export function transition(a: ActivityItem): string | null {
  const b = a.before?.status
  const c = a.after?.status
  if (typeof c !== 'string') return null
  if (typeof b !== 'string') return ACTIVITY_STATUS[c] ?? c
  return `${ACTIVITY_STATUS[b] ?? b} → ${ACTIVITY_STATUS[c] ?? c}`
}

export function Actor({ a, withName = true }: { a: ActivityItem; withName?: boolean }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-2">
      {a.actor_type === 'agent' ? <AgentMark size="sm" /> : <Avatar name={a.actor} size="sm" />}
      {withName && <span className="truncate text-strong">{a.actor_type === 'agent' ? 'Sift agent' : a.actor}</span>}
    </span>
  )
}

/** Audit timeline (proposal history). */
export function Timeline({ items }: { items: ActivityItem[] }) {
  return (
    <ol className="rounded-2xl bg-bg-white px-4 lg:rounded-none lg:bg-transparent lg:px-0">
      {items.map((a, i) => (
        <li key={a.id} className={cn('flex gap-3 py-3', i > 0 && 'border-t border-stroke')}>
          <div className="pt-0.5">{a.actor_type === 'agent' ? <AgentMark size="sm" /> : <Avatar name={a.actor} size="sm" />}</div>
          <div className="min-w-0 flex-1">
            <div className="text-ui text-strong">{a.summary}</div>
            <div className="mt-0.5 text-ui-sm text-soft">
              {a.actor_type === 'agent' ? 'Sift agent' : a.actor} · {f.dateTime(a.at)}
              {transition(a) && <> · {transition(a)}</>}
            </div>
          </div>
        </li>
      ))}
    </ol>
  )
}

/** Desktop: list and detail side by side. Mobile: list, then detail on its own page. */
export function ListDetail({
  list,
  detail,
  hasSelection,
}: {
  list: ReactNode
  detail: ReactNode
  hasSelection: boolean
}) {
  return (
    <div className="lg:grid lg:grid-cols-[300px_1fr] lg:gap-10">
      <div className={cn(hasSelection && 'hidden lg:block')}>{list}</div>
      <div className={cn(!hasSelection && 'hidden lg:block', 'min-w-0')}>{detail}</div>
    </div>
  )
}

export function BackOnMobile({ to, label }: { to: string; label: string }) {
  return (
    <a href={href(to)} className="mb-3 inline-flex h-9 items-center text-ui text-sub lg:hidden">
      ‹ {label}
    </a>
  )
}
